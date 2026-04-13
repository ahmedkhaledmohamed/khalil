#!/usr/bin/env python3
"""Khalil — Personal AI Assistant. FastAPI server + Telegram bot."""

import asyncio
import json
import logging
import os
import re
import sys
from datetime import date, datetime, timezone

# Add khalil directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import anthropic
import httpx
import keyring
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

import channels.registry as channel_registry
from channels import ActionButton, Channel, ChannelType, SentMessage
from channels.message_context import MessageContext
from channels.telegram import TelegramChannel

from config import (
    ActionType,
    AutonomyLevel,
    CLAUDE_MODEL,
    CLAUDE_BASE_URL,
    CLAUDE_API_KEY_HEADER,
    GOOGLE_BASE_URL,
    GOOGLE_MODEL,
    KEYRING_SERVICE,
    LLM_BACKEND,
    MAX_CONTEXT_TOKENS,
    OLLAMA_LLM_MODEL,
    OLLAMA_URL,
    OPENAI_BASE_URL,
    OPENAI_MODEL,
    SENSITIVE_PATTERNS,
    SWARM_ENABLED,
    TIMEZONE,
)
from knowledge.indexer import init_db
from knowledge.search import hybrid_search, get_stats
from knowledge.context import get_relevant_context, get_section_names
from autonomy import AutonomyController

import re as _re_module

# Regex to strip internal tags from display (MCP calls, capability gaps)
_internal_tag_re = re.compile(r'\[(?:MCP_CALL|CAPABILITY_GAP):[^\]]*\]')

# #72: Compile redaction patterns once at module load
_REDACT_PATTERNS = [_re_module.compile(p, _re_module.IGNORECASE) for p in SENSITIVE_PATTERNS]


def _redact_sensitive(text: str) -> str:
    """Replace sensitive patterns (PII, credentials) with [REDACTED] in log output."""
    for pat in _REDACT_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


# --- Artifact Generation Mode ---
# Note: artifact detection now lives in intent.py (is_artifact_request)


def _extract_artifact_path(query: str) -> str | None:
    """Extract target file path from the query, or return None for auto-path."""
    # Look for explicit paths: /path/to/file.html, ~/Developer/...
    m = re.search(r'(?:to|at|in)\s+([~/][\w/.\-]+\.\w+)', query)
    if m:
        return os.path.expanduser(m.group(1))
    # Look for bare paths
    m = re.search(r'([~/][\w/.\-]+\.(?:html|css|js|py|md|txt))', query)
    if m:
        return os.path.expanduser(m.group(1))
    return None


async def _generate_artifact(query: str, context: str, chat_id, progress_msg, channel) -> str | None:
    """Generate a complete file artifact via single LLM call — bypasses tool-use loop.

    1. Auto-search KB for additional context
    2. Single streaming LLM call with max_tokens=16000
    3. Write output to target path
    4. Return confirmation message
    """
    from pathlib import Path as _Path

    target_path = _extract_artifact_path(query)
    if not target_path:
        # Generate default path from query
        slug = re.sub(r'[^\w\s-]', '', query.lower())[:50].strip().replace(' ', '-')
        target_path = os.path.expanduser(f"~/Developer/Personal/presentations/{slug}/index.html")

    # Determine file type for system prompt
    ext = os.path.splitext(target_path)[1].lstrip('.')
    file_type = {
        'html': 'HTML', 'css': 'CSS', 'js': 'JavaScript', 'py': 'Python',
        'md': 'Markdown', 'txt': 'plain text', 'json': 'JSON', 'sh': 'Bash script',
    }.get(ext, ext.upper())

    await progress_msg.edit(f"\U0001f3d7 Generating {file_type} artifact...")

    # Phase 1: Enhance context with KB search
    try:
        kb_results = await asyncio.wait_for(hybrid_search(query, limit=8), timeout=15.0)
        if kb_results:
            kb_text = "\n\n".join(
                f"[{doc.get('category', '')}] {doc.get('title', '')}\n{doc.get('content', '')[:800]}"
                for doc in kb_results
            )
            context = f"[Source: knowledge base — relevant documents]\n{kb_text}\n\n{context}"
    except Exception as e:
        log.debug("Artifact KB search failed: %s", e)

    # Phase 2: Single LLM call — generate complete file content
    system = (
        f"Generate a COMPLETE {file_type} file. Output ONLY the file content — no explanation, "
        f"no markdown fences, no commentary before or after. The output will be saved directly "
        f"to {target_path}.\n\n"
        f"The user asked: {query}\n\n"
        "Use the provided context to inform the content. Make it production-quality, "
        "well-structured, and complete. Do not leave TODOs or placeholders."
    )
    user_message = f"Context:\n\n{context}\n\n---\n\nGenerate the complete {file_type} file now."

    try:
        if _taskforce_client:
            response = await _taskforce_client.chat.completions.create(
                model=CLAUDE_MODEL,
                max_tokens=16000,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_message},
                ],
                timeout=60.0,  # generous — generating a full file
                temperature=0.3,  # slight creativity for content
            )
            content = response.choices[0].message.content or ""
        elif claude:
            response = await claude.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": user_message}],
                timeout=60.0,
            )
            content = response.content[0].text
        else:
            return None  # No LLM available

        if not content or len(content) < 50:
            log.warning("Artifact generation produced empty/short content: %d chars", len(content))
            return None

        # Strip markdown fences if the LLM wrapped the output
        if content.startswith("```"):
            lines = content.split("\n")
            lines = lines[1:]  # Remove opening fence
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            content = "\n".join(lines)

        # Phase 3: Write to disk
        target = _Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

        # VERIFY: file actually exists and has content
        if not target.exists() or target.stat().st_size < 50:
            log.error("Artifact verification failed: %s does not exist or is empty", target_path)
            return None

        line_count = content.count("\n") + 1
        char_count = len(content)

        log.info("Artifact generated and VERIFIED: %s (%d lines, %d chars)", target_path, line_count, char_count)

        # Record signal
        try:
            from learning import record_signal
            record_signal("artifact_generated", {
                "path": target_path,
                "type": file_type,
                "lines": line_count,
                "chars": char_count,
            })
        except Exception:
            pass

        save_message(chat_id, "assistant", f"Created {target_path} ({line_count} lines)")

        confirmation = (
            f"\u2705 **{file_type} artifact created**\n\n"
            f"**Path:** `{target_path}`\n"
            f"**Size:** {line_count} lines, {char_count:,} chars\n\n"
            "Open it in your browser or editor to review. Want me to modify anything?"
        )
        try:
            await progress_msg.edit(confirmation)
        except Exception:
            await progress_msg.delete()
            await channel.send_message(chat_id, confirmation)

        return confirmation

    except asyncio.TimeoutError:
        log.error("Artifact generation timed out (60s)")
        return None
    except Exception as e:
        log.error("Artifact generation failed: %s", e)
        return None


def _should_try_swarm(query: str) -> bool:
    """Cheap heuristic: should we attempt parallel agent decomposition?

    Returns True only for queries that look multi-intent (conjunctions, comma-separated
    verbs, etc.). Avoids the expensive LLM decomposition call for simple queries.
    """
    if not SWARM_ENABLED:
        return False
    if len(query) < 40:
        return False
    try:
        from orchestrator import looks_like_multi_step
        return looks_like_multi_step(query)
    except Exception:
        return False


class _JsonFormatter(logging.Formatter):
    """Simple JSON log formatter with sensitive data redaction."""
    def format(self, record):
        import json as _json
        msg = _redact_sensitive(record.getMessage())
        entry = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": msg,
        }
        if record.exc_info and record.exc_info[0]:
            entry["exception"] = _redact_sensitive(self.formatException(record.exc_info))
        return _json.dumps(entry)


_handler = logging.StreamHandler()
_handler.setFormatter(_JsonFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_handler])
log = logging.getLogger("khalil")

# --- Circuit Breaker (#20) ---

class CircuitBreaker:
    """Simple circuit breaker for external API calls.

    After `threshold` consecutive failures, opens the circuit for `cooldown_seconds`.
    During cooldown, calls are rejected immediately without hitting the API.
    """
    def __init__(self, name: str, threshold: int = 5, cooldown_seconds: int = 300):
        self.name = name
        self.threshold = threshold
        self.cooldown_seconds = cooldown_seconds
        self._failures = 0
        self._opened_at: float | None = None

    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        import time
        elapsed = time.time() - self._opened_at
        if elapsed >= self.cooldown_seconds:
            # Half-open: allow one attempt
            self._opened_at = None
            self._failures = 0
            log.info("Circuit breaker '%s' half-open — allowing retry", self.name)
            return False
        return True

    def record_success(self):
        self._failures = 0
        self._opened_at = None

    def record_failure(self):
        self._failures += 1
        if self._failures >= self.threshold and self._opened_at is None:
            import time
            self._opened_at = time.time()
            log.warning(
                "Circuit breaker '%s' OPEN after %d failures — cooldown %ds",
                self.name, self._failures, self.cooldown_seconds,
            )


# Circuit breakers for external services
_cb_gmail = CircuitBreaker("gmail")
_cb_calendar = CircuitBreaker("calendar")
_cb_ollama = CircuitBreaker("ollama", threshold=3, cooldown_seconds=60)
# Separate foreground/background circuit breakers — background task failures
# must not trip the user-facing breaker (see: April 13 failure chain).
_cb_claude_fg = CircuitBreaker("claude_fg", threshold=5, cooldown_seconds=30)
_cb_claude_bg = CircuitBreaker("claude_bg", threshold=2, cooldown_seconds=120)
_cb_claude = _cb_claude_fg  # backward compat for any remaining references

# Gate: suppress background summarization while tool-use loop is active
_tool_loop_active: set[int] = set()

# Error dedup — suppress identical errors within 60s window
_last_llm_error: tuple[str, float] = ("", 0.0)


# --- Globals ---
app = FastAPI(title="Khalil", docs_url=None, redoc_url=None)
scheduler = AsyncIOScheduler()
db_conn = None
autonomy: AutonomyController = None
claude: anthropic.AsyncAnthropic = None
_taskforce_client = None  # OpenAI-compatible client for Taskforce proxy (Anthropic)
_taskforce_client_long = None  # Separate pool for long-running generation (generate_file)
_openai_client = None     # OpenAI-compatible client for Taskforce proxy (OpenAI)
_google_client = None     # OpenAI-compatible client for Taskforce proxy (Google)
telegram_app: Application | None = None
channel: Channel | None = None  # Primary channel instance (set during bot startup)
OWNER_CHAT_ID: int | None = None  # Loaded from DB on startup, updated on first message


def _persist_owner_chat_id(chat_id: int):
    """Save owner chat ID to DB so notifications work after restart."""
    if db_conn:
        db_conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('owner_chat_id', ?)",
            (str(chat_id),),
        )
        db_conn.commit()


def get_secret(key: str) -> str | None:
    """Get secret from keyring, fall back to environment variable."""
    val = keyring.get_password(KEYRING_SERVICE, key)
    if val:
        return val
    return os.environ.get(key.upper().replace("-", "_"))


def contains_sensitive_data(text: str) -> bool:
    """Check if text contains sensitive patterns."""
    for pattern in SENSITIVE_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


_MD2_ESCAPE_CHARS = r"_*[]()~`>#+-=|{}.!"


