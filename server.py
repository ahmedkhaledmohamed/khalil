#!/usr/bin/env python3
"""Khalil — Personal AI Assistant. FastAPI server + Telegram bot."""

import asyncio
import json
import logging
import os
import re
import sys
from datetime import date

# Add khalil directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import anthropic
import httpx
import keyring
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
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
from channels import ActionButton, Channel, SentMessage
from channels.message_context import MessageContext
from channels.telegram import TelegramChannel

from config import (
    ActionType,
    AutonomyLevel,
    CLAUDE_MODEL,
    KEYRING_SERVICE,
    LLM_BACKEND,
    MAX_CONTEXT_TOKENS,
    OLLAMA_LLM_MODEL,
    OLLAMA_URL,
    SENSITIVE_PATTERNS,
    TIMEZONE,
)
from knowledge.indexer import init_db
from knowledge.search import hybrid_search, get_stats
from knowledge.context import get_relevant_context, get_section_names
from autonomy import AutonomyController

import re as _re_module

# #72: Compile redaction patterns once at module load
_REDACT_PATTERNS = [_re_module.compile(p, _re_module.IGNORECASE) for p in SENSITIVE_PATTERNS]


def _redact_sensitive(text: str) -> str:
    """Replace sensitive patterns (PII, credentials) with [REDACTED] in log output."""
    for pat in _REDACT_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


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


# --- Globals ---
app = FastAPI(title="Khalil", docs_url=None, redoc_url=None)
scheduler = AsyncIOScheduler()
db_conn = None
autonomy: AutonomyController = None
claude: anthropic.AsyncAnthropic = None
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
    )


async def _reply_with_keyboard(ctx: MessageContext, text: str, reply_markup, parse_mode=None):
    """Reply with Telegram-specific keyboard markup. Falls back to plain text on other channels."""
    if ctx._raw_update and ctx._raw_update.message:
        await ctx._raw_update.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    else:
        await ctx.reply(text, parse_mode=parse_mode)


CONVERSATION_CONTEXT_WINDOW = 10  # max messages sent to LLM for context
CONVERSATION_MIN_WINDOW = 4      # minimum messages to include


def save_message(chat_id: int, role: str, content: str):
    """Save a message to conversation history. All messages are kept for reflection analysis."""
    db_conn.execute(
        "INSERT INTO conversations (chat_id, role, content) VALUES (?, ?, ?)",
        (chat_id, role, content),
    )
    db_conn.commit()


def _compute_topic_similarity(text_a: str, text_b: str) -> float:
    """#66: Simple word-overlap similarity between two texts. Returns 0.0-1.0."""
    words_a = set(text_a.lower().split())
    words_b = set(text_b.lower().split())
    # Remove common stopwords
    stopwords = {"the", "a", "an", "is", "are", "was", "were", "i", "you", "my", "your",
                 "it", "this", "that", "to", "of", "in", "for", "on", "with", "and", "or"}
    words_a -= stopwords
    words_b -= stopwords
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union) if union else 0.0


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


LLM_TIMEOUT = 60.0  # seconds — Ollama can be slow on first call
CLAUDE_TIMEOUT = 30.0
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


# #18: Graceful degradation chain — Ollama local → Ollama cloud (kimi) → Claude Sonnet → Claude Haiku → cached
_OLLAMA_CLOUD_FALLBACK = "kimi-k2.5:cloud"
_FALLBACK_MODELS = [CLAUDE_MODEL, "claude-haiku-4-5-20251001"]


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
    """Fall back through Claude model chain when Ollama is down.

    Tries: Ollama cloud (kimi) → Claude Sonnet → Claude Haiku → last cached response.
    """
    # Try Ollama cloud model first (free, no API key needed)
    kimi_result = await _fallback_to_ollama_cloud(query, context, system, user_message)
    if kimi_result:
        return kimi_result

    client = claude
    if not client:
        api_key = get_secret("anthropic-api-key")
        if not api_key:
            return _get_cached_response(query)
        try:
            client = anthropic.AsyncAnthropic(api_key=api_key)
        except Exception:
            return _get_cached_response(query)

    for model in _FALLBACK_MODELS:
        try:
            response = await client.messages.create(
                model=model,
                max_tokens=1500,
                system=system,
                messages=[{"role": "user", "content": user_message}],
                timeout=CLAUDE_TIMEOUT,
            )
            text = response.content[0].text
            log.info("Fell back to %s (Ollama unavailable)", model)
            return text
        except Exception as e:
            log.warning("Fallback model %s failed: %s", model, e)
            continue

    # All models failed — try cached response
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


async def ask_llm(query: str, context: str, system_extra: str = "", model: str | None = None) -> str:
    """Send query + context to LLM for reasoning. Supports Ollama (local) and Claude (cloud).

    Args:
        model: Explicit model override. If None, model_router selects based on query complexity.

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

    system = (
        f"{_temporal}"
        "You are Khalil, Ahmed's personal AI assistant. "
        "You have deep knowledge of his life, career, family, finances, and projects. "
        "Answer based on the provided context from his personal archives. "
        "Be direct, specific, and personal — you know him. "
        "If the context doesn't contain the answer, say so honestly.\n\n"
        "CAPABILITIES: You run on Ahmed's Mac and can execute macOS shell commands. "
        "This means you CAN check running processes (pgrep, ps), count app windows "
        "(osascript), check disk space (df), list files (ls), open apps (open -a), "
        "and perform other local system queries. If the user asks about their machine "
        "state, DO NOT suggest they run a command — just tell them you'll check. "
        "The shell execution happens automatically through your action system.\n\n"
        "EXTENSIONS: You also have these capabilities via installed extensions:\n"
        f"{_get_extension_capabilities_text()}"
        "If the user asks for something covered by an extension, tell them to use that command.\n\n"
        f"{_get_mcp_tools_text()}"
        "IMPORTANT: If the user asks you to DO something that you cannot execute "
        "AND no extension covers it "
        "(e.g., read Slack messages, post to Twitter, create a Jira ticket, book a flight), "
        "include this exact tag in your response:\n"
        "[CAPABILITY_GAP: short_name | /command_name | one-line description]\n"
        "Example: [CAPABILITY_GAP: slack_reader | /slack | Read and search Slack messages]\n"
        "Still respond naturally to the user — the tag is for internal processing.\n\n"
        f"{style_hint}"
        f"{system_extra}"
    )

    user_message = f"Context from Ahmed's archives:\n\n{context}\n\n---\n\nQuestion: {query}"

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

    if LLM_BACKEND == "claude" and claude and not _force_local:
        try:
            response = await claude.messages.create(
                model=_selected_model,
                max_tokens=1500,
                system=system,
                messages=[{"role": "user", "content": user_message}],
                timeout=CLAUDE_TIMEOUT,
            )
            return response.content[0].text
        except Exception as e:
            log.error("Claude API call failed: %s", e)
            from learning import record_signal
            record_signal("llm_failure", {"backend": "claude", "error": f"{type(e).__name__}: {e}"[:200]})
            return f"⚠️ LLM unavailable (Claude error: {type(e).__name__}). Try again later."

    # Default: Ollama local LLM
    # #20: Circuit breaker — skip Ollama if circuit is open
    if _cb_ollama.is_open():
        log.warning("Ollama circuit breaker open — skipping to Claude fallback")
        fallback = await _fallback_to_claude(query, context, system, user_message)
        if fallback:
            return fallback
        return "⚠️ LLM unavailable — Ollama circuit breaker open and Claude fallback failed."

    try:
        async with httpx.AsyncClient(timeout=LLM_TIMEOUT) as client:
            response = await client.post(
                f"{OLLAMA_URL}/api/chat",
                json={
                    "model": OLLAMA_LLM_MODEL,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_message},
                    ],
                    "stream": False,
                },
            )
            response.raise_for_status()
            _cb_ollama.record_success()
            return response.json()["message"]["content"]
    except httpx.TimeoutException:
        log.error("Ollama LLM call timed out after %.0fs", LLM_TIMEOUT)
        _cb_ollama.record_failure()
        from learning import record_signal
        record_signal("llm_failure", {"backend": "ollama", "error": "timeout"})
        return "⚠️ LLM timed out. Ollama may be overloaded — try again in a moment."
    except httpx.ConnectError:
        log.error("Cannot connect to Ollama at %s", OLLAMA_URL)
        _cb_ollama.record_failure()
        from learning import record_signal
        record_signal("llm_failure", {"backend": "ollama", "error": "connection_refused"})
        # Attempt Ollama recovery
        if await _try_recover_ollama():
            # Retry the request after recovery
            try:
                async with httpx.AsyncClient(timeout=LLM_TIMEOUT) as client:
                    response = await client.post(
                        f"{OLLAMA_URL}/api/chat",
                        json={
                            "model": OLLAMA_LLM_MODEL,
                            "messages": [
                                {"role": "system", "content": system},
                                {"role": "user", "content": user_message},
                            ],
                            "stream": False,
                        },
                    )
                    response.raise_for_status()
                    return response.json()["message"]["content"]
            except Exception:
                pass  # Fall through to Claude fallback
        # Fall back to Claude API
        fallback = await _fallback_to_claude(query, context, system, user_message)
        if fallback:
            return fallback
        return "⚠️ LLM unavailable — Ollama is not running and Claude fallback failed. Start Ollama with: ollama serve"
    except (httpx.HTTPError, KeyError) as e:
        log.error("Ollama LLM call failed: %s", e)
        from learning import record_signal
        record_signal("llm_failure", {"backend": "ollama", "error": f"{type(e).__name__}: {e}"[:200]})
        return f"⚠️ LLM error: {type(e).__name__}. Check Ollama logs."


# Alias for backward compatibility with scheduler/digests references
ask_claude = ask_llm


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
    (r"\bcreate\s+(?:a\s+)?(?:github\s+)?issue\b", "gh_issue"),
    (r"\bopen\s+(?:a\s+)?(?:github\s+)?issue\b", "gh_issue"),
    (r"\bfile\s+(?:an?\s+)?(?:github\s+)?issue\b", "gh_issue"),
    (r"\bnew\s+(?:github\s+)?issue\b", "gh_issue"),
    # #53: GitHub PR status monitoring
    (r"\bcheck\s+(?:my\s+)?(?:pull\s+requests?|prs?)\b", "gh_pr_status"),
    (r"\b(?:pr|pull\s+request)\s+status\b", "gh_pr_status"),
    (r"\blist\s+(?:my\s+)?(?:open\s+)?(?:pull\s+requests?|prs?)\b", "gh_pr_status"),
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
    # #51: Spotify playback control
    (r"\b(?:play|resume)\s+music\b", "shell"),
    (r"\b(?:pause|stop)\s+music\b", "shell"),
    (r"\b(?:next|skip)\s+(?:song|track)\b", "shell"),
    (r"\b(?:what'?s\s+playing|now\s+playing|current\s+(?:song|track))\b", "shell"),
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
    """Quick regex check if text looks like an action request. Returns hint or None."""
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
}


def find_matching_action(query: str) -> str | None:
    """Check if a query matches an existing granular action by keyword overlap.

    Returns action type string or None. Used to detect intent pattern misses
    when a query falls through to the LLM but could have been handled directly.
    """
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

    # #40: Spotlight file search — "find file X", "locate my .py files"
    m = re.search(r"\b(?:find|search\s+for|locate)\s+(?:a\s+)?(?:file\s+(?:named?\s+)?|files?\s+)?['\"]?([^'\"]+?)['\"]?\s*$", text_lower)
    if m:
        search_term = m.group(1).strip()
        if search_term:
            return {"action": "shell", "command": f"mdfind 'kMDItemFSName == \"{search_term}\"'", "description": f"Search for file: {search_term}"}

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

    # Spotlight file search
    if re.search(r"\b(?:find|locate|search\s+for)\s+(?:a\s+|that\s+|the\s+)?(?:file|document|pdf|image|photo|spreadsheet|presentation)\b", text_lower):
        query_match = re.search(r"\b(?:find|locate|search\s+for)\s+(?:a\s+|that\s+|the\s+)?(?:file|document|pdf|image|photo|spreadsheet|presentation)\s+(?:called|named|about)?\s*(.+)$", text_lower)
        search_q = query_match.group(1).strip() if query_match else text_stripped
        return {"action": "macos_spotlight", "query": search_q, "description": f"Search files: {search_q}"}

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

    response = await ask_llm(prompt, "", system_extra="Respond with JSON or NONE only. No explanation.")

    response = response.strip()
    if response.upper() == "NONE" or response.startswith("⚠️"):
        return None

    # Try to parse JSON from response
    try:
        # Handle LLM wrapping in markdown code blocks
        if "```" in response:
            response = response.split("```")[1]
            if response.startswith("json"):
                response = response[4:]
        return json.loads(response.strip())
    except (json.JSONDecodeError, IndexError):
        log.debug("Intent detection returned non-JSON: %s", response[:100])
        return None


async def _execute_with_retry(cmd: str, description: str, update, max_retries: int = 1):
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
        return await _execute_with_retry(corrected, description, update, max_retries=0)

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
    """Handle a detected action intent. Returns True if handled."""
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

    if action == "reminder":
        from actions.reminders import _parse_relative_time, create_reminder

        time_str = intent.get("time", "")
        text = intent.get("text", "")
        if not text:
            return False

        due_at = _parse_relative_time(time_str) if time_str else None
        if not due_at:
            await ctx.reply(
                f"I understood you want a reminder for: {text}\n"
                f"But I couldn't parse the time \"{time_str}\".\n"
                "Try: /remind in 2 hours {text}"
            )
            return True

        result = create_reminder(text, due_at)
        await ctx.reply(
            f"⏰ Reminder set!\n\n"
            f"#{result['id']}: {result['text']}\n"
            f"Due: {result['due_at']}"
        )
        return True

    elif action == "email":
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
            f"Write a concise, professional email body for Ahmed to send.\n"
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

    elif action == "calendar":
        try:
            from actions.calendar import get_today_events, format_events_text
            events = await get_today_events()
            await ctx.reply(format_events_text(events))
        except Exception as e:
            from learning import record_signal
            record_signal("action_execution_failure", {
                "action": "calendar", "error": str(e)[:200],
            })
            await ctx.reply(f"❌ Calendar fetch failed: {e}")
        return True

    elif action == "cursor_status":
        from actions.terminal import get_cursor_status, format_cursor_status
        status = await get_cursor_status()
        autonomy.log_audit("cursor_status", "Checked Cursor IDE status", result="ok")
        await ctx.reply(format_cursor_status(status))
        return True

    elif action == "cursor_extensions":
        from actions.terminal import get_cursor_extensions
        extensions = await get_cursor_extensions()
        autonomy.log_audit("cursor_extensions", "Listed Cursor extensions", result="ok")
        if extensions:
            text = f"🧩 Cursor Extensions ({len(extensions)}):\n" + "\n".join(f"  • {e}" for e in extensions)
        else:
            text = "🧩 No Cursor extensions found (or Cursor not running)"
        await ctx.reply(text)
        return True

    elif action == "terminal_status":
        from actions.terminal import get_terminal_status, format_terminal_status
        status = await get_terminal_status()
        autonomy.log_audit("terminal_status", "Checked terminal sessions", result="ok")
        await ctx.reply(format_terminal_status(status))
        return True

    elif action == "terminal_exec":
        from actions.terminal import send_to_iterm
        cmd = intent.get("command", "")
        session = intent.get("session", "current")
        if not cmd:
            return False
        # Always requires approval — injecting into live terminal
        action_id = autonomy.create_pending_action(
            "terminal_exec",
            f"Run in terminal: {cmd}",
            {"command": cmd, "session": session},
        )
        await _reply_with_keyboard(ctx,
            f"📟 Send to iTerm ({session}):\n\n`{cmd}`\n\n{autonomy.format_level()}",
            approve_deny_keyboard(), parse_mode="Markdown")
        return True

    elif action == "terminal_new_tab":
        from actions.terminal import create_iterm_tab
        cmd = intent.get("command")
        if autonomy.needs_approval("terminal_new_tab"):
            desc = f"New terminal tab" + (f" running: {cmd}" if cmd else "")
            action_id = autonomy.create_pending_action(
                "terminal_new_tab", desc, {"command": cmd},
            )
            await _reply_with_keyboard(ctx, 
                f"📟 {desc}\n\n{autonomy.format_level()}", approve_deny_keyboard(), parse_mode="Markdown")
        else:
            result = await create_iterm_tab(cmd)
            if result["success"]:
                autonomy.log_audit("terminal_new_tab", "Created new terminal tab", result="ok")
                await ctx.reply("📟 New terminal tab opened" + (f"\nRunning: `{cmd}`" if cmd else ""))
            else:
                await ctx.reply(f"⚠️ Failed to create tab: {result['error']}")
        return True

    elif action == "cursor_open":
        from actions.terminal import cursor_open
        path = intent.get("path", "")
        line = intent.get("line")
        if not path:
            return False
        # cursor -g is READ (navigates), cursor <folder> could open new window
        import os
        is_dir = os.path.isdir(os.path.expanduser(path))
        action_name = "cursor_open_project" if is_dir else "cursor_open"
        if autonomy.needs_approval(action_name):
            desc = f"Open in Cursor: {path}" + (f":{line}" if line else "")
            action_id = autonomy.create_pending_action(
                action_name, desc, {"path": path, "line": line},
            )
            await _reply_with_keyboard(ctx,
            f"🖥 {desc}\n\n{autonomy.format_level()}",
            approve_deny_keyboard(), parse_mode="Markdown")
        else:
            result = await cursor_open(path, line)
            if result["success"]:
                autonomy.log_audit(action_name, f"Opened {path}" + (f":{line}" if line else ""), result="ok")
                await ctx.reply(f"🖥 Opened in Cursor: {path}" + (f":{line}" if line else ""))
            else:
                await ctx.reply(f"⚠️ Failed: {result['error']}")
        return True

    elif action == "cursor_diff":
        from actions.terminal import cursor_diff
        f1 = intent.get("file1", "")
        f2 = intent.get("file2", "")
        if not f1 or not f2:
            return False
        result = await cursor_diff(f1, f2)
        if result["success"]:
            autonomy.log_audit("cursor_diff", f"Diff: {f1} vs {f2}", result="ok")
            await ctx.reply(f"🖥 Diff opened in Cursor: {f1} vs {f2}")
        else:
            await ctx.reply(f"⚠️ Failed: {result['error']}")
        return True

    elif action == "cursor_terminal_status":
        from actions.terminal import get_cursor_terminal_status, format_cursor_terminal_status
        status = await get_cursor_terminal_status()
        autonomy.log_audit("cursor_terminal_status", "Checked Cursor terminals", result="ok")
        await ctx.reply(format_cursor_terminal_status(status))
        return True

    elif action == "cursor_terminal_exec":
        cmd = intent.get("command", "")
        if not cmd:
            return False
        target = intent.get("target", "0")  # default to first terminal
        # Always requires approval — injecting into Cursor terminal
        action_id = autonomy.create_pending_action(
            "cursor_terminal_exec",
            f"Run in Cursor terminal: {cmd}",
            {"command": cmd, "target": target},
        )
        await _reply_with_keyboard(ctx,
            f"🖥 Send to Cursor terminal:\n\n`{cmd}`\n\n{autonomy.format_level()}",
            approve_deny_keyboard(), parse_mode="Markdown")
        return True

    elif action == "cursor_terminal_new":
        from actions.terminal import bridge_create_terminal
        cmd = intent.get("command")
        if autonomy.needs_approval("cursor_terminal_new"):
            desc = f"New Cursor terminal" + (f" running: {cmd}" if cmd else "")
            action_id = autonomy.create_pending_action(
                "cursor_terminal_new", desc, {"command": cmd},
            )
            await _reply_with_keyboard(ctx, 
                f"🖥 {desc}\n\n{autonomy.format_level()}", approve_deny_keyboard(), parse_mode="Markdown")
        else:
            result = await bridge_create_terminal(command=cmd)
            if result.get("error"):
                await ctx.reply(f"⚠️ {result['error']}")
            else:
                autonomy.log_audit("cursor_terminal_new", "Created Cursor terminal", result="ok")
                msg = "🖥 New Cursor terminal created"
                if cmd:
                    msg += f"\nRunning: `{cmd}`"
                await ctx.reply(msg, parse_mode="Markdown")
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
                result, final_cmd = await _execute_with_retry(cmd, description, update)
                autonomy.log_audit(action_name, f"Executed: {final_cmd}", {"command": final_cmd}, f"exit={result['returncode']}")
                if result["returncode"] != 0:
                    from learning import record_signal
                    record_signal("action_execution_failure", {
                        "action": "shell", "command": final_cmd,
                        "exit_code": result["returncode"],
                        "stderr": result["stderr"][:200],
                    })
                    await _try_inline_healing(ctx)
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

    # --- Browser automation ---

    elif action == "browser_navigate":
        url = intent.get("url", "")
        from actions.browser import navigate_and_screenshot, is_financial_url
        if is_financial_url(url):
            await ctx.reply("Blocked: Cannot automate financial sites.")
            return True
        await ctx.typing()
        screenshot_path, title = await navigate_and_screenshot(url)
        if screenshot_path:
            await ctx.reply_photo(screenshot_path, caption=f"Page: {title}\nURL: {url}")
        else:
            await ctx.reply(f"Navigation result: {title}")
        return True

    elif action == "browser_extract":
        url = intent.get("url", "")
        selector = intent.get("selector")
        from actions.browser import extract_page_text, is_financial_url
        if is_financial_url(url):
            await ctx.reply("Blocked: Cannot automate financial sites.")
            return True
        await ctx.typing()
        text = await extract_page_text(url, selector)
        await ctx.reply(text[:4000])
        return True

    elif action == "browser_screenshot":
        url = intent.get("url", "")
        from actions.browser import navigate_and_screenshot, is_financial_url
        if is_financial_url(url):
            await ctx.reply("Blocked: Cannot automate financial sites.")
            return True
        await ctx.typing()
        path, title = await navigate_and_screenshot(url)
        if path:
            await ctx.reply_photo(path, caption=title)
        else:
            await ctx.reply(title)
        return True

    # --- macOS awareness ---

    elif action == "macos_apps":
        from actions.macos import get_running_apps
        apps = await get_running_apps()
        if apps:
            text = f"🖥 Running Apps ({len(apps)}):\n" + "\n".join(f"  • {a}" for a in sorted(apps))
        else:
            text = "⚠️ Couldn't retrieve running apps."
        await ctx.reply(text)
        return True

    elif action == "macos_frontmost":
        from actions.macos import get_frontmost_app, get_active_window_title
        app = await get_frontmost_app()
        title = await get_active_window_title()
        if app:
            text = f"🖥 Frontmost: {app}"
            if title:
                text += f"\n📄 Window: {title}"
        else:
            text = "⚠️ Couldn't determine frontmost app."
        await ctx.reply(text)
        return True

    elif action == "macos_system_info":
        from actions.macos import get_system_info
        info = await get_system_info()
        lines = ["💻 System Info:"]
        if "battery_percent" in info:
            charge = "⚡ charging" if info.get("battery_charging") else "🔋"
            lines.append(f"  Battery: {info['battery_percent']}% {charge}")
        if "storage_available" in info:
            lines.append(f"  Storage: {info['storage_used']} used / {info['storage_total']} total ({info['storage_available']} free)")
        if "memory_total_gb" in info:
            lines.append(f"  Memory: {info['memory_total_gb']} GB")
        if "cpu_brand" in info:
            lines.append(f"  CPU: {info['cpu_brand']}")
        await ctx.reply("\n".join(lines) if len(lines) > 1 else "⚠️ Couldn't retrieve system info.")
        return True

    elif action == "macos_spotlight":
        from actions.macos import spotlight_search
        query = intent.get("query", "")
        if not query:
            return False
        results = await spotlight_search(query)
        if results:
            lines = [f"🔍 Spotlight: {len(results)} results for \"{query}\":\n"]
            for r in results:
                size_str = ""
                if r.get("size"):
                    kb = r["size"] / 1024
                    size_str = f" ({kb:.0f} KB)" if kb < 1024 else f" ({kb/1024:.1f} MB)"
                lines.append(f"  📄 {r['name']}{size_str}\n     {r['path']}")
            text = "\n".join(lines)
        else:
            text = f"🔍 No files found for \"{query}\"."
        await ctx.reply(text[:4000])
        return True

    elif action == "macos_browser_tabs":
        from actions.macos import get_browser_tabs
        browser = intent.get("browser", "Safari")
        tabs = await get_browser_tabs(browser)
        if tabs:
            lines = [f"🌐 {browser} Tabs ({len(tabs)}):"]
            for t in tabs[:20]:
                lines.append(f"  • {t['title'][:60]}\n    {t['url']}")
            text = "\n".join(lines)
        else:
            text = f"🌐 No tabs found in {browser} (or {browser} not running)."
        await ctx.reply(text[:4000])
        return True

    # --- Web search ---

    elif action == "web_search":
        from actions.web import web_search, format_search_results
        query = intent.get("query", "")
        if not query:
            return False
        await ctx.reply(f"🔍 Searching: {query}...")
        results = await web_search(query)
        await ctx.reply(
            format_search_results(results),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return True

    # --- iMessage ---

    elif action == "imessage_read":
        from actions.imessage import get_recent_messages, format_messages
        contact = intent.get("contact")
        messages = await get_recent_messages(contact=contact, limit=15)
        header = f"💬 Messages from {contact}:" if contact else "💬 Recent messages:"
        await ctx.reply(f"{header}\n\n{format_messages(messages)}")
        return True

    elif action == "imessage_recent":
        from actions.imessage import get_recent_contacts
        contacts = await get_recent_contacts(limit=15)
        if contacts:
            text = "💬 Recent contacts:\n" + "\n".join(f"  • {c}" for c in contacts)
        else:
            text = "💬 No recent contacts found (check Full Disk Access permission)."
        await ctx.reply(text)
        return True

    elif action == "imessage_search":
        from actions.imessage import search_messages, format_messages
        query = intent.get("query", "")
        if not query:
            return False
        messages = await search_messages(query, limit=10)
        await ctx.reply(f"💬 Messages matching \"{query}\":\n\n{format_messages(messages)}")
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
        "/sync — Sync new emails\n"
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
        "/clear — Clear conversation history\n"
        "/help — Show this message"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ctx = _ctx_from_update(update)
    await cmd_start(update, context)


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
            shell_result, final_cmd = await _execute_with_retry(cmd, result["description"], update)
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
                shell_result, final_cmd = await _execute_with_retry(cmd, result["description"], update)
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

                async def _execute_single_step(step):
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

        # Strip optional "to" keyword: "/email draft to ahmed@gmail.com ..." → skip "to"
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
                f"Write a concise, professional email body for Ahmed to send.\n"
                f"To: {to_addr}\nSubject: {subject}\n\n"
                "Write only the email body, no greeting or signature — Ahmed will add those. "
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
    from actions.projects import resolve_project, get_project_status, list_projects, get_open_tasks

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
            f"Unknown project: {name}\n\nKnown: zia, tiny-grounds, bezier, khalil"
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
            f"Answer Ahmed's finance question based on his financial records:\n\n{query}",
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
            f"Answer Ahmed's work question based on sprint planning data:\n\n{query}",
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
            "Review Ahmed's current goals. Are they on track? What's missing? "
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


async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show system health status."""
    ctx = _ctx_from_update(update)
    from monitoring import run_health_check

    report = await run_health_check()
    ollama = report["ollama"]
    db = report["database"]

    lines = [f"🏥 System Health: {report['status'].upper()}\n"]

    # Ollama
    if ollama["status"] == "ok":
        lines.append(f"✅ Ollama: OK ({len(ollama.get('models', []))} models)")
    else:
        lines.append(f"❌ Ollama: {ollama.get('error', 'down')}")

    # Database
    if db["status"] == "ok":
        lines.append(f"✅ Database: {db['documents']} docs")
        lines.append(f"   Reminders: {db['active_reminders']} active")
        lines.append(f"   Pending actions: {db['pending_actions']}")
        lines.append(f"   Last email sync: {db['last_email_sync']}")
    else:
        lines.append(f"❌ Database: {db.get('error', 'unavailable')}")

    # Issues
    if report["issues"]:
        lines.append(f"\n⚠️ Issues:")
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


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle free-text messages — the main conversational flow."""
    ctx = _ctx_from_update(update)
    import time as _time
    _msg_start = _time.monotonic()

    global OWNER_CHAT_ID
    if OWNER_CHAT_ID is None:
        OWNER_CHAT_ID = ctx.chat_id
        _persist_owner_chat_id(OWNER_CHAT_ID)

    query = update.message.text if update.message else None
    if not query:
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

    # Try natural language action detection
    # 0. Check if query matches an extension command — route directly
    action_hint = _looks_like_action(query)
    if action_hint and action_hint not in ("reminder", "email", "calendar", "shell"):
        # Extension command — route directly to the handler
        handled = await _try_extension_handler(action_hint, query, update, context)
        if handled:
            return

    # 1. Direct mapping for unambiguous patterns (no LLM needed)
    direct_intent = _try_direct_shell_intent(query)
    if direct_intent:
        direct_intent["llm_generated"] = False  # safe — pattern-matched, not LLM
        direct_intent["user_query"] = query
        handled = await handle_action_intent(direct_intent, ctx)
        if handled:
            return
    # 2. LLM-based detection for ambiguous patterns
    if action_hint is None:
        action_hint = _looks_like_action(query)
    if action_hint:
        intent = await detect_intent(query)
        if intent:
            # #3: Track intent detection accuracy — pattern hint vs LLM result
            try:
                llm_action = intent.get("action", "unknown")
                record_signal("intent_accuracy", {
                    "pattern_hint": action_hint,
                    "llm_action": llm_action,
                    "match": action_hint == llm_action,
                    "query": query[:100],
                })
            except Exception:
                pass
            intent["user_query"] = query
            handled = await handle_action_intent(intent, ctx)
            if handled:
                return
        else:
            # Pattern matched but LLM failed to extract intent — record for self-healing
            from learning import record_signal
            record_signal("intent_detection_failure", {
                "query": query[:200],
                "action_hint": action_hint,
            })
            # Try immediate self-healing if this is a recurring failure
            await _try_inline_healing(ctx)

    # Multi-step task orchestration: if the query looks compound, try decomposing
    from orchestrator import looks_like_multi_step, decompose_request, execute_plan as execute_task_plan, format_plan_summary, ensure_table as ensure_plans_table
    if looks_like_multi_step(query):
        try:
            ensure_plans_table()
            steps = await decompose_request(query, "", ask_llm)
            if len(steps) >= 2:
                # Show plan to user
                plan_lines = [f"I'll handle this in {len(steps)} steps:"]
                for i, step in enumerate(steps, 1):
                    plan_lines.append(f"{i}. {step.description}")
                plan_text = "\n".join(plan_lines)

                async def _execute_single_step(step):
                    """Execute a single TaskStep by routing through detect_intent + handle_action_intent."""
                    from orchestrator import TaskStep
                    # Map orchestrator action types to intent dicts
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
                        # Fallback: use detect_intent on the step description
                        intent = await detect_intent(step.description)
                    if intent:
                        intent["user_query"] = step.description
                        # Execute via handle_action_intent — but we need to capture the result
                        # Since handle_action_intent sends messages directly, we treat success as no exception
                        handled = await handle_action_intent(intent, ctx)
                        if handled:
                            return f"Completed: {step.description}"
                    return f"Executed: {step.description}"

                if autonomy.needs_approval("shell_write"):
                    # GUIDED/SUPERVISED: ask for confirmation
                    action_id = autonomy.create_pending_action(
                        "task_plan",
                        f"Execute {len(steps)}-step plan: {query[:100]}",
                        {"query": query, "steps": [s.to_dict() for s in steps]},
                    )
                    await _reply_with_keyboard(ctx,
            f"📋 {plan_text}\n\n{autonomy.format_level()}",
            approve_deny_keyboard())
                    return
                else:
                    # AUTONOMOUS: execute immediately
                    await ctx.reply(f"📋 {plan_text}\n\nExecuting...")
                    plan_result = await execute_task_plan(
                        steps, query, channel, chat_id, _execute_single_step,
                    )
                    await ctx.reply(format_plan_summary(plan_result))
                    return
        except Exception as e:
            log.warning("Multi-step orchestration failed, falling through: %s", e)

    # Show progress indicator — uses channel abstraction
    progress_msg = await channel.send_message(chat_id, "🔍 Thinking...")

    # Search knowledge base
    results = await hybrid_search(query, limit=6)
    archive_context = truncate_context(results) if results else "No relevant archive data found."

    # Get relevant CONTEXT.md sections
    personal_context = get_relevant_context(query, max_chars=2000)

    # Get conversation history for multi-turn context
    conversation = get_conversation_history(chat_id)

    # #67: Combine context with source citations for cross-source fusion
    full_context = f"[Source: CONTEXT.md]\n{personal_context}\n\n[Source: knowledge base search]\n{archive_context}"
    if conversation:
        full_context = f"[Source: conversation history]\n{conversation}\n\n{full_context}"

    # Live state injection — real-time awareness of calendar, email, Cursor, reminders
    live_context = ""
    try:
        from state.collector import collect_live_state, format_for_prompt
        live = await collect_live_state()
        live_context = format_for_prompt(live)
    except Exception as e:
        log.warning("Live state collection failed: %s", e)

    # Prepend live state before archive context so LLM sees current state first
    if live_context:
        full_context = f"[Source: live state]\n{live_context}\n\n{full_context}"

    # Ask LLM
    response = await ask_claude(query, full_context)

    # MCP tool call interception — if LLM wants to use an MCP tool, execute it
    _mcp_call_re = re.compile(r'\[MCP_CALL:\s*([\w.-]+)\.([\w.-]+)\s*\|\s*(\{.*?\})\]')
    _mcp_match = _mcp_call_re.search(response)
    if _mcp_match:
        mcp_server = _mcp_match.group(1)
        mcp_tool = _mcp_match.group(2)
        try:
            mcp_args = json.loads(_mcp_match.group(3))
        except json.JSONDecodeError:
            mcp_args = {}
        try:
            from mcp_client import MCPClientManager
            mcp_result = await MCPClientManager.get_instance().call_tool(
                mcp_server, mcp_tool, mcp_args,
            )
            # Re-query LLM with the tool result appended
            tool_context = (
                f"{full_context}\n\n"
                f"[MCP tool result from {mcp_server}.{mcp_tool}]\n{mcp_result}"
            )
            response = await ask_claude(query, tool_context)
        except Exception as e:
            log.warning("MCP tool call %s.%s failed: %s", mcp_server, mcp_tool, e)
            response = _mcp_call_re.sub("", response).strip()

    # Always strip CAPABILITY_GAP tags before user-facing display
    _gap_tag_re = re.compile(r'\[CAPABILITY_GAP:\s*\w+\s*\|\s*/\w+\s*\|\s*.+?\]')
    _gap_match = _gap_tag_re.search(response)
    display_response = _gap_tag_re.sub("", response).strip() if _gap_match else response

    # Save assistant response to conversation history (with tag stripped)
    save_message(chat_id, "assistant", display_response)

    # Track search misses for self-improvement
    from learning import detect_search_miss, record_signal
    if detect_search_miss(response):
        record_signal("search_miss", {"query": query[:200]})

    # Track digest engagement — if user message arrived shortly after a digest
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
        pass  # Non-critical

    # Detect embedded shell commands — LLM suggests running them instead of executing
    suggested_cmd = _extract_shell_from_response(response)
    if suggested_cmd:
        # Record signal + trigger healing immediately — this is deterministic, don't wait for nightly
        record_signal("response_suggests_manual_action", {
            "query": query[:200], "suggested_cmd": suggested_cmd,
        })
        await _try_inline_healing(ctx)
        from actions.shell import classify_command, format_output
        classification = classify_command(suggested_cmd)
        if classification != ActionType.DANGEROUS:
            intent = {
                "action": "shell",
                "command": suggested_cmd,
                "description": f"Extracted from LLM response",
                "user_query": query,
                "llm_generated": True,
            }
            await progress_msg.delete()
            handled = await handle_action_intent(intent, ctx)
            if handled:
                return

    # Detect capability gaps — offer to self-extend
    # 1. Check for structured [CAPABILITY_GAP: ...] tag first
    gap_match = re.search(
        r'\[CAPABILITY_GAP:\s*(\w+)\s*\|\s*(/\w+)\s*\|\s*(.+?)\]',
        response,
    )
    if _gap_match:
        record_signal("capability_gap_detected", {"query": query[:200], "structured": True})
        gap_groups = re.search(
            r'\[CAPABILITY_GAP:\s*(\w+)\s*\|\s*(/\w+)\s*\|\s*(.+?)\]', response
        )
        if gap_groups:
            spec = {
                "name": gap_groups.group(1),
                "command": gap_groups.group(2).lstrip("/"),
                "description": gap_groups.group(3).strip(),
                "original_query": query,
            }
            try:
                await progress_msg.edit(display_response)
            except Exception:
                await progress_msg.delete()
                await channel.send_message(chat_id, display_response)
            await _handle_self_extend_with_spec(spec, ctx)
            return
    # 2. Fallback: phrase-based detection
    try:
        from actions.extend import detect_capability_gap, handle_self_extend
        if detect_capability_gap(display_response):
            # Cross-check: is this a pattern miss for an existing action?
            matched_action = find_matching_action(query)
            if matched_action:
                record_signal("intent_pattern_miss", {
                    "query": query[:200],
                    "matched_action": matched_action,
                    "llm_response_snippet": display_response[:200],
                })
                log.info("Intent pattern miss: query=%r matched=%s", query[:80], matched_action)
                await _try_inline_healing(ctx)
                # Skip self-extension — this is a pattern miss, not a capability gap
            else:
                record_signal("capability_gap_detected", {"query": query[:200]})
                await _try_inline_healing(ctx)
                await handle_self_extend(query, ctx._raw_update, ask_claude)
    except Exception as e:
        log.debug("Capability gap detection failed: %s", e)

    # Replace progress message with response (tag already stripped)
    try:
        await progress_msg.edit(display_response)
    except Exception:
        # If edit fails (e.g., message too long), send as new message
        await progress_msg.delete()
        await channel.send_message(chat_id, display_response)

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
    """Channel-agnostic message handler for non-Telegram channels."""
    import time as _time
    _msg_start = _time.monotonic()

    global OWNER_CHAT_ID
    query = ctx.incoming.text if ctx.incoming else ""
    if not query:
        return

    chat_id = ctx.chat_id

    if contains_sensitive_data(query):
        await ctx.reply(
            "Your message appears to contain sensitive data. "
            "I'll proceed but won't include raw sensitive values in API calls."
        )

    save_message(chat_id, "user", query)

    direct_intent = _try_direct_shell_intent(query)
    if direct_intent:
        direct_intent["llm_generated"] = False
        direct_intent["user_query"] = query
        handled = await handle_action_intent(direct_intent, ctx)
        if handled:
            return

    action_hint = _looks_like_action(query)
    if action_hint and action_hint not in ("reminder", "email", "calendar", "shell"):
        pass  # Extension commands rely on Telegram Update -- skip on other channels
    elif action_hint:
        intent = await detect_intent(query)
        if intent:
            intent["user_query"] = query
            handled = await handle_action_intent(intent, ctx)
            if handled:
                return

    progress_msg = await ctx.reply("Thinking...")

    results = await hybrid_search(query, limit=6)
    archive_context = truncate_context(results) if results else "No relevant archive data found."
    personal_context = get_relevant_context(query, max_chars=2000)
    conversation = get_conversation_history(chat_id)

    full_context = f"[Source: CONTEXT.md]\n{personal_context}\n\n[Source: knowledge base search]\n{archive_context}"
    if conversation:
        full_context = f"[Source: conversation history]\n{conversation}\n\n{full_context}"

    live_context = ""
    try:
        from state.collector import collect_live_state, format_for_prompt
        live = await collect_live_state()
        live_context = format_for_prompt(live)
    except Exception as e:
        log.warning("Live state collection failed: %s", e)

    if live_context:
        full_context = f"[Source: live state]\n{live_context}\n\n{full_context}"

    response = await ask_claude(query, full_context)

    _gap_tag_re = re.compile(r'\[CAPABILITY_GAP:\s*\w+\s*\|\s*/\w+\s*\|\s*.+?\]')
    display_response = _gap_tag_re.sub("", response).strip()

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
    application.add_handler(CommandHandler("run", cmd_run))
    application.add_handler(CommandHandler("learn", cmd_learn))
    application.add_handler(CommandHandler("feedback", cmd_feedback))
    application.add_handler(CommandHandler("extensions", cmd_extensions))
    application.add_handler(CommandHandler("mcp", cmd_mcp))
    application.add_handler(CommandHandler("commitments", cmd_commitments))
    application.add_handler(CommandHandler("tasks", cmd_tasks))

    # Dynamically register extension handlers
    _load_extensions(application)

    application.add_handler(CallbackQueryHandler(handle_callback))
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
        BotCommand("sync", "Sync new emails"),
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
            from learning import decay_preferences
            archived = decay_preferences()
            if archived and _can_send():
                names = ", ".join(a["key"] for a in archived[:5])
                await channel.send_message(
                    OWNER_CHAT_ID,
                    f"🗑 Archived {len(archived)} stale preference(s): {names}"
                )
        except Exception as e:
            log.warning("Preference decay failed: %s", e)

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
            is_standup_meeting,
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

    log.info("Scheduler jobs registered")


@app.on_event("startup")
async def startup():
    global db_conn, autonomy, claude

    log.info("Khalil starting up...")

    # Initialize database
    db_conn = init_db()
    # M11: Create meeting intelligence tables
    from actions.meetings import ensure_tables as _ensure_meeting_tables
    _ensure_meeting_tables(db_conn)
    autonomy = AutonomyController(db_conn)
    # Share DB connection with learning module
    from learning import set_conn as set_learning_conn
    set_learning_conn(db_conn)
    log.info(f"Autonomy level: {autonomy.format_level()}")

    # Load persisted owner chat ID so notifications work after restart
    row = db_conn.execute("SELECT value FROM settings WHERE key = 'owner_chat_id'").fetchone()
    if row:
        OWNER_CHAT_ID = int(row[0])
        log.info("Loaded owner chat ID: %d", OWNER_CHAT_ID)

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
    except Exception as e:
        log.warning("MCP client initialization failed: %s", e)

    # Start Telegram bot
    asyncio.create_task(start_telegram_bot())

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
            log.info("Discord token not configured \u2014 skipping Discord channel")
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
        # Send report to owner via Telegram if there are issues
        if test_results["overall"] != "ok" and OWNER_CHAT_ID and channel:
            try:
                await channel.send_message(OWNER_CHAT_ID, report)
            except Exception as e:
                log.warning("Could not send startup report to Telegram: %s", e)
    except Exception as e:
        log.warning("Startup self-test failed: %s", e)

    log.info("Khalil is ready.")


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