def escape_md2(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    return re.sub(r"([" + re.escape(_MD2_ESCAPE_CHARS) + r"])", r"\\\1", text)


def approve_deny_keyboard() -> InlineKeyboardMarkup:
    """Create inline keyboard with Approve/Deny buttons."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data="action_approve"),
            InlineKeyboardButton("❌ Deny", callback_data="action_deny"),
        ]
    ])


def _ctx_from_update(update: Update) -> MessageContext:
    """Build a MessageContext from a Telegram Update."""
    ch = channel_registry.get("telegram")
    incoming = TelegramChannel.extract_incoming(update) if update.message else None
    return MessageContext(
        channel=ch,
        chat_id=update.effective_chat.id if update.effective_chat else 0,
        user_id=update.effective_user.id if update.effective_user else None,
        incoming=incoming,
        _raw_update=update,
        auto_save_replies=True,
        _save_fn=save_message,
    )


async def _reply_with_keyboard(ctx: MessageContext, text: str, reply_markup, parse_mode=None):
    """Reply with Telegram-specific keyboard markup. Falls back to plain text on other channels."""
    if ctx._raw_update and ctx._raw_update.message:
        await ctx._raw_update.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        # Auto-save keyboard replies to conversation history too
        if ctx.auto_save_replies and ctx._save_fn and text:
            try:
                ctx._save_fn(ctx.chat_id, "assistant", text[:4000])
            except Exception:
                pass
    else:
        await ctx.reply(text, parse_mode=parse_mode)


from config import (
    CONVERSATION_CONTEXT_WINDOW,
    CONVERSATION_MIN_WINDOW,
    SUMMARIZE_THRESHOLD,
    SESSION_GAP_SECONDS,
)

# --- Rate limiting ---
import time as _time_mod
_user_msg_times: dict[int, list[float]] = {}
_RATE_LIMIT_MAX = 10   # messages per window
_RATE_LIMIT_WINDOW = 60.0  # seconds


def _check_rate_limit(chat_id: int) -> bool:
    """Return True if within rate limit, False if throttled."""
    if chat_id == "eval":  # bypass for eval runner
        return True
    now = _time_mod.monotonic()
    times = _user_msg_times.get(chat_id, [])
    times = [t for t in times if now - t < _RATE_LIMIT_WINDOW]
    if len(times) >= _RATE_LIMIT_MAX:
        return False
    times.append(now)
    _user_msg_times[chat_id] = times
    return True


# --- Khalil identity block (shared across all system prompts) ---
KHALIL_IDENTITY = (
    "You are Khalil — Ahmed Khaled's autonomous AI assistant.\n\n"
    "WHO AHMED IS: Senior PM at Spotify (Client Messaging, Toronto). "
    "Deep engineering background (10+ years), ships end-to-end. "
    "Side projects: Bézier (AI design), Zia (shipped iOS app), Tiny Grounds. "
    "Values rigor, structure, measurable impact, critical thinking.\n\n"
    "HOW YOU THINK:\n"
    "1. UNDERSTAND: What is Ahmed actually asking for? What's the end goal?\n"
    "2. PLAN: What steps? What info do I need? Which tools help?\n"
    "3. EXECUTE: Do the work. Use the right tools. Don't plan endlessly.\n"
    "4. VERIFY: Did I deliver what was asked? Is it complete?\n"
    "5. REPORT: Show the result, not the process.\n"
    "If step 3 fails, adapt — try a different approach, don't repeat the same thing.\n\n"
    "YOUR RESOURCES:\n"
    "- Knowledge base: indexed documents (search_knowledge tool)\n"
    "- SQLite database at data/khalil.db (queryable via shell + sqlite3)\n"
    "  Tables: documents, conversations, interaction_signals, learned_preferences, "
    "tool_analytics, reminders, settings, audit_log\n"
    "- Shell access: macOS commands, git, sqlite3, python3\n"
    "- File generation: generate_file tool — creates complete files in one pass\n"
    "- Parallel agents: delegate_tasks — runs subtasks simultaneously\n"
    "- Background monitor: spawn_watcher — long-running task tracking\n"
    "- APIs: Gmail, Calendar, Drive, GitHub, Spotify, Slack (via MCP)\n"
    "- Self-modification: your code is at ~/Developer/Personal/scripts/khalil/\n\n"
    "PRINCIPLES:\n"
    "- Execute, don't plan. Show results, not status updates or checklists.\n"
    "- Never give a status update. If a task fails, explain what failed and try a different approach.\n"
    "- For novel tasks, reason from first principles using available tools.\n"
    "- Never say 'I can't' — figure out how with what you have.\n"
    "- If a tool fails, try a different approach immediately.\n"
    "- Don't search forever. 1-2 searches max, then act.\n"
    "- For deep context: search_knowledge to FIND docs, then read_full_document to GET full text.\n\n"
)


def save_message(chat_id: int, role: str, content: str,
                 message_type: str = "text", metadata: str | None = None):
    """Save a message to conversation history.

    Args:
        message_type: "text" (default), "tool_call", or "tool_result"
        metadata: JSON string with structured data (tool name, call ID, etc.)
    """
    # Session boundary detection: if gap > 2h, summarize the previous session
    last_msg = db_conn.execute(
        "SELECT id, timestamp FROM conversations WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
        (chat_id,),
    ).fetchone()
    if last_msg and last_msg[1]:
        try:
            from datetime import datetime, timedelta
            last_time = datetime.strptime(last_msg[1], "%Y-%m-%d %H:%M:%S")
            gap = (datetime.utcnow() - last_time).total_seconds()
            if gap > SESSION_GAP_SECONDS:
                asyncio.get_event_loop().call_soon(
                    lambda cid=chat_id: asyncio.ensure_future(_summarize_session(cid))
                )
        except Exception as e:
            log.debug("Session gap detection failed: %s", e)

    # Truncate oversized content to prevent DB bloat
    _MAX_MSG_CONTENT = 8000
    if content and len(content) > _MAX_MSG_CONTENT:
        log.info("Truncating %s message from %d to %d chars", role, len(content), _MAX_MSG_CONTENT)
        content = content[:_MAX_MSG_CONTENT]

    db_conn.execute(
        "INSERT INTO conversations (chat_id, role, content, message_type, metadata) VALUES (?, ?, ?, ?, ?)",
        (chat_id, role, content, message_type, metadata),
    )
    db_conn.commit()

    # Cache embedding for user messages (fire-and-forget, enables semantic similarity)
    if role == "user" and message_type == "text" and content:
        try:
            asyncio.get_event_loop().call_soon(
                lambda c=content: asyncio.ensure_future(_cache_msg_embedding(c))
            )
        except Exception:
            pass  # No event loop (eval, CLI, etc.)

    # Rolling summarization: trigger if unsummarized messages exceed threshold
    _check_summarization_needed(chat_id)


def _check_summarization_needed(chat_id: int):
    """Check if we have enough unsummarized messages to trigger a summary."""
    # Defer summarization while tool-use loop is active for this chat —
    # background LLM calls during tool-use waste API capacity and can trip circuit breakers.
    if int(chat_id) in _tool_loop_active:
        return
    try:
        # Find the last summarized message ID for this chat
        last_summary = db_conn.execute(
            "SELECT message_range_end FROM conversation_summaries WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        last_summarized_id = last_summary[0] if last_summary else 0

        # Count unsummarized messages
        unsummarized = db_conn.execute(
            "SELECT COUNT(*) FROM conversations WHERE chat_id = ? AND id > ?",
            (chat_id, last_summarized_id),
        ).fetchone()[0]

        if unsummarized >= SUMMARIZE_THRESHOLD:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_run_summarization(chat_id, last_summarized_id))
            except RuntimeError:
                pass  # No event loop — skip async summarization
    except Exception as e:
        log.debug("Summarization check failed: %s", e)


async def _run_summarization(chat_id: int, after_id: int):
    """Summarize unsummarized messages for a chat and extract memories."""
    try:
        rows = db_conn.execute(
            "SELECT id, role, content FROM conversations WHERE chat_id = ? AND id > ? ORDER BY id ASC LIMIT 50",
            (chat_id, after_id),
        ).fetchall()
        if not rows:
            return

        conv_text = "\n".join(f"{r[1].title()}: {r[2][:300]}" for r in rows)
        summary = await ask_llm(
            f"Summarize this conversation. Preserve: key decisions made, action items, "
            f"facts mentioned, user preferences expressed, and the current topic. Under 400 words.\n\n{conv_text}",
            "",
            system_extra="You are summarizing a conversation for future context. Be concise but complete.",
            _background=True,
        )
        if not summary or summary.startswith("⚠️"):
            return

        msg_start = rows[0][0]
        msg_end = rows[-1][0]
        db_conn.execute(
            "INSERT INTO conversation_summaries (chat_id, summary, message_range_start, message_range_end, message_count) "
            "VALUES (?, ?, ?, ?, ?)",
            (chat_id, summary, msg_start, msg_end, len(rows)),
        )
        db_conn.commit()
        log.info("Summarized %d messages for chat %s (ids %s-%s)", len(rows), chat_id, msg_start, msg_end)

        # Extract memories from the summary (background — don't trip foreground CB)
        try:
            from learning import extract_memories
            _bg_ask_llm = lambda q, c, **kw: ask_llm(q, c, **kw, _background=True)
            await extract_memories(chat_id, summary, _bg_ask_llm)
        except Exception as e:
            log.warning("Memory extraction failed: %s", e)

    except Exception as e:
        log.warning("Conversation summarization failed for chat %s: %s", chat_id, e)


async def _summarize_session(chat_id: int):
    """Summarize a completed session (triggered by session gap detection)."""
    last_summary = db_conn.execute(
        "SELECT message_range_end FROM conversation_summaries WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
        (chat_id,),
    ).fetchone()
    last_summarized_id = last_summary[0] if last_summary else 0

    # Only summarize if there are 5+ unsummarized messages
    count = db_conn.execute(
        "SELECT COUNT(*) FROM conversations WHERE chat_id = ? AND id > ?",
        (chat_id, last_summarized_id),
    ).fetchone()[0]

    if count >= 5:
        await _run_summarization(chat_id, last_summarized_id)


def _compute_topic_similarity(text_a: str, text_b: str) -> float:
    """Topic similarity: embedding cosine (if cached) → Jaccard fallback.

    Uses pre-computed embeddings from _msg_embedding_cache when available,
    which gives much better semantic matching (e.g., "what's the status" ≈
    "how did it go"). Falls back to word-overlap Jaccard when embeddings
    aren't cached yet.
    """
    # Try embedding-based cosine similarity first
    emb_a = _msg_embedding_cache.get(text_a)
    emb_b = _msg_embedding_cache.get(text_b)
    if emb_a is not None and emb_b is not None:
        dot = sum(a * b for a, b in zip(emb_a, emb_b))
        norm_a = sum(a * a for a in emb_a) ** 0.5
        norm_b = sum(b * b for b in emb_b) ** 0.5
        if norm_a > 0 and norm_b > 0:
            return max(0.0, dot / (norm_a * norm_b))

    # Fallback: Jaccard word overlap
    words_a = set(text_a.lower().split())
    words_b = set(text_b.lower().split())
    stopwords = {"the", "a", "an", "is", "are", "was", "were", "i", "you", "my", "your",
                 "it", "this", "that", "to", "of", "in", "for", "on", "with", "and", "or"}
    words_a -= stopwords
    words_b -= stopwords
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union) if union else 0.0


# Embedding cache for recent messages (populated async, consumed sync)
_msg_embedding_cache: dict[str, list[float]] = {}
_MSG_EMBEDDING_CACHE_MAX = 100


async def _cache_msg_embedding(text: str):
    """Fire-and-forget: embed a message and cache for similarity lookups."""
    if not text or len(text) < 5 or text in _msg_embedding_cache:
        return
    try:
        from knowledge.embedder import embed_text
        emb = await embed_text(text[:500])  # cap length for efficiency
        if emb:
            _msg_embedding_cache[text] = emb
            # Evict oldest if cache too large
            if len(_msg_embedding_cache) > _MSG_EMBEDDING_CACHE_MAX:
                oldest_key = next(iter(_msg_embedding_cache))
                del _msg_embedding_cache[oldest_key]
    except Exception:
        pass  # Ollama unavailable — Jaccard fallback will be used


def get_conversation_history(chat_id: int) -> str:
    """Get recent conversation history formatted for LLM context.

    #66: Dynamic context window — includes more messages when topic is coherent,
    fewer when the topic has shifted.
    """
    rows = db_conn.execute(
        "SELECT role, content FROM conversations WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
        (chat_id, CONVERSATION_CONTEXT_WINDOW),
    ).fetchall()
    if not rows:
        return ""

    # Reverse to chronological order
    rows = list(reversed(rows))

    # Dynamic windowing: walk backward from newest, stop when topic diverges
    if len(rows) > CONVERSATION_MIN_WINDOW:
        latest_text = rows[-1][1]
        window_size = CONVERSATION_MIN_WINDOW
        for i in range(len(rows) - CONVERSATION_MIN_WINDOW - 1, -1, -1):
            sim = _compute_topic_similarity(latest_text, rows[i][1])
            if sim >= 0.1:  # Even slight topical overlap = include
                window_size = len(rows) - i
            else:
                break
        window_size = max(CONVERSATION_MIN_WINDOW, min(window_size, len(rows)))
        rows = rows[-window_size:]

    lines = [f"{r[0].title()}: {r[1]}" for r in rows]
    return "Recent conversation:\n" + "\n".join(lines)


def _detect_re_ask(chat_id: int, query: str) -> bool:
    """Detect if user is re-asking a recent question (implicit quality signal)."""
    try:
        row = db_conn.execute(
            "SELECT content, timestamp FROM conversations "
            "WHERE chat_id = ? AND role = 'user' AND message_type = 'text' "
            "ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        if not row:
            return False
        prev_content, ts = row
        from datetime import datetime as _dt
        last_time = _dt.strptime(ts, "%Y-%m-%d %H:%M:%S")
        if (_dt.utcnow() - last_time).total_seconds() > 600:
            return False
        return _compute_topic_similarity(query, prev_content) > 0.5
    except Exception:
        return False


async def get_conversation_context(chat_id: int, query: str) -> str:
    """Build rich conversation context: memories + summary + recent messages.

    Three-tier context:
    1. Long-term memories (semantic search over extracted facts/decisions/preferences)
    2. Session summary (latest conversation_summary for this chat)
    3. Recent raw messages (last 8 for immediate context)
    """
    parts = []

    # 1. Search conversation memories relevant to this query
    try:
        from knowledge.search import search_memories
        memories = await search_memories(query, limit=5)
        if memories:
            memory_lines = [f"- [{m['memory_type']}] {m['content']}" for m in memories]
            parts.append("[Source: conversation memories]\n" + "\n".join(memory_lines))
    except Exception as e:
        log.debug("Memory search unavailable: %s", e)

    # 2. Get latest conversation summary for session context
    try:
        summary_row = db_conn.execute(
            "SELECT summary FROM conversation_summaries WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        if summary_row:
            parts.append(f"[Source: previous conversation summary]\n{summary_row[0]}")
    except Exception:
        pass  # Table may not exist yet

    # 2.5. Active task plans — so LLM knows about in-progress work
    try:
        from orchestrator import get_active_plans_for_chat, ensure_table as ensure_plans_table
        ensure_plans_table()
        active_plans = get_active_plans_for_chat(chat_id)
        if active_plans:
            plan_lines = []
            for plan in active_plans:
                plan_lines.append(f"Plan: {plan.query[:100]}")
                plan_lines.append(f"  Status: {plan.status} (ID: {plan.plan_id})")
                for step in plan.steps:
                    status_label = {"completed": "DONE", "failed": "FAILED", "pending": "TODO",
                                    "running": "RUNNING", "blocked": "BLOCKED", "skipped": "SKIPPED"}.get(step.status, "?")
                    line = f"  [{status_label}] {step.description}"
                    if step.result:
                        line += f" -> {step.result[:150]}"
                    if step.error:
                        line += f" ERROR: {step.error[:100]}"
                    plan_lines.append(line)
            parts.append("[Source: active task plans]\n" + "\n".join(plan_lines))
    except Exception as e:
        log.debug("Active plans injection failed: %s", e)

    # 3. Recent messages (last 30 raw rows — tool exchanges use 2 rows each,
    #    so 30 rows ≈ 10-15 logical turns with tool use)
    rows = db_conn.execute(
        "SELECT role, content, message_type, metadata FROM conversations WHERE chat_id = ? ORDER BY id DESC LIMIT 30",
        (chat_id,),
    ).fetchall()
    if rows:
        rows = list(reversed(rows))
        lines = []
        for r in rows:
            role, content, msg_type, meta = r[0], r[1], r[2] or "text", r[3]
            if msg_type == "tool_call":
                # Show tool calls compactly
                try:
                    info = json.loads(meta) if meta else {}
                    tool_name = info.get("tool_name", "tool")
                    lines.append(f"Assistant: [Called tool: {tool_name}]")
                except Exception:
                    lines.append(f"Assistant: [Tool call]")
            elif msg_type == "tool_result":
                # Show tool results truncated
                try:
                    info = json.loads(meta) if meta else {}
                    tool_name = info.get("tool_name", "tool")
                    lines.append(f"Tool ({tool_name}): {content[:2000]}")
                except Exception:
                    lines.append(f"Tool: {content[:2000]}")
            else:
                lines.append(f"{role.title()}: {content}")
        parts.append("[Source: recent messages]\n" + "\n".join(lines))

    return "\n\n".join(parts)


def clear_conversation(chat_id: int):
    """Clear conversation history for a chat."""
    db_conn.execute("DELETE FROM conversations WHERE chat_id = ?", (chat_id,))
    db_conn.commit()


def truncate_context(results: list[dict], max_chars: int = MAX_CONTEXT_TOKENS * 4) -> str:
    """Format search results into context string, respecting token limits.

    #67: Each result is tagged with a [Source: ...] citation for cross-source fusion.
    """
    lines = []
    total = 0
    for r in results:
        category = r.get('category', '')
        title = r['title']
        # #67: Build a source citation tag from category and title
        source_tag = f"[Source: {category} — {title}]" if category else f"[Source: {title}]"
        entry = f"{source_tag}\n{r['content']}\n"
        if total + len(entry) > max_chars:
            break
        lines.append(entry)
        total += len(entry)
    return "\n---\n".join(lines)


def _get_extension_capabilities_text() -> str:
    """Build a text list of installed extension capabilities for the system prompt."""
    from config import EXTENSIONS_DIR
    if not EXTENSIONS_DIR or not EXTENSIONS_DIR.exists():
        return "(none installed)\n"
    lines = []
    for manifest_path in sorted(EXTENSIONS_DIR.glob("*.json")):
        try:
            manifest = json.loads(manifest_path.read_text())
            lines.append(f"- /{manifest['command']} — {manifest['description']}")
        except Exception:
            continue
    return ("\n".join(lines) + "\n") if lines else "(none installed)\n"


def _get_mcp_tools_text() -> str:
    """Build a text list of available MCP tools for the system prompt."""
    try:
        from mcp_client import MCPClientManager
        manager = MCPClientManager.get_instance()
        tools = getattr(manager, "_cached_tools", [])
        if not tools:
            return ""
        lines = []
        for t in tools:
            lines.append(f"- {t['server']}.{t['name']} — {t['description']}")
        return (
            "\nMCP TOOLS: You can call external tools from connected MCP servers. "
            "To use one, include this exact tag in your response:\n"
            "[MCP_CALL: server_name.tool_name | {\"arg\": \"value\"}]\n"
            "Available tools:\n" + "\n".join(lines) + "\n"
        )
    except Exception:
        return ""


LLM_TIMEOUT = 20.0  # seconds — Ollama local (fast when running)
CLAUDE_TIMEOUT = 15.0  # per-model timeout — 8s was too aggressive for Taskforce proxy
FALLBACK_BUDGET = 15.0  # total seconds for the entire fallback chain
_ollama_recovery_attempted = False


async def _try_recover_ollama() -> bool:
    """Detect dead Ollama process, attempt restart. Returns True if recovered."""
    global _ollama_recovery_attempted
    import subprocess
    if _ollama_recovery_attempted:
        return False  # Only try once per session to avoid loops
    _ollama_recovery_attempted = True

    # Check if Ollama process is running
    try:
        result = subprocess.run(["pgrep", "-x", "ollama"], capture_output=True)
        if result.returncode == 0:
            log.info("Ollama process found but not responding — may be hung")
            return False

        # Process not running — try to start it
        log.warning("Ollama process not running. Attempting restart...")
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Give it a moment to start
        import asyncio
        await asyncio.sleep(3)

        # Verify it started
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
            if resp.status_code == 200:
                log.info("Ollama restarted successfully")
                _ollama_recovery_attempted = False  # Reset so we can recover again later
                return True
    except Exception as e:
        log.warning("Ollama recovery failed: %s", e)
    return False


# #18: Graceful degradation chain — Ollama local → Ollama cloud (kimi) → Claude → OpenAI → Google → cached
_OLLAMA_CLOUD_FALLBACK = "kimi-k2.5:cloud"
_FALLBACK_MODELS = [CLAUDE_MODEL, "claude-sonnet-4-20250514", "claude-haiku-4-5-20251001"]

# Backup provider fallback chain (tried after all Claude models fail)
_BACKUP_PROVIDERS: list[tuple[str, str]] = [
    # (client_attr, model) — client_attr is the name of the global variable
    ("_openai_client", OPENAI_MODEL),
    ("_google_client", GOOGLE_MODEL),
]


async def _fallback_to_ollama_cloud(query: str, context: str, system: str, user_message: str) -> str | None:
    """Try Ollama cloud model (kimi-k2.5) before falling back to Claude."""
    if contains_sensitive_data(query):
        log.info("Skipping Ollama cloud fallback — sensitive query")
        return None
    try:
        async with httpx.AsyncClient(timeout=LLM_TIMEOUT) as client:
            response = await client.post(
                f"{OLLAMA_URL}/api/chat",
                json={
                    "model": _OLLAMA_CLOUD_FALLBACK,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_message},
                    ],
                    "stream": False,
                },
            )
            response.raise_for_status()
            log.info("Fell back to %s (local Ollama unavailable)", _OLLAMA_CLOUD_FALLBACK)
            return response.json()["message"]["content"]
    except Exception as e:
        log.warning("Ollama cloud fallback (%s) failed: %s", _OLLAMA_CLOUD_FALLBACK, e)
        return None


async def _fallback_to_claude(query: str, context: str, system: str, user_message: str) -> str | None:
    """Fall back through LLM provider chain when primary is down.

    Tries: Ollama cloud (kimi) → Claude Sonnet → Claude Haiku → GPT-5.2 → Gemini 2.5 Flash → cached.
    Uses a total latency budget (FALLBACK_BUDGET) — gives up after budget expires.
    """
    import time as _fb_time
    _budget_start = _fb_time.monotonic()

    def _budget_remaining() -> float:
        return max(0, FALLBACK_BUDGET - (_fb_time.monotonic() - _budget_start))

    # Try Ollama cloud model first (free, no API key needed)
    if _budget_remaining() > 2:
        kimi_result = await _fallback_to_ollama_cloud(query, context, system, user_message)
        if kimi_result:
            return kimi_result

    # Use Taskforce client if available, else native Anthropic
    _use_taskforce = _taskforce_client is not None
    client = _taskforce_client or claude
    if not client:
        api_key = get_secret("anthropic-api-key")
        if not api_key:
            return _get_cached_response(query)
        try:
            if CLAUDE_BASE_URL:
                from openai import AsyncOpenAI
                client = AsyncOpenAI(
                    api_key=api_key, base_url=CLAUDE_BASE_URL,
                    default_headers={CLAUDE_API_KEY_HEADER: api_key} if CLAUDE_API_KEY_HEADER else {},
                )
                _use_taskforce = True
            else:
                client = anthropic.AsyncAnthropic(api_key=api_key)
        except Exception:
            return _get_cached_response(query)

    _msgs = [{"role": "system", "content": system}, {"role": "user", "content": user_message}]

    for model in _FALLBACK_MODELS:
        _remaining = _budget_remaining()
        if _remaining < 1:
            log.warning("Fallback budget exhausted (%.1fs), skipping remaining models", FALLBACK_BUDGET)
            break
        _timeout = min(CLAUDE_TIMEOUT, _remaining)
        try:
            if _use_taskforce:
                response = await client.chat.completions.create(
                    model=model, max_tokens=1500, messages=_msgs, timeout=_timeout,
                )
                text = response.choices[0].message.content
            else:
                response = await client.messages.create(
                    model=model, max_tokens=1500, system=system,
                    messages=[{"role": "user", "content": user_message}], timeout=_timeout,
                )
                text = response.content[0].text
            log.info("Fell back to %s (Ollama unavailable) in %.1fs", model, FALLBACK_BUDGET - _budget_remaining())
            return text
        except Exception as e:
            log.warning("Fallback model %s failed: %s", model, e)
            continue

    # Try backup providers (OpenAI, Google) — only if budget remains
    for client_attr, model in _BACKUP_PROVIDERS:
        _remaining = _budget_remaining()
        if _remaining < 1:
            log.warning("Fallback budget exhausted, skipping backup providers")
            break
        backup_client = globals().get(client_attr)
        if not backup_client:
            continue
        try:
            response = await backup_client.chat.completions.create(
                model=model, max_tokens=1500, messages=_msgs, timeout=min(CLAUDE_TIMEOUT, _remaining),
            )
            text = response.choices[0].message.content
            log.info("Fell back to backup provider %s (%s)", model, client_attr)
            return text
        except Exception as e:
            log.warning("Backup provider %s (%s) failed: %s", model, client_attr, e)
            continue

    # All models failed or budget exhausted — try cached response
    return _get_cached_response(query)


def _get_cached_response(query: str) -> str | None:
    """Return a recent cached response for a similar query, or None."""
    if not db_conn:
        return None
    try:
        # Find recent assistant response where user asked something similar
        rows = db_conn.execute(
            "SELECT content FROM conversations WHERE role = 'assistant' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if rows:
            log.info("Using cached response (all LLM backends unavailable)")
            return f"⚠️ LLM unavailable — here's my last response (may not be relevant):\n\n{rows[0][:500]}"
    except Exception:
        pass
    return None


async def ask_llm(query: str, context: str, system_extra: str = "", model: str | None = None,
                  _background: bool = False) -> str:
    """Send query + context to LLM for reasoning. Supports Ollama (local) and Claude (cloud).

    Args:
        model: Explicit model override. If None, model_router selects based on query complexity.
        _background: If True, use background circuit breaker (won't trip user-facing CB).

    Returns an error message (not raises) if the LLM is unreachable.
    """
    # M9: Inject all active learned preferences into system prompt
    style_hint = ""
    try:
        from learning import get_active_response_preferences
        style_hint = get_active_response_preferences()
    except Exception:
        # Fallback to legacy single-preference approach
        try:
            from learning import get_preference
            response_style = get_preference("response_style")
            if response_style:
                parts = []
                if response_style.get("length"):
                    parts.append(f"Keep responses {response_style['length']}.")
                if response_style.get("format"):
                    parts.append(f"Prefer {response_style['format']} format.")
                if parts:
                    style_hint = "\n" + " ".join(parts) + "\n"
        except Exception:
            pass  # Preferences not available yet (DB not initialized)

    # Temporal context — inject current date/time into every LLM call
    from datetime import datetime as _dt
    import zoneinfo
    _now = _dt.now(zoneinfo.ZoneInfo(TIMEZONE))
    _temporal = (
        f"CURRENT TIME: {_now.strftime('%A, %B %d, %Y at %I:%M %p %Z')} "
        f"(Q{(_now.month - 1) // 3 + 1} {_now.year})\n\n"
    )

    # --- Selective context injection: only inject relevant skill descriptions ---
    try:
        from skills import get_registry
        _skill_context = get_registry().get_context_for_intent(query)
    except Exception:
        _skill_context = ""

    system = (
        f"{_temporal}"
        f"{KHALIL_IDENTITY}"
        "CAPABILITIES: You run on Ahmed's Mac and can execute macOS shell commands "
        "and access many services through your action system.\n"
        f"{_skill_context}\n\n"
        "If Ahmed asks about his machine state, DO NOT suggest he run a command "
        "— just tell him you'll check. Actions execute automatically.\n\n"
        "HONESTY RULE: NEVER pretend to execute tools or create files. "
        "Do NOT output '[Called tool: X]' or fake tool results in text. "
        "If you cannot execute an action, say so honestly.\n\n"
        f"{_get_mcp_tools_text()}"
        "IMPORTANT: If the user asks you to DO something that you cannot execute "
        "AND no skill or extension covers it, "
        "include this exact tag in your response:\n"
        "[CAPABILITY_GAP: short_name | /command_name | one-line description]\n"
        "Example: [CAPABILITY_GAP: slack_reader | /slack | Read and search Slack messages]\n"
        "Still respond naturally to the user — the tag is for internal processing.\n\n"
        f"{style_hint}"
        f"{system_extra}"
    )

    user_message = f"Context from personal archives:\n\n{context}\n\n---\n\nQuestion: {query}"

    # M6: Smart model routing — select model based on query complexity
    from model_router import route_query
    _routed_tier, _routed_model = route_query(query)
    _selected_model = model or _routed_model
    try:
        from learning import record_signal
        record_signal("model_routed", {"tier": _routed_tier.value, "model": _selected_model})
    except Exception:
        pass

    # #78: Privacy-aware LLM routing — force Ollama for sensitive queries
    import re as _re
    _force_local = any(_re.search(p, query, _re.IGNORECASE) for p in SENSITIVE_PATTERNS)
    if _force_local and LLM_BACKEND == "claude":
        log.info("Privacy routing: sensitive query forced to local Ollama")
        # Fall through to Ollama path below instead of Claude

    if LLM_BACKEND == "claude" and (claude or _taskforce_client) and not _force_local:
        # Circuit breaker: skip Claude if it's been failing repeatedly
        # Use background CB for background tasks so their failures don't kill user requests
        _cb = _cb_claude_bg if _background else _cb_claude_fg
        global _last_llm_error
        if _cb.is_open():
            log.warning("Claude circuit breaker open — trying backup providers")
            _backup_msgs = [{"role": "system", "content": system}, {"role": "user", "content": user_message}]
            for _bp_attr, _bp_model in _BACKUP_PROVIDERS:
                _bp_client = globals().get(_bp_attr)
                if not _bp_client:
                    continue
                try:
                    _bp_resp = await _bp_client.chat.completions.create(
                        model=_bp_model, max_tokens=1500, messages=_backup_msgs, timeout=CLAUDE_TIMEOUT,
                    )
                    try:
                        from learning import record_signal
                        record_signal("llm_fallback", {"primary": "claude", "provider": _bp_attr, "model": _bp_model, "reason": "circuit_breaker"})
                    except Exception:
                        pass
                    return _bp_resp.choices[0].message.content
                except Exception:
                    continue
            return "⚠️ LLM temporarily unavailable. Try again in a few minutes."

        _max_retries = 2  # only retry for rate limits; timeouts fail fast
        for _attempt in range(1, _max_retries + 1):
            try:
                if _taskforce_client:
                    # Taskforce proxy: OpenAI-compatible API
                    _msgs = [{"role": "system", "content": system}, {"role": "user", "content": user_message}]
                    response = await _taskforce_client.chat.completions.create(
                        model=_selected_model,
                        max_tokens=1500,
                        messages=_msgs,
                        timeout=CLAUDE_TIMEOUT,
                    )
                    _cb.record_success()
                    return response.choices[0].message.content
                else:
                    # Native Anthropic API
                    response = await claude.messages.create(
                        model=_selected_model,
                        max_tokens=1500,
                        system=system,
                        messages=[{"role": "user", "content": user_message}],
                        timeout=CLAUDE_TIMEOUT,
                    )
                    _cb.record_success()
                    return response.content[0].text
            except Exception as e:
                _cb.record_failure()
                _err_str = str(e).lower()
                _is_rate_limit = "429" in _err_str or "rate" in _err_str or "overloaded" in _err_str
                _is_timeout = "timeout" in _err_str
                # Only retry on rate limits; timeouts → immediate fallback
                if _is_rate_limit and _attempt < _max_retries:
                    _delay = min(2.0 * (2 ** (_attempt - 1)), 8.0)
                    log.warning("Claude rate limited (attempt %d/%d), retrying in %.1fs", _attempt, _max_retries, _delay)
                    await asyncio.sleep(_delay)
                    continue
                if _is_timeout:
                    log.warning("Claude timed out after %.0fs — immediate fallback", CLAUDE_TIMEOUT)
                else:
                    log.error("Claude API call failed: %s", e)
                from learning import record_signal
                record_signal("llm_failure", {"backend": "claude", "error": f"{type(e).__name__}: {e}"[:200]})
                # Try backup providers before giving up
                _backup_msgs = [{"role": "system", "content": system}, {"role": "user", "content": user_message}]
                for _bp_attr, _bp_model in _BACKUP_PROVIDERS:
                    _bp_client = globals().get(_bp_attr)
                    if not _bp_client:
                        continue
                    try:
                        _bp_resp = await _bp_client.chat.completions.create(
                            model=_bp_model, max_tokens=1500, messages=_backup_msgs, timeout=CLAUDE_TIMEOUT,
                        )
                        log.info("Claude failed, fell back to %s (%s)", _bp_model, _bp_attr)
                        try:
                            record_signal("llm_fallback", {"primary": "claude", "provider": _bp_attr, "model": _bp_model, "reason": "retry_exhausted"})
                        except Exception:
                            pass
                        return _bp_resp.choices[0].message.content
                    except Exception as _bp_e:
                        log.warning("Backup %s failed: %s", _bp_model, _bp_e)
                        continue
                # Error dedup: suppress identical errors within 60s
                import time as _time
                _err_type = type(e).__name__
                _now = _time.time()
                if _err_type == _last_llm_error[0] and (_now - _last_llm_error[1]) < 60:
                    return "⚠️ Still experiencing API issues. Try again shortly."
                _last_llm_error = (_err_type, _now)
                if _is_rate_limit:
                    return "⚠️ Rate limited across all providers. Try again in a minute."

                # Last resort: try local Ollama when all cloud providers fail
                log.info("All cloud providers failed — trying local Ollama as last resort")
                try:
                    import httpx as _hx
                    _ollama_sys = system
                    if "qwen3" in OLLAMA_LLM_MODEL:
                        _ollama_sys = system + "\n\n/no_think"
                    async with _hx.AsyncClient(timeout=30.0) as _oc:
                        _or = await _oc.post(
                            f"{OLLAMA_URL}/api/chat",
                            json={"model": OLLAMA_LLM_MODEL, "stream": False,
                                  "messages": [{"role": "system", "content": _ollama_sys},
                                               {"role": "user", "content": user_message}]},
                        )
                        _or.raise_for_status()
                        _ollama_text = _or.json()["message"]["content"]
                        if _ollama_text:
                            log.info("Ollama last-resort fallback succeeded")
                            try:
                                from learning import record_signal
                                record_signal("llm_fallback", {"provider": "ollama_last_resort"})
                            except Exception:
                                pass
                            return _ollama_text
                except Exception as _oe:
                    log.warning("Ollama last-resort also failed: %s", _oe)

                return f"⚠️ LLM unavailable (all providers failed, including local Ollama). Try again later."

    # Default: Ollama local LLM
    # #20: Circuit breaker — skip Ollama if circuit is open
    if _cb_ollama.is_open():
        log.warning("Ollama circuit breaker open — skipping to Claude fallback")
        fallback = await _fallback_to_claude(query, context, system, user_message)
        if fallback:
            return fallback
        return "⚠️ LLM unavailable — Ollama circuit breaker open and Claude fallback failed."

    # qwen3 is a thinking model — disable thinking for simple/standard queries
    _ollama_system = system
    if _routed_tier.value != "complex" and "qwen3" in OLLAMA_LLM_MODEL:
        _ollama_system = system + "\n\n/no_think"

    _ollama_payload = {
        "model": OLLAMA_LLM_MODEL,
        "messages": [
            {"role": "system", "content": _ollama_system},
            {"role": "user", "content": user_message},
        ],
        "stream": False,
    }

    async def _ollama_call() -> str:
        async with httpx.AsyncClient(timeout=LLM_TIMEOUT) as client:
            resp = await client.post(f"{OLLAMA_URL}/api/chat", json=_ollama_payload)
            resp.raise_for_status()
            return resp.json()["message"]["content"]

    # Retry loop: handles timeouts with a second attempt
    _connect_failed = False
    for _attempt in range(1, 3):
        try:
            result = await _ollama_call()
            _cb_ollama.record_success()
            return result
        except httpx.TimeoutException:
            log.warning("Ollama timed out (attempt %d/2)", _attempt)
            if _attempt < 2:
                await asyncio.sleep(2)
                continue
            _cb_ollama.record_failure()
            from learning import record_signal
            record_signal("llm_failure", {"backend": "ollama", "error": "timeout"})
            return "⚠️ LLM timed out. Ollama may be overloaded — try again in a moment."
        except httpx.ConnectError:
            _connect_failed = True
            break
        except (httpx.HTTPError, KeyError) as e:
            log.error("Ollama LLM call failed: %s", e)
            from learning import record_signal
            record_signal("llm_failure", {"backend": "ollama", "error": f"{type(e).__name__}: {e}"[:200]})
            return f"⚠️ LLM error: {type(e).__name__}. Check Ollama logs."

    # ConnectError path: Ollama is not running — try recovery then fallback
    if _connect_failed:
        log.error("Cannot connect to Ollama at %s", OLLAMA_URL)
        _cb_ollama.record_failure()
        from learning import record_signal
        record_signal("llm_failure", {"backend": "ollama", "error": "connection_refused"})
        if await _try_recover_ollama():
            try:
                result = await _ollama_call()
                _cb_ollama.record_success()
                return result
            except Exception:
                pass
        fallback = await _fallback_to_claude(query, context, system, user_message)
        if fallback:
            return fallback
        return "⚠️ LLM unavailable — Ollama is not running and Claude fallback failed. Start Ollama with: ollama serve"


# Alias for backward compatibility with scheduler/digests references
ask_claude = ask_llm


# --- Streaming LLM ---

# Minimum interval between Telegram message edits (Telegram rate-limits edits)
_STREAM_EDIT_INTERVAL = 0.8  # seconds
# Minimum new characters before triggering an edit (avoid edits for tiny chunks)
_STREAM_MIN_DELTA = 40


async def ask_llm_stream(query: str, context: str, system_extra: str = "", model: str | None = None):
    """Streaming version of ask_llm. Yields text chunks as they arrive.

    Falls back to non-streaming ask_llm if streaming isn't supported by the backend.
    Yields the full accumulated text at the end if no chunks were yielded.
    """
    # Build system prompt (same logic as ask_llm)
    style_hint = ""
    try:
        from learning import get_active_response_preferences
        style_hint = get_active_response_preferences()
    except Exception:
        pass

    from datetime import datetime as _dt
    import zoneinfo
    _now = _dt.now(zoneinfo.ZoneInfo(TIMEZONE))
    _temporal = (
        f"CURRENT TIME: {_now.strftime('%A, %B %d, %Y at %I:%M %p %Z')} "
        f"(Q{(_now.month - 1) // 3 + 1} {_now.year})\n\n"
    )

    try:
        from skills import get_registry
        _skill_context = get_registry().get_context_for_intent(query)
    except Exception:
        _skill_context = ""

    system = (
        f"{_temporal}"
        f"{KHALIL_IDENTITY}"
        "CAPABILITIES: You run on Ahmed's Mac and can execute macOS shell commands "
        "and access many services through your action system.\n"
        f"{_skill_context}\n\n"
        "If Ahmed asks about his machine state, DO NOT suggest he run a command "
        "— just tell him you'll check. Actions execute automatically.\n\n"
        "HONESTY RULE: NEVER pretend to execute tools or create files. "
        "Do NOT output '[Called tool: X]' or fake tool results in text. "
        "If you cannot execute an action, say so honestly.\n\n"
        f"{_get_mcp_tools_text()}"
        "IMPORTANT: If the user asks you to DO something that you cannot execute "
        "AND no skill or extension covers it, "
        "include this exact tag in your response:\n"
        "[CAPABILITY_GAP: short_name | /command_name | one-line description]\n"
        "Example: [CAPABILITY_GAP: slack_reader | /slack | Read and search Slack messages]\n"
        "Still respond naturally to the user — the tag is for internal processing.\n\n"
        f"{style_hint}"
        f"{system_extra}"
    )

    user_message = f"Context from personal archives:\n\n{context}\n\n---\n\nQuestion: {query}"

    from model_router import route_query
    _routed_tier, _routed_model = route_query(query)
    _selected_model = model or _routed_model

    # Privacy routing
    import re as _re
    _force_local = any(_re.search(p, query, _re.IGNORECASE) for p in SENSITIVE_PATTERNS)

    if LLM_BACKEND == "claude" and (_taskforce_client or claude) and not _force_local:
        if _cb_claude_fg.is_open():
            # Circuit breaker open — fall back to non-streaming
            result = await ask_llm(query, context, system_extra, model)
            yield result
            return

        try:
            if _taskforce_client:
                # Taskforce proxy — non-streaming (streaming returns empty SSE)
                _msgs = [{"role": "system", "content": system}, {"role": "user", "content": user_message}]
                response = await _taskforce_client.chat.completions.create(
                    model=_selected_model,
                    max_tokens=1500,
                    messages=_msgs,
                    timeout=CLAUDE_TIMEOUT,
                )
                text = response.choices[0].message.content if response.choices else ""
                if text:
                    yield text
                _cb_claude_fg.record_success()
            else:
                # Native Anthropic: streaming
                async with claude.messages.stream(
                    model=_selected_model,
                    max_tokens=1500,
                    system=system,
                    messages=[{"role": "user", "content": user_message}],
                ) as stream:
                    async for text in stream.text_stream:
                        yield text
                _cb_claude_fg.record_success()
        except Exception as e:
            _cb_claude_fg.record_failure()
            _err_str = str(e).lower()
            _is_timeout = "timeout" in _err_str
            if _is_timeout:
                log.warning("Streaming Claude timed out after %.0fs — direct fallback to backup", CLAUDE_TIMEOUT)
            else:
                log.error("Streaming LLM failed: %s — falling back to backup providers", e)
            from learning import record_signal
            record_signal("llm_failure", {"backend": "claude", "error": f"{type(e).__name__}: {e}"[:200]})
            # Try backup providers directly (skip re-entering ask_llm retry loop)
            _backup_msgs = [{"role": "system", "content": system}, {"role": "user", "content": user_message}]
            for _bp_attr, _bp_model in _BACKUP_PROVIDERS:
                _bp_client = globals().get(_bp_attr)
                if not _bp_client:
                    continue
                try:
                    _bp_resp = await _bp_client.chat.completions.create(
                        model=_bp_model, max_tokens=1500, messages=_backup_msgs, timeout=CLAUDE_TIMEOUT,
                    )
                    log.info("Streaming fallback: %s succeeded", _bp_model)
                    try:
                        record_signal("llm_fallback", {"primary": "claude", "provider": _bp_attr, "model": _bp_model, "reason": "streaming_timeout"})
                    except Exception:
                        pass
                    yield _bp_resp.choices[0].message.content
                    return
                except Exception as _bp_e:
                    log.warning("Streaming fallback %s failed: %s", _bp_model, _bp_e)
                    continue
            # All providers failed — last resort non-streaming
            result = await ask_llm(query, context, system_extra, model)
            yield result
            return
    else:
        # Ollama: stream via /api/chat with stream=true
        _routed_tier_o, _ = route_query(query)
        _ollama_system = system
        if _routed_tier_o.value != "complex" and "qwen3" in OLLAMA_LLM_MODEL:
            _ollama_system = system + "\n\n/no_think"

        try:
            async with httpx.AsyncClient(timeout=LLM_TIMEOUT) as client:
                async with client.stream(
                    "POST",
                    f"{OLLAMA_URL}/api/chat",
                    json={
                        "model": OLLAMA_LLM_MODEL,
                        "messages": [
                            {"role": "system", "content": _ollama_system},
                            {"role": "user", "content": user_message},
                        ],
                        "stream": True,
                    },
                ) as resp:
                    resp.raise_for_status()
                    import json as _json
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            data = _json.loads(line)
                            content = data.get("message", {}).get("content", "")
                            if content:
                                yield content
                        except _json.JSONDecodeError:
                            continue
        except Exception as e:
            log.warning("Ollama streaming failed: %s — falling back", e)
            result = await ask_llm(query, context, system_extra, model)
            yield result


# ---------------------------------------------------------------------------
# Tool-use LLM loop — the LLM picks tools, we execute them, loop until done
# ---------------------------------------------------------------------------

class _ToolCaptureContext:
    """Fake MessageContext that captures reply text instead of sending to Telegram.

    Passed to skill handlers during tool-use so their ctx.reply() calls
    are captured as tool results instead of sent to the user.
    """
    def __init__(self):
        self.captured: list[str] = []
        self._raw_update = None

    async def reply(self, text: str, **kwargs):
        if text:
            self.captured.append(text)
        return None

    async def reply_photo(self, *args, **kwargs):
        return None

    async def reply_voice(self, *args, **kwargs):
        return None

    async def typing(self):
        pass

    def get_result(self) -> str:
        return "\n".join(self.captured) if self.captured else "(no output)"


_MAX_TOOL_ITERATIONS = 12  # raised from 8 — compound artifacts (presentations, multi-file) need ~10 calls
_MAX_TOOL_AUTO_ITERATIONS = 10  # raised from 6 — 10 auto + 2 forced synthesis

# Phase-aware execution: tool categories for research cap enforcement
_RESEARCH_TOOLS = {"search_knowledge", "read_full_document", "web_search"}
_ACTION_TOOLS = {"generate_file", "shell", "delegate_tasks", "spawn_watcher"}


class _PhaseTracker:
    """Track tool-use phases within a single call_llm_with_tools invocation.

    For artifact tasks, enforces a research cap and escalates toward generate_file.
    For non-artifact tasks, only provides logging — no restrictions.
    """

    def __init__(self, is_artifact: bool):
        self.is_artifact = is_artifact
        self.consecutive_research = 0
        self.total_research = 0
        self.has_called_action = False
        self.generate_file_attempted = False
        self.generate_file_failed = False

    def record(self, tool_names: list[str]):
        """Record tools used this iteration."""
        if not tool_names:
            return
        has_research = any(t in _RESEARCH_TOOLS for t in tool_names)
        has_action = any(t in _ACTION_TOOLS for t in tool_names)
        if has_action:
            self.has_called_action = True
            self.consecutive_research = 0
            if "generate_file" in tool_names:
                self.generate_file_attempted = True
        elif has_research:
            self.consecutive_research += 1
            self.total_research += 1

    def get_config(self, iteration: int, base_tools: list[dict]) -> tuple:
        """Return (tool_choice, tools, phase_prompt) for this iteration.

        Escalation ladder (artifact tasks only):
        Level 0: free research (consecutive < 4 and total < 6)
        Level 1: nudge prompt (consecutive == 4 or total == 6)
        Level 2: remove research tools (consecutive == 5 or total == 7)
        Level 3: force generate_file via tool_choice
        """
        if iteration >= _MAX_TOOL_AUTO_ITERATIONS:
            return "none", base_tools, None

        # Non-artifact tasks: no phase restrictions
        if not self.is_artifact:
            return "auto", base_tools, None

        # If already called an action tool, don't interfere
        if self.has_called_action:
            return "auto", base_tools, None

        # Escalation is based on max(consecutive, total) to catch both patterns
        _level = max(self.consecutive_research, self.total_research)

        # Level 0: free research (< 4)
        if _level < 4:
            return "auto", base_tools, None

        # Level 1: nudge (4-4)
        if _level <= 4:
            return "auto", base_tools, (
                "You have gathered sufficient context. Call generate_file NOW "
                "with the information you have. Do not search further."
            )

        # Level 2: restrict — remove research tools (5)
        if _level <= 5:
            restricted = [t for t in base_tools
                          if t["function"]["name"] not in _RESEARCH_TOOLS]
            return "auto", restricted, (
                "Research tools are no longer available. "
                "Call generate_file to create the requested artifact."
            )

        # Level 3: force generate_file (6+)
        return (
            {"type": "function", "function": {"name": "generate_file"}},
            base_tools,
            "You MUST call generate_file now.",
        )


# Preamble detection: catch LLM responses that announce intent instead of delivering results
_PREAMBLE_RE = re.compile(
    r"^(now\s+)?(?:let\s+me\s+|i(?:'ll|\s+will)\s+(?:now\s+)?)"
    r"(?:gather|look|check|search|find|create|prepare|analyze|compile|review|examine)",
    re.IGNORECASE,
)


def _is_preamble_response(text: str) -> bool:
    """Detect responses that announce intent instead of delivering results."""
    if len(text) > 500:
        return False  # Long enough to contain real content
    return bool(_PREAMBLE_RE.search(text.strip()))



def _get_tool_source_path(action_type: str) -> str:
    """Map action_type to source file path via skill registry."""
    try:
        from skills import get_registry
        skill = get_registry().get_by_action(action_type)
        if skill:
            return f"actions/{skill.module_name}.py"
    except Exception:
        pass
    return f"actions/{action_type}.py"


def _check_result_needs_reflection(tool_name: str, result_text: str, query: str) -> str | None:
    """Check if a tool result is inadequate and return a reflection prompt if so.

    Injects a mandatory reflection directive into the tool result so the LLM
    MUST read it before deciding its next action. This prevents the LLM from
    ignoring poor results and working around them with shell commands.
    """
    result_lower = result_text.lower()

    # Skip error JSON — those already have suggestions
    if result_text.startswith('{"error":'):
        return None

    detail = None

    # High unmatched ratio
    processed_m = re.search(r"processed\s+(\d+)", result_lower)
    unmatched_m = re.search(r"(\d+)\s+unmatched", result_lower)
    if processed_m and unmatched_m:
        processed = int(processed_m.group(1))
        unmatched = int(unmatched_m.group(1))
        if processed > 0 and unmatched / processed > 0.5:
            detail = f"{unmatched}/{processed} unmatched ({unmatched * 100 // processed}%)"

    # Zero results
    if not detail and re.search(r"\b0\s+(?:labeled|processed|matched|found)\b", result_lower):
        detail = "zero results"

    # Command not found (tool confusion — tried shell instead of action)
    if not detail and "command not found" in result_lower:
        detail = "command not found"

    if not detail:
        return None

    source_path = _get_tool_source_path(tool_name)
    khalil_dir = "~/Developer/Personal/scripts/khalil"
    log.info("Tool result reflection injected for '%s': %s", tool_name, detail)

    return (
        f"\n⚠️ SELF-IMPROVEMENT REQUIRED — This tool produced poor results ({detail}).\n"
        f"\n"
        f"MANDATORY STEPS (do these IN ORDER before anything else):\n"
        f"\n"
        f"1. READ the tool's source code:\n"
        f"   shell(command=\"cat {khalil_dir}/{source_path}\")\n"
        f"\n"
        f"2. DIAGNOSE: Explain to Ahmed WHY results were poor. "
        f"Look at the matching logic, rules, default parameters, and what's missing.\n"
        f"\n"
        f"3. PROPOSE: List specific changes to {source_path} that would fix this. "
        f"Include which functions to modify and what the fix looks like.\n"
        f"   Also check: does this tool support what the user actually asked for? "
        f"(e.g., does it archive emails, or only label them?)\n"
        f"\n"
        f"4. ASK Ahmed: \"Want me to implement these changes and open a PR?\"\n"
        f"\n"
        f"RULES:\n"
        f"- Do NOT create new scripts, files, or workarounds.\n"
        f"- Do NOT use shell to build a replacement solution.\n"
        f"- The fix MUST go into {source_path} — improve the existing tool.\n"
        f"- If the tool is missing a feature (e.g., archiving), propose adding it to THIS tool."
    )


async def _execute_tool_call(tool_call) -> str:
    """Execute a single tool call from the LLM and return the result text.

    Tool name IS the action_type (one-tool-per-action design).
    Falls back to legacy action-enum extraction for backward compat.
    """
    import time as _time
    from skills import get_registry
    registry = get_registry()

    fn_name = tool_call.function.name
    try:
        args = json.loads(tool_call.function.arguments)
    except json.JSONDecodeError:
        _record_tool_analytics(fn_name, "", False, 0, error="invalid_json")
        return json.dumps({"error": True, "type": "invalid_input",
                           "message": f"Invalid JSON arguments for {fn_name}",
                           "suggestion": "Check parameter format and try again"})

    # Validate required parameters before dispatch
    _missing = _check_required_params(fn_name, args)
    if _missing:
        _record_tool_analytics(fn_name, json.dumps(args), False, 0, error="missing_params")
        return json.dumps({"error": True, "type": "missing_params",
                           "message": f"{fn_name} requires: {', '.join(_missing)}",
                           "suggestion": f"Call {fn_name} again with the required parameters"})

    # M7: Pre-fetch context before tool execution
    try:
        from knowledge.prefetch import prefetch_for_tool
        args = await prefetch_for_tool(fn_name, args)
    except Exception as e:
        log.debug("Prefetch failed for %s: %s", fn_name, e)

    # Handle search_knowledge tool directly (not from skill registry)
    if fn_name == "search_knowledge":
        search_query = args.get("query", "")
        if not search_query:
            return json.dumps({"error": True, "message": "Missing query parameter"})
        try:
            results = await asyncio.wait_for(hybrid_search(search_query, limit=8), timeout=15.0)
            if not results:
                _record_tool_analytics(fn_name, search_query, True, 0)
                return json.dumps({"results": [], "message": "No results found in knowledge base."})
            formatted = []
            for doc in results:
                formatted.append({
                    "title": doc.get("title", "")[:100],
                    "category": doc.get("category", ""),
                    "content": doc.get("content", "")[:1500],
                })
            _record_tool_analytics(fn_name, search_query, True, 0)
            return json.dumps({"results": formatted, "count": len(formatted)})
        except asyncio.TimeoutError:
            _record_tool_analytics(fn_name, search_query, False, 15, error="timeout")
            return json.dumps({"error": True, "message": "Knowledge search timed out (15s). Try a simpler query."})
        except Exception as e:
            _record_tool_analytics(fn_name, search_query, False, 0, error=str(e)[:200])
            return json.dumps({"error": True, "message": f"Search failed: {e}"})

    # Handle read_full_document tool — reassemble full document from KB chunks
    if fn_name == "read_full_document":
        category = args.get("category", "")
        title_prefix = args.get("title_prefix", "")
        max_chars = args.get("max_chars", 8000)
        if not category:
            return json.dumps({"error": True, "message": "Missing category parameter"})
        try:
            import sqlite3 as _sql
            from config import DB_PATH
            _conn = _sql.connect(str(DB_PATH))
            _conn.row_factory = _sql.Row

            if title_prefix:
                rows = _conn.execute(
                    "SELECT title, content FROM documents WHERE category = ? AND title LIKE ? "
                    "ORDER BY id",
                    (category, f"%{title_prefix}%"),
                ).fetchall()
            else:
                rows = _conn.execute(
                    "SELECT title, content FROM documents WHERE category = ? ORDER BY id",
                    (category,),
                ).fetchall()
            _conn.close()

            if not rows:
                # Try prefix match on category
                _conn = _sql.connect(str(DB_PATH))
                _conn.row_factory = _sql.Row
                rows = _conn.execute(
                    "SELECT title, content FROM documents WHERE category LIKE ? ORDER BY id LIMIT 50",
                    (category + "%",),
                ).fetchall()
                _conn.close()

            if not rows:
                _record_tool_analytics(fn_name, category, False, 0, error="not_found")
                return json.dumps({"error": True, "message": f"No documents found for category '{category}'"})

            # Reassemble chunks into full document
            full_text = ""
            current_title = ""
            for r in rows:
                title = r["title"]
                content = r["content"]
                if title != current_title:
                    if current_title:
                        full_text += "\n\n---\n\n"
                    full_text += f"## {title}\n\n"
                    current_title = title
                full_text += content + "\n"
                if len(full_text) >= max_chars:
                    full_text = full_text[:max_chars] + "\n\n[... truncated at max_chars]"
                    break

            _record_tool_analytics(fn_name, f"{category}/{title_prefix}", True, 0)
            return json.dumps({
                "success": True,
                "category": category,
                "chunks": len(rows),
                "chars": len(full_text),
                "content": full_text,
            })
        except Exception as e:
            _record_tool_analytics(fn_name, category, False, 0, error=str(e)[:200])
            return json.dumps({"error": True, "message": f"Read failed: {e}"})

    # Handle generate_file tool — artifact generation mode
    if fn_name == "generate_file":
        description = args.get("description", "")
        target_path = args.get("target_path", "")
        file_type = args.get("file_type", "")
        if not description:
            return json.dumps({"error": True, "message": "Missing description parameter"})
        if not target_path:
            slug = re.sub(r'[^\w\s-]', '', description.lower())[:40].strip().replace(' ', '-')
            target_path = f"~/Developer/Personal/presentations/{slug}/index.html"

        try:
            # Build context from conversation + KB search
            _ctx_parts = []
            try:
                kb = await asyncio.wait_for(hybrid_search(description, limit=8), timeout=15.0)
                if kb:
                    _ctx_parts.append("\n".join(
                        f"[{d.get('category','')}] {d.get('title','')}\n{d.get('content','')[:600]}"
                        for d in kb
                    ))
            except Exception:
                pass

            context_text = "\n\n".join(_ctx_parts) if _ctx_parts else "No additional context found."

            # Determine file type
            import os as _os
            ext = _os.path.splitext(target_path)[1].lstrip('.') or file_type or 'html'
            type_label = {'html': 'HTML', 'py': 'Python', 'md': 'Markdown', 'css': 'CSS',
                          'js': 'JavaScript', 'json': 'JSON', 'sh': 'Bash'}.get(ext, ext.upper())

            # Model cascade with retry — try multiple models until one succeeds
            gen_system = (
                f"Generate a COMPLETE {type_label} file. Output ONLY the file content — "
                "no explanation, no markdown fences, no commentary. "
                "Make it production-quality, well-structured, and complete."
            )
            gen_user = (
                f"Description: {description}\n\n"
                f"Context from knowledge base:\n{context_text}\n\n"
                f"Generate the complete {type_label} file now."
            )

            from config import CLAUDE_MODEL_FAST
            _GEN_CASCADE = [
                (CLAUDE_MODEL, 300.0, "opus"),        # 5 min — 16K token generation takes 3-5 min
                (CLAUDE_MODEL_FAST, 180.0, "sonnet"),  # 3 min — faster model, still needs time
            ]
            # Add Ollama as final fallback if available
            if LLM_BACKEND == "ollama" or OLLAMA_URL:
                _GEN_CASCADE.append(("local", 180.0, "ollama"))

            content = ""
            _used_model = ""
            _attempts = 0

            for _model, _timeout, _label in _GEN_CASCADE:
                _attempts += 1
                try:
                    if _model == "local":
                        # Ollama fallback
                        import httpx as _hx
                        async with _hx.AsyncClient(timeout=_timeout) as _ollama_c:
                            _resp = await _ollama_c.post(
                                f"{OLLAMA_URL}/api/chat",
                                json={"model": OLLAMA_LLM_MODEL, "stream": False,
                                      "messages": [{"role": "system", "content": gen_system},
                                                   {"role": "user", "content": gen_user}]},
                            )
                            _resp.raise_for_status()
                            content = _resp.json()["message"]["content"]
                    elif _taskforce_client_long or _taskforce_client:
                        # Use separate long-running client to avoid holding main pool connections
                        _gen_client = _taskforce_client_long or _taskforce_client
                        resp = await asyncio.wait_for(
                            _gen_client.chat.completions.create(
                                model=_model, max_tokens=16000,
                                messages=[{"role": "system", "content": gen_system},
                                          {"role": "user", "content": gen_user}],
                                timeout=_timeout, temperature=0.3,
                            ),
                            timeout=_timeout,
                        )
                        content = resp.choices[0].message.content or ""
                    elif claude:
                        resp = await asyncio.wait_for(
                            claude.messages.create(
                                model=_model, max_tokens=16000, system=gen_system,
                                messages=[{"role": "user", "content": gen_user}],
                                timeout=_timeout,
                            ),
                            timeout=_timeout,
                        )
                        content = resp.content[0].text
                    else:
                        continue

                    if content and len(content) >= 50:
                        _used_model = _label
                        log.info("generate_file succeeded with %s (attempt %d)", _label, _attempts)
                        break
                    else:
                        log.warning("generate_file %s returned short content (%d chars), trying next", _label, len(content))
                        content = ""
                except (asyncio.TimeoutError, Exception) as _e:
                    log.warning("generate_file %s failed (attempt %d): %s", _label, _attempts, str(_e)[:100])
                    try:
                        from learning import record_signal
                        record_signal("artifact_failed", {
                            "model": _label, "attempt": _attempts,
                            "error": str(_e)[:200], "description": description[:200],
                        })
                    except Exception:
                        pass
                    continue

            if not content or len(content) < 50:
                _record_tool_analytics(fn_name, description, False, 0, error=f"all_models_failed_after_{_attempts}_attempts")
                return json.dumps({"error": True, "message": f"File generation failed after {_attempts} attempts across all models. The API may be overloaded — try again in a few minutes."})

            # Strip markdown fences if present
            if content.startswith("```"):
                _lines = content.split("\n")
                _lines = _lines[1:]
                if _lines and _lines[-1].strip().startswith("```"):
                    _lines = _lines[:-1]
                content = "\n".join(_lines)

            # Write to disk
            from pathlib import Path as _Path
            expanded = _os.path.expanduser(target_path)
            _Path(expanded).parent.mkdir(parents=True, exist_ok=True)
            _Path(expanded).write_text(content, encoding="utf-8")

            # VERIFY: file actually exists and has content
            if not _Path(expanded).exists() or _Path(expanded).stat().st_size < 50:
                _record_tool_analytics(fn_name, description, False, 0, error="verification_failed")
                return json.dumps({"error": True, "message": f"Verification failed: {expanded} was not created or is empty"})

            line_count = content.count("\n") + 1
            _record_tool_analytics(fn_name, description, True, 0)
            try:
                from learning import record_signal
                record_signal("artifact_generated", {
                    "path": expanded, "type": type_label,
                    "lines": line_count, "model": _used_model, "attempts": _attempts,
                })
            except Exception:
                pass

            return json.dumps({
                "success": True,
                "path": expanded,
                "lines": line_count,
                "chars": len(content),
                "model": _used_model,
                "attempts": _attempts,
                "message": f"Created {expanded} ({line_count} lines, {len(content):,} chars) via {_used_model}",
            })
        except Exception as e:
            _record_tool_analytics(fn_name, description, False, 0, error=str(e)[:200])
            return json.dumps({"error": True, "message": f"Generation failed: {e}"})

    # Handle delegate_tasks tool — parallel sub-agent execution
    if fn_name == "delegate_tasks":
        tasks = args.get("tasks", [])
        if not tasks or not isinstance(tasks, list):
            return json.dumps({"error": True, "message": "Missing or invalid tasks array"})
        if len(tasks) > 5:
            tasks = tasks[:5]
        try:
            from agents.pool import fan_out_named
            task_dict = {f"task_{i+1}": t for i, t in enumerate(tasks)}
            results = await asyncio.wait_for(fan_out_named(task_dict), timeout=45.0)
            _record_tool_analytics(fn_name, json.dumps(tasks)[:200], True, 0)
            return json.dumps({
                "success": True,
                "results": results,
                "count": len(results),
            })
        except asyncio.TimeoutError:
            _record_tool_analytics(fn_name, json.dumps(tasks)[:200], False, 45, error="timeout")
            return json.dumps({"error": True, "message": "Parallel tasks timed out (45s)"})
        except Exception as e:
            _record_tool_analytics(fn_name, json.dumps(tasks)[:200], False, 0, error=str(e)[:200])
            return json.dumps({"error": True, "message": f"Delegation failed: {e}"})

    # Handle spawn_watcher tool — background task with completion condition
    if fn_name == "spawn_watcher":
        task = args.get("task", "")
        condition = args.get("condition", "")
        follow_up = args.get("follow_up")
        if not task or not condition:
            return json.dumps({"error": True, "message": "Missing task or condition"})
        try:
            from agents.coordinator import spawn_background_agent
            agent = spawn_background_agent(
                task=task,
                context={"query": task},
                completion_condition=condition,
                follow_up_action=follow_up,
            )
            _record_tool_analytics(fn_name, task[:200], True, 0)
            return json.dumps({
                "success": True,
                "agent_id": agent.id,
                "message": f"Background watcher started: {agent.id}. Monitoring: {condition}",
            })
        except Exception as e:
            _record_tool_analytics(fn_name, task[:200], False, 0, error=str(e)[:200])
            return json.dumps({"error": True, "message": f"Watcher failed: {e}"})

    # New: tool name IS the action (one-tool-per-action)
    action = fn_name

    # Legacy compat: if caller sent an "action" param, use it
    if "action" in args:
        action = args.pop("action")

    # Look up handler from skill registry
    handler = registry.get_handler(action)
    if handler is None:
        # Fallback: try the function name as skill name (old pattern)
        handler = registry.get_handler(fn_name)
        if handler is None:
            _record_tool_analytics(fn_name, json.dumps(args), False, 0, error="no_handler")
            return json.dumps({"error": True, "type": "unknown_tool",
                               "message": f"No handler found for '{fn_name}'",
                               "suggestion": "This tool may not be available. Try a different approach."})

    # Execute with a capture context so replies become tool results
    capture_ctx = _ToolCaptureContext()
    intent = {"action": action, **args, "tool_mode": True}

    t0 = _time.monotonic()
    _audit_success = False
    _audit_error = ""
    _result_text = ""
    try:
        result = await asyncio.wait_for(
            handler(action, intent, capture_ctx),
            timeout=60,  # increased from 30s for shell/claude_code
        )
        elapsed = _time.monotonic() - t0
        _result_text = capture_ctx.get_result()
        _record_tool_analytics(fn_name, json.dumps(args), True, elapsed)
        _audit_success = True
        return _result_text
    except asyncio.TimeoutError:
        elapsed = _time.monotonic() - t0
        _audit_error = "timeout"
        _record_tool_analytics(fn_name, json.dumps(args), False, elapsed, error="timeout")
        _result_text = json.dumps({"error": True, "type": "timeout",
                           "message": f"{fn_name} timed out after 60s",
                           "suggestion": "The operation took too long. Try a simpler request."})
        return _result_text
    except Exception as e:
        elapsed = _time.monotonic() - t0
        _audit_error = str(e)[:200]
        _record_tool_analytics(fn_name, json.dumps(args), False, elapsed, error=_audit_error)
        _result_text = json.dumps({"error": True, "type": "execution_error",
                           "message": f"{fn_name} failed: {_audit_error}",
                           "suggestion": "Check the parameters and try again, or use an alternative approach."})
        return _result_text
    finally:
        # #19: Audit trail — structured JSONL per tool execution
        try:
            _audit_entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "tool": fn_name,
                "action": action,
                "args_summary": json.dumps(args)[:200],
                "success": _audit_success,
                "error": _audit_error,
                "latency_ms": int((_time.monotonic() - t0) * 1000),
                "result_summary": _result_text[:200] if _result_text else "",
            }
            from config import DATA_DIR
            _audit_path = DATA_DIR / "audit_trail.jsonl"
            with open(_audit_path, "a") as _af:
                _af.write(json.dumps(_audit_entry) + "\n")
        except Exception:
            pass  # Audit should never break tool execution


def _check_required_params(tool_name: str, args: dict) -> list[str]:
    """Check if required parameters are present. Returns list of missing param names."""
    _REQUIRED = {
        "calendar_create": ["summary", "start_time"],
        "email": ["to", "subject"],
        "reminder": ["text"],
        "send_to_terminal": ["command"],
        "send_to_claude": ["command", "target"],
        "shell": ["command"],
        "type_text": ["command"],
        "click": ["command"],
    }
    required = _REQUIRED.get(tool_name, [])
    return [p for p in required if p not in args or not args[p]]


def _record_tool_analytics(tool_name: str, params: str, success: bool,
                           latency: float, error: str = ""):
    """Record tool usage analytics to the DB."""
    try:
        db_conn.execute(
            "INSERT INTO tool_analytics (tool_name, params, success, latency_s, error, created_at) "
            "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (tool_name, params[:2000], success, round(latency, 3), error[:500]),
        )
        db_conn.commit()
    except Exception:
        pass  # analytics should never break tool execution

    # #21: Update trust scores for autonomy promotion/demotion
    try:
        if autonomy:
            if success:
                autonomy.record_tool_success(tool_name)
            else:
                autonomy.record_tool_failure(tool_name)
    except Exception:
        pass


def _get_failing_tools() -> str:
    """Query tool_analytics for recently failing tools (#46).

    Returns a system prompt fragment warning the LLM about broken tools.
    """
    try:
        rows = db_conn.execute("""
            SELECT tool_name, COUNT(*) as fails, MAX(error) as last_error
            FROM tool_analytics
            WHERE success = 0
              AND created_at > datetime('now', '-1 hour')
            GROUP BY tool_name
            HAVING fails >= 2
            ORDER BY fails DESC
            LIMIT 5
        """).fetchall()
        if not rows:
            return ""
        lines = ["KNOWN TOOL ISSUES (last hour):"]
        for r in rows:
            lines.append(f"- {r['tool_name']}: {r['fails']} failures ({r['last_error'][:80]})")
        lines.append("Avoid these tools or try alternative approaches.\n")
        return "\n".join(lines)
    except Exception:
        return ""


def _build_system_prompt(query: str, style_hint: str = "", system_extra: str = "") -> str:
    """Build the system prompt for tool-use calls. Shared with ask_llm."""
    from datetime import datetime as _dt
    import zoneinfo
    _now = _dt.now(zoneinfo.ZoneInfo(TIMEZONE))
    _temporal = (
        f"CURRENT TIME: {_now.strftime('%A, %B %d, %Y at %I:%M %p %Z')} "
        f"(Q{(_now.month - 1) // 3 + 1} {_now.year})\n\n"
    )

    # Inject known-broken tools to prevent wasted calls (#46)
    _failing = _get_failing_tools()

    # Dynamic capability counts
    _doc_count = ""
    try:
        _n = db_conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        _doc_count = f"Knowledge base has {_n:,} indexed documents. "
    except Exception:
        pass

    return (
        f"{_temporal}"
        f"{KHALIL_IDENTITY}"
        f"{_doc_count}"
        "TOOL RULES:\n"
        "- Call tools to DO things or CHECK things. Don't use tools for greetings or chat.\n"
        "- If a tool fails, try a different approach. Don't retry the same call.\n"
        "- Summarize tool results in natural language.\n"
        "- For DB queries: shell(command=\"sqlite3 data/khalil.db 'YOUR SQL'\")\n\n"
        f"{_failing}"
        f"{style_hint}"
        f"{system_extra}"
    )


async def call_llm_with_tools(
    query: str,
    context: str,
    chat_id: int | str,
    progress_msg,
    channel,
    system_extra: str = "",
) -> str:
    """LLM tool-use loop: the LLM picks tools, we execute, loop until text response.

    Returns the final display text. Streams the final response to Telegram.
    """
    log.info("call_llm_with_tools called: query=%s", query[:80])
    from tool_catalog import generate_tool_schemas, filter_tools_for_query
    from skills import get_registry

    # Skip tools only for pure conversational messages (exact match only).
    # "hey what's the weather" should NOT bypass — only "hey", "thanks", etc.
    _q = query.strip().lower().rstrip("?!. ")
    _GREETINGS = {"hey", "hi", "hello", "yo", "sup", "thanks", "thank you",
                  "ok", "okay", "cool", "got it", "nice", "good", "great",
                  "sure", "yes", "no", "nah", "yep", "nope", "hmm", "hm"}
    _conversational = _q in _GREETINGS  # exact match only — no more "first word" heuristic
    if _conversational:
        log.info("Conversational bypass for: %s", query[:50])
        stream_gen = ask_llm_stream(query, context, system_extra)
        result = await stream_to_telegram(chat_id, progress_msg, stream_gen, channel)
        log.info("Conversational response: %d chars", len(result))
        return result

    registry = get_registry()
    all_tools = generate_tool_schemas(registry)
    if not all_tools:
        # No tools available — fall back to plain streaming
        stream_gen = ask_llm_stream(query, context, system_extra)
        return await stream_to_telegram(chat_id, progress_msg, stream_gen, channel)

    # Dynamic tool filtering: expose only relevant tools per query (#62)
    tools = filter_tools_for_query(query, registry, all_tools)

    # Build system prompt
    style_hint = ""
    try:
        from learning import get_active_response_preferences
        style_hint = get_active_response_preferences()
    except Exception:
        pass

    system = _build_system_prompt(query, style_hint, system_extra)
    user_message = f"Context from personal archives:\n\n{context}\n\n---\n\nQuestion: {query}"

    # Inject recent conversation history for reference resolution
    # ("send that to John" → knows what "that" refers to)
    conversation_history = get_conversation_history(chat_id)
    messages = [{"role": "system", "content": system}]
    if conversation_history:
        messages.append({"role": "system", "content": conversation_history})
    messages.append({"role": "user", "content": user_message})

    # Model selection
    from model_router import route_query
    _, _routed_model = route_query(query)

    # Privacy routing — force Ollama for sensitive queries
    _force_local = any(re.search(p, query, re.IGNORECASE) for p in SENSITIVE_PATTERNS)
    if _force_local or not _taskforce_client:
        # Fall back to non-tool streaming for local/Ollama
        stream_gen = ask_llm_stream(query, context, system_extra)
        return await stream_to_telegram(chat_id, progress_msg, stream_gen, channel)

    # Tool-use loop
    # Phase-aware execution: tracks research vs action tools, escalates for artifact tasks.
    # Gate: suppress background summarization during active tool-use
    from intent import is_artifact_request as _is_artifact_req
    _is_artifact = _is_artifact_req(query)
    _phase = _PhaseTracker(is_artifact=_is_artifact)
    _tool_loop_active.add(int(chat_id))
    _progress_steps = []
    try:  # try/finally to ensure _tool_loop_active cleanup
     for iteration in range(_MAX_TOOL_ITERATIONS):
        # Phase-aware tool_choice and tool set
        _tc, _iter_tools, _phase_prompt = _phase.get_config(iteration, tools)
        if _phase_prompt:
            messages.append({"role": "user", "content": _phase_prompt})
            log.info("Phase[%d] escalation: %s", iteration, _phase_prompt[:80])

        # When switching to tool_choice="none", inject synthesis instruction
        # so the LLM knows to deliver results, not announce plans
        if iteration == _MAX_TOOL_AUTO_ITERATIONS and any(
            isinstance(m, dict) and m.get("role") == "tool" for m in messages
        ):
            messages.append({
                "role": "user",
                "content": (
                    "You have completed your tool calls. Now synthesize all the information "
                    "gathered above into a clear, complete response to the user's original question. "
                    "Do NOT announce what you plan to do — provide the actual answer."
                ),
            })

        # Circuit breaker check — don't waste iterations on a known-broken API
        if _cb_claude_fg.is_open():
            log.warning("Foreground circuit breaker open at iteration %d — falling back to streaming", iteration)
            stream_gen = ask_llm_stream(query, context, system_extra)
            return await stream_to_telegram(chat_id, progress_msg, stream_gen, channel)

        # API call with 1 retry for transient errors (429, 503, timeout)
        # Timeout scales with iteration — later iterations have larger message histories
        _tool_timeout = CLAUDE_TIMEOUT + (iteration * 3)  # 15s base + 3s per iteration
        response = None
        for _tool_attempt in range(2):
            try:
                response = await _taskforce_client.chat.completions.create(
                    model=_routed_model,
                    max_tokens=4000,
                    messages=messages,
                    tools=_iter_tools,
                    tool_choice=_tc,
                    timeout=_tool_timeout,
                    temperature=0.0,
                )
                _cb_claude_fg.record_success()
                break
            except Exception as e:
                _cb_claude_fg.record_failure()
                _err_str = str(e).lower()
                _is_transient = any(s in _err_str for s in ("429", "rate", "overloaded", "503", "timeout", "timed out"))
                if _is_transient and _tool_attempt == 0:
                    log.warning("Tool-use transient error (iteration %d), retrying in 2s: %s", iteration, e)
                    await asyncio.sleep(2.0)
                    continue
                log.error("Tool-use LLM call failed (iteration %d, attempt %d): %s", iteration, _tool_attempt + 1, e)
                if iteration == 0:
                    stream_gen = ask_llm_stream(query, context, system_extra)
                    return await stream_to_telegram(chat_id, progress_msg, stream_gen, channel)
                response = None
                break
        if response is None:
            break

        choice = response.choices[0]
        msg = choice.message

        # #12: Track token usage and cost per iteration
        _usage = getattr(response, 'usage', None)
        if _usage:
            _prompt_tok = getattr(_usage, 'prompt_tokens', 0) or 0
            _completion_tok = getattr(_usage, 'completion_tokens', 0) or 0
            # Approximate pricing (per 1M tokens): Sonnet input=$3, output=$15; Opus input=$15, output=$75
            _is_opus = "opus" in (_routed_model or "").lower()
            _input_rate = 15.0 if _is_opus else 3.0  # per 1M tokens
            _output_rate = 75.0 if _is_opus else 15.0
            _cost_usd = (_prompt_tok * _input_rate + _completion_tok * _output_rate) / 1_000_000
            try:
                from learning import record_signal
                record_signal("llm_token_usage", {
                    "model": _routed_model,
                    "prompt_tokens": _prompt_tok,
                    "completion_tokens": _completion_tok,
                    "cost_usd": round(_cost_usd, 6),
                    "iteration": iteration,
                })
            except Exception:
                pass

        log.info("Tool-use iteration %d: finish_reason=%s, tool_calls=%s, content_len=%d",
                 iteration, choice.finish_reason,
                 [tc.function.name for tc in msg.tool_calls] if msg.tool_calls else None,
                 len(msg.content or ""))

        # If the model returned tool calls, execute them
        if msg.tool_calls:
            # Append assistant message with tool calls
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in msg.tool_calls
                ],
            })

            # Update progress message to show what's happening (accumulative)
            tool_names = [tc.function.name for tc in msg.tool_calls]
            _step_label = f"Step {iteration + 1}: {', '.join(tool_names)}"
            if iteration == 0:
                _progress_steps = [_step_label]
            else:
                _progress_steps.append(_step_label)
            _progress_text = "🔧 " + " → ".join(_progress_steps) + "..."
            await _safe_edit(progress_msg, _progress_text[:4096])

            # Execute each tool call and save to conversation history
            for tc in msg.tool_calls:
                log.info("Tool call: %s(%s)", tc.function.name, tc.function.arguments[:100])

                # Save tool call to DB
                save_message(chat_id, "assistant", tc.function.arguments[:2000],
                             message_type="tool_call",
                             metadata=json.dumps({"tool_name": tc.function.name,
                                                  "tool_call_id": tc.id}))

                result_text = await _execute_tool_call(tc)

                # Save tool result to DB (raw, without reflection)
                save_message(chat_id, "tool", result_text[:2000],
                             message_type="tool_result",
                             metadata=json.dumps({"tool_name": tc.function.name,
                                                  "tool_call_id": tc.id}))

                # Inject reflection if tool result looks inadequate (first 2 iterations only)
                if iteration < 2:
                    _reflection = _check_result_needs_reflection(
                        tc.function.name, result_text, query)
                    if _reflection:
                        result_text = result_text + "\n\n" + _reflection

                # Smart truncation: keep first 5000 + last 2000 chars
                if len(result_text) > 8000:
                    result_text = result_text[:5000] + "\n\n[...truncated...]\n\n" + result_text[-2000:]
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_text[:8000],
                })

            # Phase tracking: record which tools were used this iteration
            _iter_tool_names = [tc.function.name for tc in msg.tool_calls]
            _phase.record(_iter_tool_names)
            # Detect generate_file failure
            for tc in msg.tool_calls:
                if tc.function.name == "generate_file":
                    for m in reversed(messages):
                        if isinstance(m, dict) and m.get("tool_call_id") == tc.id:
                            if '"error"' in m.get("content", "")[:200]:
                                _phase.generate_file_failed = True
                            break
            log.info("Phase[%d]: consec_research=%d, total_research=%d, has_action=%s, gen=%s",
                     iteration, _phase.consecutive_research, _phase.total_research,
                     _phase.has_called_action, _phase.generate_file_attempted)

            # Continue loop — LLM will reason about tool results
            continue

        # No tool calls — this is the final text response
        final_text = msg.content or ""
        _phase.record([])
        log.info("Tool-use final response: %d chars", len(final_text))

        # Preamble interception for artifact tasks — re-enter loop instead of exiting
        if _is_artifact and iteration < 8 and _is_preamble_response(final_text):
            log.warning("Preamble at iteration %d — re-entering loop for artifact task", iteration)
            messages.append({"role": "assistant", "content": final_text})
            messages.append({"role": "user", "content":
                "That was a preamble. Call generate_file NOW. Do not describe — ACT."})
            continue

        # Preamble detection: LLM announced intent instead of delivering results
        _has_tool_results = any(isinstance(m, dict) and m.get("role") == "tool" for m in messages)
        if final_text and _is_preamble_response(final_text) and _has_tool_results:
            log.warning("Preamble detected (%d chars) — retrying with synthesis prompt", len(final_text))
            messages.append({"role": "assistant", "content": final_text})
            messages.append({
                "role": "user",
                "content": (
                    "That response just announced what you plan to do instead of providing the answer. "
                    "Please synthesize all the tool results above into a complete response NOW."
                ),
            })
            try:
                _synth_resp = await _taskforce_client.chat.completions.create(
                    model=_routed_model,
                    max_tokens=4000,
                    messages=messages,
                    tool_choice="none",
                    timeout=CLAUDE_TIMEOUT,
                    temperature=0.0,
                )
                _retry_text = _synth_resp.choices[0].message.content or ""
                if _retry_text and len(_retry_text) > len(final_text):
                    final_text = _retry_text
                    log.info("Preamble retry produced %d chars", len(final_text))
            except Exception as _pre_e:
                log.error("Preamble synthesis retry failed: %s", _pre_e)

        # Programmatic artifact fallback — if LLM never called generate_file despite
        # having research results, construct the call directly (bypasses LLM refusal).
        if _is_artifact and _phase.total_research >= 2 and not _phase.generate_file_attempted:
            log.warning("Programmatic generate_file fallback — LLM never called generate_file")
            _collected = "\n\n".join(
                m["content"][:1000] for m in messages
                if isinstance(m, dict) and m.get("role") == "tool"
            )
            _artifact_path = _extract_artifact_path(query)
            if not _artifact_path:
                slug = re.sub(r'[^\w\s-]', '', query.lower())[:50].strip().replace(' ', '-')
                _artifact_path = os.path.expanduser(
                    f"~/Developer/Personal/presentations/{slug}/index.html"
                )

            class _SynthToolCall:
                class function:
                    name = "generate_file"
                    arguments = json.dumps({
                        "description": f"{query}\n\nContext gathered:\n{_collected[:3000]}",
                        "target_path": _artifact_path,
                    })
                id = f"programmatic_fallback_{iteration}"

            try:
                _gen_result = await _execute_tool_call(_SynthToolCall())
                if '"success"' in _gen_result[:200]:
                    final_text = _gen_result
                    log.info("Programmatic generate_file fallback succeeded")
                else:
                    log.warning("Programmatic fallback returned: %s", _gen_result[:200])
            except Exception as _pf_e:
                log.warning("Programmatic generate_file fallback failed: %s", _pf_e)

        if final_text:
            await _safe_edit(progress_msg, final_text[:4096])
        else:
            # Empty response after tool calls — force a synthesis retry
            log.warning("Tool-use returned empty final text — injecting synthesis prompt")
            # Collect what tools found
            _tool_summaries = []
            for m in messages:
                if isinstance(m, dict) and m.get("role") == "tool":
                    _tool_summaries.append(m["content"][:500])
            if _tool_summaries:
                # Ask the LLM to synthesize the gathered information
                messages.append({
                    "role": "user",
                    "content": (
                        "You gathered information using tools above but didn't produce a response. "
                        "Please synthesize all the tool results into a complete, helpful answer "
                        "to the original question. Do not call any more tools."
                    ),
                })
                try:
                    _synth_resp = await _taskforce_client.chat.completions.create(
                        model=_routed_model,
                        max_tokens=4000,
                        messages=messages,
                        tool_choice="none",
                        timeout=CLAUDE_TIMEOUT,
                        temperature=0.0,
                    )
                    final_text = _synth_resp.choices[0].message.content or ""
                    log.info("Synthesis retry produced %d chars", len(final_text))
                except Exception as _synth_e:
                    log.error("Synthesis retry failed: %s", _synth_e)
            if final_text:
                await _safe_edit(progress_msg, final_text[:4096])
            else:
                log.warning("Tool-use empty even after synthesis retry")
                # Last resort: summarize what was done
                _step_summary = " → ".join(_progress_steps) if _progress_steps else "research"
                await _safe_edit(
                    progress_msg,
                    f"I completed {_step_summary} but couldn't generate a final response. "
                    "Could you try rephrasing or breaking this into smaller steps?",
                )

        # Post-interaction reflection for tool-use path (mirrors non-tool path at ~5076)
        try:
            from evolution import post_interaction_check
            _gap_tag_re = re.compile(r'\[CAPABILITY_GAP:\s*(\w+)\s*\|\s*(/\w+)\s*\|\s*(.+?)\]')
            _gap_tags = _gap_tag_re.findall(final_text)
            # Collect tool results for adequacy analysis
            _tool_results = [
                m["content"] for m in messages
                if isinstance(m, dict) and m.get("role") == "tool"
            ]
            asyncio.create_task(post_interaction_check(
                query, final_text, 0.0, gap_tags=_gap_tags,
                tool_results=_tool_results,
            ))
        except Exception:
            pass

        # Task state tracking via TaskManager (replaces pending_task hack)
        _tool_names_used = [s.split(": ", 1)[-1] for s in _progress_steps]
        try:
            from task_manager import TaskManager
            from verification import update_task_after_response
            _tmgr = TaskManager()
            _active = _tmgr.get_active_task(chat_id)
            if _active:
                _tool_results = [
                    m["content"] for m in messages
                    if isinstance(m, dict) and m.get("role") == "tool"
                ]
                update_task_after_response(
                    _tmgr, _active, final_text, _tool_names_used, _tool_results,
                )
        except Exception as _te:
            log.debug("Task state update failed: %s", _te)

        return final_text

     # Exhausted iterations — try one final synthesis before giving up
     log.warning("Tool-use loop exhausted %d iterations — attempting final synthesis", _MAX_TOOL_ITERATIONS)
     messages.append({
        "role": "user",
        "content": (
            "You've used all available tool iterations. Based on everything gathered so far, "
            "please provide your best answer to the original question. Do not call any more tools."
        ),
     })
     try:
        _final_resp = await _taskforce_client.chat.completions.create(
            model=_routed_model,
            max_tokens=4000,
            messages=messages,
            tool_choice="none",
            timeout=CLAUDE_TIMEOUT,
            temperature=0.0,
        )
        final_text = _final_resp.choices[0].message.content or ""
        if final_text:
            await _safe_edit(progress_msg, final_text[:4096])
            return final_text
     except Exception as e:
        log.error("Final synthesis after exhaustion failed: %s", e)
        # Try backup providers with full tool-use context (Change 5)
        for _bp_attr, _bp_model in _BACKUP_PROVIDERS:
            _bp_client = globals().get(_bp_attr)
            if not _bp_client:
                continue
            try:
                _bp_resp = await _bp_client.chat.completions.create(
                    model=_bp_model, max_tokens=4000,
                    messages=messages, tool_choice="none", timeout=CLAUDE_TIMEOUT,
                )
                final_text = _bp_resp.choices[0].message.content or ""
                if final_text:
                    log.info("Exhaustion synthesis succeeded via backup %s", _bp_model)
                    await _safe_edit(progress_msg, final_text[:4096])
                    return final_text
            except Exception:
                continue

     # Last resort for artifact tasks: programmatic generate_file fallback
     if _is_artifact and _phase.total_research >= 2 and not _phase.generate_file_attempted:
        log.warning("Exhaustion path: programmatic generate_file fallback")
        _collected = "\n\n".join(
            m["content"][:1000] for m in messages
            if isinstance(m, dict) and m.get("role") == "tool"
        )
        if _collected:
            _artifact_path = _extract_artifact_path(query)
            if not _artifact_path:
                slug = re.sub(r'[^\w\s-]', '', query.lower())[:50].strip().replace(' ', '-')
                _artifact_path = os.path.expanduser(
                    f"~/Developer/Personal/presentations/{slug}/index.html"
                )
            class _ExhSynth:
                class function:
                    name = "generate_file"
                    arguments = json.dumps({
                        "description": f"{query}\n\nContext gathered:\n{_collected[:3000]}",
                        "target_path": _artifact_path,
                    })
                id = f"programmatic_exhaustion"
            try:
                _gen_result = await _execute_tool_call(_ExhSynth())
                if '"success"' in _gen_result[:200]:
                    log.info("Programmatic fallback (exhaustion) succeeded")
                    await _safe_edit(progress_msg, _gen_result[:4096])
                    return _gen_result
                else:
                    log.warning("Programmatic fallback (exhaustion) returned: %s", _gen_result[:200])
            except Exception as _pf_e:
                log.warning("Programmatic fallback (exhaustion) failed: %s", _pf_e)

     _step_summary = " → ".join(_progress_steps) if _progress_steps else "multiple steps"
     _fallback = (
        f"I completed {_step_summary} but ran out of iterations before finishing. "
        "Could you try breaking this into smaller steps?"
     )
     await _safe_edit(progress_msg, _fallback)
     return _fallback
    finally:
     # Release summarization gate — deferred summaries can now run
     _tool_loop_active.discard(int(chat_id))
     _check_summarization_needed(int(chat_id))


async def _safe_edit(msg, text: str):
    """Edit a Telegram message, ignoring failures."""
    try:
        await msg.edit(text[:4096])
    except Exception as e:
        log.warning("_safe_edit failed: %s (text_len=%d)", e, len(text))


async def stream_to_telegram(
    chat_id: int,
    progress_msg,
    stream_gen,
    channel,
) -> str:
    """Consume a streaming LLM generator and progressively update a Telegram message.

    Edits the progress_msg with accumulated text at controlled intervals to avoid
    hitting Telegram's rate limits. Returns the final complete text.
    """
    accumulated = ""
    last_edit_time = 0.0
    last_edit_len = 0
    import time as _time

    chunk_count = 0
    async for chunk in stream_gen:
        accumulated += chunk
        chunk_count += 1
        now = _time.monotonic()

        # Strip internal tags from what the user sees
        display_text = _internal_tag_re.sub("", accumulated).strip()
        new_chars = len(display_text) - last_edit_len

        # Edit if enough time has passed AND enough new content
        if new_chars >= _STREAM_MIN_DELTA and (now - last_edit_time) >= _STREAM_EDIT_INTERVAL:
            try:
                # Add typing cursor to show it's still generating
                await progress_msg.edit(display_text + " ▍")
                last_edit_time = now
                last_edit_len = len(display_text)
            except Exception:
                # Telegram rate limit or message unchanged — skip this edit
                pass

    # Final edit with complete text (no cursor), tags stripped
    log.info("stream_to_telegram: %d chunks, %d chars accumulated", chunk_count, len(accumulated))
    display_final = _internal_tag_re.sub("", accumulated).strip()
    if display_final and len(display_final) != last_edit_len:
        # Telegram enforces a 4096-char limit per message
        _TG_LIMIT = 4096
        try:
            await progress_msg.edit(display_final[:_TG_LIMIT])
        except Exception:
            # If final edit fails, delete and send fresh (chunked if needed)
            try:
                await progress_msg.delete()
            except Exception:
                pass
            for i in range(0, len(display_final), _TG_LIMIT):
                await channel.send_message(chat_id, display_final[i:i + _TG_LIMIT])

    return accumulated


# --- Intent Detection ---

# Patterns that suggest actionable intent (cheap pre-filter before LLM call)
_ACTION_PATTERNS = [
    (r"\bremind\s+me\b", "reminder"),
    (r"\bset\s+(?:a\s+)?reminder\b", "reminder"),
    (r"\bdon'?t\s+(?:let\s+me\s+)?forget\b", "reminder"),
    (r"\bemail\b.*\babout\b", "email"),
    (r"\bsend\s+(?:an?\s+)?email\b", "email"),
    (r"\bdraft\s+(?:an?\s+)?email\b", "email"),
    (r"\bwrite\s+(?:an?\s+)?email\b", "email"),
    (r"\bcalendar\b", "calendar"),
    (r"\bwhat'?s\s+on\s+(?:my\s+)?(?:schedule|calendar)\b", "calendar"),
    (r"\bmeeting(?:s)?\s+today\b", "calendar"),
    (r"\bopen\s+(?:the\s+)?(?:Safari|Chrome|Slack|Finder|Terminal|Music|Notes|Calendar|Spotify|Mail)\b", "shell"),
    (r"\bopen\s+https?://", "shell"),
    (r"\bcheck\s+(?:disk\s+)?(?:space|storage)\b", "shell"),
    (r"\brun\s+(?:the\s+)?command\b", "shell"),
    (r"\bhow\s+many\b.*\b(?:open|running|active)\b", "shell"),
    (r"\b(?:running|open)\b.*\b(?:on\s+my\s+(?:mac|machine|computer)|right\s+now)\b", "shell"),
    (r"\b(?:what|which)\s+(?:apps?|processes?|programs?)\s+(?:are\s+)?(?:running|open)\b", "shell"),
    (r"\b(?:battery|cpu|memory|ram|uptime)\b.*\b(?:status|level|usage)\b", "shell"),
    (r"\bwhat'?s\s+my\s+(?:ip|battery|uptime)\b", "shell"),
    # Email labeling / categorization
    (r"\b(?:categoriz|label|organiz|sort)\w*\s+(?:my\s+)?(?:email|inbox|mail)\b", "label"),
    (r"\b(?:email|inbox|mail)\w*\s+.*\b(?:categoriz|label|organiz|sort)\b", "label"),
    # #36: Clipboard integration
    (r"\b(?:what'?s|show|read|get|check)\s+(?:on\s+)?(?:my\s+)?clipboard\b", "clipboard_read"),
    (r"\b(?:process|summarize|analyze|translate)\s+(?:my\s+)?clipboard\b", "clipboard_process"),
    (r"\bpaste\b.*\b(?:clipboard|what\s+i\s+copied)\b", "clipboard_read"),
    # #40: Spotlight file search
    (r"\b(?:find|search\s+for|locate)\s+(?:a\s+)?file\b", "spotlight"),
    (r"\bfind\s+(?:all\s+)?(?:my\s+)?\w+\s+files?\b", "spotlight"),
    (r"\bwhere\s+is\s+(?:my\s+|the\s+)?\w+\b.*\bfile\b", "spotlight"),
    # #52: GitHub issue creation
    (r"\bcreate\s+(?:a\s+)?(?:github\s+)?issue\b", "github_create_issue"),
    (r"\bopen\s+(?:a\s+)?(?:github\s+)?issue\b", "github_create_issue"),
    (r"\bfile\s+(?:an?\s+)?(?:github\s+)?issue\b", "github_create_issue"),
    (r"\bnew\s+(?:github\s+)?issue\b", "github_create_issue"),
    # #53: GitHub PR status monitoring
    (r"\bcheck\s+(?:my\s+)?(?:pull\s+requests?|prs?)\b", "github_prs"),
    (r"\b(?:pr|pull\s+request)\s+status\b", "github_prs"),
    (r"\blist\s+(?:my\s+)?(?:open\s+)?(?:pull\s+requests?|prs?)\b", "github_prs"),
    # GitHub notifications
    (r"\bgithub\s+notifications?\b", "github_notifications"),
    (r"\bcheck\s+(?:my\s+)?(?:github\s+)?notifications?\b", "github_notifications"),
    (r"\bunread\s+(?:github\s+)?notifications?\b", "github_notifications"),
    # #41: Brew package management
    (r"\bbrew\s+(?:list|info|search|install|upgrade|uninstall|cleanup)\b", "shell"),
    (r"\blist\s+(?:my\s+)?brew\s+packages?\b", "shell"),
    (r"\binstall\s+(?:via\s+)?brew\b", "shell"),
    (r"\bwhat\s+(?:brew\s+)?packages?\s+(?:do\s+i\s+have|are\s+installed)\b", "shell"),
    # #38: Window management
    (r"\b(?:arrange|tile|put)\s+windows?\s+(?:side\s+by\s+side|split)\b", "shell"),
    (r"\bresize\s+(?:the\s+)?window\b", "shell"),
    (r"\bminimize\s+(?:all\s+)?windows?\b", "shell"),
    (r"\bshow\s+(?:all\s+)?windows?\b", "shell"),
    # #49: Google Contacts
    (r"\bfind\s+contact\b", "contacts"),
    (r"\bwho\s+is\b.*\b(?:email|phone|contact)\b", "contacts"),
    (r"\bemail\s+address\s+for\b", "contacts"),
    (r"\bsearch\s+(?:my\s+)?contacts?\b", "contacts"),
    # #56: iCloud Reminders
    (r"\badd\s+(?:to\s+)?(?:apple|icloud)\s+reminder", "icloud_reminder"),
    (r"\b(?:apple|icloud)\s+reminder", "icloud_reminder"),
    (r"\breminders?\s+app\b", "icloud_reminder"),
    (r"\bshow\s+(?:my\s+)?(?:apple|icloud)\s+reminders?\b", "icloud_reminder"),
    # #42: Network diagnostics
    (r"\b(?:check|test)\s+(?:my\s+)?(?:network|internet|connection)\b", "shell"),
    (r"\bnetwork\s+status\b", "shell"),
    (r"\bping\s+\w+", "shell"),
    (r"\b(?:check|test)\s+(?:internet|connectivity)\b", "shell"),
    (r"\bdns\s+lookup\b", "shell"),
    (r"\bnslookup\b", "shell"),
    (r"\bcheck\s+wifi\b", "shell"),
    (r"\bwifi\s+status\b", "shell"),
    # #50: Google Tasks
    (r"\b(?:my|show|list)\s+tasks?\b", "tasks"),
    (r"\btodo\s+list\b", "tasks"),
    (r"\badd\s+(?:a\s+)?task\b", "tasks"),
    (r"\bcreate\s+(?:a\s+)?task\b", "tasks"),
    # #44: Login item management
    (r"\b(?:list|show|get)\s+(?:my\s+)?(?:login|startup)\s+items?\b", "shell"),
    (r"\bstartup\s+items?\b", "shell"),
    (r"\bshow\s+launch\s+agents?\b", "shell"),
    (r"\blist\s+launch\s+agents?\b", "shell"),
    # #1: Explicit feedback
    (r"^/feedback\b", "feedback"),
    # #43: Disk cleanup assistant
    (r"\b(?:disk\s+space|storage\s+usage)\b", "shell"),
    (r"\b(?:large|biggest)\s+files?\b", "shell"),
    (r"\bclean\s+cache[s]?\b", "shell"),
    (r"\bclear\s+cache[s]?\b", "shell"),
    (r"\bclean\s+downloads?\b", "shell"),
    # #48: Slack message sending
    (r"\bsend\s+(?:a\s+)?slack\s+message\b", "slack_send"),
    (r"\bpost\s+to\s+slack\b", "slack_send"),
    (r"\bmessage\s+on\s+slack\b", "slack_send"),
    # #51: Spotify playback control (osascript)
    (r"\b(?:play|resume)\s+music\b", "shell"),
    (r"\b(?:pause|stop)\s+music\b", "shell"),
    (r"\b(?:next|skip)\s+(?:song|track)\b", "shell"),
    # Spotify data queries (API)
    (r"\b(?:what'?s\s+playing|now\s+playing|current\s+(?:song|track))\b", "spotify_now"),
    (r"\b(?:what\s+am\s+i|what'?s)\s+(?:listening|playing)\b", "spotify_now"),
    (r"\brecently\s+played\b", "spotify_recent"),
    (r"\blistening\s+history\b", "spotify_recent"),
    (r"\btop\s+(?:tracks?|songs?)\b", "spotify_top"),
    (r"\btop\s+artists?\b", "spotify_top"),
    (r"\bmost\s+played\b", "spotify_top"),
    # Weather
    (r"\b(?:what'?s\s+the\s+)?weather\b", "weather"),
    (r"\btemperature\b", "weather"),
    (r"\bforecast\b", "weather_forecast"),
    # LinkedIn
    (r"\blinkedin\s+(?:messages?|inmail)\b", "linkedin_messages"),
    (r"\brecruiter\s+messages?\b", "linkedin_messages"),
    (r"\blinkedin\s+jobs?\b", "linkedin_jobs"),
    (r"\bjob\s+search\s+linkedin\b", "linkedin_jobs"),
    (r"\blinkedin\s+(?:views?|profile\s+views?)\b", "linkedin_profile"),
    (r"\bprofile\s+views?\b", "linkedin_profile"),
    # App Store
    (r"\bapp\s+store\s+(?:rating|reviews?)\b", "appstore_ratings"),
    (r"\bzia\s+(?:rating|reviews?)\b", "appstore_ratings"),
    (r"\bapp\s+(?:downloads?|stats?)\b", "appstore_downloads"),
    (r"\bzia\s+(?:downloads?|stats?)\b", "appstore_downloads"),
    (r"\bhow\s+is\s+zia\b", "appstore_ratings"),
    # DigitalOcean
    (r"\b(?:server|droplet)\s+(?:status|health)\b", "digitalocean_status"),
    (r"\bdigitalocean\b", "digitalocean_status"),
    (r"\b(?:server|digitalocean)\s+(?:cost|bill|spend)\b", "digitalocean_spend"),
    # Notion
    (r"\bsearch\s+(?:my\s+)?notion\b", "notion_search"),
    (r"\bfind\s+in\s+notion\b", "notion_search"),
    (r"\bnotion\s+search\b", "notion_search"),
    (r"\bcreate\s+(?:a\s+)?notion\s+page\b", "notion_create"),
    # YouTube
    (r"\bsearch\s+(?:on\s+)?youtube\b", "youtube_search"),
    (r"\bfind\s+(?:a\s+)?video\b", "youtube_search"),
    (r"\byoutube\s+search\b", "youtube_search"),
    (r"\bliked\s+videos?\b", "youtube_liked"),
    (r"\byoutube\s+(?:history|liked)\b", "youtube_liked"),
    # Readwise
    (r"\breadwise\b", "readwise_highlights"),
    (r"\bbook\s+highlights?\b", "readwise_highlights"),
    (r"\bmy\s+highlights?\b", "readwise_highlights"),
    (r"\bdaily\s+review\b", "readwise_review"),
    # Apple Reminders (native app sync)
    (r"\bapple\s+reminders?\b", "apple_reminders_list"),
    (r"\biphone\s+reminders?\b", "apple_reminders_list"),
    (r"\bsync\s+(?:reminders?\s+)?(?:to\s+)?apple\b", "apple_reminders_sync"),
    # #37: Screenshot and OCR
    (r"\b(?:take|capture)\s+(?:a\s+)?screenshot\b", "screenshot"),
    (r"\bscreenshot\s+(?:of\s+)?(?:the\s+)?window\b", "screenshot"),
    (r"\bcapture\s+(?:the\s+)?screen\b", "screenshot"),
    (r"\bscreenshot\b", "screenshot"),
    # #54: Google Drive file creation
    (r"\bcreate\s+(?:a\s+)?(?:google\s+)?(?:doc|document)\b", "drive_create"),
    (r"\bcreate\s+(?:a\s+)?(?:google\s+)?(?:spreadsheet|sheet)\b", "drive_create"),
    (r"\bsave\s+to\s+(?:google\s+)?drive\b", "drive_create"),
    # #55: Multi-account Gmail
    (r"\bsearch\s+(?:my\s+)?work\s+email\b", "email_work"),
    (r"\bsearch\s+(?:my\s+)?personal\s+email\b", "email_personal"),
    (r"\bcheck\s+(?:my\s+)?work\s+(?:inbox|email|mail)\b", "email_work"),
    (r"\bcheck\s+(?:my\s+)?personal\s+(?:inbox|email|mail)\b", "email_personal"),
    # Cursor IDE awareness
    (r"\bcursor\s+(?:status|windows?|projects?|info)\b", "cursor_status"),
    (r"\b(?:what.s|which)\s+(?:(?:files?|projects?)\s+)?(?:are\s+)?open\s+in\s+cursor\b", "cursor_status"),
    (r"\b(?:what|which)\s+(?:am\s+i\s+)?(?:working\s+on|editing)\s+in\s+cursor\b", "cursor_status"),
    (r"\bcursor\s+extensions?\b", "cursor_extensions"),
    # Cursor integrated terminal (via bridge extension) — must come before generic terminal patterns
    (r"\bcursor\s+terminal\s+(?:status|list|sessions?)\b", "cursor_terminal_status"),
    (r"\b(?:what.s|what\s+is)\s+(?:running\s+)?in\s+(?:the\s+)?cursor\s+terminal\b", "cursor_terminal_status"),
    (r"\b(?:list|show)\s+(?:the\s+)?terminals?\s+in\s+cursor\b", "cursor_terminal_status"),
    (r"\brun\s+.+\s+in\s+cursor\s+terminal\b", "cursor_terminal_exec"),
    (r"\bsend\s+.+\s+to\s+cursor\s+terminal\b", "cursor_terminal_exec"),
    (r"\bnew\s+cursor\s+terminal\b", "cursor_terminal_new"),
    (r"\bcreate\s+(?:a\s+)?cursor\s+terminal\b", "cursor_terminal_new"),
    # iTerm2 / terminal awareness
    (r"\b(?:what.s|what\s+is)\s+running\s+in\s+(?:my\s+)?(?:terminal|iterm)\b", "terminal_status"),
    (r"\bterminal\s+(?:status|sessions?)\b", "terminal_status"),
    (r"\biterm\s+(?:status|sessions?)\b", "terminal_status"),
    (r"\bactive\s+(?:terminal\s+)?(?:processes|commands)\b", "terminal_status"),
    # Terminal control (must come after terminal_status to not shadow)
    (r"\brun\s+.+\s+in\s+(?:the\s+)?(?:terminal|iterm|tab|session)\b", "terminal_exec"),
    (r"\bsend\s+.+\s+to\s+(?:the\s+)?(?:terminal|iterm)\b", "terminal_exec"),
    (r"\bnew\s+(?:terminal\s+)?tab\b", "terminal_new_tab"),
    (r"\bopen\s+(?:a\s+)?(?:new\s+)?terminal(?:\s+tab)?\b", "terminal_new_tab"),
    # Cursor control
    (r"\bopen\s+.+\s+in\s+cursor\b", "cursor_open"),
    (r"\bcursor\s+open\s+", "cursor_open"),
    (r"\bjump\s+to\s+(?:line\s+)?\d+", "cursor_goto"),
    (r"\bcursor\s+diff\b", "cursor_diff"),
]


def _looks_like_action(text: str) -> str | None:
    """Quick regex check if text looks like an action request. Returns hint or None.

    Delegates to the skill registry for pattern matching. Falls back to legacy
    _ACTION_PATTERNS for any patterns not yet migrated to SKILL dicts.
    """
    from skills import get_registry
    registry = get_registry()
    action_type, _skill = registry.match_intent(text)
    if action_type:
        return action_type

    # Fallback: legacy patterns for anything not yet in a SKILL dict
    text_lower = text.lower()
    for pattern, hint in _ACTION_PATTERNS:
        if re.search(pattern, text_lower):
            return hint
    return None


# --- Intent Pattern Miss Detection ---
# Maps granular action types to keyword descriptions.
# Used to detect when a query COULD have been handled by an existing action
# but fell through intent detection due to a regex gap.
ACTION_REGISTRY = {
    "cursor_terminal_status": "cursor terminal sessions status list terminals",
    "cursor_terminal_exec": "run send command cursor terminal",
    "cursor_terminal_new": "create new cursor terminal",
    "cursor_status": "cursor ide status windows projects info",
    "cursor_extensions": "cursor extensions list",
    "cursor_open": "open file cursor jump goto line",
    "cursor_diff": "cursor diff files compare",
    "terminal_status": "terminal iterm sessions running status",
    "terminal_exec": "run send command terminal iterm",
    "terminal_new_tab": "new terminal tab",
    "contacts_search": "find contact email address search contacts people",
    "icloud_reminder_list": "apple icloud reminders list show",
    "icloud_reminder_create": "add create apple icloud reminder",
    "tasks_list": "tasks todo list show google",
    "tasks_create": "add create task todo",
    "screenshot": "screenshot capture screen window",
    "github_notifications": "github notifications unread alerts",
    "github_prs": "github pull requests prs open review",
    "github_create_issue": "github create new issue file open bug",
    "weather": "weather temperature outside today toronto",
    "weather_forecast": "weather forecast days week ahead",
    "spotify_now": "playing listening song track music spotify",
    "spotify_recent": "recently played listening history spotify",
    "spotify_top": "top tracks artists most played spotify",
    "linkedin_messages": "linkedin messages recruiter inmail",
    "linkedin_jobs": "linkedin jobs search openings",
    "linkedin_profile": "linkedin profile views",
    "appstore_ratings": "app store rating reviews zia",
    "appstore_downloads": "app store downloads stats zia",
    "digitalocean_status": "server droplet status health digitalocean",
    "digitalocean_spend": "server cost bill spend digitalocean",
    "notion_search": "notion search find pages notes",
    "notion_create": "notion create page new",
    "youtube_search": "youtube search video find",
    "youtube_liked": "youtube liked videos history",
    "readwise_highlights": "readwise highlights books reading",
    "readwise_review": "readwise daily review",
    "apple_reminders_list": "apple iphone reminders list native",
    "apple_reminders_sync": "sync reminders apple iphone",
}


def find_matching_action(query: str) -> str | None:
    """Check if a query matches an existing granular action by keyword overlap.

    Returns action type string or None. Used to detect intent pattern misses
    when a query falls through to the LLM but could have been handled directly.

    Delegates to the skill registry. Falls back to legacy ACTION_REGISTRY.
    """
    from skills import get_registry
    registry = get_registry()
    result = registry.find_keyword_match(query)
    if result:
        return result

    # Fallback: legacy registry
    query_words = set(re.findall(r'\b\w+\b', query.lower()))
    best_action = None
    best_score = 0
    for action, keywords in ACTION_REGISTRY.items():
        keyword_set = set(keywords.split())
        score = len(query_words & keyword_set)
        if score > best_score and score >= 2:
            best_score = score
            best_action = action
    return best_action


# --- #65: Conversation Topic Detection ---

_TOPIC_KEYWORDS = {
    "work": {"meeting", "sprint", "jira", "standup", "project", "deadline", "team", "slack",
             "manager", "review", "deploy", "release", "roadmap", "okr", "backlog", "ticket"},
    "finance": {"money", "investment", "stock", "tax", "budget", "expense", "salary", "bank",
                "portfolio", "crypto", "dividend", "savings", "mortgage", "rrsp", "tfsa"},
    "health": {"exercise", "gym", "workout", "diet", "sleep", "doctor", "weight", "run",
               "meditation", "calories", "steps", "health", "medical", "prescription"},
    "tech": {"code", "python", "javascript", "api", "database", "server", "bug", "git",
             "docker", "deploy", "framework", "library", "debug", "refactor", "algorithm"},
    "family": {"kids", "wife", "husband", "family", "school", "daycare", "children", "parent",
               "birthday", "vacation", "home", "weekend", "dinner", "park"},
}


def _ocr_screenshot(image_path: str = "/tmp/khalil_screenshot.png") -> str:
    """#37: OCR stub — extract text from a screenshot image.

    Full OCR requires macOS Vision framework via pyobjc or Shortcuts.
    This stub captures the intent and returns guidance.
    """
    import os
    if not os.path.exists(image_path):
        return f"No screenshot found at {image_path}. Take a screenshot first."
    # Attempt via macOS Shortcuts if available
    try:
        import subprocess
        result = subprocess.run(
            ["shortcuts", "run", "Extract Text from Image", "--input-path", image_path],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
        pass
    return (
        f"Screenshot saved to {image_path}. "
        "OCR text extraction requires macOS Vision framework (pyobjc-framework-Vision) "
        "or a configured Shortcuts automation. Install pyobjc-framework-Vision for full OCR support."
    )


def classify_message_topic(text: str) -> str:
    """#65: Classify a message into a topic using keyword matching.

    Returns one of: 'work', 'finance', 'health', 'tech', 'family', 'general'.
    """
    words = set(re.findall(r'\b\w+\b', text.lower()))
    best_topic = "general"
    best_score = 0
    for topic, keywords in _TOPIC_KEYWORDS.items():
        score = len(words & keywords)
        if score > best_score:
            best_score = score
            best_topic = topic
    return best_topic


# App name normalization for open -a
_APP_NAMES = {
    "safari": "Safari", "chrome": "Google Chrome", "slack": "Slack",
    "finder": "Finder", "terminal": "Terminal", "music": "Music",
    "notes": "Notes", "calendar": "Calendar", "spotify": "Spotify",
    "mail": "Mail", "discord": "Discord", "zoom": "zoom.us",
    "vscode": "Visual Studio Code", "vs code": "Visual Studio Code",
    "arc": "Arc", "firefox": "Firefox", "brave": "Brave Browser",
}


async def _try_extension_handler(hint: str, query: str, update, context) -> bool:
    """Try to route a query to a matching extension handler.

    Looks up extension manifests by command name matching the action hint.
    Skips disabled extensions (checked via plugin manifest).
    Returns True if handled, False otherwise.
    """
    import importlib
    from config import EXTENSIONS_DIR
    from extensions.manifest import is_extension_enabled

    if not EXTENSIONS_DIR or not EXTENSIONS_DIR.exists():
        return False

    for manifest_path in EXTENSIONS_DIR.glob("*.json"):
        if manifest_path.name == "extensions.json":
            continue
        try:
            manifest = json.loads(manifest_path.read_text())
            if manifest.get("command") == hint:
                ext_name = manifest.get("name", manifest_path.stem)
                if not is_extension_enabled(ext_name):
                    log.debug("Extension '%s' is disabled, skipping", ext_name)
                    continue
                mod = importlib.import_module(manifest["action_module"])
                handler_fn = getattr(mod, manifest["handler_function"])
                # Synthesize args from the query (pass the full query as args)
                context.args = query.split()
                await handler_fn(update, context)
                return True
        except Exception as e:
            log.warning("Extension handler %s failed: %s", hint, e)
    return False


def _try_direct_shell_intent(text: str) -> dict | None:
    """Try to map text directly to a shell intent without LLM. Returns intent dict or None."""
    text_stripped = text.strip()
    text_lower = text_stripped.lower()

    # "open <App>"
    m = re.search(r"\bopen\s+(?:the\s+)?(safari|chrome|slack|finder|terminal|music|notes|calendar|spotify|mail|discord|zoom|vscode|vs code|arc|firefox|brave)\b", text_lower)
    if m:
        app = _APP_NAMES.get(m.group(1), m.group(1).title())
        return {"action": "shell", "command": f"open -a '{app}'", "description": f"Open {app}"}

    # "open <URL>"
    m = re.search(r"\bopen\s+(https?://\S+)", text_lower)
    if m:
        url = text_stripped[m.start(1):m.end(1)]  # preserve original case in URL
        return {"action": "shell", "command": f"open {url}", "description": f"Open {url}"}

    # "check disk space / storage"
    if re.search(r"\bcheck\s+(?:disk\s+)?(?:space|storage)\b", text_lower):
        return {"action": "shell", "command": "df -h", "description": "Check disk space"}

    # "how many <app> windows open" — pre-built osascript (LLM gets this wrong)
    m = re.search(r"\bhow\s+many\s+(\w+)\s+windows?\b", text_lower)
    if m:
        app = m.group(1).title()
        return {
            "action": "shell",
            "command": f"osascript -e 'tell application \"System Events\" to count windows of process \"{app}\"'",
            "description": f"Count {app} windows",
        }

    # "what apps / processes are running"
    if re.search(r"\b(?:what|which)\s+(?:apps?|processes?|programs?)\s+(?:are\s+)?(?:running|open)\b", text_lower):
        return {"action": "shell", "command": "ps -eo comm= | sort -u | grep -v '^$'", "description": "List running processes"}

    # "what's my battery" / "battery status"
    if re.search(r"\b(?:battery|charge)\b.*\b(?:status|level|percentage|life)\b", text_lower) or \
       re.search(r"\bwhat'?s\s+my\s+battery\b", text_lower):
        return {"action": "shell", "command": "pmset -g batt", "description": "Check battery status"}

    # "what's my ip"
    if re.search(r"\bwhat'?s\s+my\s+ip\b", text_lower) or re.search(r"\bmy\s+ip\s+address\b", text_lower):
        return {"action": "shell", "command": "ipconfig getifaddr en0", "description": "Get local IP address"}

    # "uptime"
    if re.search(r"\b(?:uptime|how\s+long.*(?:running|been\s+on|up))\b", text_lower):
        return {"action": "shell", "command": "uptime", "description": "Check system uptime"}

    # #42: Network diagnostics
    if re.search(r"\b(?:check|test)\s+(?:my\s+)?(?:network|connection)\b", text_lower) or \
       re.search(r"\bnetwork\s+status\b", text_lower):
        return {"action": "shell", "command": "networksetup -getinfo Wi-Fi", "description": "Check network info"}

    if re.search(r"\bping\s+(\S+)", text_lower):
        m = re.search(r"\bping\s+(\S+)", text_lower)
        target = m.group(1) if m else "google.com"
        return {"action": "shell", "command": f"ping -c 3 {target}", "description": f"Ping {target}"}

    if re.search(r"\b(?:check|test)\s+internet\b", text_lower) or \
       re.search(r"\bcheck\s+connectivity\b", text_lower):
        return {"action": "shell", "command": "ping -c 3 google.com", "description": "Check internet connectivity"}

    if re.search(r"\bdns\s+lookup\b", text_lower) or re.search(r"\bnslookup\s+(\S+)", text_lower):
        m = re.search(r"\bnslookup\s+(\S+)", text_lower)
        target = m.group(1) if m else "google.com"
        return {"action": "shell", "command": f"nslookup {target}", "description": f"DNS lookup for {target}"}

    if re.search(r"\bcheck\s+wifi\b", text_lower) or re.search(r"\bwifi\s+status\b", text_lower):
        return {"action": "shell", "command": "networksetup -getairportnetwork en0", "description": "Check Wi-Fi status"}

    if re.search(r"\bpublic\s+ip\b", text_lower) or re.search(r"\bexternal\s+ip\b", text_lower):
        return {"action": "shell", "command": "curl -s ifconfig.me", "description": "Get public IP address"}

    # #44: Login items / launch agents
    if re.search(r"\b(?:list|show|get)\s+(?:my\s+)?(?:login|startup)\s+items?\b", text_lower) or \
       re.search(r"\bstartup\s+items?\b", text_lower):
        return {"action": "shell", "command": "osascript -e 'tell application \"System Events\" to get the name of every login item'", "description": "List login items"}

    if re.search(r"\b(?:show|list)\s+launch\s+agents?\b", text_lower):
        return {"action": "shell", "command": "ls ~/Library/LaunchAgents/", "description": "Show launch agents"}

    # Cursor IDE awareness — direct handlers (no LLM needed)
    if re.search(r"\bcursor\s+(?:status|windows?|projects?|info)\b", text_lower) or \
       re.search(r"\b(?:what.s|which)\s+(?:files?|projects?)\s+(?:are\s+)?open\s+in\s+cursor\b", text_lower) or \
       re.search(r"\b(?:what|which)\s+(?:am\s+i\s+)?(?:working\s+on|editing)\s+in\s+cursor\b", text_lower):
        return {"action": "cursor_status", "description": "Check Cursor IDE status"}

    if re.search(r"\bcursor\s+extensions?\b", text_lower):
        return {"action": "cursor_extensions", "description": "List Cursor extensions"}

    # Cursor integrated terminal (via bridge) — must come before generic terminal patterns
    if re.search(r"\bcursor\s+terminal\s+(?:status|list|sessions?)\b", text_lower) or \
       re.search(r"\b(?:what.s|what\s+is)\s+(?:running\s+)?in\s+(?:the\s+)?cursor\s+terminal\b", text_lower) or \
       re.search(r"\b(?:list|show)\s+(?:the\s+)?terminals?\s+in\s+cursor\b", text_lower):
        return {"action": "cursor_terminal_status", "description": "Check Cursor terminal sessions"}

    m = re.search(r"\brun\s+(.+?)\s+in\s+cursor\s+terminal\b", text_lower)
    if m:
        cmd = text_stripped[m.start(1):m.end(1)].strip()
        return {"action": "cursor_terminal_exec", "command": cmd, "description": f"Run in Cursor terminal: {cmd}"}

    m = re.search(r"\bsend\s+(.+?)\s+to\s+cursor\s+terminal\b", text_lower)
    if m:
        cmd = text_stripped[m.start(1):m.end(1)].strip()
        return {"action": "cursor_terminal_exec", "command": cmd, "description": f"Send to Cursor terminal: {cmd}"}

    if re.search(r"\bnew\s+cursor\s+terminal\b", text_lower) or \
       re.search(r"\bcreate\s+(?:a\s+)?cursor\s+terminal\b", text_lower):
        return {"action": "cursor_terminal_new", "description": "Create new Cursor terminal"}

    # New terminal tab (must come before terminal_status to avoid "tab" matching "status")
    if re.search(r"\bnew\s+(?:terminal\s+)?tab\b", text_lower) or \
       re.search(r"\bopen\s+(?:a\s+)?(?:new\s+)?terminal(?:\s+tab)?\b", text_lower):
        return {"action": "terminal_new_tab", "description": "Open new terminal tab"}

    # iTerm2 / terminal awareness — direct handlers
    if re.search(r"\b(?:what.s|what\s+is)\s+running\s+in\s+(?:my\s+)?(?:terminal|iterm)\b", text_lower) or \
       re.search(r"\bterminal\s+(?:status|sessions?)\b", text_lower) or \
       re.search(r"\biterm\s+(?:status|sessions?)\b", text_lower) or \
       re.search(r"\bactive\s+(?:terminal\s+)?(?:processes|commands)\b", text_lower):
        return {"action": "terminal_status", "description": "Check terminal sessions"}

    # Terminal control — "run X in terminal", "send X to terminal"
    m = re.search(r"\brun\s+(.+?)\s+in\s+(?:the\s+)?(?:terminal|iterm|tab|session)\b", text_lower)
    if m:
        cmd = text_stripped[m.start(1):m.end(1)].strip()
        return {"action": "terminal_exec", "command": cmd, "session": "current", "description": f"Run in terminal: {cmd}"}

    m = re.search(r"\bsend\s+(.+?)\s+to\s+(?:the\s+)?(?:terminal|iterm)\b", text_lower)
    if m:
        cmd = text_stripped[m.start(1):m.end(1)].strip()
        return {"action": "terminal_exec", "command": cmd, "session": "current", "description": f"Send to terminal: {cmd}"}

    # Cursor control — "open X in cursor", "cursor open X"
    m = re.search(r"\bopen\s+(.+?)\s+in\s+cursor\b", text_lower)
    if m:
        path = text_stripped[m.start(1):m.end(1)].strip()
        return {"action": "cursor_open", "path": path, "description": f"Open in Cursor: {path}"}

    m = re.search(r"\bcursor\s+open\s+(.+)$", text_lower)
    if m:
        path = text_stripped[m.start(1):m.end(1)].strip()
        return {"action": "cursor_open", "path": path, "description": f"Open in Cursor: {path}"}

    # "jump to line N in file" / "cursor goto file:line"
    m = re.search(r"\bjump\s+to\s+(?:line\s+)?(\d+)\s+in\s+(.+?)$", text_lower)
    if m:
        line = int(m.group(1))
        path = text_stripped[m.start(2):m.end(2)].strip()
        return {"action": "cursor_open", "path": path, "line": line, "description": f"Jump to {path}:{line}"}

    # "cursor diff file1 file2"
    m = re.search(r"\bcursor\s+diff\s+(\S+)\s+(\S+)", text_lower)
    if m:
        f1 = text_stripped[m.start(1):m.end(1)]
        f2 = text_stripped[m.start(2):m.end(2)]
        return {"action": "cursor_diff", "file1": f1, "file2": f2, "description": f"Diff: {f1} vs {f2}"}

    # Cursor integrated terminal (via bridge extension)
    if re.search(r"\bcursor\s+terminal\s+(?:status|list|sessions?)\b", text_lower) or \
       re.search(r"\b(?:what.s|what\s+is)\s+(?:running\s+)?in\s+(?:the\s+)?cursor\s+terminal\b", text_lower) or \
       re.search(r"\b(?:list|show)\s+(?:the\s+)?terminals?\s+in\s+cursor\b", text_lower):
        return {"action": "cursor_terminal_status", "description": "Check Cursor terminal sessions"}

    m = re.search(r"\brun\s+(.+?)\s+in\s+cursor\s+terminal\b", text_lower)
    if m:
        cmd = text_stripped[m.start(1):m.end(1)].strip()
        return {"action": "cursor_terminal_exec", "command": cmd, "description": f"Run in Cursor terminal: {cmd}"}

    m = re.search(r"\bsend\s+(.+?)\s+to\s+cursor\s+terminal\b", text_lower)
    if m:
        cmd = text_stripped[m.start(1):m.end(1)].strip()
        return {"action": "cursor_terminal_exec", "command": cmd, "description": f"Send to Cursor terminal: {cmd}"}

    if re.search(r"\bnew\s+cursor\s+terminal\b", text_lower) or \
       re.search(r"\bcreate\s+(?:a\s+)?cursor\s+terminal\b", text_lower):
        cmd = None
        m = re.search(r"\b(?:new|create\s+(?:a\s+)?)cursor\s+terminal\s+(?:and\s+)?(?:run\s+)?(.+?)$", text_lower)
        if m:
            cmd = text_stripped[m.start(1):m.end(1)].strip()
        return {"action": "cursor_terminal_new", "command": cmd, "description": "Create new Cursor terminal"}

    # #36: Clipboard — "what's on my clipboard", "read clipboard"
    if re.search(r"\b(?:what'?s|show|read|get|check)\s+(?:on\s+)?(?:my\s+)?clipboard\b", text_lower) or \
       re.search(r"\bpaste\b.*\b(?:clipboard|what\s+i\s+copied)\b", text_lower):
        return {"action": "shell", "command": "pbpaste", "description": "Read clipboard contents"}

    # #36: Clipboard — "process/summarize my clipboard"
    if re.search(r"\b(?:process|summarize|analyze|translate)\s+(?:my\s+)?clipboard\b", text_lower):
        return {"action": "shell", "command": "pbpaste", "description": "Read clipboard for processing"}

    # #49: Google Contacts search (before Spotlight to avoid "find contact" matching file search)
    m = re.search(r"\b(?:find\s+contact|email\s+address\s+for|search\s+contacts?\s+(?:for\s+)?)\s*(.+?)$", text_lower)
    if m:
        query = text_stripped[m.start(1):m.end(1)].strip()
        if query:
            return {"action": "contacts_search", "query": query, "description": f"Search contacts for: {query}"}

    # #56: iCloud Reminders (before Spotlight to avoid "find" overlap)
    if re.search(r"\bshow\s+(?:my\s+)?(?:apple|icloud)\s+reminders?\b", text_lower):
        return {"action": "icloud_reminder_list", "description": "List Apple Reminders"}

    m = re.search(r"\badd\s+(?:to\s+)?(?:apple|icloud)\s+reminders?\s+(.+?)$", text_lower)
    if m:
        reminder_text = text_stripped[m.start(1):m.end(1)].strip()
        if reminder_text:
            return {"action": "icloud_reminder_create", "text": reminder_text, "description": f"Create Apple Reminder: {reminder_text}"}

    # #40: Spotlight file search — "find file X", "locate my .py files", "find the me repo"
    # Route to the Spotlight skill handler (macos.py) which strips filler words
    _file_search_m = re.search(
        r"\b(?:find|search\s+for|locate)\s+"
        r"(?:me\s+)?(?:a\s+|the\s+|my\s+|all\s+)?"
        r"(?:file\s+(?:named?|called)\s+|files?\s+(?:named?|called)\s+)?"
        r"['\"\u201c]([^'\"\u201c\u201d]+)['\"\u201d]",  # quoted: "me", 'resume'
        text_lower,
    )
    if not _file_search_m:
        # Unquoted: "find file config.py", "locate my resume", "find the me repo"
        _file_search_m = re.search(
            r"\b(?:find|search\s+for|locate)\s+"
            r"(?:me\s+)?(?:a\s+|the\s+|my\s+|all\s+)?"
            r"(?:file\s+(?:named?|called)\s+|files?\s+(?:named?|called)\s+)"
            r"(\S+(?:\.\w+)?)",  # single token after "file named/called"
            text_lower,
        )
    if not _file_search_m:
        # Pattern with explicit "file/files/repo" keyword: "find my python files", "find the me repo"
        _file_search_m = re.search(
            r"\b(?:find|search\s+for|locate)\s+(?:me\s+)?(?:a\s+|the\s+|my\s+|all\s+)?(\S+)\s+(?:files?|repos?|folder|directory|documents?)\b",
            text_lower,
        )
    if _file_search_m:
        search_term = _file_search_m.group(1).strip().strip("'\"")
        if search_term and len(search_term) > 1:
            return {"action": "spotlight", "query": search_term, "user_query": text, "description": f"Search for file: {search_term}"}

    # #53: GitHub PR status — "check my PRs", "PR status"
    if re.search(r"\b(?:check\s+(?:my\s+)?(?:pull\s+requests?|prs?)|(?:pr|pull\s+request)\s+status|list\s+(?:my\s+)?(?:open\s+)?(?:pull\s+requests?|prs?))\b", text_lower):
        return {"action": "shell", "command": "gh pr list --author=@me --state=open", "description": "List your open pull requests"}

    # #41: Brew package management
    if re.search(r"\blist\s+(?:my\s+)?brew\s+packages?\b", text_lower) or \
       re.search(r"\bwhat\s+(?:brew\s+)?packages?\s+(?:do\s+i\s+have|are\s+installed)\b", text_lower) or \
       text_lower.strip() == "brew list":
        return {"action": "shell", "command": "brew list", "description": "List installed Homebrew packages"}

    m = re.search(r"\bbrew\s+info\s+(\S+)", text_lower)
    if m:
        pkg = m.group(1)
        return {"action": "shell", "command": f"brew info {pkg}", "description": f"Get info for brew package: {pkg}"}

    m = re.search(r"\bbrew\s+search\s+(\S+)", text_lower)
    if m:
        pkg = m.group(1)
        return {"action": "shell", "command": f"brew search {pkg}", "description": f"Search brew for: {pkg}"}

    m = re.search(r"\bbrew\s+install\s+(\S+)", text_lower)
    if m:
        pkg = m.group(1)
        return {"action": "shell", "command": f"brew install {pkg}", "description": f"Install brew package: {pkg}"}

    m = re.search(r"\bbrew\s+upgrade(?:\s+(\S+))?", text_lower)
    if m:
        pkg = m.group(1)
        cmd = f"brew upgrade {pkg}" if pkg else "brew upgrade"
        desc = f"Upgrade brew package: {pkg}" if pkg else "Upgrade all brew packages"
        return {"action": "shell", "command": cmd, "description": desc}

    m = re.search(r"\bbrew\s+uninstall\s+(\S+)", text_lower)
    if m:
        pkg = m.group(1)
        return {"action": "shell", "command": f"brew uninstall {pkg}", "description": f"Uninstall brew package: {pkg}"}

    if re.search(r"\bbrew\s+cleanup\b", text_lower):
        return {"action": "shell", "command": "brew cleanup", "description": "Clean up old brew package versions"}

    # #38: Window management
    if re.search(r"\b(?:arrange|tile|put)\s+windows?\s+(?:side\s+by\s+side|split)\b", text_lower):
        return {"action": "shell", "command": "osascript -e 'tell application \"System Events\" to set position of every window to {0, 0}'", "description": "Arrange windows side by side"}

    if re.search(r"\bresize\s+(?:the\s+)?window\b", text_lower):
        return {"action": "shell", "command": "osascript -e 'tell application \"System Events\" to set size of first window of first process whose frontmost is true to {800, 600}'", "description": "Resize front window"}

    if re.search(r"\bminimize\s+(?:all\s+)?windows?\b", text_lower):
        return {"action": "shell", "command": "osascript -e 'tell application \"System Events\" to set visible of every process to false'", "description": "Minimize all windows"}

    if re.search(r"\bshow\s+(?:all\s+)?windows?\b", text_lower) and not re.search(r"\bwhat\b", text_lower):
        return {"action": "shell", "command": "osascript -e 'tell application \"System Events\" to set visible of every process to true'", "description": "Show all windows"}

    # #52: GitHub issue creation — "create issue <title>"
    m = re.search(r"\b(?:create|open|file|new)\s+(?:a\s+)?(?:github\s+)?issue\s+(?:for\s+|about\s+|titled?\s+)?['\"]?(.+?)['\"]?\s*$", text_lower)
    if m:
        title = text_stripped[m.start(1):m.end(1)].strip().strip("'\"")
        if title:
            return {"action": "shell", "command": f"gh issue create --title '{title}'", "description": f"Create GitHub issue: {title}"}

    # #50: Google Tasks — "my tasks", "todo list", "add task <title>"
    if re.search(r"\b(?:my|show|list)\s+tasks?\b", text_lower) or re.search(r"\btodo\s+list\b", text_lower):
        return {"action": "tasks_list", "description": "List Google Tasks"}

    m = re.search(r"\b(?:add|create)\s+(?:a\s+)?task\s+(.+?)$", text_lower)
    if m:
        task_title = text_stripped[m.start(1):m.end(1)].strip().strip("'\"")
        if task_title:
            return {"action": "tasks_create", "title": task_title, "description": f"Create task: {task_title}"}

    # #43: Disk cleanup assistant (all READ — informational only)
    if re.search(r"\b(?:disk\s+space|storage\s+usage)\b", text_lower):
        return {"action": "shell", "command": "df -h /", "description": "Check disk space usage"}

    if re.search(r"\b(?:large|biggest)\s+files?\b", text_lower):
        return {"action": "shell", "command": "du -sh ~/Downloads/* ~/Desktop/* 2>/dev/null | sort -rh | head -20", "description": "Show largest files in Downloads and Desktop"}

    if re.search(r"\bclean\s+cache|clear\s+cache", text_lower):
        return {"action": "shell", "command": "du -sh ~/Library/Caches/* 2>/dev/null | sort -rh | head -10", "description": "Show cache sizes (read-only)"}

    if re.search(r"\bclean\s+downloads?\b", text_lower):
        return {"action": "shell", "command": "ls -lhS ~/Downloads/ | head -20", "description": "Show largest files in Downloads"}

    # #51: Spotify playback control via osascript
    if re.search(r"\b(?:play|resume)\s+music\b", text_lower):
        return {"action": "shell", "command": "osascript -e 'tell application \"Spotify\" to play'", "description": "Play/resume Spotify"}

    if re.search(r"\b(?:pause|stop)\s+music\b", text_lower):
        return {"action": "shell", "command": "osascript -e 'tell application \"Spotify\" to pause'", "description": "Pause Spotify"}

    if re.search(r"\b(?:next|skip)\s+(?:song|track)\b", text_lower):
        return {"action": "shell", "command": "osascript -e 'tell application \"Spotify\" to next track'", "description": "Skip to next track"}

    if re.search(r"\b(?:what'?s\s+playing|now\s+playing|current\s+(?:song|track))\b", text_lower):
        return {"action": "shell", "command": "osascript -e 'tell application \"Spotify\" to get name of current track & \" by \" & artist of current track'", "description": "Show currently playing track"}

    # #48: Slack message sending
    m = re.search(r"\b(?:send\s+(?:a\s+)?slack\s+message|post\s+to\s+slack|message\s+on\s+slack)\b.*?(?:to\s+|in\s+)?#?(\w[\w-]*)\s*[:\-]?\s*(.+?)$", text_lower)
    if m:
        channel = m.group(1)
        message_text = text_stripped[m.start(2):m.end(2)].strip()
        return {"action": "slack_send", "channel": channel, "text": message_text, "description": f"Send Slack message to #{channel}"}

    # Slack without parsed channel/message — return generic intent
    if re.search(r"\b(?:send\s+(?:a\s+)?slack\s+message|post\s+to\s+slack|message\s+on\s+slack)\b", text_lower):
        return {"action": "slack_send", "channel": None, "text": None, "description": "Send a Slack message"}

    # #37: Screenshot capture (READ — just capturing, not modifying)
    if re.search(r"\bscreenshot\s+(?:of\s+)?(?:the\s+)?window\b", text_lower):
        return {"action": "shell", "command": "screencapture -w /tmp/khalil_screenshot.png", "description": "Capture window screenshot"}

    if re.search(r"\b(?:take|capture)\s+(?:a\s+)?screenshot\b", text_lower) or \
       re.search(r"\bcapture\s+(?:the\s+)?screen\b", text_lower) or \
       text_lower.strip() == "screenshot":
        return {"action": "shell", "command": "screencapture -x /tmp/khalil_screenshot.png", "description": "Take screenshot (silent)"}

    # #54: Google Drive file creation
    m = re.search(r"\bcreate\s+(?:a\s+)?(?:google\s+)?(?:doc|document)\s+(?:called|named|titled?)?\s*['\"]?(.+?)['\"]?\s*$", text_lower)
    if m:
        title = text_stripped[m.start(1):m.end(1)].strip().strip("'\"")
        if title:
            return {"action": "drive_create_doc", "title": title, "description": f"Create Google Doc: {title}"}

    m = re.search(r"\bcreate\s+(?:a\s+)?(?:google\s+)?(?:spreadsheet|sheet)\s+(?:called|named|titled?)?\s*['\"]?(.+?)['\"]?\s*$", text_lower)
    if m:
        title = text_stripped[m.start(1):m.end(1)].strip().strip("'\"")
        if title:
            return {"action": "drive_create_sheet", "title": title, "description": f"Create Google Sheet: {title}"}

    # #55: Multi-account Gmail
    if re.search(r"\bsearch\s+(?:my\s+)?work\s+(?:email|inbox|mail)\b", text_lower) or \
       re.search(r"\bcheck\s+(?:my\s+)?work\s+(?:email|inbox|mail)\b", text_lower):
        return {"action": "email_search", "account": "work", "description": "Search work email"}

    if re.search(r"\bsearch\s+(?:my\s+)?personal\s+(?:email|inbox|mail)\b", text_lower) or \
       re.search(r"\bcheck\s+(?:my\s+)?personal\s+(?:email|inbox|mail)\b", text_lower):
        return {"action": "email_search", "account": "personal", "description": "Search personal email"}

    # --- macOS awareness ---

    # Running apps
    if re.search(r"\b(?:what\s+apps?|which\s+apps?|running\s+apps?|open\s+apps?|active\s+apps?)\b", text_lower) or \
       re.search(r"\bapps?\s+(?:are\s+)?(?:running|open|active)\b", text_lower):
        return {"action": "macos_apps", "description": "List running applications"}

    # What am I working on / frontmost app
    if re.search(r"\b(?:what\s+am\s+i\s+(?:working\s+on|doing)|what'?s?\s+(?:in\s+)?(?:focus|foreground)|frontmost\s+app|active\s+app|current\s+app)\b", text_lower):
        return {"action": "macos_frontmost", "description": "Show frontmost application"}

    # System info
    if re.search(r"\b(?:system\s+info|system\s+status|mac\s+(?:info|status|health))\b", text_lower):
        return {"action": "macos_system_info", "description": "Show system information"}

    # Spotlight file search — "find a file called X", "locate the document about Y"
    if re.search(r"\b(?:find|locate|search\s+for)\s+(?:a\s+|that\s+|the\s+)?(?:file|document|pdf|image|photo|spreadsheet|presentation)\b", text_lower):
        query_match = re.search(r"\b(?:file|document|pdf|image|photo|spreadsheet|presentation)\s+(?:called|named|about)\s+(.+)$", text_lower)
        if query_match:
            search_q = query_match.group(1).strip()
        else:
            # Strip common filler words and pass remainder as query
            search_q = re.sub(r"\b(?:find|locate|search\s+for|a|an|the|that|my|on\s+my\s+machine|please)\b", "", text_lower).strip()
            search_q = re.sub(r"\b(?:file|document|pdf|image|photo|spreadsheet|presentation)s?\b", "", search_q).strip()
        if search_q:
            return {"action": "spotlight", "query": search_q, "user_query": text, "description": f"Search files: {search_q}"}

    # Browser tabs
    if re.search(r"\b(?:what\s+tabs?|open\s+tabs?|browser\s+tabs?|safari\s+tabs?|chrome\s+tabs?)\b", text_lower):
        browser = "Google Chrome" if "chrome" in text_lower else "Safari"
        return {"action": "macos_browser_tabs", "browser": browser, "description": f"List {browser} tabs"}

    # --- Web search ---

    if re.search(r"\b(?:search\s+(?:the\s+)?(?:web|internet|online)|google|look\s+up|search\s+for)\b", text_lower):
        query_match = re.search(r"\b(?:search\s+(?:the\s+)?(?:web|internet|online)\s+for|google|look\s+up|search\s+for)\s+(.+)$", text_lower)
        search_q = query_match.group(1).strip() if query_match else text_stripped
        return {"action": "web_search", "query": search_q, "description": f"Web search: {search_q}"}

    # --- iMessage ---

    # Recent texts from someone
    m = re.search(r"\b(?:what\s+did|texts?\s+from|messages?\s+from|imessages?\s+from)\s+(.+?)(?:\s+(?:text|send|message|say))?\s*(?:\bme\b)?\s*\??$", text_lower)
    if m:
        contact = text_stripped[m.start(1):m.end(1)].strip()
        return {"action": "imessage_read", "contact": contact, "description": f"Read messages from {contact}"}

    if re.search(r"\b(?:recent\s+(?:texts?|messages?|imessages?)|who\s+texted|who\s+messaged)\b", text_lower):
        return {"action": "imessage_recent", "description": "Show recent messages"}

    if re.search(r"\bsearch\s+(?:my\s+)?(?:texts?|messages?|imessages?)\b", text_lower):
        query_match = re.search(r"\bsearch\s+(?:my\s+)?(?:texts?|messages?|imessages?)\s+(?:for\s+)?(.+)$", text_lower)
        search_q = query_match.group(1).strip() if query_match else text_stripped
        return {"action": "imessage_search", "query": search_q, "description": f"Search messages: {search_q}"}

    # --- Browser automation ---

    # "go to <url> and screenshot" / "navigate to <url> and capture"
    m = re.search(r'\b(?:go\s+to|navigate\s+to|open)\s+(https?://\S+)\s+and\s+(?:screenshot|capture)', text_lower)
    if m:
        url = text_stripped[m.start(1):m.end(1)]
        return {"action": "browser_screenshot", "url": url, "description": "Screenshot webpage"}

    # "extract text from <url>" / "scrape <url>"
    m = re.search(r'\b(?:extract|scrape|get)\s+(?:text|data|content)\s+(?:from|at)\s+(https?://\S+)', text_lower)
    if m:
        url = text_stripped[m.start(1):m.end(1)]
        return {"action": "browser_extract", "url": url, "description": "Extract page text"}

    # "screenshot the page at <url>"
    m = re.search(r'\bscreenshot\s+(?:the\s+)?(?:page|site|website)\s+(?:at\s+)?(https?://\S+)', text_lower)
    if m:
        url = text_stripped[m.start(1):m.end(1)]
        return {"action": "browser_screenshot", "url": url, "description": "Screenshot webpage"}

    return None


async def _try_inline_healing(ctx: MessageContext):
    """Check for recurring failures and trigger self-healing immediately if threshold met."""
    try:
        from healing import detect_recurring_failures, run_self_healing
        triggers = detect_recurring_failures()
        if triggers and OWNER_CHAT_ID and channel:
            await run_self_healing(triggers, channel, OWNER_CHAT_ID)
    except Exception as e:
        log.debug("Inline self-healing check failed: %s", e)


async def detect_intent(query: str) -> dict | None:
    """Use LLM to extract structured intent from natural language.

    Returns dict like {"action": "reminder", "text": "...", "time": "..."}
    or None if the message is just a question (not an action request).
    """
    prompt = (
        "Analyze this message and determine if it's an ACTION REQUEST or just a QUESTION.\n\n"
        f"Message: \"{query}\"\n\n"
        "If it's an action request, respond with ONLY a JSON object (no markdown, no explanation):\n"
        '- Reminder: {"action": "reminder", "text": "<what to remember>", "time": "<when, e.g. tomorrow 9am, in 2 hours>"}\n'
        '- Email: {"action": "email", "to": "<recipient or description>", "subject": "<topic>", "context_query": "<search term for context>"}\n'
        '- Calendar: {"action": "calendar"}\n'
        '- Shell command: {"action": "shell", "command": "<the exact macOS shell command>", "description": "<brief description>"}\n\n'
        "If it's just a question or conversation (not asking you to DO something), respond with exactly: NONE"
    )

    from llm import ActionIntent, parse_llm_json

    response = await ask_llm(prompt, "", system_extra="Respond with JSON or NONE only. No explanation.")

    response = response.strip()
    if response.upper() == "NONE" or response.startswith("⚠️"):
        return None

    intent = parse_llm_json(response, ActionIntent)
    if intent is None:
        log.debug("Intent detection returned non-JSON: %s", response[:100])
        return None
    return intent.model_dump(exclude_defaults=True)


async def _execute_with_retry(cmd: str, description: str, max_retries: int = 1):
    """Execute a shell command with LLM-based retry on correctable errors.

    Returns (result_dict, final_cmd) — the command may have been corrected.
    """
    from actions.shell import execute_shell, classify_error, would_escalate, classify_command, format_output
    from learning import record_signal

    result = await execute_shell(cmd)
    if result["returncode"] == 0:
        return result, cmd

    error_class = classify_error(result["returncode"], result["stderr"])

    if error_class == "transient":
        await asyncio.sleep(2)
        result = await execute_shell(cmd)
        return result, cmd

    if error_class == "correctable" and max_retries > 0:
        correction_prompt = (
            f"This macOS shell command failed:\n$ {cmd}\n"
            f"Error: {result['stderr'][:500]}\n\n"
            "Generate a corrected command that achieves the same goal. "
            "Output ONLY the shell command, nothing else."
        )
        corrected = (await ask_llm(correction_prompt, "", system_extra="Output a single shell command. No explanation.")).strip()
        # Strip markdown code fences if present
        if corrected.startswith("```"):
            lines = corrected.split("\n")
            corrected = "\n".join(l for l in lines if not l.startswith("```")).strip()

        if not corrected or would_escalate(cmd, corrected):
            record_signal("shell_retry", {
                "original_cmd": cmd, "corrected_cmd": corrected,
                "error": result["stderr"][:200], "error_class": error_class,
                "rejected": True, "reason": "escalation" if corrected else "empty",
            })
            return result, cmd

        record_signal("shell_retry", {
            "original_cmd": cmd, "corrected_cmd": corrected,
            "error": result["stderr"][:200], "error_class": error_class,
        })
        return await _execute_with_retry(corrected, description, max_retries=0)

    # permanent — return as-is
    return result, cmd


def _extract_shell_from_response(response: str) -> str | None:
    """Extract a shell command from an LLM response that suggests running it manually.

    Returns the command string if found, None otherwise.
    """
    # Match commands in code blocks (```sh, ```bash, or bare ```)
    m = re.search(r"```(?:sh|bash|shell)?\s*\n(.+?)\n```", response, re.DOTALL)
    if not m:
        return None
    candidate = m.group(1).strip()
    # Filter out multi-line scripts or comments-only blocks
    lines = [l for l in candidate.split("\n") if l.strip() and not l.strip().startswith("#")]
    if len(lines) != 1:
        return None
    return lines[0].strip()


async def _interpret_shell_output(user_query: str, cmd: str, result: dict) -> str | None:
    """Ask LLM to interpret shell output as a natural language answer to the user's question.

    Returns interpreted text, or None if interpretation fails or isn't applicable.
    """
    stdout = result.get("stdout", "").strip()
    if not stdout or not user_query:
        return None
    try:
        interpretation = await ask_llm(
            f"The user asked: \"{user_query}\"\n"
            f"I ran this command: {cmd}\n"
            f"Output: {stdout[:500]}\n\n"
            "Give a brief, direct answer to the user's question based on the output. "
            "One or two sentences max. No preamble.",
            "",
            system_extra="Answer the user's question directly based on the command output. Be concise.",
        )
        interpretation = interpretation.strip()
        if interpretation and not interpretation.startswith("⚠️"):
            return interpretation
    except Exception as e:
        log.debug("Shell output interpretation failed: %s", e)
    return None


async def handle_action_intent(intent: dict, ctx: MessageContext) -> bool:
    """Handle a detected action intent. Returns True if handled.

    First tries skill-registry handlers (from SKILL dicts in action modules).
    Falls back to legacy elif chain for actions not yet migrated.
    """
    action = intent.get("action")

    # #10: Track capability usage for heatmap
    if action:
        try:
            from learning import record_signal
            record_signal("capability_usage", {"action": action})
        except Exception:
            pass

    # M12: Auto-detect goal progress from user actions
    try:
        from learning import check_goal_relevance, record_goal_progress
        from scheduler.planning import map_goal_to_domain
        description = intent.get("description", "") or str(intent.get("text", ""))
        if description:
            related_goal = check_goal_relevance(description)
            if related_goal:
                domain = map_goal_to_domain(related_goal)
                record_goal_progress(related_goal, domain, description[:200])
    except Exception:
        pass

    # --- Informational query redirect ---
    # When the user asks a question that triggers terminal_exec, redirect to
    # shell execution so we capture output instead of fire-and-forget to iTerm.
    if action == "terminal_exec" and intent.get("command"):
        user_query = intent.get("user_query", "")
        _q = user_query.lower().strip()
        _is_question = (
            "?" in _q
            or _q.startswith(("what", "how", "which", "list", "find", "show", "check", "any", "is ", "are ", "do "))
            or _re_module.search(r"\b(what|how many|where|which)\b", _q)
        )
        if _is_question:
            cmd = intent["command"]
            from actions.shell import execute_shell, classify_command, format_output
            classification = classify_command(cmd)
            if classification.name in ("READ", "WRITE"):
                try:
                    result = await asyncio.to_thread(execute_shell, cmd)
                    if result["returncode"] == 0:
                        # Interpret output as natural language answer
                        interpretation = await _interpret_shell_output(user_query, cmd, result)
                        if interpretation:
                            await ctx.reply(interpretation)
                        else:
                            await ctx.reply(f"```\n{format_output(result, cmd)}\n```", parse_mode="Markdown")
                        # Save to conversation context for follow-up queries
                        chat_id = ctx._raw_update.effective_chat.id if ctx._raw_update else None
                        if chat_id:
                            save_message(chat_id, "assistant", f"[Executed: {cmd}]\n{result['stdout'][:500]}")
                        return True
                    else:
                        await ctx.reply(f"```\n{format_output(result, cmd)}\n```", parse_mode="Markdown")
                        return True
                except Exception as e:
                    log.warning("Shell capture failed for informational query: %s", e)
                    # Fall through to normal terminal_exec handler

    # --- Skill registry dispatch ---
    # Try skill handlers first (from SKILL dicts in action modules).
    # If a handler exists and returns True, we're done.
    if action:
        try:
            # Inject server facilities so handlers can use autonomy, approval, etc.
            intent["_server"] = {
                "autonomy": autonomy,
                "reply_with_keyboard": _reply_with_keyboard,
                "approve_deny_keyboard": approve_deny_keyboard,
            }
            from skills import get_registry
            handler = get_registry().get_handler(action)
            if handler is not None:
                # Track reply state before handler runs
                _had_reply_before = getattr(ctx, '_replied', False)
                try:
                    result = await asyncio.wait_for(
                        handler(action, intent, ctx),
                        timeout=30,
                    )
                except asyncio.TimeoutError:
                    log.error("Skill handler timed out for %s (30s)", action)
                    await ctx.reply(f"⚠️ {action} timed out after 30s. Try again or check /health.")
                    return True
                except Exception as handler_err:
                    log.error("Skill handler raised for %s: %s", action, handler_err)
                    await ctx.reply(f"⚠️ {action} encountered an error: {handler_err}")
                    return True
                if result:
                    # Handler claimed success — ensure user got a response
                    if not getattr(ctx, '_replied', False) and not _had_reply_before:
                        log.warning("Handler %s returned True but never called ctx.reply()", action)
                        await ctx.reply(f"✅ {action} completed.")
                    return True
                # Handler returned falsy — if it did reply, treat as handled
                if getattr(ctx, '_replied', False) and not _had_reply_before:
                    return True
        except Exception as e:
            log.error("Skill dispatch failed for %s: %s", action, e)
            await ctx.reply(f"⚠️ {action} failed: {e}")
            # Fall through to legacy dispatch / LLM for a helpful response

    # --- Legacy dispatch (for actions not yet migrated to skill handlers) ---

    if action == "email":
        to_addr = intent.get("to", "")
        subject = intent.get("subject", "")
        context_query = intent.get("context_query", subject)

        if not to_addr or not subject:
            await ctx.reply(
                "I understood you want to send an email, but I need more detail.\n"
                "Try: /email draft <to> <subject>"
            )
            return True

        await ctx.reply(f"📝 Drafting email to {to_addr} about: {subject}...")

        personal_context = get_relevant_context(context_query, max_chars=1500)
        body = await ask_claude(
            f"Write a concise, professional email body for the user to send.\n"
            f"To: {to_addr}\nSubject: {subject}\n\n"
            "Write only the email body, no greeting or signature. Keep it under 200 words.",
            personal_context,
        )

        action_id = autonomy.create_pending_action(
            "send_email",
            f"Send email to {to_addr}: {subject}",
            {"to": to_addr, "subject": subject, "body": body},
        )

        await _reply_with_keyboard(ctx,
            f"📝 Draft ready:\n\nTo: {to_addr}\nSubject: {subject}\n\n{body}\n\n"
            f"---\n{autonomy.format_level()}",
            approve_deny_keyboard())
        return True

    elif action == "shell":
        from actions.shell import classify_command, execute_shell, format_output
        cmd = intent.get("command", "")
        description = intent.get("description", "")
        user_query = intent.get("user_query", "")
        llm_generated = intent.get("llm_generated", True)  # default True for safety
        if not cmd:
            return False

        classification = classify_command(cmd)
        if classification == ActionType.DANGEROUS:
            autonomy.log_audit("shell_dangerous", f"BLOCKED: {cmd}", result="blocked")
            await ctx.reply(f"🚫 Command blocked (dangerous):\n`{cmd}`", parse_mode="Markdown")
            return True

        # Direct pattern-matched commands: respect normal classification
        # LLM-generated commands: always WRITE floor (prevent prompt injection)
        if not llm_generated:
            action_name = f"shell_{classification.value}"  # shell_read or shell_write
            if not autonomy.needs_approval(action_name):
                result, final_cmd = await _execute_with_retry(cmd, description)
                autonomy.log_audit(action_name, f"Executed: {final_cmd}", {"command": final_cmd}, f"exit={result['returncode']}")
                if result["returncode"] != 0:
                    from learning import record_signal
                    record_signal("action_execution_failure", {
                        "action": "shell", "command": final_cmd,
                        "exit_code": result["returncode"],
                        "stderr": result["stderr"][:200],
                    })
                    await _try_inline_healing(ctx)
                # Save to conversation context for follow-up queries
                chat_id = ctx._raw_update.effective_chat.id if ctx._raw_update else None
                if chat_id and result["stdout"]:
                    save_message(chat_id, "assistant", f"[Executed: {final_cmd}]\n{result['stdout'][:500]}")
                # Interpret output as natural language answer when triggered by a user question
                if result["returncode"] == 0 and user_query:
                    interpretation = await _interpret_shell_output(user_query, final_cmd, result)
                    if interpretation:
                        await ctx.reply(interpretation)
                        return True
                await ctx.reply(f"```\n{format_output(result, final_cmd)}\n```", parse_mode="Markdown")
                return True

        # Guardian review for RISKY / uncategorized shell actions
        from actions.guardian import review_tool_call, GuardianVerdict
        guardian_result = await review_tool_call(
            "shell_write", cmd, {"llm_generated": llm_generated, "description": description},
        )
        if guardian_result.verdict == GuardianVerdict.BLOCK:
            autonomy.log_audit("shell_write", f"GUARDIAN BLOCKED: {cmd}", result="guardian_blocked")
            try:
                from learning import record_signal
                record_signal("guardian_blocked", {"action": "shell_write", "command": cmd, "reason": guardian_result.reason})
            except Exception:
                pass
            await ctx.reply(
                f"🛡 Guardian blocked this command:\n\n`{cmd}`\n\n"
                f"Reason: {guardian_result.reason}",
                parse_mode="Markdown",
            )
            return True

        # Needs approval — show Approve/Deny (include guardian reason if NEEDS_CONFIRMATION)
        guardian_note = ""
        if guardian_result.verdict == GuardianVerdict.NEEDS_CONFIRMATION:
            guardian_note = f"\n\n🛡 Guardian: {guardian_result.reason}"

        action_id = autonomy.create_pending_action(
            "shell_write",
            f"Run: {cmd}",
            {"command": cmd, "llm_generated": llm_generated, "user_query": user_query},
        )
        await _reply_with_keyboard(ctx,
            f"🖥 I'd run this command:\n\n`{cmd}`\n\n{description}{guardian_note}\n\n"
            f"{autonomy.format_level()}",
            approve_deny_keyboard(), parse_mode="Markdown")
        return True

    return False


# --- Telegram Handlers ---


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ctx = _ctx_from_update(update)
    global OWNER_CHAT_ID
    OWNER_CHAT_ID = ctx.chat_id
    _persist_owner_chat_id(OWNER_CHAT_ID)
    await ctx.reply(
        "Khalil is online.\n\n"
        "Send me any question about your life, work, finances, or projects.\n\n"
        "Commands:\n"
        "/search <query> — Search your archives\n"
        "/mode — Show/change autonomy level\n"
        "/approve — Approve pending action\n"
        "/deny — Deny pending action\n"
        "/brief — Get your morning brief\n"
        "/email — Search/draft emails\n"
        "/drive — Search Google Drive\n"
        "/remind — Set/list reminders\n"
        "/stats — Knowledge base stats\n"
        "/sync — Sync all live sources (email, Notion, Readwise, Tasks)\n"
        "/jobs — Check for new job matches\n"
        "/calendar — Today's calendar events\n"
        "/finance — Financial dashboard\n"
        "/work — Sprint dashboard & epics\n"
        "/goals — Track quarterly goals\n"
        "/commitments — Track meeting commitments\n"
        "/project — Project status tracking\n"
        "/nudge — What needs attention right now\n"
        "/audit — View recent actions\n"
        "/health — System health status\n"
        "/dev — Dev environment (Cursor + terminal)\n"
        "/run — Run a shell command\n"
        "/backup — Export backup\n"
        "/export — Export knowledge to git (portable)\n"
        "/clear — Clear conversation history\n"
        "/help — Show this message"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ctx = _ctx_from_update(update)
    from skills import get_registry
    registry = get_registry()
    help_text = registry.format_help_by_category()
    help_text += (
        "\n\nCore commands:\n"
        "/brief — Morning brief  /health — System health\n"
        "/remind — Reminders  /calendar — Today's events\n"
        "/email — Email  /search — Search archives\n"
        "/mode — Autonomy level  /help — This message"
    )
    await ctx.reply(help_text)


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ctx = _ctx_from_update(update)
    query = " ".join(context.args) if context.args else ""
    if not query:
        await ctx.reply("Usage: /search <query>")
        return

    await ctx.reply(f"🔍 Searching: {query}")
    results = await hybrid_search(query, limit=5)

    if not results:
        await ctx.reply("No results found.")
        return

    text = f"📋 Found {len(results)} results:\n\n"
    for r in results:
        match_icon = "🧠" if r.get("match_type") == "semantic" else "🔤"
        text += f"{match_icon} **{r['title'][:60]}**\n"
        text += f"   [{r['category']}] {r['content'][:300]}...\n\n"

    await ctx.reply(text, parse_mode=None)


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ctx = _ctx_from_update(update)
    if context.args:
        mode_name = context.args[0].lower()

        # M9: /mode patterns — show learned approval patterns
        if mode_name == "patterns":
            await ctx.reply(autonomy.format_patterns())
            return

        level_map = {
            "supervised": AutonomyLevel.SUPERVISED,
            "guided": AutonomyLevel.GUIDED,
            "autonomous": AutonomyLevel.AUTONOMOUS,
        }
        if mode_name not in level_map:
            await ctx.reply(
                f"Unknown mode. Options: {', '.join(level_map.keys())}, patterns"
            )
            return
        autonomy.set_level(level_map[mode_name])
        await ctx.reply(f"Mode changed to: {autonomy.format_level()}")
    else:
        await ctx.reply(f"Current mode: {autonomy.format_level()}")


async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ctx = _ctx_from_update(update)
    action = autonomy.get_latest_pending()
    if not action:
        await ctx.reply("No pending actions.")
        return

    result = autonomy.approve_action(action["id"])
    if not result:
        await ctx.reply("Failed to approve action.")
        return

    await ctx.reply(f"✅ Approved: {result['description']}\nExecuting...")

    try:
        # Shell actions get retry support
        if result["action_type"] in ("shell_write", "shell_read"):
            import json as _json
            payload = _json.loads(result["payload"]) if isinstance(result["payload"], str) else result["payload"]
            cmd = payload["command"]
            user_query = payload.get("user_query", "")
            from actions.shell import format_output
            shell_result, final_cmd = await _execute_with_retry(cmd, result["description"])
            autonomy.log_audit(result["action_type"], f"Executed: {final_cmd}", payload, f"exit={shell_result['returncode']}")
            if shell_result["returncode"] != 0:
                from learning import record_signal
                record_signal("action_execution_failure", {
                    "action": "shell", "command": final_cmd,
                    "exit_code": shell_result["returncode"],
                    "stderr": shell_result["stderr"][:200],
                })
            if shell_result["returncode"] == 0 and user_query:
                interpretation = await _interpret_shell_output(user_query, final_cmd, shell_result)
                if interpretation:
                    await ctx.reply(interpretation)
                else:
                    await ctx.reply(f"```\n{format_output(shell_result, final_cmd)}\n```", parse_mode="Markdown")
            else:
                await ctx.reply(f"```\n{format_output(shell_result, final_cmd)}\n```", parse_mode="Markdown")
        else:
            status_msg = await autonomy.execute_action(result)
            await ctx.reply(status_msg)
    except Exception as e:
        log.error(f"Action execution failed: {e}")
        from learning import record_signal
        record_signal("action_execution_failure", {
            "action": result.get("action_type", "unknown"),
            "error": str(e)[:200],
        })
        await ctx.reply(f"❌ Execution failed: {e}")


async def cmd_deny(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ctx = _ctx_from_update(update)
    action = autonomy.get_latest_pending()
    if not action:
        await ctx.reply("No pending actions.")
        return

    if autonomy.deny_action(action["id"]):
        await ctx.reply(f"❌ Denied: {action['description']}")
    else:
        await ctx.reply("Failed to deny action.")


async def _handle_self_extend_with_spec(spec: dict, ctx: MessageContext):
    """Offer to build a capability from a structured gap spec."""
    from actions.extend import classify_complexity, _pending_extensions

    complexity = classify_complexity(spec)
    label = "complex (Claude Code)" if complexity == "complex" else "simple"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"⚡ Generate ({label})",
                callback_data=f"extend_generate:{spec['name']}",
            ),
            InlineKeyboardButton("Skip", callback_data="extend_skip"),
        ]
    ])
    _pending_extensions[spec["name"]] = spec
    await _reply_with_keyboard(ctx,
        f"I detected a capability gap: **{spec['description']}**\n"
        f"I can build a `/{spec.get('command', spec['name'])}` command for this.",
        keyboard, parse_mode="Markdown")


async def _run_extension_build(spec: dict, ch: Channel, chat_id: int):
    """Run extension build in background, notify on completion."""
    try:
        await ch.send_message(chat_id, f"🔧 Building `{spec['name']}` capability...")
        from actions.extend import generate_and_pr
        result = await generate_and_pr({"spec": spec})
        await ch.send_message(chat_id, f"✅ {result}")
    except Exception as e:
        log.error("Extension build failed for %s: %s", spec["name"], e)
        await ch.send_message(chat_id, f"❌ Failed to build `{spec['name']}`: {e}")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard button presses."""
    ctx = _ctx_from_update(update)
    query = update.callback_query
    await query.answer()

    if query.data == "action_approve":
        action = autonomy.get_latest_pending()
        if not action:
            await query.edit_message_text("No pending actions.")
            return

        result = autonomy.approve_action(action["id"])
        if not result:
            await query.edit_message_text("Failed to approve action.")
            return

        await query.edit_message_text(f"✅ Approved: {result['description']}\nExecuting...")
        try:
            if result["action_type"] in ("shell_write", "shell_read"):
                import json as _json
                payload = _json.loads(result["payload"]) if isinstance(result["payload"], str) else result["payload"]
                cmd = payload["command"]
                user_query = payload.get("user_query", "")
                from actions.shell import format_output
                shell_result, final_cmd = await _execute_with_retry(cmd, result["description"])
                autonomy.log_audit(result["action_type"], f"Executed: {final_cmd}", payload, f"exit={shell_result['returncode']}")
                if shell_result["returncode"] != 0:
                    from learning import record_signal
                    record_signal("action_execution_failure", {
                        "action": "shell", "command": final_cmd,
                        "exit_code": shell_result["returncode"],
                        "stderr": shell_result["stderr"][:200],
                    })
                if shell_result["returncode"] == 0 and user_query:
                    interpretation = await _interpret_shell_output(user_query, final_cmd, shell_result)
                    if interpretation:
                        await query.message.reply_text(interpretation)
                    else:
                        await query.message.reply_text(f"```\n{format_output(shell_result, final_cmd)}\n```", parse_mode="Markdown")
                else:
                    await query.message.reply_text(f"```\n{format_output(shell_result, final_cmd)}\n```", parse_mode="Markdown")
            elif result["action_type"] == "terminal_exec":
                import json as _json
                payload = _json.loads(result["payload"]) if isinstance(result["payload"], str) else result["payload"]
                from actions.terminal import send_to_iterm
                iterm_result = await send_to_iterm(payload["command"], payload.get("session", "current"))
                autonomy.log_audit("terminal_exec", f"Sent: {payload['command']}", payload,
                                   "ok" if iterm_result["success"] else iterm_result["error"])
                if iterm_result["success"]:
                    await query.message.reply_text(f"📟 Sent to terminal: `{payload['command']}`", parse_mode="Markdown")
                else:
                    await query.message.reply_text(f"⚠️ Failed: {iterm_result['error']}")

            elif result["action_type"] == "terminal_new_tab":
                import json as _json
                payload = _json.loads(result["payload"]) if isinstance(result["payload"], str) else result["payload"]
                from actions.terminal import create_iterm_tab
                tab_result = await create_iterm_tab(payload.get("command"))
                autonomy.log_audit("terminal_new_tab", "Created tab", payload,
                                   "ok" if tab_result["success"] else tab_result["error"])
                if tab_result["success"]:
                    cmd = payload.get("command")
                    await query.message.reply_text("📟 New terminal tab opened" + (f"\nRunning: `{cmd}`" if cmd else ""))
                else:
                    await query.message.reply_text(f"⚠️ Failed: {tab_result['error']}")

            elif result["action_type"] in ("cursor_open", "cursor_open_project"):
                import json as _json
                payload = _json.loads(result["payload"]) if isinstance(result["payload"], str) else result["payload"]
                from actions.terminal import cursor_open
                open_result = await cursor_open(payload["path"], payload.get("line"))
                autonomy.log_audit(result["action_type"], f"Opened {payload['path']}", payload,
                                   "ok" if open_result["success"] else open_result["error"])
                if open_result["success"]:
                    line_str = f":{payload['line']}" if payload.get("line") else ""
                    await query.message.reply_text(f"🖥 Opened in Cursor: {payload['path']}{line_str}")
                else:
                    await query.message.reply_text(f"⚠️ Failed: {open_result['error']}")

            elif result["action_type"] == "cursor_terminal_exec":
                import json as _json
                payload = _json.loads(result["payload"]) if isinstance(result["payload"], str) else result["payload"]
                from actions.terminal import bridge_send_command
                bridge_result = await bridge_send_command(payload.get("target", "0"), payload["command"])
                autonomy.log_audit("cursor_terminal_exec", f"Sent: {payload['command']}", payload,
                                   "ok" if not bridge_result.get("error") else bridge_result["error"])
                if bridge_result.get("error"):
                    await query.message.reply_text(f"⚠️ {bridge_result['error']}")
                else:
                    await query.message.reply_text(f"🖥 Sent to Cursor terminal: `{payload['command']}`", parse_mode="Markdown")

            elif result["action_type"] == "cursor_terminal_new":
                import json as _json
                payload = _json.loads(result["payload"]) if isinstance(result["payload"], str) else result["payload"]
                from actions.terminal import bridge_create_terminal
                bridge_result = await bridge_create_terminal(command=payload.get("command"))
                autonomy.log_audit("cursor_terminal_new", "Created Cursor terminal", payload,
                                   "ok" if not bridge_result.get("error") else bridge_result.get("error"))
                if bridge_result.get("error"):
                    await query.message.reply_text(f"⚠️ {bridge_result['error']}")
                else:
                    cmd = payload.get("command")
                    await query.message.reply_text("🖥 New Cursor terminal created" + (f"\nRunning: `{cmd}`" if cmd else ""), parse_mode="Markdown")

            elif result["action_type"] == "task_plan":
                import json as _json
                payload = _json.loads(result["payload"]) if isinstance(result["payload"], str) else result["payload"]
                from orchestrator import TaskStep, execute_plan as execute_task_plan, format_plan_summary, ensure_table as ensure_plans_table
                ensure_plans_table()
                steps = [TaskStep.from_dict(s) for s in payload.get("steps", [])]
                original_query = payload.get("query", "")

                async def _execute_single_step(step, prior_results=None):
                    # Route through execution bus when available
                    from execution import get_execution_bus, ExecutionContext, ExecutionSource
                    bus = get_execution_bus()
                    if bus:
                        exec_ctx = ExecutionContext(
                            source=ExecutionSource.ORCHESTRATOR,
                            parent_plan_id=payload.get("plan_id"),
                            prior_results=prior_results or {},
                            chat_id=query.message.chat_id,
                        )
                        params = dict(step.params)
                        params["description"] = step.description
                        params["user_query"] = step.description
                        if prior_results:
                            params["context"] = "\n".join(f"{k}: {v}" for k, v in prior_results.items())
                        result = await bus.execute(step.action, params, exec_ctx)
                        if result.success:
                            return result.output or f"Completed: {step.description}"
                        # Fall through to legacy path on bus failure
                        if result.error and "No handler" not in (result.error or ""):
                            return f"Error: {result.error}"

                    # Legacy fallback
                    intent = None
                    if step.action == "reminder":
                        intent = {"action": "reminder", "text": step.params.get("text", step.description), "time": step.params.get("time", "")}
                    elif step.action == "email":
                        intent = {"action": "email", "to": step.params.get("to", ""), "subject": step.params.get("subject", ""), "context_query": step.params.get("context_query", "")}
                    elif step.action == "calendar":
                        intent = {"action": "calendar"}
                    elif step.action == "shell":
                        intent = {"action": "shell", "command": step.params.get("command", ""), "description": step.description, "llm_generated": True}
                    else:
                        intent = await detect_intent(step.description)
                    if intent:
                        intent["user_query"] = step.description
                        if prior_results:
                            intent["context"] = "\n".join(f"{k}: {v}" for k, v in prior_results.items())
                        handled = await handle_action_intent(intent, ctx)
                        if handled:
                            return f"Completed: {step.description}"
                    return f"Executed: {step.description}"

                plan_result = await execute_task_plan(
                    steps, original_query, channel, query.message.chat_id, _execute_single_step,
                )
                await query.message.reply_text(format_plan_summary(plan_result))

            else:
                status_msg = await autonomy.execute_action(result)
                await query.message.reply_text(status_msg)
        except Exception as e:
            log.error(f"Action execution failed: {e}")
            from learning import record_signal
            record_signal("action_execution_failure", {
                "action": result.get("action_type", "unknown"),
                "error": str(e)[:200],
            })
            await query.message.reply_text(f"❌ Execution failed: {e}")

    elif query.data == "action_deny":
        action = autonomy.get_latest_pending()
        if not action:
            await query.edit_message_text("No pending actions.")
            return

        if autonomy.deny_action(action["id"]):
            await query.edit_message_text(f"❌ Denied: {action['description']}")
        else:
            await query.edit_message_text("Failed to deny action.")

    elif query.data.startswith("extend_generate:"):
        ext_name = query.data.split(":", 1)[1]
        from actions.extend import get_pending_extension
        spec = get_pending_extension(ext_name)
        if not spec:
            await query.edit_message_text("Extension request expired. Try again.")
            return

        await query.edit_message_text(
            f"⚡ Building **{spec['description']}** in background...",
            parse_mode="Markdown",
        )

        # Run build in background — non-blocking
        chat_id = query.message.chat_id
        if channel and chat_id:
            asyncio.create_task(_run_extension_build(spec, channel, chat_id))

    elif query.data == "extend_skip":
        await query.edit_message_text("Skipped. Let me know if you change your mind.")

    elif query.data.startswith("insight_apply:"):
        insight_id = int(query.data.split(":", 1)[1])
        from learning import apply_insight
        if apply_insight(insight_id):
            await query.edit_message_text("Applied. Preference saved.")
        else:
            await query.edit_message_text("Could not apply — insight may already be resolved.")

    elif query.data.startswith("insight_dismiss:"):
        insight_id = int(query.data.split(":", 1)[1])
        from learning import dismiss_insight
        if dismiss_insight(insight_id):
            await query.edit_message_text("Dismissed.")
        else:
            await query.edit_message_text("Could not dismiss — insight may already be resolved.")

    elif query.data.startswith("wf_approve:"):
        wf_id = query.data.split(":", 1)[1]
        from workflows import get_engine
        engine = get_engine()
        if engine:
            wf = engine.get_workflow(wf_id)
            if wf:
                from datetime import datetime as _dt_wf
                from datetime import timezone as _tz_wf
                engine._conn.execute(
                    "UPDATE workflows SET enabled = 1, updated_at = ? WHERE id = ?",
                    (_dt_wf.now(_tz_wf.utc).isoformat(), wf_id),
                )
                engine._conn.commit()
                await query.edit_message_text(f"Workflow enabled: {wf.name}")
            else:
                await query.edit_message_text("Workflow not found.")
        else:
            await query.edit_message_text("Workflow engine not available.")

    elif query.data.startswith("wf_dismiss:"):
        wf_id = query.data.split(":", 1)[1]
        from learning import record_signal
        record_signal("workflow_proposal_dismissed", {"workflow_id": wf_id, "pattern_key": wf_id})
        from workflows import get_engine
        engine = get_engine()
        if engine:
            engine._conn.execute("DELETE FROM workflows WHERE id = ?", (wf_id,))
            engine._conn.commit()
        await query.edit_message_text("Dismissed.")


async def cmd_insights(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show pending insights with interactive approve/dismiss buttons."""
    ctx = _ctx_from_update(update)
    from learning import get_insights
    pending = get_insights(status="pending", limit=5)
    if not pending:
        await ctx.reply("No pending insights. Check back after the next reflection cycle.")
        return

    await ctx.reply(f"{len(pending)} pending insight(s):")
    for insight in pending:
        buttons = [[
            ActionButton("Apply", f"insight_apply:{insight['id']}"),
            ActionButton("Dismiss", f"insight_dismiss:{insight['id']}"),
        ]]
        category = insight.get("category", "general")
        summary = insight.get("summary", "")
        recommendation = insight.get("recommendation", "")
        await ctx.reply(
            f"[{category}] {summary}\nRecommendation: {recommendation}",
            buttons=buttons,
        )


async def cmd_workflows(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List and manage reactive workflows."""
    ctx = _ctx_from_update(update)
    from workflows import get_engine
    engine = get_engine()
    if not engine:
        await ctx.reply("Workflow engine not initialized.")
        return

    if context.args:
        sub = context.args[0].lower()
        if sub == "run" and len(context.args) > 1:
            wf_id = context.args[1]
            wf = engine.get_workflow(wf_id)
            if not wf:
                await ctx.reply(f"Workflow {wf_id} not found.")
                return
            await ctx.reply(f"⚡ Running {wf.name}...")
            await engine._try_execute(wf, "manual", {"manual": True})
            await ctx.reply(f"✅ {wf.name} completed.")
            return
        if sub == "disable" and len(context.args) > 1:
            engine.unregister(context.args[1])
            await ctx.reply(f"Disabled workflow: {context.args[1]}")
            return
        if sub == "enable" and len(context.args) > 1:
            wf_id = context.args[1]
            engine._conn.execute(
                "UPDATE workflows SET enabled = 1 WHERE id = ?", (wf_id,)
            )
            engine._conn.commit()
            await ctx.reply(f"Enabled workflow: {wf_id}")
            return

    await ctx.reply(engine.format_workflows_list())


async def cmd_brief(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ctx = _ctx_from_update(update)
    progress = await ctx.reply("📰 Generating brief...")

    from scheduler.digests import generate_morning_brief

    brief = await generate_morning_brief(ask_claude)
    try:
        await progress.edit(brief)
    except Exception:
        await progress.delete()
        await ctx.reply(brief)


async def cmd_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /email command: /email search <query> or /email draft <to> <subject>"""
    ctx = _ctx_from_update(update)
    args = context.args or []
    if not args:
        await ctx.reply(
            "Usage:\n"
            "  /email search <query> — Search live Gmail\n"
            "  /email draft <to> <subject> — Draft an email"
        )
        return

    subcommand = args[0].lower()

    if subcommand == "search":
        query = " ".join(args[1:])
        if not query:
            await ctx.reply("Usage: /email search <query>")
            return

        await ctx.reply(f"📧 Searching Gmail: {query}")
        try:
            from actions.gmail import search_emails
            emails = await search_emails(query, max_results=5)
        except Exception as e:
            await ctx.reply(f"Gmail search failed: {e}")
            return

        if not emails:
            await ctx.reply("No emails found.")
            return

        text = f"📧 Found {len(emails)} emails:\n\n"
        for e in emails:
            text += f"From: {e['from'][:40]}\n"
            text += f"Subject: {e['subject'][:60]}\n"
            text += f"Date: {e['date'][:20]}\n"
            preview = e.get('body', '')[:300] or e['snippet'][:200]
            text += f"{preview}...\n\n"

        await ctx.reply(text)

    elif subcommand == "draft":
        if len(args) < 3:
            await ctx.reply("Usage: /email draft <to> <subject words...>")
            return

        # Strip optional "to" keyword: "/email draft to user@example.com ..." → skip "to"
        remaining = args[1:]
        if remaining and remaining[0].lower() == "to":
            remaining = remaining[1:]

        if not remaining:
            await ctx.reply("Usage: /email draft [to] <email> <subject words...>")
            return

        to_addr = remaining[0]

        # Strip optional "subject" keyword
        subject_parts = remaining[1:]
        if subject_parts and subject_parts[0].lower() == "subject":
            subject_parts = subject_parts[1:]

        if not subject_parts:
            await ctx.reply("Usage: /email draft [to] <email> <subject words...>")
            return

        # Split subject and body on "body" keyword if present
        # e.g. "/email draft to x@y.com subject Hello body Here is my message"
        subject_str = " ".join(subject_parts)
        user_body = None
        for i, part in enumerate(subject_parts):
            if part.lower() == "body":
                subject_str = " ".join(subject_parts[:i])
                user_body = " ".join(subject_parts[i + 1:])
                break

        if not subject_str:
            await ctx.reply("Usage: /email draft [to] <email> <subject words...> [body <text>]")
            return

        subject = subject_str

        if user_body:
            body = user_body
        elif len(subject.split()) <= 2:
            # Subject too vague for LLM to generate a meaningful body
            await ctx.reply(
                "Subject is too short to generate a body. Either:\n"
                "- Add more detail to the subject\n"
                "- Provide the body directly: /email draft <to> <subject> body <your message>"
            )
            return
        else:
            await ctx.reply(f"📝 Drafting email to {to_addr}...")

            # Use LLM to generate the email body from a descriptive subject
            personal_context = get_relevant_context(subject, max_chars=1500)
            body = await ask_claude(
                f"Write a concise, professional email body for the user to send.\n"
                f"To: {to_addr}\nSubject: {subject}\n\n"
                "Write only the email body, no greeting or signature — the user will add those. "
                "Keep it under 200 words. Only include facts that are clearly implied by the subject. "
                "Do NOT invent details, projects, or specifics that aren't in the subject.",
                personal_context,
            )

        # Create pending action for approval
        action_id = autonomy.create_pending_action(
            "send_email",
            f"Send email to {to_addr}: {subject}",
            {"to": to_addr, "subject": subject, "body": body},
        )

        await _reply_with_keyboard(ctx,
            f"📝 Draft ready:\n\n"
            f"To: {to_addr}\n"
            f"Subject: {subject}\n\n"
            f"{body}\n\n"
            f"---\n"
            f"⚡ Action: Send email via Gmail\n"
            f"{autonomy.format_level()}",
            approve_deny_keyboard())

    else:
        await ctx.reply(
            "Unknown subcommand. Use: /email search <query> or /email draft <to> <subject>"
        )


async def cmd_drive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /drive command: /drive search <query> or /drive recent"""
    ctx = _ctx_from_update(update)
    args = context.args or []
    if not args:
        await ctx.reply(
            "Usage:\n"
            "  /drive search <query> — Search Google Drive\n"
            "  /drive recent [days] — Recently modified files"
        )
        return

    subcommand = args[0].lower()

    if subcommand == "search":
        query = " ".join(args[1:])
        if not query:
            await ctx.reply("Usage: /drive search <query>")
            return

        await ctx.reply(f"📁 Searching Drive: {query}")
        try:
            from actions.drive import search_files
            files = await search_files(query, max_results=8)
        except Exception as e:
            await ctx.reply(f"Drive search failed: {e}")
            return

        if not files:
            await ctx.reply("No files found.")
            return

        text = f"📁 Found {len(files)} files:\n\n"
        for f in files:
            text += f"📄 {f['name']}\n"
            text += f"   Modified: {f['modified']} | {f['link']}\n\n"

        await ctx.reply(text)

    elif subcommand == "recent":
        days = int(args[1]) if len(args) > 1 and args[1].isdigit() else 7
        await ctx.reply(f"📁 Files modified in last {days} days...")
        try:
            from actions.drive import list_recent
            files = await list_recent(days=days, max_results=10)
        except Exception as e:
            await ctx.reply(f"Drive query failed: {e}")
            return

        if not files:
            await ctx.reply("No recent files found.")
            return

        text = f"📁 {len(files)} files modified in last {days} days:\n\n"
        for f in files:
            text += f"📄 {f['name']} ({f['modified']})\n"

        await ctx.reply(text)

    else:
        await ctx.reply(
            "Unknown subcommand. Use: /drive search <query> or /drive recent"
        )


async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /remind command: /remind list, /remind cancel <id>, /remind <time> <text>, /remind recurring ..."""
    ctx = _ctx_from_update(update)
    from actions.reminders import (
        _parse_relative_time, create_reminder, list_reminders, cancel_reminder,
        _parse_natural_cron, create_recurring, list_recurring, cancel_recurring,
    )

    args = context.args or []
    if not args:
        await ctx.reply(
            "Usage:\n"
            "  /remind in 2 hours Buy groceries\n"
            "  /remind tomorrow 9am Review PR\n"
            "  /remind list — Show active reminders\n"
            "  /remind cancel <id> — Cancel a reminder\n"
            "  /remind recurring every Monday 9am Review sprint\n"
            "  /remind recurring list\n"
            "  /remind recurring cancel <id>"
        )
        return

    subcommand = args[0].lower()

    if subcommand == "recurring":
        if len(args) < 2:
            await ctx.reply(
                "Usage:\n"
                "  /remind recurring every Monday 9am Review sprint\n"
                "  /remind recurring every day Check email\n"
                "  /remind recurring first of month Review RRSP\n"
                "  /remind recurring list\n"
                "  /remind recurring cancel <id>"
            )
            return

        recur_sub = args[1].lower()

        if recur_sub == "list":
            recurring = list_recurring()
            if not recurring:
                await ctx.reply("No active recurring reminders.")
                return
            text = f"🔄 {len(recurring)} recurring reminders:\n\n"
            for r in recurring:
                text += f"#{r['id']} — {r['text']}\n   Cron: {r['cron_expression']} | Next: {r['next_fire_at'][:16]}\n\n"
            await ctx.reply(text)

        elif recur_sub == "cancel":
            if len(args) < 3 or not args[2].isdigit():
                await ctx.reply("Usage: /remind recurring cancel <id>")
                return
            if cancel_recurring(int(args[2])):
                await ctx.reply(f"✅ Recurring reminder #{args[2]} cancelled.")
            else:
                await ctx.reply(f"Recurring #{args[2]} not found or already cancelled.")

        else:
            # Parse: /remind recurring <schedule> <text>
            schedule_args = args[1:]  # everything after "recurring"
            cron_expr = None
            reminder_text = None
            for i in range(min(8, len(schedule_args)), 0, -1):
                schedule_part = " ".join(schedule_args[:i])
                parsed = _parse_natural_cron(schedule_part)
                if parsed:
                    cron_expr = parsed
                    reminder_text = " ".join(schedule_args[i:])
                    break

            if not cron_expr or not reminder_text:
                await ctx.reply(
                    "Couldn't parse schedule. Try:\n"
                    "  /remind recurring every monday 9am Review sprint\n"
                    "  /remind recurring every day Check email\n"
                    "  /remind recurring first of month Review RRSP"
                )
                return

            result = create_recurring(reminder_text, cron_expr)
            await ctx.reply(
                f"🔄 Recurring reminder set!\n\n"
                f"#{result['id']}: {result['text']}\n"
                f"Schedule: {result['cron_expression']}\n"
                f"Next: {result['next_fire_at'][:16]}"
            )
        return

    elif subcommand == "list":
        reminders = list_reminders()
        if not reminders:
            await ctx.reply("No active reminders.")
            return
        text = f"⏰ {len(reminders)} active reminders:\n\n"
        for r in reminders:
            text += f"#{r['id']} — {r['text']}\n   Due: {r['due_at']}\n\n"
        await ctx.reply(text)

    elif subcommand == "cancel":
        if len(args) < 2 or not args[1].isdigit():
            await ctx.reply("Usage: /remind cancel <id>")
            return
        if cancel_reminder(int(args[1])):
            await ctx.reply(f"✅ Reminder #{args[1]} cancelled.")
        else:
            await ctx.reply(f"Reminder #{args[1]} not found or already done.")

    else:
        # Parse time expression + reminder text
        full_text = " ".join(args)
        # Try parsing progressively longer prefixes as time
        due_at = None
        reminder_text = None
        for i in range(min(5, len(args)), 0, -1):
            time_part = " ".join(args[:i])
            parsed = _parse_relative_time(time_part)
            if parsed:
                due_at = parsed
                reminder_text = " ".join(args[i:])
                break

        if not due_at or not reminder_text:
            await ctx.reply(
                "Couldn't parse time. Try:\n"
                "  /remind in 30 minutes Call dentist\n"
                "  /remind tomorrow 9am Review PR"
            )
            return

        result = create_reminder(reminder_text, due_at)
        await ctx.reply(
            f"⏰ Reminder set!\n\n"
            f"#{result['id']}: {result['text']}\n"
            f"Due: {result['due_at']}\n\n"
            f"Use /remind list to see all, /remind cancel {result['id']} to remove."
        )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ctx = _ctx_from_update(update)
    clear_conversation(ctx.chat_id)
    await ctx.reply("🧹 Conversation history cleared.")


async def cmd_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ctx = _ctx_from_update(update)
    target = context.args[0].lower() if context.args else "all"

    if target == "email":
        await ctx.reply("📧 Syncing emails...")
        try:
            from actions.gmail_sync import sync_new_emails
            result = await sync_new_emails()
            await ctx.reply(
                f"✅ Email sync complete: {result['fetched']} fetched, {result['indexed']} indexed."
            )
        except Exception as e:
            log.error("Email sync failed: %s", e)
            await ctx.reply(f"❌ Email sync failed: {e}")
    elif target == "all":
        await ctx.reply("🔄 Syncing all live sources (email, Notion, Readwise, Tasks, work email)...")
        try:
            from knowledge.live_sources import index_all_live_sources
            results = await index_all_live_sources(db_conn)
            # Also sync personal email
            from actions.gmail_sync import sync_new_emails
            email_result = await sync_new_emails()
            lines = []
            for src, count in results.items():
                status = f"✅ {count}" if count > 0 else "⚠️ 0"
                lines.append(f"  {src}: {status}")
            lines.append(f"  personal_email: ✅ {email_result['indexed']}")
            total = sum(results.values()) + email_result["indexed"]
            await ctx.reply(f"🔄 Sync complete — {total} items indexed:\n" + "\n".join(lines))
        except Exception as e:
            log.error("Full sync failed: %s", e)
            await ctx.reply(f"❌ Sync failed: {e}")
    else:
        await ctx.reply("Usage: /sync [all|email]\nDefault: /sync all")


async def cmd_enrich(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /enrich [topic] — manually trigger knowledge enrichment."""
    ctx = _ctx_from_update(update)
    topic = " ".join(context.args) if context.args else None

    if topic:
        await ctx.reply(f"🔍 Enriching knowledge for: {topic}...")
        forced = [topic]
    else:
        await ctx.reply("🔍 Detecting knowledge gaps and enriching...")
        forced = None

    try:
        from knowledge.indexer import init_db
        from scheduler.enrichment import enrich_knowledge
        conn = init_db()
        result = await enrich_knowledge(conn, forced_queries=forced)

        if result["docs_indexed"] == 0:
            if forced:
                await ctx.reply(f"No useful content found for \"{topic}\".")
            else:
                await ctx.reply("No knowledge gaps detected in the last 7 days.")
        else:
            lines = [f"✅ Enrichment complete: {result['docs_indexed']} docs indexed\n"]
            for d in result["details"]:
                if d["indexed"] > 0:
                    lines.append(f"  • {d['query']}: {d['indexed']} docs from {len(d['urls'])} pages")
            await ctx.reply("\n".join(lines))
    except Exception as e:
        log.error("Enrichment failed: %s", e)
        await ctx.reply(f"❌ Enrichment failed: {e}")


async def cmd_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ctx = _ctx_from_update(update)
    await ctx.reply("💼 Checking for new job matches...")
    try:
        from actions.jobs import fetch_new_jobs, format_jobs_text
        jobs = await fetch_new_jobs()
        await ctx.reply(format_jobs_text(jobs))
    except Exception as e:
        log.error("Job scraper failed: %s", e)
        await ctx.reply(f"❌ Job scraper failed: {e}")


async def cmd_project(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /project command: view project status."""
    ctx = _ctx_from_update(update)
    from actions.projects import resolve_project, get_project_status, list_projects, get_open_tasks, KNOWN_PROJECTS

    args = context.args or []
    if not args:
        projects = list_projects()
        await ctx.reply(
            f"📋 Projects\n\n{projects}\n\n"
            "Usage: /project <name> — detailed status\n"
            "       /project <name> tasks — open tasks"
        )
        return

    name = args[0]
    key = resolve_project(name)
    if not key:
        await ctx.reply(
            f"Unknown project: {name}\n\nKnown: {', '.join(KNOWN_PROJECTS)}"
        )
        return

    subcommand = args[1].lower() if len(args) > 1 else ""

    if subcommand == "tasks":
        tasks = get_open_tasks(key)
        if not tasks:
            await ctx.reply(f"No open tasks for {key}.")
        else:
            text = f"📝 Open tasks for {key}:\n\n" + "\n".join(f"- [ ] {t}" for t in tasks)
            await ctx.reply(text)
    else:
        status = get_project_status(key)
        await ctx.reply(status)


async def cmd_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /calendar command: show today's or upcoming events."""
    ctx = _ctx_from_update(update)
    args = context.args or []
    days = 1
    if args and args[0].isdigit():
        days = min(int(args[0]), 30)

    await ctx.reply(f"📅 Fetching calendar events ({days} day{'s' if days > 1 else ''})...")
    try:
        from actions.calendar import get_today_events, get_upcoming_events, format_events_text
        if days == 1:
            events = await get_today_events()
        else:
            events = await get_upcoming_events(days=days)
        await ctx.reply(format_events_text(events))
    except FileNotFoundError as e:
        await ctx.reply(f"⚠️ Calendar not configured: {e}")
    except Exception as e:
        log.error("Calendar fetch failed: %s", e)
        await ctx.reply(f"❌ Calendar fetch failed: {e}")


async def cmd_finance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /finance command: show financial dashboard or detailed views."""
    ctx = _ctx_from_update(update)
    from actions.finance import (
        format_dashboard_text,
        get_deadlines,
        format_deadlines_text,
        get_portfolio_summary,
        get_rsu_summary,
    )

    args = context.args or []
    subcommand = args[0].lower() if args else ""

    if subcommand == "deadlines":
        deadlines = get_deadlines()
        await ctx.reply(
            f"📅 Financial Deadlines\n\n{format_deadlines_text(deadlines)}"
        )

    elif subcommand == "portfolio":
        portfolio = get_portfolio_summary()
        if not portfolio:
            await ctx.reply("No portfolio data found.")
            return
        # Truncate for Telegram (4096 char limit)
        await ctx.reply(f"📊 Portfolio\n\n{portfolio[:3500]}")

    elif subcommand == "rsu":
        rsu = get_rsu_summary()
        if not rsu:
            await ctx.reply("No RSU data found.")
            return
        await ctx.reply(f"📈 RSU Summary\n\n{rsu[:3500]}")

    elif subcommand == "ask" and len(args) > 1:
        query = " ".join(args[1:])
        await ctx.reply(f"🔍 Analyzing: {query}")
        personal_context = get_relevant_context("finance investment rrsp tfsa rsu", max_chars=3000)
        results = await hybrid_search(query, limit=5, category="email:finance")
        archive_context = truncate_context(results) if results else ""
        full_context = f"{personal_context}\n\n{archive_context}"
        answer = await ask_claude(
            f"Answer this finance question based on the user's financial records:\n\n{query}",
            full_context,
            system_extra=f"Today's date: {date.today().isoformat()}",
        )
        await ctx.reply(answer)

    else:
        dashboard = format_dashboard_text()
        await ctx.reply(
            f"💰 Financial Dashboard\n\n{dashboard}\n\n"
            "Sub-commands:\n"
            "  /finance deadlines — All deadlines\n"
            "  /finance portfolio — Full portfolio\n"
            "  /finance rsu — RSU/tax summary\n"
            "  /finance ask <question> — Ask about finances"
        )


async def cmd_work(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /work command: sprint dashboard, P0s, filter by theme/owner."""
    ctx = _ctx_from_update(update)
    from actions.work import (
        get_sprint_summary, get_p0_epics, get_epics_by_theme,
        get_epics_by_owner, get_in_progress,
    )

    args = context.args or []
    if not args:
        await ctx.reply(get_sprint_summary())
        return

    subcommand = args[0].lower()

    if subcommand == "p0":
        await ctx.reply(get_p0_epics())

    elif subcommand == "progress":
        await ctx.reply(get_in_progress())

    elif subcommand == "theme" and len(args) > 1:
        theme = " ".join(args[1:])
        await ctx.reply(get_epics_by_theme(theme))

    elif subcommand == "owner" and len(args) > 1:
        name = " ".join(args[1:])
        await ctx.reply(get_epics_by_owner(name))

    elif subcommand == "ask" and len(args) > 1:
        query = " ".join(args[1:])
        await ctx.reply(f"🔍 Analyzing: {query}")
        work_context = get_sprint_summary() + "\n\n" + get_p0_epics()
        results = await hybrid_search(query, limit=5, category="work:planning")
        if results:
            work_context += "\n\n" + truncate_context(results)
        answer = await ask_claude(
            f"Answer this work question based on sprint planning data:\n\n{query}",
            work_context,
            system_extra=f"Today's date: {date.today().isoformat()}",
        )
        await ctx.reply(answer)

    else:
        await ctx.reply(
            "Usage:\n"
            "  /work — Sprint dashboard\n"
            "  /work p0 — P0 epics\n"
            "  /work progress — In-progress items\n"
            "  /work theme <name> — Filter by theme\n"
            "  /work owner <name> — Filter by owner\n"
            "  /work ask <question> — Ask about work"
        )


async def cmd_goals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /goals command: view, add, complete goals."""
    ctx = _ctx_from_update(update)
    from actions.goals import (
        get_current_goals, get_all_goals, add_goal, complete_goal, get_goal_summary,
    )

    args = context.args or []
    if not args:
        await ctx.reply(get_current_goals())
        return

    subcommand = args[0].lower()

    if subcommand == "all":
        await ctx.reply(get_all_goals())

    elif subcommand == "add" and len(args) >= 3:
        category = args[1]
        text = " ".join(args[2:])
        await ctx.reply(add_goal(category, text))

    elif subcommand == "done" and len(args) >= 3:
        category = args[1]
        try:
            index = int(args[2])
        except ValueError:
            await ctx.reply("Usage: /goals done <category> <number>")
            return
        await ctx.reply(complete_goal(category, index))

    elif subcommand == "review":
        await ctx.reply("🔍 Reviewing goals...")
        from actions.work import get_sprint_summary
        goal_text = get_current_goals()
        work_text = get_sprint_summary()
        review_context = f"Goals:\n{goal_text}\n\nWork:\n{work_text}"
        answer = await ask_claude(
            "Review the user's current goals. Are they on track? What's missing? "
            "What should he focus on this week? Be direct and specific.",
            review_context,
            system_extra=f"Today's date: {date.today().isoformat()}",
        )
        await ctx.reply(answer)

    elif subcommand == "progress":
        # M12: Goal progress summary with signals
        from scheduler.planning import get_goal_progress_summary
        from learning import get_weekly_goal_progress
        summary = get_goal_progress_summary()
        signals = get_weekly_goal_progress(days=7)
        if signals:
            summary += "\n\nRecent activity:"
            for s in signals[:5]:
                summary += f"\n  - {s['description']} (x{s['count']})"
        await ctx.reply(summary)

    elif subcommand == "plan":
        # M12: Trigger quarterly planning prompt on demand
        await ctx.reply("📋 Generating quarterly plan...")
        from scheduler.planning import generate_planning_prompt
        prompt = await generate_planning_prompt(ask_claude)
        await ctx.reply(prompt)

    elif subcommand == "midreview":
        # M12: Trigger mid-quarter review on demand
        await ctx.reply("📊 Generating mid-quarter review...")
        from scheduler.planning import generate_mid_quarter_review
        review = await generate_mid_quarter_review(ask_claude)
        await ctx.reply(review)

    elif subcommand == "align":
        # M12: Check goal-domain alignment and conflicts
        await ctx.reply("🔗 Checking goal alignment...")
        from scheduler.planning import map_goal_to_domain, detect_goal_conflicts, _estimate_weekly_hours
        from actions.goals import GOALS_FILE, _parse_goals, _current_quarter as _cq
        quarter = _cq()
        goal_list = []
        try:
            if GOALS_FILE.exists():
                content = GOALS_FILE.read_text(encoding="utf-8")
                goals_data = _parse_goals(content)
                q_goals = goals_data.get(quarter, {})
                for cat, items in q_goals.items():
                    for item in items:
                        if not item["done"]:
                            domain = map_goal_to_domain(item["text"])
                            hours = _estimate_weekly_hours(item["text"])
                            goal_list.append({
                                "text": item["text"],
                                "category": cat,
                                "domain": domain,
                                "estimated_hours": hours,
                            })
        except Exception:
            pass

        if not goal_list:
            await ctx.reply(f"No active goals in {quarter} to analyze.")
            return

        lines = [f"Goal-Domain Alignment ({quarter}):\n"]
        for g in goal_list:
            lines.append(f"  [{g['domain']}] {g['text']} (~{g['estimated_hours']:.0f}h/week)")

        conflicts = await detect_goal_conflicts(goal_list)
        if conflicts:
            lines.append("\nConflicts:")
            for c in conflicts:
                lines.append(f"  ⚠ {c}")
        else:
            lines.append("\nNo capacity conflicts detected.")

        await ctx.reply("\n".join(lines))

    else:
        await ctx.reply(
            "Usage:\n"
            "  /goals — Current quarter goals\n"
            "  /goals all — All quarters\n"
            "  /goals add <category> <text> — Add a goal\n"
            "  /goals done <category> <number> — Mark done\n"
            "  /goals review — LLM-powered reflection\n"
            "  /goals progress — Weekly progress with signals\n"
            "  /goals plan — Trigger quarterly planning\n"
            "  /goals midreview — Mid-quarter review\n"
            "  /goals align — Check goal-domain alignment\n"
            "\nCategories: career, health, learning, personal"
        )


async def cmd_commitments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /commitments command: view, add, complete commitments from meetings."""
    ctx = _ctx_from_update(update)
    from actions.meetings import (
        list_commitments, complete_commitment, add_commitment, format_commitments,
    )

    args = context.args or []
    if not args:
        commitments = list_commitments("open")
        if not commitments:
            await ctx.reply("No open commitments.")
            return
        text = f"Open Commitments ({len(commitments)}):\n\n{format_commitments(commitments)}"
        await ctx.reply(text)
        return

    subcommand = args[0].lower()

    if subcommand == "done" and len(args) >= 2:
        try:
            cid = int(args[1])
        except ValueError:
            await ctx.reply("Usage: /commitments done <id>")
            return
        if complete_commitment(cid):
            await ctx.reply(f"Commitment #{cid} marked as done.")
        else:
            await ctx.reply(f"Commitment #{cid} not found or already done.")

    elif subcommand == "add" and len(args) >= 3:
        # /commitments add <person> <commitment text>
        person = args[1]
        commitment = " ".join(args[2:])
        result = add_commitment("(manual)", person, commitment)
        await ctx.reply(
            f"Commitment #{result['id']} added: {person} -> {commitment}"
        )

    elif subcommand == "all":
        open_c = list_commitments("open")
        done_c = list_commitments("done")
        text = f"Open ({len(open_c)}):\n{format_commitments(open_c)}"
        if done_c:
            text += f"\n\nDone ({len(done_c)}):\n{format_commitments(done_c)}"
        await ctx.reply(text)

    else:
        await ctx.reply(
            "Usage:\n"
            "  /commitments — Open commitments\n"
            "  /commitments all — All commitments\n"
            "  /commitments done <id> — Mark done\n"
            "  /commitments add <person> <text> — Add manually"
        )


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /tasks command: list active and recent task plans from the orchestrator."""
    ctx = _ctx_from_update(update)
    from orchestrator import list_active_plans, ensure_table as ensure_plans_table
    try:
        ensure_plans_table()
        plans = list_active_plans()
    except Exception as e:
        await ctx.reply(f"Failed to load plans: {e}")
        return

    if not plans:
        await ctx.reply("No task plans found.")
        return

    status_icons = {
        "in_progress": "🔄",
        "completed": "✅",
        "partial_failure": "⚠️",
        "blocked": "🚫",
    }

    lines = [f"📋 Task Plans ({len(plans)}):"]
    for p in plans[:10]:
        icon = status_icons.get(p["status"], "❓")
        query_short = p["query"][:60] + ("..." if len(p["query"]) > 60 else "")
        lines.append(
            f"\n{icon} {query_short}\n"
            f"   ID: {p['plan_id']} | {p['step_count']} steps | {p['status']}\n"
            f"   Created: {p['created_at'][:16]}"
        )
        if p.get("completed_at"):
            lines.append(f"   Completed: {p['completed_at'][:16]}")

    await ctx.reply("\n".join(lines))


async def cmd_nudge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Synthesis-driven nudge — capacity score, top risks, top actions."""
    ctx = _ctx_from_update(update)
    try:
        from synthesis.aggregator import aggregate_all_domains
        from synthesis.capacity import detect_overcommitment, capacity_report_to_text

        snapshot = await aggregate_all_domains()
        report = await detect_overcommitment(snapshot)

        # Score label
        score = report.capacity_score
        if score > 80:
            emoji, label = "🔴", "OVERCOMMITTED"
        elif score > 60:
            emoji, label = "🟠", "Heavy Load"
        elif score > 40:
            emoji, label = "🟡", "Busy"
        else:
            emoji, label = "🟢", "Comfortable"

        lines = [f"{emoji} Capacity: {score}/100 ({label})\n"]

        # Top 3 risks
        if report.risk_areas:
            lines.append("Top risks:")
            for risk in report.risk_areas[:3]:
                lines.append(f"  - {risk}")

        # Top 3 recommendations
        if report.recommendations:
            lines.append("\nRecommended actions:")
            for rec in report.recommendations[:3]:
                lines.append(f"  > {rec}")

        if not report.risk_areas and not report.recommendations:
            lines.append("All clear — no immediate risks detected.")

        await ctx.reply("\n".join(lines))

    except Exception as e:
        # Fallback to legacy proactive checks
        log.error("Synthesis nudge failed, falling back: %s", e)
        from scheduler.proactive import run_proactive_checks

        findings = run_proactive_checks()
        if not findings:
            await ctx.reply("All clear — nothing needs attention.")
            return

        text = "Proactive Check — things that need attention:\n\n" + "\n\n".join(findings)
        await ctx.reply(text)


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Execute a shell command on the local machine."""
    ctx = _ctx_from_update(update)
    cmd = " ".join(context.args) if context.args else ""
    if not cmd:
        await ctx.reply(
            "Usage: /run <command>\n\n"
            "Examples:\n"
            "  /run open -a Safari\n"
            "  /run ls ~/Downloads\n"
            "  /run df -h\n"
            "  /run brew list"
        )
        return

    from actions.shell import classify_command, execute_shell, format_output

    classification = classify_command(cmd)

    if classification == ActionType.DANGEROUS:
        autonomy.log_audit("shell_dangerous", f"BLOCKED: {cmd}", result="blocked")
        await ctx.reply(f"🚫 Command blocked (dangerous):\n`{cmd}`", parse_mode="Markdown")
        return

    action_name = "shell_read" if classification == ActionType.READ else "shell_write"

    if autonomy.needs_approval(action_name):
        action_id = autonomy.create_pending_action(
            action_name,
            f"Run: {cmd}",
            {"command": cmd},
        )
        label = "safe" if classification == ActionType.READ else "risky"
        await _reply_with_keyboard(ctx,
            f"🖥 Shell command requires approval:\n\n`{cmd}`\n\n"
            f"Classification: {label}\n{autonomy.format_level()}",
            approve_deny_keyboard(), parse_mode="Markdown")
        return

    # Auto-execute (safe command in GUIDED/AUTONOMOUS mode)
    autonomy.log_audit(action_name, f"Auto-run: {cmd}", result="executing")
    result = await execute_shell(cmd)
    autonomy.log_audit(action_name, f"Completed: {cmd}", result=f"exit={result['returncode']}")
    output = format_output(result, cmd)
    await ctx.reply(output)


async def cmd_audit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ctx = _ctx_from_update(update)
    entries = autonomy.get_audit_log(limit=10)
    if not entries:
        await ctx.reply("No audit log entries yet.")
        return
    text = f"📋 Last {len(entries)} actions:\n\n"
    for e in entries:
        text += f"#{e['id']} [{e['autonomy_level']}] {e['action_type']}\n"
        text += f"   {e['description'][:60]}\n"
        text += f"   Result: {e['result'] or '—'} | {e['timestamp'][:16]}\n\n"
    await ctx.reply(text)


async def cmd_trust(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """M12: Show autonomy graduation status — what's auto-approved and what's learning."""
    ctx = _ctx_from_update(update)
    try:
        from learning import get_graduation_status, get_approval_patterns
        policies = get_graduation_status()
        if not policies:
            await ctx.reply("No graduation data yet. Approval patterns build trust over time.")
            return

        graduated = [p for p in policies if p["graduated"]]
        learning = [p for p in policies if not p["graduated"]]

        lines = ["🎓 **Autonomy Trust Report**\n"]

        if graduated:
            lines.append("**Auto-approved:**")
            for p in graduated:
                lines.append(f"  ✅ {p['action_type']} ({p['context_key']})")

        if learning:
            lines.append("\n**Learning:**")
            for p in learning[:10]:
                rate = f"{p['success_rate']:.0%}" if p['total'] > 0 else "n/a"
                lines.append(
                    f"  📊 {p['action_type']} ({p['context_key']}): "
                    f"{p['current']}/{p['required']} approvals, {rate} success"
                )

        await ctx.reply("\n".join(lines))
    except Exception as e:
        await ctx.reply(f"Trust report failed: {e}")


async def cmd_agents(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """M11: Show background agent status."""
    ctx = _ctx_from_update(update)
    try:
        from agents.coordinator import get_background_agents
        agents = get_background_agents()
        if not agents:
            await ctx.reply("No background agents. Spawn one with a complex research task.")
            return

        status_icons = {"running": "🔄", "completed": "✅", "failed": "❌", "expired": "💤"}
        lines = ["🤖 **Background Agents**\n"]
        for a in agents[:10]:
            icon = status_icons.get(a["status"], "❓")
            lines.append(f"{icon} **{a['id']}**: {a['task'][:60]}")
            if a["final_result"]:
                lines.append(f"   → {a['final_result'][:100]}")
            if a["progress"]:
                lines.append(f"   Progress: {len(a['progress'])} step(s)")

        await ctx.reply("\n".join(lines))
    except Exception as e:
        await ctx.reply(f"Agent status failed: {e}")


async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show system health status including all integrations."""
    ctx = _ctx_from_update(update)
    await ctx.reply("Running health checks...")

    from monitoring import run_health_check

    report = await run_health_check()
    ollama = report["ollama"]
    db = report["database"]
    integrations = report.get("integrations", {})

    lines = [f"🏥 System Health: {report['status'].upper()}\n"]

    # Core infrastructure
    lines.append("**Infrastructure**")
    if ollama["status"] == "ok":
        lines.append(f"  ✅ Ollama ({len(ollama.get('models', []))} models)")
    else:
        lines.append(f"  ❌ Ollama: {ollama.get('error', 'down')}")

    if db["status"] == "ok":
        lines.append(f"  ✅ Database ({db['documents']} docs, {db['active_reminders']} reminders)")
    else:
        lines.append(f"  ❌ Database: {db.get('error', 'unavailable')}")

    # Integrations
    icon_map = {"ok": "✅", "error": "❌", "degraded": "⚠️", "not_configured": "➖"}
    integration_names = {
        "calendar": "Google Calendar",
        "gmail": "Gmail",
        "spotify": "Spotify",
        "claude": "Claude API",
        "github": "GitHub CLI",
        "oauth": "OAuth Tokens",
    }

    lines.append("\n**Integrations**")
    for key in ["calendar", "gmail", "spotify", "claude", "github", "oauth"]:
        info = integrations.get(key, {})
        icon = icon_map.get(info.get("status", "error"), "❓")
        label = integration_names.get(key, key)
        detail = ""
        if key == "calendar" and info.get("status") == "ok":
            detail = f" ({info.get('events_today', 0)} events today)"
        elif key == "oauth" and info.get("status") == "ok":
            detail = f" ({info.get('healthy', 0)}/{info.get('total', 0)} healthy)"
        elif key == "oauth" and info.get("unhealthy"):
            detail = f" (unhealthy: {', '.join(info['unhealthy'])})"
        elif info.get("status") == "error":
            detail = f": {info.get('error', 'unknown')[:80]}"
        lines.append(f"  {icon} {label}{detail}")

    # Issues summary
    if report["issues"]:
        lines.append(f"\n⚠️ **Issues ({len(report['issues'])})**")
        for issue in report["issues"]:
            lines.append(f"  • {issue}")

    # Scheduler
    jobs = scheduler.get_jobs()
    lines.append(f"\n📅 Scheduler: {len(jobs)} jobs")

    await ctx.reply("\n".join(lines))


async def cmd_dev(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show dev environment status — Cursor windows + terminal sessions + bridge."""
    ctx = _ctx_from_update(update)
    from actions.terminal import (
        get_cursor_status, format_cursor_status,
        get_terminal_status, format_terminal_status,
        get_frontmost_app,
        get_cursor_terminal_status, format_cursor_terminal_status,
    )

    cursor_status, terminal_status, frontmost, bridge_status = await asyncio.gather(
        get_cursor_status(),
        get_terminal_status(),
        get_frontmost_app(),
        get_cursor_terminal_status(),
    )

    lines = ["🖥 Dev Environment\n"]
    lines.append(format_cursor_status(cursor_status))
    lines.append("")
    lines.append(format_terminal_status(terminal_status))
    # Show Cursor integrated terminal if bridge is running
    if not bridge_status.get("error"):
        lines.append("")
        lines.append(format_cursor_terminal_status(bridge_status))
    if frontmost:
        lines.append(f"\n🔍 Frontmost: {frontmost}")

    await ctx.reply("\n".join(lines))


async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /backup command: export or list backups."""
    ctx = _ctx_from_update(update)
    from actions.backup import export_backup, list_backups, format_backup_summary

    args = context.args or []
    subcommand = args[0].lower() if args else "export"

    if subcommand == "list":
        backups = list_backups()
        if not backups:
            await ctx.reply("No backups found.")
            return
        text = f"📦 {len(backups)} backup(s):\n\n"
        for b in backups[:10]:
            text += f"  {b['filename']} ({b['size_kb']} KB)\n  Created: {b['created']}\n\n"
        await ctx.reply(text)

    else:
        await ctx.reply("📦 Creating backup...")
        try:
            path = export_backup()
            summary = format_backup_summary(path)
            await ctx.reply(f"✅ Backup created!\n\n{summary}")
        except Exception as e:
            log.error("Backup failed: %s", e)
            await ctx.reply(f"❌ Backup failed: {e}")


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /export command: export portable knowledge to git-synced directory."""
    ctx = _ctx_from_update(update)
    from actions.backup import export_knowledge, import_knowledge, format_knowledge_summary

    args = context.args or []
    subcommand = args[0].lower() if args else "export"

    if subcommand == "import":
        await ctx.reply("📥 Importing knowledge...")
        try:
            counts = import_knowledge()
            if "error" in counts:
                await ctx.reply(f"❌ {counts['error']}")
            else:
                summary = format_knowledge_summary(counts)
                await ctx.reply(f"✅ Knowledge imported!\n\n{summary}")
        except Exception as e:
            log.error("Knowledge import failed: %s", e)
            await ctx.reply(f"❌ Import failed: {e}")
    else:
        await ctx.reply("📚 Exporting knowledge...")
        try:
            counts = export_knowledge()
            summary = format_knowledge_summary(counts)
            await ctx.reply(f"✅ Knowledge exported!\n\n{summary}")
        except Exception as e:
            log.error("Knowledge export failed: %s", e)
            await ctx.reply(f"❌ Export failed: {e}")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ctx = _ctx_from_update(update)
    stats = get_stats()
    text = f"📊 Knowledge Base\n\nTotal documents: {stats['total_documents']}\n\n"
    for cat, count in list(stats["by_category"].items())[:15]:
        text += f"  {cat}: {count}\n"
    text += f"\nMode: {autonomy.format_level()}"
    await ctx.reply(text)


async def cmd_learn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /learn command — view and manage self-improvement insights."""
    ctx = _ctx_from_update(update)
    from learning import get_insights, list_preferences, apply_insight, dismiss_insight, reset_preferences

    args = context.args or []
    subcommand = args[0].lower() if args else ""

    if subcommand == "preferences":
        prefs = list_preferences()
        if not prefs:
            await ctx.reply("No learned preferences yet. Khalil will start learning from your interactions over time.")
            return
        text = f"🧠 {len(prefs)} Learned Preferences:\n\n"
        for p in prefs:
            conf_bar = "●" * int(p["confidence"] * 10) + "○" * (10 - int(p["confidence"] * 10))
            text += f"  {p['key']}: {p['value']}\n  Confidence: [{conf_bar}] {p['confidence']:.1f}\n\n"
        await ctx.reply(text)

    elif subcommand == "apply" and len(args) > 1 and args[1].isdigit():
        if apply_insight(int(args[1])):
            await ctx.reply(f"✅ Insight #{args[1]} applied.")
        else:
            await ctx.reply(f"Insight #{args[1]} not found or not pending.")

    elif subcommand == "dismiss" and len(args) > 1 and args[1].isdigit():
        if dismiss_insight(int(args[1])):
            await ctx.reply(f"❌ Insight #{args[1]} dismissed.")
        else:
            await ctx.reply(f"Insight #{args[1]} not found or not pending.")

    elif subcommand == "reset":
        reset_preferences()
        await ctx.reply("🧹 All learned preferences cleared.")

    elif subcommand == "history":
        insights = get_insights(limit=15)
        if not insights:
            await ctx.reply("No insights yet. Khalil generates insights from weekly reflection.")
            return
        text = f"🧠 Insight History ({len(insights)}):\n\n"
        for i in insights:
            status_icon = {"pending": "⏳", "applied": "✅", "dismissed": "❌", "superseded": "🔄"}.get(i["status"], "?")
            text += f"#{i['id']} {status_icon} [{i['category']}]\n  {i['summary']}\n  {i['recommendation'][:80]}\n\n"
        await ctx.reply(text)

    else:
        # Default: show last 5 insights
        insights = get_insights(limit=5)
        if not insights:
            await ctx.reply(
                "🧠 Khalil Self-Improvement\n\n"
                "No insights yet. Khalil analyzes your interactions weekly to learn your preferences.\n\n"
                "Commands:\n"
                "  /learn — Recent insights\n"
                "  /learn preferences — Active learned preferences\n"
                "  /learn apply <id> — Apply a pending insight\n"
                "  /learn dismiss <id> — Dismiss an insight\n"
                "  /learn history — All insights\n"
                "  /learn reset — Clear all preferences"
            )
            return
        text = "🧠 Recent Insights:\n\n"
        for i in insights:
            status_icon = {"pending": "⏳", "applied": "✅", "dismissed": "❌"}.get(i["status"], "?")
            text += f"#{i['id']} {status_icon} [{i['category']}]\n  {i['summary']}\n"
            if i["status"] == "pending":
                text += f"  → /learn apply {i['id']} | /learn dismiss {i['id']}\n"
            text += "\n"
        await ctx.reply(text)


async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming photo messages — download and route to doc_extract skill."""
    import tempfile
    import os

    ctx = _ctx_from_update(update)
    if not update.message or not update.message.photo:
        return

    caption = (update.message.caption or "").strip()
    if not caption:
        # No caption — treat as generic text extraction
        caption = "extract text"

    await ctx.typing()

    # Download the largest photo
    photo = update.message.photo[-1]  # highest resolution
    photo_path = tempfile.mktemp(suffix=".jpg")
    try:
        tg_file = await context.bot.get_file(photo.file_id)
        await tg_file.download_to_drive(photo_path)
    except Exception as e:
        log.error("Failed to download photo: %s", e)
        await ctx.reply("Could not download the photo.")
        return

    # Match intent from caption
    from skills import get_registry
    registry = get_registry()
    action_type, skill = registry.match_intent(caption)

    if action_type and skill and skill.name == "doc_extract":
        handler = registry.get_handler(action_type)
        if handler:
            intent = {"query": caption, "user_query": caption, "image_path": photo_path}
            try:
                await handler(action_type, intent, ctx)
            finally:
                if os.path.exists(photo_path):
                    os.unlink(photo_path)
            return

    # Default: extract text from the image
    from actions.doc_extract import extract_text
    try:
        await ctx.reply("🔍 Extracting text from image...")
        text = await extract_text(photo_path)
        await ctx.reply(f"📄 {text}")
    except Exception as e:
        log.error("Photo extraction failed: %s", e)
        await ctx.reply(f"Could not extract text: {e}")
    finally:
        if os.path.exists(photo_path):
            os.unlink(photo_path)


async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming voice messages — transcribe, route through SkillRegistry, process.

    Voice pipeline:
    1. Download and transcribe audio → text
    2. Match intent via SkillRegistry to check voice config
    3. If skill requires confirmation, ask before executing
    4. Route transcribed text through handle_message()
    5. Optionally reply with TTS audio if VOICE_REPLY_ENABLED
    """
    import tempfile
    import os
    from actions.voice import convert_ogg_to_wav, transcribe_voice
    from config import VOICE_REPLY_ENABLED

    ctx = _ctx_from_update(update)

    if not update.message or not (update.message.voice or update.message.audio):
        return

    voice = update.message.voice or update.message.audio
    await ctx.typing()

    # Download voice file
    ogg_path = tempfile.mktemp(suffix=".ogg")
    try:
        tg_file = await context.bot.get_file(voice.file_id)
        await tg_file.download_to_drive(ogg_path)
    except Exception as e:
        log.error("Failed to download voice: %s", e)
        await ctx.reply("Could not download voice message.")
        return

    # Convert and transcribe
    wav_path = await convert_ogg_to_wav(ogg_path)
    if not wav_path:
        await ctx.reply("Could not convert voice message (is ffmpeg installed?).")
        for p in [ogg_path]:
            if os.path.exists(p):
                os.unlink(p)
        return

    text = await transcribe_voice(wav_path)

    # Cleanup temp files
    for p in [ogg_path, wav_path]:
        if os.path.exists(p):
            os.unlink(p)

    if not text:
        await ctx.reply("Could not transcribe voice message (is Whisper available?).")
        return

    await ctx.reply(f'🎤 Heard: "{text}"')

    # Check if the matched skill needs voice confirmation before executing
    from skills import get_registry
    registry = get_registry()
    action_type, skill = registry.match_intent(text)

    if action_type and registry.needs_voice_confirmation(action_type):
        skill_name = skill.name if skill else action_type
        await ctx.reply(
            f"⚠️ Voice command matched **{skill_name}** — this action requires confirmation.\n"
            f"Reply 'yes' to proceed, or anything else to cancel."
        )
        # Store pending voice command for confirmation
        _voice_pending[ctx.chat_id] = text
        return

    # Mark this message as voice-originated for response style hints
    update.message.text = text
    ctx._voice_mode = True  # type: ignore[attr-defined]
    await handle_message(update, context)

    # Optional TTS reply
    if VOICE_REPLY_ENABLED:
        await _send_voice_reply(ctx)


# Pending voice commands awaiting confirmation (chat_id -> transcribed text)
_voice_pending: dict[int, str] = {}


async def _handle_voice_confirmation(text: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if this text is a confirmation for a pending voice command.

    Returns True if handled (caller should stop processing), False otherwise.
    """
    ctx = _ctx_from_update(update)
    pending = _voice_pending.pop(ctx.chat_id, None)
    if pending is None:
        return False

    if text.strip().lower() in ("yes", "y", "confirm", "do it", "go ahead", "proceed"):
        update.message.text = pending
        ctx._voice_mode = True  # type: ignore[attr-defined]
        await handle_message(update, context)
        return True
    else:
        await ctx.reply("Voice command cancelled.")
        return True


async def _send_voice_reply(ctx) -> None:
    """Send TTS audio reply for the last text response (best-effort)."""
    try:
        from actions.voice import synthesize_speech
        # Get the last reply text from context (if available)
        last_text = getattr(ctx, '_last_reply_text', None)
        if not last_text:
            return
        audio_path = await synthesize_speech(last_text[:200])
        if audio_path:
            await ctx.reply_voice(audio_path)
            import os
            os.unlink(audio_path)
    except Exception as e:
        log.debug("TTS reply failed: %s", e)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle free-text messages — the main conversational flow."""
    ctx = _ctx_from_update(update)
    import time as _time
    _msg_start = _time.monotonic()

    global OWNER_CHAT_ID
    if OWNER_CHAT_ID is None:
        OWNER_CHAT_ID = ctx.chat_id
        _persist_owner_chat_id(OWNER_CHAT_ID)

    query = (update.message.text if update.message else None)
    if not query or not query.strip():
        if update.message:
            await update.message.reply_text("I didn't catch that \u2014 what can I help you with?")
        return
    query = query.strip()
    if len(query) < 2:
        await update.message.reply_text("I didn't catch that \u2014 what can I help you with?")
        return

    # Rate limiting
    chat_id = update.effective_chat.id
    if not _check_rate_limit(chat_id):
        await update.message.reply_text("⏱️ Too many messages — please wait a moment.")
        return

    # Check for pending voice confirmation
    if await _handle_voice_confirmation(query, update, context):
        return

    # Check for sensitive data in query
    if contains_sensitive_data(query):
        await ctx.reply(
            "⚠️ Your message appears to contain sensitive data. "
            "I'll proceed but won't include raw sensitive values in API calls."
        )

    # Save user message to conversation history
    chat_id = ctx.chat_id
    save_message(chat_id, "user", query)

    # Acknowledge pending follow-ups — user is engaging
    try:
        from agent_loop import acknowledge_follow_ups
        acknowledge_follow_ups()
    except Exception:
        pass

    # Daily plan approval: "approve" creates reminders from pending plan
    if query.strip().lower() in ("approve", "approve plan"):
        try:
            row = db_conn.execute("SELECT value FROM settings WHERE key = 'pending_daily_plan'").fetchone()
            if row and row[0]:
                import json as _json
                from actions.reminders import create_reminder
                from datetime import datetime as _dt, timedelta
                from zoneinfo import ZoneInfo as _ZI
                actions = _json.loads(row[0])
                now_dt = _dt.now(_ZI(TIMEZONE))
                created = []
                for i, a in enumerate(actions):
                    # Space reminders 2h apart starting from now + 1h
                    due = now_dt + timedelta(hours=1 + i * 2)
                    create_reminder(f"📋 {a['description']} (~{a['time_estimate']})", due)
                    created.append(a["description"])
                db_conn.execute("DELETE FROM settings WHERE key = 'pending_daily_plan'")
                db_conn.commit()
                items = "\n".join(f"  {i+1}. {d}" for i, d in enumerate(created))
                await ctx.reply(f"✅ Plan approved! Created {len(created)} reminders:\n{items}")
                return
        except Exception as e:
            log.debug("Daily plan approval failed: %s", e)

    if query.strip().lower() in ("dismiss", "dismiss plan"):
        try:
            db_conn.execute("DELETE FROM settings WHERE key = 'pending_daily_plan'")
            db_conn.commit()
            from learning import record_signal
            record_signal("daily_plan_dismissed", {"query": query})
            await ctx.reply("📋 Plan dismissed. I'll learn from this for tomorrow.")
            return
        except Exception:
            pass

    # M9: Record activity timing for smart proactive alerts
    try:
        from scheduler.proactive import record_activity_timing
        record_activity_timing("user_active")
    except Exception:
        pass

    # Track user corrections for self-healing
    _CORRECTION_PATTERNS = [
        r"^no[,.]?\s+i\s+(?:meant|want)", r"^that'?s\s+not\s+what",
        r"^wrong[,.]", r"^not\s+that", r"^try\s+again",
    ]
    if any(re.search(p, query.lower()) for p in _CORRECTION_PATTERNS):
        from learning import record_signal
        record_signal("user_correction", {"query": query[:200]})
        await _try_inline_healing(ctx)

    # #61: Implicit preference detection — detect preferences in messages
    _PREFERENCE_PATTERNS = [
        (r"\bi\s+prefer\s+(.+?)(?:\.|$)", "general_preference"),
        (r"\b(?:always|never)\s+(.+?)(?:\.|$)", "behavioral_preference"),
        (r"\b(?:i\s+like|i\s+want)\s+(?:it\s+)?(?:when\s+)?(?:you\s+)?(.+?)(?:\.|$)", "style_preference"),
        (r"\b(?:don'?t|stop|quit)\s+(.+?)(?:\.|$)", "negative_preference"),
        (r"\buse\s+(?:bullet\s+points?|lists?|markdown|short\s+(?:answers?|responses?))\b", "format_preference"),
    ]
    for p, ptype in _PREFERENCE_PATTERNS:
        pm = re.search(p, query.lower())
        if pm:
            try:
                from learning import record_signal
                record_signal("implicit_preference", {
                    "type": ptype, "text": query[:200], "match": pm.group(0)[:100],
                })
            except Exception:
                pass
            break

    # --- Fast-path: unambiguous shell patterns (no LLM needed) ---
    direct_intent = _try_direct_shell_intent(query)
    if direct_intent:
        direct_intent["llm_generated"] = False
        direct_intent["user_query"] = query
        handled = await handle_action_intent(direct_intent, ctx)
        if handled:
            return

    # --- Tool-use LLM loop: LLM picks tools, we execute, loop until done ---
    progress_msg = await channel.send_message(chat_id, "🔍 Thinking...")

    try:
        # --- Intent Classification + Task State ---
        from intent import classify_intent, Intent
        from task_manager import TaskManager

        _task_mgr = TaskManager()
        _active_task = _task_mgr.get_active_task(chat_id)
        _intent = classify_intent(query, has_active_task=(_active_task is not None))
        log.info("Intent: %s | Active task: %s | Query: %s",
                 _intent.value, _active_task.id if _active_task else "none", query[:60])

        # Create task for TASK intents
        if _intent == Intent.TASK and not _active_task:
            from intent import is_artifact_request
            _task_type = "artifact" if is_artifact_request(query) else "task"
            _active_task = _task_mgr.create_task(chat_id, query, _task_type)

        # Record attempt for continuations
        if _intent == Intent.CONTINUATION and _active_task:
            _task_mgr.record_attempt(_active_task.id)
            # Check if task should be reset after repeated failures
            if _task_mgr.should_reset(_active_task):
                _task_mgr.reset_task(_active_task.id)
                _active_task = _task_mgr.get_active_task(chat_id)

        # --- Context Assembly (intent-aware) ---
        _t_ctx_start = _time_mod.monotonic()
        from context import assemble_context
        voice_mode = getattr(ctx, '_voice_mode', False)
        full_context = await assemble_context(
            intent=_intent,
            query=query,
            chat_id=chat_id,
            task=_active_task,
            voice_mode=voice_mode,
        )
        _t_search = _time_mod.monotonic() - _t_ctx_start
        _t_context = 0  # included in assemble_context

        # Strategy tools (generate_file, delegate_tasks, spawn_watcher) are now available
        # in the tool-use loop — the LLM decides when to use them, no hardcoded bypass needed.

        # Call LLM with tool-use (falls back to plain streaming for Ollama/privacy)
        _t_llm_start = _time_mod.monotonic()
        display_response = await call_llm_with_tools(
            query, full_context, chat_id, progress_msg, channel,
        )
        _t_llm = _time_mod.monotonic() - _t_llm_start

        # Record component latency breakdown for P95 diagnosis
        try:
            from learning import record_signal
            record_signal("response_latency_breakdown", {
                "search_ms": int(_t_search * 1000),
                "context_ms": int(_t_context * 1000),
                "llm_ms": int(_t_llm * 1000),
                "total_ms": int((_time_mod.monotonic() - _msg_start) * 1000),
            })
        except Exception:
            pass
    except Exception as e:
        log.error("Message handler failed for query '%s': %s", query[:100], e, exc_info=True)
        display_response = "Sorry, I hit an internal error processing that. Please try again."
        await _safe_edit(progress_msg, display_response)

    # Note: hallucination detection moved to generic handler only (handle_message_generic).
    # The Telegram tool-use path (call_llm_with_tools) legitimately includes "[Called tool:"
    # in its formatted responses — detecting here caused false positives.

    # Save to conversation history
    save_message(chat_id, "assistant", display_response)

    # --- Analytics (non-blocking, no Telegram messages) ---
    from learning import detect_search_miss, record_signal
    if detect_search_miss(display_response):
        record_signal("search_miss", {"query": query[:200]})

    try:
        last_digest = db_conn.execute(
            "SELECT created_at FROM interaction_signals WHERE signal_type = 'digest_sent' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if last_digest:
            from datetime import datetime, timedelta
            digest_time = datetime.strptime(last_digest[0], "%Y-%m-%d %H:%M:%S")
            if datetime.utcnow() - digest_time < timedelta(minutes=30):
                record_signal("digest_engaged", {"digest_time": last_digest[0]})
    except Exception:
        pass

    # #2: Record response latency
    _latency_ms = (_time.monotonic() - _msg_start) * 1000
    try:
        record_signal("response_latency", {"latency_ms": round(_latency_ms, 1), "query_len": len(query)})
    except Exception:
        pass

    # #1: Conversation success scoring — record completion signal
    try:
        topic = classify_message_topic(query)
        record_signal("conversation_success", {
            "query": query[:200],
            "topic": topic,
            "latency_ms": round(_latency_ms, 1),
            "had_correction": any(re.search(p, query.lower()) for p in _CORRECTION_PATTERNS),
        })
    except Exception:
        pass



async def handle_message_generic(ctx: MessageContext):
    """Channel-agnostic message handler for non-Telegram channels (Slack, Discord, etc.)."""
    import time as _time
    _msg_start = _time.monotonic()

    global OWNER_CHAT_ID
    query = (ctx.incoming.text if ctx.incoming else "").strip()
    if not query or len(query) < 2:
        await ctx.reply("I didn't catch that — what can I help you with?")
        return

    chat_id = ctx.chat_id

    # Rate limiting
    if not _check_rate_limit(chat_id):
        await ctx.reply("⏱️ Too many messages — please wait a moment.")
        return

    # Enable auto-save so action handler replies are recorded in conversation history
    ctx.auto_save_replies = True
    ctx._save_fn = save_message

    if contains_sensitive_data(query):
        await ctx.reply(
            "Your message appears to contain sensitive data. "
            "I'll proceed but won't include raw sensitive values in API calls."
        )

    save_message(chat_id, "user", query)

    # Detect re-asks (implicit quality signal)
    if _detect_re_ask(chat_id, query):
        try:
            from learning import record_signal
            record_signal("user_re_ask", {"query": query[:200]})
        except Exception:
            pass

    # 1. Skill-pattern matching → try direct dispatch before LLM or shell
    action_hint = _looks_like_action(query)
    if action_hint:
        # --- Eval trace ---
        try:
            from eval.trace import emit_trace
            emit_trace("skill_pattern", action=action_hint)
        except ImportError:
            pass
        # Try direct handler dispatch — skip LLM for pattern-matched skills
        direct_intent = {"action": action_hint, "action_type": action_hint, "user_query": query, "llm_generated": False}
        handled = await handle_action_intent(direct_intent, ctx)
        if handled:
            return
        # Handler didn't handle it — fall through to LLM for parameter extraction
        intent = await detect_intent(query)
        if intent:
            # --- Eval trace ---
            try:
                from eval.trace import emit_trace
                emit_trace("llm_intent", action=intent.get("action_type"))
            except ImportError:
                pass
            intent["user_query"] = query
            handled = await handle_action_intent(intent, ctx)
            if handled:
                return

    # 2. Direct shell intent mapping (no LLM needed) — after skill patterns
    direct_intent = _try_direct_shell_intent(query)
    if direct_intent:
        # --- Eval trace ---
        try:
            from eval.trace import emit_trace
            emit_trace("direct_shell", action=direct_intent.get("action_type"))
        except ImportError:
            pass
        direct_intent["llm_generated"] = False
        direct_intent["user_query"] = query
        handled = await handle_action_intent(direct_intent, ctx)
        if handled:
            return

    # 3. Fall through to conversational LLM flow
    progress_msg = await ctx.reply("Thinking...")

    # Parallel context gathering — these are independent async ops
    async def _gather_search():
        try:
            return await asyncio.wait_for(hybrid_search(query, limit=6), timeout=15.0)
        except Exception as e:
            log.warning("hybrid_search failed/timed out: %s", e)
            return []

    async def _gather_live():
        try:
            from state.collector import collect_live_state, format_for_prompt
            live = await asyncio.wait_for(collect_live_state(), timeout=5.0)
            return format_for_prompt(live)
        except Exception as e:
            log.warning("Live state collection failed: %s", e)
            return ""

    results, conversation_context, live_context = await asyncio.gather(
        _gather_search(),
        get_conversation_context(chat_id, query),
        _gather_live(),
    )

    archive_context = truncate_context(results) if results else "No relevant archive data found."
    personal_context = get_relevant_context(query, max_chars=2000)

    full_context = f"[Source: CONTEXT.md]\n{personal_context}\n\n[Source: knowledge base search]\n{archive_context}"
    if conversation_context:
        full_context = f"{conversation_context}\n\n{full_context}"

    if live_context:
        full_context = f"[Source: live state]\n{live_context}\n\n{full_context}"

    # Voice mode: brief, speakable responses
    if getattr(ctx, '_voice_mode', False):
        full_context = (
            "[Voice mode: User is speaking via voice. Keep your response concise "
            "(1-3 sentences), conversational, and easy to read aloud. "
            "Avoid markdown formatting, bullet lists, and emojis.]\n\n"
            + full_context
        )

    # Swarm check: for multi-intent queries, try parallel agent decomposition
    if _should_try_swarm(query):
        try:
            from agents.coordinator import decompose_to_swarm, run_swarm, synthesize_results
            sub_agents = await decompose_to_swarm(query, full_context, ask_claude)
            if sub_agents:
                log.info("Swarm decomposition: %d agents for query: %s", len(sub_agents), query[:80])
                await progress_msg.edit("\U0001f41d Running parallel agents...")
                swarm_result = await run_swarm(sub_agents)
                response = await synthesize_results(query, swarm_result, ask_claude)
                try:
                    from learning import record_signal
                    record_signal("swarm_used", {
                        "query": query[:200],
                        "agent_count": len(sub_agents),
                        "success_count": len(swarm_result.results),
                        "error_count": len(swarm_result.errors),
                        "elapsed_ms": swarm_result.elapsed_ms,
                    })
                except Exception:
                    pass
                save_message(chat_id, "assistant", response)
                try:
                    await progress_msg.edit(response)
                except Exception:
                    await progress_msg.delete()
                    await ctx.channel.send_message(chat_id, response)
                return
        except Exception as e:
            log.warning("Swarm decomposition failed, falling through to standard path: %s", e)
            try:
                from learning import record_signal
                record_signal("swarm_failed", {"query": query[:200], "error": str(e)[:200]})
            except Exception:
                pass

    # Conversational mode: no skill matched, optimize for quality dialogue
    _conv_extra = (
        "CONVERSATION MODE: No action skill matched this query. "
        "Respond as a thoughtful, knowledgeable personal assistant. "
        "Be warm but concise. Draw on the provided context to personalize your response. "
        "If the user is making small talk, engage naturally. "
        "If they're asking a knowledge question, answer directly from context or general knowledge. "
        "Do NOT suggest the user run commands or check things manually — if you can't help, say so. "
        "Do NOT include [CAPABILITY_GAP] tags for conversational queries.\n\n"
    )
    response = await ask_claude(query, full_context, system_extra=_conv_extra)

    # Extract capability gap tags BEFORE stripping from display
    _gap_tag_re = re.compile(r'\[CAPABILITY_GAP:\s*(\w+)\s*\|\s*(/\w+)\s*\|\s*(.+?)\]')
    _gap_tags = _gap_tag_re.findall(response)  # list of (name, command, description)
    display_response = _gap_tag_re.sub("", response).strip()

    # Detect hallucinated tool calls in conversational mode
    from verification import detect_hallucinated_tools
    if detect_hallucinated_tools(display_response):
        log.warning("Hallucinated tool invocation detected in generic handler — suppressing")
        display_response = (
            "\u26a0\ufe0f I wasn't able to execute this action. "
            "Please try again — I need the tool execution system to be available."
        )

    save_message(chat_id, "assistant", display_response)

    try:
        await progress_msg.edit(display_response)
    except Exception:
        await progress_msg.delete()
        await ctx.channel.send_message(chat_id, display_response)

    _latency_ms = (_time.monotonic() - _msg_start) * 1000
    try:
        from learning import record_signal
        record_signal("response_latency", {"latency_ms": round(_latency_ms, 1), "query_len": len(query)})
    except Exception:
        pass

    # Post-interaction evolution signal collection (fire-and-forget, no LLM)
    # Pass raw response + extracted gap tags so evolution can record capability gaps
    try:
        from evolution import post_interaction_check
        asyncio.create_task(post_interaction_check(query, response, _latency_ms, gap_tags=_gap_tags))
    except Exception:
        pass


async def cmd_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """#1: Record explicit user feedback on conversation quality."""
    ctx = _ctx_from_update(update)
    from learning import record_signal
    args = context.args
    if not args:
        await ctx.reply(
            "Usage: /feedback <positive|negative> [comment]\n"
            "Example: /feedback positive Great answer!\n"
            "Example: /feedback negative Didn't understand my question"
        )
        return
    sentiment = args[0].lower()
    if sentiment not in ("positive", "negative"):
        await ctx.reply("Feedback must be 'positive' or 'negative'.")
        return
    comment = " ".join(args[1:]) if len(args) > 1 else ""
    score = 1.0 if sentiment == "positive" else -1.0
    record_signal("explicit_feedback", {
        "sentiment": sentiment,
        "comment": comment[:500],
    }, value=score)
    await ctx.reply(f"Thanks for the feedback! Recorded as {sentiment}.")


async def cmd_mcp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manage MCP server connections: list, add, remove, tools, test."""
    ctx = _ctx_from_update(update)
    from mcp_client import MCPClientManager, MCPServerConfig

    manager = MCPClientManager.get_instance()
    args = context.args or []
    sub = args[0].lower() if args else "list"

    if sub == "list":
        statuses = manager.get_server_status()
        if not statuses:
            await ctx.reply(
                "No MCP servers configured.\n"
                "Add one with: /mcp add <name> <command> [args...]"
            )
            return
        lines = ["MCP Servers\n"]
        for s in statuses:
            icon = "+" if s["status"] == "connected" else "-"
            lines.append(f"[{icon}] {s['name']} — {s['command']} ({s['status']})")
        await ctx.reply("\n".join(lines))

    elif sub == "add" and len(args) >= 3:
        name = args[1]
        command = args[2]
        cmd_args = args[3:] if len(args) > 3 else []
        config = MCPServerConfig(name=name, command=command, args=cmd_args)
        manager.add_config(config)
        # Try to connect immediately
        client = await manager.get_client(name)
        if client and client.is_connected:
            tools = await client.list_tools()
            manager._cached_tools = await manager.get_all_tools()
            await ctx.reply(
                f"MCP server '{name}' added and connected.\n"
                f"Available tools: {len(tools)}"
            )
        else:
            await ctx.reply(
                f"MCP server '{name}' added but connection failed.\n"
                f"Check that '{command}' is installed and working."
            )

    elif sub == "remove" and len(args) >= 2:
        name = args[1]
        client = manager._clients.get(name)
        if client:
            await client.disconnect()
            del manager._clients[name]
        if manager.remove_config(name):
            manager._cached_tools = await manager.get_all_tools()
            await ctx.reply(f"Removed MCP server '{name}'.")
        else:
            await ctx.reply(f"MCP server '{name}' not found.")

    elif sub == "tools":
        tools = await manager.get_all_tools()
        if not tools:
            await ctx.reply("No MCP tools available. Connect a server first.")
            return
        lines = ["MCP Tools\n"]
        for t in tools:
            lines.append(f"  {t['server']}.{t['name']} — {t['description'][:80]}")
        await ctx.reply("\n".join(lines))

    elif sub == "test" and len(args) >= 2:
        name = args[1]
        client = await manager.get_client(name)
        if not client:
            await ctx.reply(f"MCP server '{name}' not found.")
            return
        if client.is_connected:
            tools = await client.list_tools()
            await ctx.reply(
                f"'{name}' is connected.\nTools: {len(tools)}"
            )
        else:
            await client.reconnect()
            if client.is_connected:
                tools = await client.list_tools()
                manager._cached_tools = await manager.get_all_tools()
                await ctx.reply(
                    f"'{name}' reconnected.\nTools: {len(tools)}"
                )
            else:
                await ctx.reply(f"'{name}' connection failed.")

    else:
        await ctx.reply(
            "Usage:\n"
            "/mcp — list configured servers\n"
            "/mcp add <name> <command> [args...]\n"
            "/mcp remove <name>\n"
            "/mcp tools — list all available tools\n"
            "/mcp test <name> — test connection"
        )


async def cmd_extensions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manage extensions: list, enable, disable, info."""
    ctx = _ctx_from_update(update)
    from extensions.manifest import (
        list_extensions, set_extension_enabled, load_manifest,
    )

    args = context.args or []
    sub = args[0].lower() if args else "list"

    if sub == "list":
        exts = list_extensions()
        if not exts:
            await ctx.reply("No extensions registered.")
            return
        lines = ["📦 Extensions\n"]
        for ext in exts:
            status = "✅" if ext.get("enabled") else "❌"
            lines.append(f"{status} **{ext['name']}** — {ext.get('description', 'no description')}")
        await ctx.reply("\n".join(lines), parse_mode="Markdown")

    elif sub == "enable" and len(args) >= 2:
        name = args[1]
        if set_extension_enabled(name, True):
            await ctx.reply(f"✅ Extension '{name}' enabled. Restart to apply.")
        else:
            await ctx.reply(f"Extension '{name}' not found in manifest.")

    elif sub == "disable" and len(args) >= 2:
        name = args[1]
        if set_extension_enabled(name, False):
            await ctx.reply(f"❌ Extension '{name}' disabled. Restart to apply.")
        else:
            await ctx.reply(f"Extension '{name}' not found in manifest.")

    elif sub == "info" and len(args) >= 2:
        name = args[1]
        manifest = load_manifest()
        entry = manifest["extensions"].get(name)
        if not entry:
            await ctx.reply(f"Extension '{name}' not found.")
            return
        status = "enabled" if entry.get("enabled") else "disabled"
        lines = [
            f"📦 **{name}**\n",
            f"Status: {status}",
            f"Version: {entry.get('version', '?')}",
            f"Type: {entry.get('action_type', '?')}",
            f"Description: {entry.get('description', '—')}",
            f"Created: {entry.get('created_at', '?')}",
            f"PR: {entry.get('source_pr') or '—'}",
        ]
        patterns = entry.get("intent_patterns", [])
        if patterns:
            lines.append(f"Patterns: {', '.join(patterns)}")
        await ctx.reply("\n".join(lines), parse_mode="Markdown")

    else:
        await ctx.reply(
            "Usage:\n"
            "/extensions — list all extensions\n"
            "/extensions enable <name>\n"
            "/extensions disable <name>\n"
            "/extensions info <name>"
        )


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ctx = _ctx_from_update(update)
    await ctx.reply(
        "Unknown command. Send /help to see available commands."
    )


# --- Startup ---


def _wrap_extension_handler(handler_fn, extension_name: str):
    """Wrap extension handler to record usage and failures for monitoring (#31)."""
    async def wrapper(update, context):
        try:
            from learning import record_signal
            record_signal("extension_usage", {"extension": extension_name, "status": "invoked"})
        except Exception:
            pass
        try:
            result = await handler_fn(update, context)
            try:
                from learning import record_signal
                record_signal("extension_usage", {"extension": extension_name, "status": "success"})
            except Exception:
                pass
            return result
        except Exception as e:
            log.error("Extension %s failed: %s", extension_name, e)
            try:
                from learning import record_signal
                record_signal("extension_usage", {"extension": extension_name, "status": "error", "error": str(e)[:200]})
            except Exception:
                pass
            try:
                from learning import record_signal
                record_signal("extension_runtime_failure", {
                    "extension": extension_name,
                    "error": str(e)[:500],
                })
            except Exception:
                pass
            await update.message.reply_text(f"Extension error: {e}")
    return wrapper


# --- #48: Slack Message Sending ---

async def send_slack_message(channel: str, text: str) -> str:
    """Send a message to Slack via incoming webhook.

    Webhook URL is stored in keyring as 'slack-webhook-url'.
    Returns a status message.
    """
    webhook_url = get_secret("slack-webhook-url")
    if not webhook_url:
        return ("Slack webhook not configured. Set it with:\n"
                "  python3 -c \"import keyring; keyring.set_password('khalil-assistant', 'slack-webhook-url', 'YOUR_URL')\"")

    payload = {"text": text}
    if channel:
        payload["channel"] = f"#{channel}" if not channel.startswith("#") else channel

    async with httpx.AsyncClient() as client:
        resp = await client.post(webhook_url, json=payload, timeout=10)
        if resp.status_code == 200:
            return f"Message sent to #{channel}."
        return f"Slack API error: {resp.status_code} — {resp.text[:200]}"


# --- #25: Extension Re-registration Helper ---

def reregister_extension(application, name: str) -> str:
    """Re-register a single extension's command handler on a running Application.

    Call after hot_reload_extension() to update the Telegram handler.
    Returns status message.
    """
    import importlib
    from config import EXTENSIONS_DIR

    manifest_path = EXTENSIONS_DIR / f"{name}.json"
    if not manifest_path.exists():
        return f"Extension '{name}' manifest not found."

    try:
        manifest = json.loads(manifest_path.read_text())
        module_name = manifest["action_module"]
        handler_name = manifest["handler_function"]
        command = manifest["command"]

        mod = sys.modules.get(module_name)
        if mod is None:
            mod = importlib.import_module(module_name)

        handler_fn = getattr(mod, handler_name)
        wrapped = _wrap_extension_handler(handler_fn, manifest.get("name", command))

        # Remove existing handler for this command if present
        for group_handlers in application.handlers.values():
            for h in group_handlers:
                if isinstance(h, CommandHandler) and command in h.commands:
                    group_handlers.remove(h)
                    break

        application.add_handler(CommandHandler(command, wrapped))
        return f"Extension '{name}' re-registered as /{command}."
    except Exception as e:
        return f"Failed to re-register '{name}': {e}"


def _load_extensions(application):
    """Dynamically register command handlers from extension manifests.

    Bootstraps the plugin manifest on first run, then only loads
    extensions that are enabled in extensions.json.
    """
    import importlib
    from config import EXTENSIONS_DIR
    from extensions.manifest import bootstrap_manifest, is_extension_enabled

    if not EXTENSIONS_DIR.exists():
        return

    # Ensure manifest exists with entries for all existing extensions
    bootstrap_manifest()

    for manifest_path in sorted(EXTENSIONS_DIR.glob("*.json")):
        if manifest_path.name == "extensions.json":
            continue
        try:
            manifest = json.loads(manifest_path.read_text())
            module_name = manifest["action_module"]
            handler_name = manifest["handler_function"]
            command = manifest["command"]
            ext_name = manifest.get("name", manifest_path.stem)

            # Skip disabled extensions
            if not is_extension_enabled(ext_name):
                log.info("Extension '%s' is disabled, skipping load", ext_name)
                continue

            # Import the action module
            mod = importlib.import_module(module_name)
            handler_fn = getattr(mod, handler_name)

            # Call ensure_tables if it exists
            if hasattr(mod, "ensure_tables") and db_conn:
                mod.ensure_tables(db_conn)

            # Wrap handler to record runtime failures for healing
            wrapped = _wrap_extension_handler(handler_fn, manifest.get("name", command))
            application.add_handler(CommandHandler(command, wrapped))
            log.info("Extension loaded: /%s from %s", command, module_name)
        except Exception as e:
            log.error("Failed to load extension %s: %s", manifest_path.name, e)


async def start_telegram_bot():
    """Start the Telegram bot with polling."""
    token = get_secret("telegram-bot-token")
    if not token:
        log.error(
            "Telegram bot token not found. Set it with:\n"
            "  python3 -c \"import keyring; keyring.set_password('khalil-assistant', 'telegram-bot-token', 'YOUR_TOKEN')\"\n"
            "  or set TELEGRAM_BOT_TOKEN environment variable."
        )
        return

    application = Application.builder().token(token).build()

    # Register handlers
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("search", cmd_search))
    application.add_handler(CommandHandler("mode", cmd_mode))
    application.add_handler(CommandHandler("approve", cmd_approve))
    application.add_handler(CommandHandler("deny", cmd_deny))
    application.add_handler(CommandHandler("brief", cmd_brief))
    application.add_handler(CommandHandler("email", cmd_email))
    application.add_handler(CommandHandler("drive", cmd_drive))
    application.add_handler(CommandHandler("remind", cmd_remind))
    application.add_handler(CommandHandler("stats", cmd_stats))
    application.add_handler(CommandHandler("audit", cmd_audit))
    application.add_handler(CommandHandler("clear", cmd_clear))
    application.add_handler(CommandHandler("sync", cmd_sync))
    application.add_handler(CommandHandler("enrich", cmd_enrich))
    application.add_handler(CommandHandler("jobs", cmd_jobs))
    application.add_handler(CommandHandler("calendar", cmd_calendar))
    application.add_handler(CommandHandler("project", cmd_project))
    application.add_handler(CommandHandler("finance", cmd_finance))
    application.add_handler(CommandHandler("work", cmd_work))
    application.add_handler(CommandHandler("goals", cmd_goals))
    application.add_handler(CommandHandler("nudge", cmd_nudge))
    application.add_handler(CommandHandler("health", cmd_health))
    application.add_handler(CommandHandler("dev", cmd_dev))
    application.add_handler(CommandHandler("backup", cmd_backup))
    application.add_handler(CommandHandler("export", cmd_export))
    application.add_handler(CommandHandler("run", cmd_run))
    application.add_handler(CommandHandler("learn", cmd_learn))
    application.add_handler(CommandHandler("feedback", cmd_feedback))
    application.add_handler(CommandHandler("extensions", cmd_extensions))
    application.add_handler(CommandHandler("mcp", cmd_mcp))
    application.add_handler(CommandHandler("commitments", cmd_commitments))
    application.add_handler(CommandHandler("tasks", cmd_tasks))
    application.add_handler(CommandHandler("insights", cmd_insights))
    application.add_handler(CommandHandler("workflows", cmd_workflows))
    application.add_handler(CommandHandler("trust", cmd_trust))
    application.add_handler(CommandHandler("agents", cmd_agents))

    # Dynamically register extension handlers
    _load_extensions(application)

    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice_message))
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Set bot commands for Telegram menu
    await application.bot.set_my_commands([
        BotCommand("search", "Search your archives"),
        BotCommand("mode", "View/change autonomy level"),
        BotCommand("approve", "Approve pending action"),
        BotCommand("deny", "Deny pending action"),
        BotCommand("brief", "Morning brief"),
        BotCommand("email", "Search/draft emails"),
        BotCommand("drive", "Search Google Drive"),
        BotCommand("remind", "Set/list reminders"),
        BotCommand("stats", "Knowledge base stats"),
        BotCommand("audit", "View recent actions"),
        BotCommand("sync", "Sync all sources (email, Notion, Readwise, Tasks)"),
        BotCommand("jobs", "Check for new job matches"),
        BotCommand("calendar", "Today's calendar events"),
        BotCommand("finance", "Financial dashboard"),
        BotCommand("work", "Sprint dashboard & epics"),
        BotCommand("goals", "Track quarterly goals"),
        BotCommand("project", "Project status tracking"),
        BotCommand("clear", "Clear conversation history"),
        BotCommand("nudge", "Proactive check — what needs attention"),
        BotCommand("health", "System health status"),
        BotCommand("backup", "Export backup"),
        BotCommand("run", "Run a shell command"),
        BotCommand("learn", "Self-improvement insights"),
        BotCommand("mcp", "Manage MCP server connections"),
        BotCommand("commitments", "Track meeting commitments"),
        BotCommand("tasks", "View active task plans"),
        BotCommand("insights", "View/manage pending insights"),
        BotCommand("help", "Show help"),
    ])

    log.info("Telegram bot starting...")
    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)

    global telegram_app, channel
    telegram_app = application
    channel = TelegramChannel.from_application(application)
    channel_registry.register("telegram", channel)

    return application


def _setup_scheduler():
    """Register scheduled jobs."""
    from scheduler.tasks import sync_emails, send_morning_brief, send_financial_alert, send_weekly_summary, send_career_alert, send_friday_reflection, run_reflection, run_micro_reflection

    def _can_send():
        return channel and OWNER_CHAT_ID

    async def _morning_brief_job():
        if _can_send():
            await send_morning_brief(channel, OWNER_CHAT_ID, ask_claude)
        else:
            log.warning("Morning brief skipped: no channel or owner chat ID yet")

    # #25: Daily anticipation pass — runs after morning brief
    async def _daily_anticipation_job():
        if _can_send():
            try:
                from scheduler.proactive import daily_anticipation
                findings = await daily_anticipation(ask_llm_fn=ask_claude)
                if findings:
                    msg = "\U0001f52e **Daily Anticipation**\n\n" + "\n".join(findings)
                    await channel.send_message(OWNER_CHAT_ID, msg)
            except Exception as e:
                log.warning("Daily anticipation failed: %s", e)

    async def _financial_alert_job():
        if _can_send():
            await send_financial_alert(channel, OWNER_CHAT_ID, ask_claude)

    async def _weekly_summary_job():
        if _can_send():
            await send_weekly_summary(channel, OWNER_CHAT_ID, ask_claude)

    async def _reminder_check_job():
        if not _can_send():
            return
        from actions.reminders import check_due_reminders, check_recurring_due
        # One-shot reminders
        fired = check_due_reminders()
        for r in fired:
            await channel.send_message(OWNER_CHAT_ID, f"⏰ Reminder!\n\n{r['text']}")
            log.info(f"Reminder #{r['id']} fired: {r['text']}")
        # Recurring reminders
        recurring_fired = check_recurring_due()
        for r in recurring_fired:
            await channel.send_message(OWNER_CHAT_ID, f"🔄 Recurring Reminder!\n\n{r['text']}")
            log.info(f"Recurring #{r['id']} fired: {r['text']}")

    # Morning brief at 7:00 AM every day
    scheduler.add_job(
        _morning_brief_job,
        CronTrigger(hour=7, minute=0, timezone=TIMEZONE),
        id="morning_brief",
        name="Morning Brief",
        replace_existing=True,
    )

    scheduler.add_job(
        _daily_anticipation_job,
        CronTrigger(hour=7, minute=10, timezone=TIMEZONE),
        id="daily_anticipation",
        name="#25: Daily Anticipation",
        replace_existing=True,
    )

    # Goal-driven daily plan at 7:05 AM (right after morning brief)
    async def _daily_plan_job():
        if not _can_send():
            return
        try:
            from scheduler.planning import generate_daily_plan, format_daily_plan
            actions = await generate_daily_plan(ask_claude)
            if actions:
                text = format_daily_plan(actions)
                await channel.send_message(OWNER_CHAT_ID, text)
                # Store pending plan for approval handling
                try:
                    import json as _json
                    db_conn.execute(
                        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                        ("pending_daily_plan", _json.dumps([
                            {"description": a.description, "time_estimate": a.time_estimate,
                             "linked_goal": a.linked_goal, "priority": a.priority}
                            for a in actions
                        ])),
                    )
                    db_conn.commit()
                except Exception:
                    pass
                log.info("Daily plan sent: %d actions", len(actions))
        except Exception as e:
            log.warning("Daily plan generation failed: %s", e)

    scheduler.add_job(
        _daily_plan_job,
        CronTrigger(hour=7, minute=5, timezone=TIMEZONE),
        id="daily_plan",
        name="Goal-Driven Daily Plan",
        replace_existing=True,
    )

    # Financial alerts on the 1st and 15th of each month at 9 AM
    scheduler.add_job(
        _financial_alert_job,
        CronTrigger(day="1,15", hour=9, minute=0, timezone=TIMEZONE),
        id="financial_alert",
        name="Financial Alert",
        replace_existing=True,
    )

    # Weekly summary every Sunday at 6 PM
    scheduler.add_job(
        _weekly_summary_job,
        CronTrigger(day_of_week="sun", hour=18, minute=0, timezone=TIMEZONE),
        id="weekly_summary",
        name="Weekly Summary",
        replace_existing=True,
    )

    # Check for due reminders every 60 seconds
    scheduler.add_job(
        _reminder_check_job,
        "interval",
        seconds=60,
        id="reminder_check",
        name="Reminder Check",
        replace_existing=True,
    )

    # Email sync every 6 hours
    scheduler.add_job(
        sync_emails,
        CronTrigger(hour="*/6", minute=15, timezone=TIMEZONE),
        id="email_sync",
        name="Email Sync",
        replace_existing=True,
    )

    # Daily career alert at 10 AM
    async def _career_alert_job():
        if _can_send():
            await send_career_alert(channel, OWNER_CHAT_ID)

    scheduler.add_job(
        _career_alert_job,
        CronTrigger(hour=10, minute=0, timezone=TIMEZONE),
        id="career_alert",
        name="Career Alert",
        replace_existing=True,
    )

    # Friday reflection at 5 PM
    async def _friday_reflection_job():
        if _can_send():
            await send_friday_reflection(channel, OWNER_CHAT_ID, ask_claude)

    scheduler.add_job(
        _friday_reflection_job,
        CronTrigger(day_of_week="fri", hour=17, minute=0, timezone=TIMEZONE),
        id="friday_reflection",
        name="Friday Reflection",
        replace_existing=True,
    )

    # Daily self-check at 8 PM — notify if something is wrong
    async def _self_check_job():
        if not _can_send():
            return
        from monitoring import generate_self_check_message
        msg = await generate_self_check_message()
        if msg:
            await channel.send_message(OWNER_CHAT_ID, msg)
            log.warning("Self-check found issues — notified owner")

    scheduler.add_job(
        _self_check_job,
        CronTrigger(hour=20, minute=0, timezone=TIMEZONE),
        id="self_check",
        name="Daily Self-Check",
        replace_existing=True,
    )

    # #4: Configurable reflection cadence — read from settings, default to existing schedule
    _refl_weekly_day = "sun"
    _refl_weekly_hour = 17
    _refl_micro_hour = 23
    if db_conn:
        try:
            row = db_conn.execute("SELECT value FROM settings WHERE key = 'reflection_weekly_day'").fetchone()
            if row:
                _refl_weekly_day = row[0]
            row = db_conn.execute("SELECT value FROM settings WHERE key = 'reflection_weekly_hour'").fetchone()
            if row:
                _refl_weekly_hour = int(row[0])
            row = db_conn.execute("SELECT value FROM settings WHERE key = 'reflection_micro_hour'").fetchone()
            if row:
                _refl_micro_hour = int(row[0])
        except Exception:
            pass

    # M9: Weekly preference decay — run before reflection
    async def _preference_decay_job():
        try:
            from learning import decay_preferences, gc_old_signals
            archived = decay_preferences()
            # #27: Garbage collect old signals (>30 days)
            pruned = gc_old_signals()
            if _can_send() and (archived or pruned > 0):
                parts = []
                if archived:
                    names = ", ".join(a["key"] for a in archived[:5])
                    parts.append(f"archived {len(archived)} stale preference(s): {names}")
                if pruned > 0:
                    parts.append(f"pruned {pruned} old signals (>30d)")
                await channel.send_message(
                    OWNER_CHAT_ID,
                    f"\U0001f5d1 Memory maintenance: {'; '.join(parts)}"
                )
        except Exception as e:
            log.warning("Preference decay / signal GC failed: %s", e)

    scheduler.add_job(
        _preference_decay_job,
        CronTrigger(day_of_week="sun", hour=20, minute=30, timezone=TIMEZONE),
        id="preference_decay",
        name="M9: Preference Decay",
        replace_existing=True,
    )

    # Weekly reflection (configurable day/hour)
    async def _weekly_reflection_job():
        if _can_send():
            await run_reflection(channel, OWNER_CHAT_ID, ask_claude)

    scheduler.add_job(
        _weekly_reflection_job,
        CronTrigger(day_of_week=_refl_weekly_day, hour=_refl_weekly_hour, minute=0, timezone=TIMEZONE),
        id="weekly_reflection",
        name="Weekly Reflection",
        replace_existing=True,
    )

    # Daily micro-reflection + self-healing check (configurable hour)
    async def _micro_reflection_job():
        await run_micro_reflection(ask_claude, channel=channel, chat_id=OWNER_CHAT_ID)

        # Verify past heal outcomes and detect regressions
        if _can_send():
            try:
                from healing import check_heal_outcomes, detect_healing_regressions
                failed_heals = check_heal_outcomes()
                for h in failed_heals:
                    fp = h.get("fingerprint", "unknown")
                    await channel.send_message(
                        OWNER_CHAT_ID,
                        f"Self-heal ineffective: `{fp}` — signals recurred after patch. Will retry on next cycle.",
                    )
                regressions = detect_healing_regressions()
                for r in regressions:
                    fp = r.get("fingerprint", "unknown")
                    await channel.send_message(
                        OWNER_CHAT_ID,
                        f"Warning: heal for `{fp}` may have caused new failures. Manual review recommended.",
                    )
            except Exception as e:
                log.warning("Heal outcome check failed: %s", e)

    scheduler.add_job(
        _micro_reflection_job,
        CronTrigger(hour=_refl_micro_hour, minute=0, timezone=TIMEZONE),
        id="micro_reflection",
        name="Daily Micro-Reflection",
        replace_existing=True,
    )

    # Proactive alerts — Wednesday 12 PM
    async def _proactive_alerts_job():
        if not _can_send():
            return
        from scheduler.proactive import run_proactive_checks
        findings = run_proactive_checks()
        if findings:
            text = "🔔 Proactive Check — things that need attention:\n\n" + "\n\n".join(findings)
            await channel.send_message(OWNER_CHAT_ID, text)
            log.info("Proactive alert sent: %d findings", len(findings))
        else:
            log.info("Proactive check: all clear")

    scheduler.add_job(
        _proactive_alerts_job,
        CronTrigger(day_of_week="wed", hour=12, minute=0, timezone=TIMEZONE),
        id="proactive_alerts",
        name="Proactive Alerts",
        replace_existing=True,
    )

    # Weekly eval metrics — Sunday 4 AM, compute production metrics and save report
    async def _weekly_eval_metrics_job():
        try:
            from eval.metrics import compute_metrics, save_metrics, print_metrics
            snapshot = compute_metrics()
            path = save_metrics(snapshot)
            log.info("Weekly eval metrics saved: %s", path)
            if _can_send():
                lines = []
                for name, val in [
                    ("Task Completion", snapshot.task_completion_rate),
                    ("Tool Success", snapshot.tool_success_rate),
                    ("User Corrections", snapshot.user_correction_rate),
                    ("Self-Heal", snapshot.self_heal_success_rate),
                ]:
                    if val is not None:
                        lines.append(f"  {name}: {val:.0%}")
                if lines:
                    await channel.send_message(
                        OWNER_CHAT_ID,
                        "📊 Weekly Eval Metrics\n\n" + "\n".join(lines),
                    )
        except Exception as e:
            log.warning("Weekly eval metrics failed: %s", e)

    scheduler.add_job(
        _weekly_eval_metrics_job,
        CronTrigger(day_of_week="sun", hour=4, minute=0, timezone=TIMEZONE),
        id="weekly_eval_metrics",
        name="Weekly Eval Metrics",
        replace_existing=True,
    )

    # OAuth token refresh — every 6 hours, proactively refresh before expiry
    async def _oauth_refresh_job():
        from oauth_utils import proactive_token_refresh
        async def _notify(msg):
            if _can_send():
                await channel.send_message(OWNER_CHAT_ID, msg)
        await proactive_token_refresh(notify_fn=_notify)

    scheduler.add_job(
        _oauth_refresh_job,
        CronTrigger(hour="*/6", minute=30, timezone=TIMEZONE),
        id="oauth_refresh",
        name="OAuth Token Refresh",
        replace_existing=True,
    )

    # Daily signal-to-preference auto-extraction at 2 AM
    async def _auto_extract_job():
        try:
            from learning import auto_extract_preferences
            extracted = auto_extract_preferences()
            if extracted:
                log.info("Auto-extracted %d preferences from signals", len(extracted))
        except Exception as e:
            log.warning("Auto-extraction failed: %s", e)

    scheduler.add_job(
        _auto_extract_job,
        CronTrigger(hour=2, minute=0, timezone=TIMEZONE),
        id="auto_extract_preferences",
        name="Signal-to-Preference Extraction",
        replace_existing=True,
    )

    # M8.5: State-aware proactive alerts — every 30 minutes during work hours
    async def _state_alerts_job():
        if not _can_send():
            return
        from scheduler.state_alerts import run_state_aware_checks
        await run_state_aware_checks(channel, OWNER_CHAT_ID)

    scheduler.add_job(
        _state_alerts_job,
        "interval",
        minutes=30,
        id="state_alerts",
        name="M8.5: State-Aware Alerts",
        replace_existing=True,
    )

    # Agentic Evolution Cycle — 4x/day (3 AM, 9 AM, 3 PM, 9 PM)
    async def _evolution_cycle_job():
        if not _can_send():
            return
        try:
            from evolution import execute_evolution_cycle
            result = await execute_evolution_cycle(channel, OWNER_CHAT_ID, ask_claude, autonomy)
            if result.prs_created:
                await channel.send_message(
                    OWNER_CHAT_ID,
                    f"**Evolution cycle**: {result.executed} improvements executed\n"
                    f"PRs: {', '.join(result.prs_created)}",
                )
            elif result.candidates_found:
                log.info("Evolution cycle: %d candidates, %d executed, no PRs", result.candidates_found, result.executed)
        except Exception as e:
            log.warning("Evolution cycle job failed: %s", e)

    scheduler.add_job(
        _evolution_cycle_job,
        CronTrigger(hour="3,9,15,21", minute=0, timezone=TIMEZONE),
        id="evolution_cycle",
        name="Agentic Evolution Cycle",
        replace_existing=True,
    )

    # Dev environment state polling — every 60 seconds
    async def _dev_state_poll_job():
        if not _can_send():
            return
        from scheduler.tasks import poll_dev_state
        await poll_dev_state(channel, OWNER_CHAT_ID)

    scheduler.add_job(
        _dev_state_poll_job,
        "interval",
        seconds=60,
        id="dev_state_poll",
        name="Dev State Poll",
        replace_existing=True,
    )

    # M11: Pre-meeting brief — check every 5 minutes for upcoming meetings
    async def _meeting_brief_job():
        if not _can_send():
            return
        from state.calendar_provider import get_next_meeting
        from actions.meetings import should_send_meeting_brief, build_meeting_context

        event = await get_next_meeting(within_minutes=20)
        if not event:
            return

        # Only trigger for 13-17 min window (avoids repeated sends)
        minutes_until = event.get("minutes_until", 0)
        if not (13 <= minutes_until <= 17):
            return

        if not should_send_meeting_brief(event):
            return

        try:
            brief = await build_meeting_context(event)
            await channel.send_message(
                OWNER_CHAT_ID,
                f"Meeting in {minutes_until} min:\n\n{brief}",
            )
            log.info("Pre-meeting brief sent for: %s", event.get("title"))
        except Exception as e:
            log.error("Failed to send meeting brief: %s", e)

    scheduler.add_job(
        _meeting_brief_job,
        "interval",
        minutes=5,
        id="meeting_brief",
        name="M11: Pre-Meeting Brief",
        replace_existing=True,
    )

    # M11: Post-meeting follow-up — check every 5 minutes for recently ended meetings
    async def _meeting_followup_job():
        if not _can_send():
            return
        from actions.meetings import (
            get_recently_ended_meetings, make_meeting_key,
            should_prompt_followup, record_followup_prompt,
            is_standup_meeting, ingest_post_meeting_transcript,
        )

        ended = get_recently_ended_meetings()
        for event in ended:
            if is_standup_meeting(event.get("title", "")):
                continue
            key = make_meeting_key(event)
            if not should_prompt_followup(key):
                continue

            record_followup_prompt(key)
            title = event.get("title", "(no title)")
            attendees = event.get("attendees", [])
            names = ", ".join(
                a.split("@")[0].replace(".", " ").title()
                for a in attendees[:3]
            )
            if len(attendees) > 3:
                names += f" +{len(attendees) - 3}"

            # Try to auto-ingest transcript from Google Drive
            transcript_summary = None
            try:
                transcript_summary = await ingest_post_meeting_transcript(event, ask_claude)
            except Exception as e:
                log.debug("Transcript ingestion failed for '%s': %s", title, e)

            if transcript_summary:
                await channel.send_message(OWNER_CHAT_ID, transcript_summary)
                log.info("Auto-ingested transcript for: %s", title)
            else:
                await channel.send_message(
                    OWNER_CHAT_ID,
                    f"Meeting '{title}' with {names} just ended.\n"
                    "Any action items? Reply here and I'll track them.\n"
                    "(I'll stop asking after 30 min.)",
                )
            log.info("Post-meeting follow-up prompt sent for: %s", title)

    scheduler.add_job(
        _meeting_followup_job,
        "interval",
        minutes=5,
        id="meeting_followup",
        name="M11: Post-Meeting Follow-up",
        replace_existing=True,
    )

    # M12: Quarterly planning — check daily at 9 AM if it's a planning trigger date
    async def _quarterly_planning_job():
        if _can_send():
            from scheduler.tasks import send_quarterly_planning
            await send_quarterly_planning(channel, OWNER_CHAT_ID, ask_claude)

    scheduler.add_job(
        _quarterly_planning_job,
        CronTrigger(hour=9, minute=30, timezone=TIMEZONE),
        id="quarterly_planning",
        name="M12: Quarterly Planning Check",
        replace_existing=True,
    )

    # M12: Mid-quarter review — check daily at 9 AM if it's a review date
    async def _mid_quarter_review_job():
        if _can_send():
            from scheduler.tasks import send_mid_quarter_review
            await send_mid_quarter_review(channel, OWNER_CHAT_ID, ask_claude)

    scheduler.add_job(
        _mid_quarter_review_job,
        CronTrigger(hour=9, minute=45, timezone=TIMEZONE),
        id="mid_quarter_review",
        name="M12: Mid-Quarter Review Check",
        replace_existing=True,
    )

    # Knowledge enrichment — Wed + Sat at 2 PM
    async def _knowledge_enrichment_job():
        if _can_send():
            from scheduler.tasks import run_knowledge_enrichment
            await run_knowledge_enrichment(channel, OWNER_CHAT_ID)

    scheduler.add_job(
        _knowledge_enrichment_job,
        CronTrigger(day_of_week="wed,sat", hour=14, minute=0, timezone=TIMEZONE),
        id="knowledge_enrichment",
        name="Knowledge Enrichment",
        replace_existing=True,
    )

    # Live source indexing — Notion, Readwise, Google Tasks, work email
    async def _live_source_indexing_job():
        try:
            from knowledge.live_sources import index_all_live_sources
            results = await index_all_live_sources(db_conn)
            total = sum(results.values())
            log.info("Live source indexing complete: %s (total: %d)", results, total)
        except Exception as e:
            log.warning("Live source indexing failed: %s", e)

    scheduler.add_job(
        _live_source_indexing_job,
        CronTrigger(hour=2, minute=30, timezone=TIMEZONE),
        id="live_source_indexing",
        name="Live Source Indexing",
        replace_existing=True,
    )

    # Knowledge export — daily at 3:00 AM, commits to git
    async def _knowledge_export_job():
        try:
            from actions.backup import export_knowledge
            counts = export_knowledge()
            total = sum(counts.values())
            log.info("Scheduled knowledge export: %d rows across %d tables", total, len(counts))
        except Exception as e:
            log.warning("Scheduled knowledge export failed: %s", e)

    scheduler.add_job(
        _knowledge_export_job,
        CronTrigger(hour=3, minute=0, timezone=TIMEZONE),
        id="knowledge_export",
        name="Knowledge Export",
        replace_existing=True,
    )

    # Full DB backup — daily at 3:15 AM, uploads gzipped DB as GitHub Release asset
    async def _full_db_backup_job():
        try:
            from actions.backup import backup_full_db
            result = backup_full_db()
            if result.get("status") == "success":
                log.info("Scheduled DB backup: %s MB → %s", result["size_mb"], result["tag"])
            else:
                log.warning("Scheduled DB backup failed: %s", result.get("error", "unknown"))
        except Exception as e:
            log.warning("Scheduled DB backup failed: %s", e)

    scheduler.add_job(
        _full_db_backup_job,
        CronTrigger(hour=3, minute=15, timezone=TIMEZONE),
        id="full_db_backup",
        name="Full DB Backup",
        replace_existing=True,
    )

    # Email inbox categorization — daily at 8:00 AM
    async def _email_categorize_job():
        try:
            from actions.email_categorizer import handle_intent
            from types import SimpleNamespace
            # Minimal context — just needs a reply function for logging
            _ctx = SimpleNamespace(reply=lambda msg: log.info("Email categorizer: %s", msg))
            await handle_intent("label", {}, _ctx)
            log.info("Scheduled email categorization completed")
        except Exception as e:
            log.warning("Scheduled email categorization failed: %s", e)

    scheduler.add_job(
        _email_categorize_job,
        CronTrigger(hour=8, minute=0, timezone=TIMEZONE),
        id="email_categorize",
        name="Email Categorization",
        replace_existing=True,
    )

    log.info("Scheduler jobs registered")


@app.on_event("startup")
async def startup():
    global db_conn, autonomy, claude, OWNER_CHAT_ID

    log.info("Khalil starting up...")

    # Initialize database
    db_conn = init_db()

    # Record boot time for restart detection (before overwriting, save previous)
    from datetime import datetime as _dt, timezone as _tz
    _prev_boot = db_conn.execute(
        "SELECT value FROM settings WHERE key = 'last_boot_time'"
    ).fetchone()
    if _prev_boot and _prev_boot[0]:
        db_conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('previous_boot_time', ?)",
            (_prev_boot[0],),
        )
    _boot_time = _dt.now(_tz.utc).isoformat()
    db_conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('last_boot_time', ?)",
        (_boot_time,),
    )
    db_conn.commit()
    log.info("Boot time recorded: %s (previous: %s)", _boot_time, _prev_boot[0] if _prev_boot else "none")

    # M11: Create meeting intelligence tables
    from actions.meetings import ensure_tables as _ensure_meeting_tables
    _ensure_meeting_tables(db_conn)
    autonomy = AutonomyController(db_conn)
    # Share DB connection with learning module
    from learning import set_conn as set_learning_conn
    set_learning_conn(db_conn)
    log.info(f"Autonomy level: {autonomy.format_level()}")

    # Initialize unified execution bus (M1)
    from execution import init_execution_bus
    from skills import get_registry
    _execution_bus = init_execution_bus(
        get_registry_fn=get_registry,
        autonomy_controller=autonomy,
        ask_llm_fn=None,  # set after ask_llm is defined
    )

    # Load persisted owner chat ID so notifications work after restart
    row = db_conn.execute("SELECT value FROM settings WHERE key = 'owner_chat_id'").fetchone()
    if row and row[0]:
        try:
            OWNER_CHAT_ID = int(row[0])
            log.info("Loaded owner chat ID: %d", OWNER_CHAT_ID)
        except (ValueError, TypeError):
            log.warning("Invalid owner_chat_id '%s' — send /start to re-register", row[0])

    # Initialize LLM backend
    if LLM_BACKEND == "claude":
        api_key = get_secret("anthropic-api-key")
        if not api_key:
            log.error(
                "Claude backend selected but no API key found. Set it with:\n"
                "  python3 -c \"import keyring; keyring.set_password('khalil-assistant', 'anthropic-api-key', 'YOUR_KEY')\"\n"
                "  or set ANTHROPIC_API_KEY environment variable.\n"
                "  Or switch to Ollama: set LLM_BACKEND = 'ollama' in config.py"
            )
            return
        global _taskforce_client, _taskforce_client_long
        if CLAUDE_BASE_URL:
            # Taskforce proxy uses OpenAI-compatible API
            from openai import AsyncOpenAI
            _headers = {CLAUDE_API_KEY_HEADER: api_key} if CLAUDE_API_KEY_HEADER else {}
            _taskforce_client = AsyncOpenAI(
                api_key=api_key,
                base_url=CLAUDE_BASE_URL,
                default_headers=_headers,
                max_retries=1,  # 1 SDK retry for connection-level failures; app-level retry handles 429
            )
            # Separate client for long-running generation (generate_file) —
            # won't hold connections from the main pool during 5-minute calls
            _taskforce_client_long = AsyncOpenAI(
                api_key=api_key,
                base_url=CLAUDE_BASE_URL,
                default_headers=_headers,
                max_retries=0,  # generate_file has its own model cascade
            )
            log.info(f"LLM backend: Claude ({CLAUDE_MODEL}) via Taskforce {CLAUDE_BASE_URL}")
            # Initialize backup provider clients (OpenAI, Google) via Taskforce
            global _openai_client, _google_client
            if OPENAI_BASE_URL:
                _openai_client = AsyncOpenAI(
                    api_key=api_key, base_url=OPENAI_BASE_URL,
                    default_headers={CLAUDE_API_KEY_HEADER: api_key} if CLAUDE_API_KEY_HEADER else {},
                )
                log.info(f"Backup LLM: OpenAI ({OPENAI_MODEL}) via {OPENAI_BASE_URL}")
            if GOOGLE_BASE_URL:
                _google_client = AsyncOpenAI(
                    api_key=api_key, base_url=GOOGLE_BASE_URL,
                    default_headers={CLAUDE_API_KEY_HEADER: api_key} if CLAUDE_API_KEY_HEADER else {},
                )
                log.info(f"Backup LLM: Google ({GOOGLE_MODEL}) via {GOOGLE_BASE_URL}")
        else:
            claude = anthropic.AsyncAnthropic(api_key=api_key)
            log.info(f"LLM backend: Claude ({CLAUDE_MODEL})")
    else:
        log.info(f"LLM backend: Ollama ({OLLAMA_LLM_MODEL})")
        # Health check: verify Ollama is reachable
        from knowledge.embedder import check_ollama
        if await check_ollama():
            log.info("Ollama health check: OK")
        else:
            log.warning(
                "Ollama health check FAILED — LLM and embeddings will be unavailable. "
                "Start Ollama with: ollama serve"
            )

    # Proactive OAuth token refresh
    try:
        from oauth_utils import proactive_token_refresh
        token_problems = await proactive_token_refresh()
        if token_problems:
            log.warning("OAuth token issues at startup: %s", token_problems)
        else:
            log.info("OAuth tokens: all healthy")
    except Exception as e:
        log.warning("OAuth token check failed: %s", e)

    # Initialize MCP client manager (connect to configured external MCP servers)
    try:
        from mcp_client import MCPClientManager
        mcp_manager = MCPClientManager.get_instance()
        await mcp_manager.initialize()
        mcp_manager._cached_tools = await mcp_manager.get_all_tools()
        log.info("MCP client manager initialized (%d tools available)",
                 len(mcp_manager._cached_tools))
        # M2: Register MCP tools as Khalil skills
        try:
            from mcp_skill_bridge import register_mcp_skills
            from skills import get_registry
            count = await register_mcp_skills(get_registry())
            if count:
                log.info("MCP skill bridge: registered %d tools as skills", count)
        except Exception as bridge_err:
            log.warning("MCP skill bridge failed: %s", bridge_err)
    except Exception as e:
        log.warning("MCP client initialization failed: %s", e)

    # Register webhook handlers
    from webhooks.github import GitHubWebhookHandler
    from webhooks.registry import register as register_webhook
    register_webhook("github", GitHubWebhookHandler())

    # Wire up execution bus with LLM + composite actions (M8: Layer Composition)
    from execution import get_execution_bus, ExecutionContext, ExecutionSource, ExecutionResult
    _exec_bus = get_execution_bus()
    if _exec_bus:
        _exec_bus._ask_llm = ask_llm

        # M8: Register composite action handlers
        async def _composite_orchestrate(params: dict, ctx: ExecutionContext) -> ExecutionResult:
            """Decompose a natural language task into a plan and execute it."""
            from orchestrator import decompose_request, execute_plan as execute_task_plan, format_plan_summary, ensure_table as ensure_plans_table
            ensure_plans_table()
            task_desc = params.get("task", params.get("description", ""))
            if not task_desc:
                return ExecutionResult(success=False, output="", error="No task description provided")
            steps = await decompose_request(task_desc, "", ask_llm)
            if not steps:
                return ExecutionResult(success=False, output="", error="Could not decompose into steps")
            child_ctx = ctx.child(ExecutionSource.ORCHESTRATOR, parent_plan_id=None)
            async def _step_fn(step, prior_results=None):
                r = await _exec_bus.execute(
                    step.action, {**step.params, "description": step.description},
                    child_ctx,
                )
                return r.output if r.success else f"Error: {r.error}"
            plan_result = await execute_task_plan(
                steps, task_desc, channel, ctx.chat_id or OWNER_CHAT_ID, _step_fn,
                ask_llm_fn=ask_llm,
            )
            return ExecutionResult(success=plan_result.status == "completed",
                                   output=format_plan_summary(plan_result))

        async def _composite_tool_reason(params: dict, ctx: ExecutionContext) -> ExecutionResult:
            """Run a query through the tool-use LLM loop."""
            query_text = params.get("query", params.get("task", ""))
            if not query_text:
                return ExecutionResult(success=False, output="", error="No query provided")
            response = await ask_llm(query_text, "", "")
            return ExecutionResult(success=True, output=response)

        async def _composite_workflow(params: dict, ctx: ExecutionContext) -> ExecutionResult:
            """Trigger a named workflow."""
            workflow_name = params.get("workflow", params.get("name", ""))
            if not workflow_name:
                return ExecutionResult(success=False, output="", error="No workflow name provided")
            try:
                from workflows import get_engine
                engine = get_engine()
                if not engine:
                    return ExecutionResult(success=False, output="", error="Workflow engine not initialized")
                wf = engine.get_workflow(workflow_name)
                if not wf:
                    return ExecutionResult(success=False, output="", error=f"Workflow '{workflow_name}' not found")
                results = await engine._execute_steps(wf, params.get("event_data", {}))
                output = "\n".join(
                    f"{'ok' if r.get('ok') else 'fail'}: {r.get('action', '?')}" for r in results
                )
                return ExecutionResult(success=all(r.get("ok") for r in results), output=output)
            except Exception as e:
                return ExecutionResult(success=False, output="", error=str(e)[:500])

        _exec_bus.register_composite_action("orchestrate", _composite_orchestrate)
        _exec_bus.register_composite_action("tool_reason", _composite_tool_reason)
        _exec_bus.register_composite_action("workflow", _composite_workflow)

    # Initialize workflow engine
    try:
        from workflows import init_engine as init_workflow_engine
        from learning import register_signal_hook
        wf_engine = init_workflow_engine(
            conn=db_conn, channel=channel, chat_id=OWNER_CHAT_ID,
            ask_llm_fn=ask_llm,
        )
        register_signal_hook(wf_engine.evaluate_signal)
        log.info("Workflow engine initialized (%d workflows)", len(wf_engine.list_workflows()))
    except Exception as e:
        log.warning("Workflow engine not started: %s", e)

    # Start Telegram bot — await so `channel` is set before agent loop check
    try:
        await start_telegram_bot()
    except Exception as e:
        log.error("Telegram bot startup failed: %s", e)

    # Start Slack channel if configured
    try:
        slack_bot_token = get_secret("slack-bot-token")
        slack_app_token = get_secret("slack-app-token")
        if slack_bot_token and slack_app_token:
            from channels.slack import SlackChannel
            slack_ch = SlackChannel(slack_bot_token, slack_app_token)
            channel_registry.register("slack", slack_ch)
            asyncio.create_task(slack_ch.start_socket_mode(handle_message_generic))
            log.info("Slack channel started")
        else:
            log.info("Slack tokens not configured — skipping Slack channel")
    except Exception as e:
        log.warning("Slack channel not started: %s", e)

    # Start WhatsApp channel if configured
    try:
        wa_token = keyring.get_password(KEYRING_SERVICE, "whatsapp-access-token")
        wa_phone_id = keyring.get_password(KEYRING_SERVICE, "whatsapp-phone-number-id")
        if wa_token and wa_phone_id:
            from channels.whatsapp import WhatsAppChannel
            wa_ch = WhatsAppChannel(wa_phone_id, wa_token)
            channel_registry.register("whatsapp", wa_ch)
            log.info("WhatsApp channel registered")
        else:
            log.info("WhatsApp tokens not configured — skipping WhatsApp channel")
    except Exception as e:
        log.warning("Failed to register WhatsApp channel: %s", e)

    # Start Discord channel if configured
    try:
        discord_token = get_secret("discord-bot-token")
        if discord_token:
            from channels.discord import DiscordChannel
            discord_ch = DiscordChannel(discord_token)
            channel_registry.register("discord", discord_ch)
            asyncio.create_task(discord_ch.start_bot(handle_message_generic))
            log.info("Discord channel started")
        else:
            log.info("Discord token not configured — skipping Discord channel")
    except Exception as e:
        log.warning("Failed to start Discord channel: %s", e)

    # Start scheduler
    _setup_scheduler()
    scheduler.start()
    log.info(f"Scheduler started with {len(scheduler.get_jobs())} jobs")

    # #21: Startup self-test — check all subsystems and report
    try:
        from monitoring import run_startup_self_test, format_startup_report
        test_results = await run_startup_self_test()
        report = format_startup_report(test_results)
        log.info("Startup self-test:\n%s", report)

        # Pipeline smoke test — verify the agent pipeline is wired correctly
        from monitoring import run_pipeline_smoke_test, format_pipeline_smoke_report
        pipeline_results = run_pipeline_smoke_test()
        pipeline_report = format_pipeline_smoke_report(pipeline_results)
        log.info("Pipeline smoke test:\n%s", pipeline_report)
        if pipeline_results["overall"] != "ok":
            test_results["overall"] = "degraded"
            test_results.setdefault("issues", []).extend(pipeline_results["issues"])
            report += "\n\n" + pipeline_report

        # Send report to owner via Telegram if there are issues
        if test_results["overall"] != "ok" and OWNER_CHAT_ID and channel:
            try:
                await channel.send_message(OWNER_CHAT_ID, report)
            except Exception as e:
                log.warning("Could not send startup report to Telegram: %s", e)
    except Exception as e:
        log.warning("Startup self-test failed: %s", e)

    # Start agent loop — continuous sense-think-act background process
    from config import AGENT_LOOP_ENABLED, AGENT_LOOP_INTERVAL_S, AGENT_LOOP_QUIET_HOURS
    if AGENT_LOOP_ENABLED and OWNER_CHAT_ID and channel:
        from agent_loop import AgentLoop
        _agent_loop = AgentLoop(
            channel=channel,
            chat_id=OWNER_CHAT_ID,
            autonomy=autonomy,
            ask_llm_fn=ask_llm,
            interval_s=AGENT_LOOP_INTERVAL_S,
            quiet_hours=AGENT_LOOP_QUIET_HOURS,
        )
        asyncio.create_task(_agent_loop.start())
        log.info("Agent loop started (interval=%ds)", AGENT_LOOP_INTERVAL_S)
    else:
        log.info("Agent loop disabled (KHALIL_AGENT_LOOP=%s, owner=%s, channel=%s)",
                 AGENT_LOOP_ENABLED, OWNER_CHAT_ID, bool(channel))

    log.info("Khalil is ready.")

    # Auto-resume: if restarting, check for unfinished work and notify user
    if OWNER_CHAT_ID and channel and _prev_boot and _prev_boot[0]:
        asyncio.create_task(_auto_resume_after_restart(channel, OWNER_CHAT_ID))


async def _auto_resume_after_restart(channel, chat_id: int):
    """After a restart, check for unfinished work and proactively resume."""
    await asyncio.sleep(5)  # Let everything initialize
    try:
        from memory.session_continuity import get_last_tool_use_context
        tool_ctx = get_last_tool_use_context(chat_id, max_chars=1500)
        if not tool_ctx:
            log.info("Auto-resume: no in-progress work found")
            return

        # Extract the user's original query from the context
        lines = tool_ctx.split("\n")
        task_line = next((l for l in lines if l.startswith("User asked:")), "")
        task_desc = task_line.replace("User asked:", "").strip()[:200] if task_line else "a task"

        await channel.send_message(
            chat_id,
            f"🔄 I restarted and found unfinished work:\n\n"
            f"**{task_desc}**\n\n"
            f"Want me to continue where I left off?",
        )
        log.info("Auto-resume: notified user about unfinished work: %s", task_desc[:80])
    except Exception as e:
        log.warning("Auto-resume check failed: %s", e)


@app.get("/health")
async def health():
    from monitoring import run_health_check

    report = await run_health_check()
    jobs = [
        {"id": j.id, "name": j.name, "next_run": str(j.next_run_time)}
        for j in scheduler.get_jobs()
    ]
    report["autonomy_level"] = autonomy.format_level() if autonomy else "not initialized"
    report["scheduled_jobs"] = jobs
    return report


# --- WhatsApp webhook endpoints (must be before generic {source} catch-all) ---

@app.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request):
    """Handle inbound WhatsApp messages via Meta Cloud API webhook."""
    body = await request.body()
    payload = json.loads(body)

    whatsapp_ch = channel_registry.get("whatsapp")
    if not whatsapp_ch:
        return {"ok": True}  # Acknowledge but ignore if not configured

    from channels.whatsapp import WhatsAppChannel
    incoming = WhatsAppChannel.extract_incoming(payload)
    if not incoming:
        return {"ok": True}  # Not a text message

    ctx = MessageContext(
        channel=whatsapp_ch,
        chat_id=incoming.chat_id,
        user_id=incoming.user_id,
        channel_type=ChannelType.WHATSAPP,
        incoming=incoming,
    )

    # Process in background to return 200 quickly (WhatsApp requires fast response)
    asyncio.create_task(_process_whatsapp_message(ctx))
    return {"ok": True}


async def _process_whatsapp_message(ctx: MessageContext):
    """Process an inbound WhatsApp message through the standard pipeline."""
    try:
        query = ctx.incoming.text if ctx.incoming else ""
        if not query:
            return

        chat_id = ctx.chat_id

        # Save user message to conversation history
        save_message(chat_id, "user", query)

        # Try intent detection first (actions: email, reminder, calendar, etc.)
        intent = await detect_intent(query)
        if intent:
            intent["user_query"] = query
            handled = await handle_action_intent(intent, ctx)
            if handled:
                return

        # Fall through to conversational LLM flow — parallel context gathering
        async def _ch_search():
            try:
                return await asyncio.wait_for(hybrid_search(query, limit=6), timeout=15.0)
            except Exception:
                return []

        results, conversation_context = await asyncio.gather(
            _ch_search(),
            get_conversation_context(chat_id, query),
        )
        archive_context = truncate_context(results) if results else "No relevant archive data found."
        personal_context = get_relevant_context(query, max_chars=2000)

        full_context = f"[Source: CONTEXT.md]\n{personal_context}\n\n[Source: knowledge base search]\n{archive_context}"
        if conversation_context:
            full_context = f"{conversation_context}\n\n{full_context}"

        response = await ask_claude(query, full_context)

        # Strip capability gap tags before display
        _gap_tag_re = re.compile(r'\[CAPABILITY_GAP:\s*\w+\s*\|\s*/\w+\s*\|\s*.+?\]')
        display_response = _gap_tag_re.sub("", response).strip()

        save_message(chat_id, "assistant", display_response)
        await ctx.reply(display_response)
    except Exception as e:
        log.error("WhatsApp message processing failed: %s", e)


@app.get("/webhook/whatsapp")
async def whatsapp_verify(request: Request):
    """Handle Meta webhook verification challenge."""
    params = dict(request.query_params)
    mode = params.get("hub.mode", "")
    token = params.get("hub.verify_token", "")
    challenge = params.get("hub.challenge", "")

    verify_token = keyring.get_password(KEYRING_SERVICE, "whatsapp-verify-token")
    if mode == "subscribe" and token == verify_token:
        log.info("WhatsApp webhook verified")
        return int(challenge)

    log.warning("WhatsApp webhook verification failed")
    return PlainTextResponse("Forbidden", status_code=403)


# --- Generic webhook endpoints (catch-all for GitHub, etc.) ---

@app.post("/webhook/{source}")
async def webhook_endpoint(source: str, request: Request):
    """Handle inbound webhook events from external services."""
    from webhooks.registry import get as get_webhook_handler

    handler = get_webhook_handler(source)
    if not handler:
        raise HTTPException(status_code=404, detail=f"No handler for source: {source}")

    body = await request.body()
    if not await handler.validate(dict(request.headers), body):
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = json.loads(body)
    message = await handler.handle(payload)

    if message and OWNER_CHAT_ID:
        primary = channel_registry.get_primary()
        if primary:
            await primary.send_message(OWNER_CHAT_ID, f"\U0001f514 Webhook ({source}):\n\n{message}")

    return {"ok": True}


@app.get("/webhook/{source}")
async def webhook_verify(source: str, request: Request):
    """Handle webhook verification challenges (e.g., WhatsApp, Slack)."""
    params = dict(request.query_params)
    if "hub.challenge" in params:
        verify_token = keyring.get_password(KEYRING_SERVICE, f"webhook-verify-{source}")
        if params.get("hub.verify_token") == verify_token:
            return int(params["hub.challenge"])
    return {"error": "Verification failed"}


@app.on_event("shutdown")
async def shutdown():
    """Clean up resources on server shutdown."""
    try:
        from mcp_client import MCPClientManager
        await MCPClientManager.get_instance().shutdown()
    except Exception as e:
        log.warning("MCP client shutdown error: %s", e)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=8033, reload=False, log_level="info")
