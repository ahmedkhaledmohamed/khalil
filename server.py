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

class _JsonFormatter(logging.Formatter):
    """Simple JSON log formatter."""
    def format(self, record):
        import json as _json
        entry = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0]:
            entry["exception"] = self.formatException(record.exc_info)
        return _json.dumps(entry)


_handler = logging.StreamHandler()
_handler.setFormatter(_JsonFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_handler])
log = logging.getLogger("khalil")

# --- Globals ---
app = FastAPI(title="Khalil", docs_url=None, redoc_url=None)
scheduler = AsyncIOScheduler()
db_conn = None
autonomy: AutonomyController = None
claude: anthropic.AsyncAnthropic = None
telegram_app: Application | None = None
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


CONVERSATION_CONTEXT_WINDOW = 10  # messages sent to LLM for context


def save_message(chat_id: int, role: str, content: str):
    """Save a message to conversation history. All messages are kept for reflection analysis."""
    db_conn.execute(
        "INSERT INTO conversations (chat_id, role, content) VALUES (?, ?, ?)",
        (chat_id, role, content),
    )
    db_conn.commit()


def get_conversation_history(chat_id: int) -> str:
    """Get recent conversation history formatted for LLM context (windowed)."""
    rows = db_conn.execute(
        "SELECT role, content FROM conversations WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
        (chat_id, CONVERSATION_CONTEXT_WINDOW),
    ).fetchall()
    if not rows:
        return ""
    # Reverse to chronological order
    rows = list(reversed(rows))
    lines = [f"{r[0].title()}: {r[1]}" for r in rows]
    return "Recent conversation:\n" + "\n".join(lines)


def clear_conversation(chat_id: int):
    """Clear conversation history for a chat."""
    db_conn.execute("DELETE FROM conversations WHERE chat_id = ?", (chat_id,))
    db_conn.commit()


def truncate_context(results: list[dict], max_chars: int = MAX_CONTEXT_TOKENS * 4) -> str:
    """Format search results into context string, respecting token limits."""
    lines = []
    total = 0
    for r in results:
        entry = f"[{r.get('category', '')}] {r['title']}\n{r['content']}\n"
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


async def _fallback_to_claude(query: str, context: str, system: str, user_message: str) -> str | None:
    """Fall back to Claude API when Ollama is down. Returns None if Claude unavailable."""
    if not claude:
        # Try to initialize Claude on-the-fly
        api_key = get_secret("anthropic-api-key")
        if not api_key:
            return None
        try:
            temp_claude = anthropic.AsyncAnthropic(api_key=api_key)
            response = await temp_claude.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=1500,
                system=system,
                messages=[{"role": "user", "content": user_message}],
                timeout=CLAUDE_TIMEOUT,
            )
            log.info("Fell back to Claude API (Ollama unavailable)")
            return response.content[0].text
        except Exception as e:
            log.error("Claude fallback also failed: %s", e)
            return None
    else:
        try:
            response = await claude.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=1500,
                system=system,
                messages=[{"role": "user", "content": user_message}],
                timeout=CLAUDE_TIMEOUT,
            )
            log.info("Fell back to Claude API (Ollama unavailable)")
            return response.content[0].text
        except Exception:
            return None


async def ask_llm(query: str, context: str, system_extra: str = "") -> str:
    """Send query + context to LLM for reasoning. Supports Ollama (local) and Claude (cloud).

    Returns an error message (not raises) if the LLM is unreachable.
    """
    # Inject learned preferences into system prompt
    style_hint = ""
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

    if LLM_BACKEND == "claude" and claude:
        try:
            response = await claude.messages.create(
                model=CLAUDE_MODEL,
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
    except httpx.TimeoutException:
        log.error("Ollama LLM call timed out after %.0fs", LLM_TIMEOUT)
        from learning import record_signal
        record_signal("llm_failure", {"backend": "ollama", "error": "timeout"})
        return "⚠️ LLM timed out. Ollama may be overloaded — try again in a moment."
    except httpx.ConnectError:
        log.error("Cannot connect to Ollama at %s", OLLAMA_URL)
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
]


def _looks_like_action(text: str) -> str | None:
    """Quick regex check if text looks like an action request. Returns hint or None."""
    text_lower = text.lower()
    for pattern, hint in _ACTION_PATTERNS:
        if re.search(pattern, text_lower):
            return hint
    return None


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
    Returns True if handled, False otherwise.
    """
    import importlib
    from config import EXTENSIONS_DIR

    if not EXTENSIONS_DIR or not EXTENSIONS_DIR.exists():
        return False

    for manifest_path in EXTENSIONS_DIR.glob("*.json"):
        try:
            manifest = json.loads(manifest_path.read_text())
            if manifest.get("command") == hint:
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

    return None


async def _try_inline_healing(update: Update):
    """Check for recurring failures and trigger self-healing immediately if threshold met."""
    try:
        from healing import detect_recurring_failures, run_self_healing
        triggers = detect_recurring_failures()
        if triggers and OWNER_CHAT_ID:
            bot = telegram_app.bot if telegram_app else None
            if bot:
                await run_self_healing(triggers, bot, OWNER_CHAT_ID)
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


async def handle_action_intent(intent: dict, update: Update) -> bool:
    """Handle a detected action intent. Returns True if handled."""
    action = intent.get("action")

    if action == "reminder":
        from actions.reminders import _parse_relative_time, create_reminder

        time_str = intent.get("time", "")
        text = intent.get("text", "")
        if not text:
            return False

        due_at = _parse_relative_time(time_str) if time_str else None
        if not due_at:
            await update.message.reply_text(
                f"I understood you want a reminder for: {text}\n"
                f"But I couldn't parse the time \"{time_str}\".\n"
                "Try: /remind in 2 hours {text}"
            )
            return True

        result = create_reminder(text, due_at)
        await update.message.reply_text(
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
            await update.message.reply_text(
                "I understood you want to send an email, but I need more detail.\n"
                "Try: /email draft <to> <subject>"
            )
            return True

        await update.message.reply_text(f"📝 Drafting email to {to_addr} about: {subject}...")

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

        await update.message.reply_text(
            f"📝 Draft ready:\n\nTo: {to_addr}\nSubject: {subject}\n\n{body}\n\n"
            f"---\n{autonomy.format_level()}",
            reply_markup=approve_deny_keyboard(),
        )
        return True

    elif action == "calendar":
        try:
            from actions.calendar import get_today_events, format_events_text
            events = await get_today_events()
            await update.message.reply_text(format_events_text(events))
        except Exception as e:
            from learning import record_signal
            record_signal("action_execution_failure", {
                "action": "calendar", "error": str(e)[:200],
            })
            await update.message.reply_text(f"❌ Calendar fetch failed: {e}")
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
            await update.message.reply_text(f"🚫 Command blocked (dangerous):\n`{cmd}`", parse_mode="Markdown")
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
                    await _try_inline_healing(update)
                # Interpret output as natural language answer when triggered by a user question
                if result["returncode"] == 0 and user_query:
                    interpretation = await _interpret_shell_output(user_query, final_cmd, result)
                    if interpretation:
                        await update.message.reply_text(interpretation)
                        return True
                await update.message.reply_text(f"```\n{format_output(result, final_cmd)}\n```", parse_mode="Markdown")
                return True

        # Needs approval — show Approve/Deny
        action_id = autonomy.create_pending_action(
            "shell_write",
            f"Run: {cmd}",
            {"command": cmd, "llm_generated": llm_generated, "user_query": user_query},
        )
        await update.message.reply_text(
            f"🖥 I'd run this command:\n\n`{cmd}`\n\n{description}\n\n"
            f"{autonomy.format_level()}",
            reply_markup=approve_deny_keyboard(),
            parse_mode="Markdown",
        )
        return True

    return False


# --- Telegram Handlers ---


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global OWNER_CHAT_ID
    OWNER_CHAT_ID = update.effective_chat.id
    _persist_owner_chat_id(OWNER_CHAT_ID)
    await update.message.reply_text(
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
        "/project — Project status tracking\n"
        "/nudge — What needs attention right now\n"
        "/audit — View recent actions\n"
        "/health — System health status\n"
        "/run — Run a shell command\n"
        "/backup — Export backup\n"
        "/clear — Clear conversation history\n"
        "/help — Show this message"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args) if context.args else ""
    if not query:
        await update.message.reply_text("Usage: /search <query>")
        return

    await update.message.reply_text(f"🔍 Searching: {query}")
    results = await hybrid_search(query, limit=5)

    if not results:
        await update.message.reply_text("No results found.")
        return

    text = f"📋 Found {len(results)} results:\n\n"
    for r in results:
        match_icon = "🧠" if r.get("match_type") == "semantic" else "🔤"
        text += f"{match_icon} **{r['title'][:60]}**\n"
        text += f"   [{r['category']}] {r['content'][:300]}...\n\n"

    await update.message.reply_text(text, parse_mode=None)


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        mode_name = context.args[0].lower()
        level_map = {
            "supervised": AutonomyLevel.SUPERVISED,
            "guided": AutonomyLevel.GUIDED,
            "autonomous": AutonomyLevel.AUTONOMOUS,
        }
        if mode_name not in level_map:
            await update.message.reply_text(
                f"Unknown mode. Options: {', '.join(level_map.keys())}"
            )
            return
        autonomy.set_level(level_map[mode_name])
        await update.message.reply_text(f"Mode changed to: {autonomy.format_level()}")
    else:
        await update.message.reply_text(f"Current mode: {autonomy.format_level()}")


async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    action = autonomy.get_latest_pending()
    if not action:
        await update.message.reply_text("No pending actions.")
        return

    result = autonomy.approve_action(action["id"])
    if not result:
        await update.message.reply_text("Failed to approve action.")
        return

    await update.message.reply_text(f"✅ Approved: {result['description']}\nExecuting...")

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
                    await update.message.reply_text(interpretation)
                else:
                    await update.message.reply_text(f"```\n{format_output(shell_result, final_cmd)}\n```", parse_mode="Markdown")
            else:
                await update.message.reply_text(f"```\n{format_output(shell_result, final_cmd)}\n```", parse_mode="Markdown")
        else:
            status_msg = await autonomy.execute_action(result)
            await update.message.reply_text(status_msg)
    except Exception as e:
        log.error(f"Action execution failed: {e}")
        from learning import record_signal
        record_signal("action_execution_failure", {
            "action": result.get("action_type", "unknown"),
            "error": str(e)[:200],
        })
        await update.message.reply_text(f"❌ Execution failed: {e}")


async def cmd_deny(update: Update, context: ContextTypes.DEFAULT_TYPE):
    action = autonomy.get_latest_pending()
    if not action:
        await update.message.reply_text("No pending actions.")
        return

    if autonomy.deny_action(action["id"]):
        await update.message.reply_text(f"❌ Denied: {action['description']}")
    else:
        await update.message.reply_text("Failed to deny action.")


async def _handle_self_extend_with_spec(spec: dict, update):
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
    await update.message.reply_text(
        f"I detected a capability gap: **{spec['description']}**\n"
        f"I can build a `/{spec.get('command', spec['name'])}` command for this.",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )


async def _run_extension_build(spec: dict, bot, chat_id: int):
    """Run extension build in background, notify on completion."""
    try:
        await bot.send_message(chat_id, f"🔧 Building `{spec['name']}` capability...")
        from actions.extend import generate_and_pr
        result = await generate_and_pr({"spec": spec})
        await bot.send_message(chat_id, f"✅ {result}")
    except Exception as e:
        log.error("Extension build failed for %s: %s", spec["name"], e)
        await bot.send_message(chat_id, f"❌ Failed to build `{spec['name']}`: {e}")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard button presses."""
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
        bot = telegram_app.bot if telegram_app else None
        chat_id = query.message.chat_id
        if bot and chat_id:
            asyncio.create_task(_run_extension_build(spec, bot, chat_id))

    elif query.data == "extend_skip":
        await query.edit_message_text("Skipped. Let me know if you change your mind.")


async def cmd_brief(update: Update, context: ContextTypes.DEFAULT_TYPE):
    progress = await update.message.reply_text("📰 Generating brief...")

    from scheduler.digests import generate_morning_brief

    brief = await generate_morning_brief(ask_claude)
    try:
        await progress.edit_text(brief)
    except Exception:
        await progress.delete()
        await update.message.reply_text(brief)


async def cmd_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /email command: /email search <query> or /email draft <to> <subject>"""
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage:\n"
            "  /email search <query> — Search live Gmail\n"
            "  /email draft <to> <subject> — Draft an email"
        )
        return

    subcommand = args[0].lower()

    if subcommand == "search":
        query = " ".join(args[1:])
        if not query:
            await update.message.reply_text("Usage: /email search <query>")
            return

        await update.message.reply_text(f"📧 Searching Gmail: {query}")
        try:
            from actions.gmail import search_emails
            emails = await search_emails(query, max_results=5)
        except Exception as e:
            await update.message.reply_text(f"Gmail search failed: {e}")
            return

        if not emails:
            await update.message.reply_text("No emails found.")
            return

        text = f"📧 Found {len(emails)} emails:\n\n"
        for e in emails:
            text += f"From: {e['from'][:40]}\n"
            text += f"Subject: {e['subject'][:60]}\n"
            text += f"Date: {e['date'][:20]}\n"
            preview = e.get('body', '')[:300] or e['snippet'][:200]
            text += f"{preview}...\n\n"

        await update.message.reply_text(text)

    elif subcommand == "draft":
        if len(args) < 3:
            await update.message.reply_text("Usage: /email draft <to> <subject words...>")
            return

        # Strip optional "to" keyword: "/email draft to ahmed@gmail.com ..." → skip "to"
        remaining = args[1:]
        if remaining and remaining[0].lower() == "to":
            remaining = remaining[1:]

        if not remaining:
            await update.message.reply_text("Usage: /email draft [to] <email> <subject words...>")
            return

        to_addr = remaining[0]

        # Strip optional "subject" keyword
        subject_parts = remaining[1:]
        if subject_parts and subject_parts[0].lower() == "subject":
            subject_parts = subject_parts[1:]

        if not subject_parts:
            await update.message.reply_text("Usage: /email draft [to] <email> <subject words...>")
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
            await update.message.reply_text("Usage: /email draft [to] <email> <subject words...> [body <text>]")
            return

        subject = subject_str

        if user_body:
            body = user_body
        elif len(subject.split()) <= 2:
            # Subject too vague for LLM to generate a meaningful body
            await update.message.reply_text(
                "Subject is too short to generate a body. Either:\n"
                "- Add more detail to the subject\n"
                "- Provide the body directly: /email draft <to> <subject> body <your message>"
            )
            return
        else:
            await update.message.reply_text(f"📝 Drafting email to {to_addr}...")

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

        await update.message.reply_text(
            f"📝 Draft ready:\n\n"
            f"To: {to_addr}\n"
            f"Subject: {subject}\n\n"
            f"{body}\n\n"
            f"---\n"
            f"⚡ Action: Send email via Gmail\n"
            f"{autonomy.format_level()}",
            reply_markup=approve_deny_keyboard(),
        )

    else:
        await update.message.reply_text(
            "Unknown subcommand. Use: /email search <query> or /email draft <to> <subject>"
        )


async def cmd_drive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /drive command: /drive search <query> or /drive recent"""
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage:\n"
            "  /drive search <query> — Search Google Drive\n"
            "  /drive recent [days] — Recently modified files"
        )
        return

    subcommand = args[0].lower()

    if subcommand == "search":
        query = " ".join(args[1:])
        if not query:
            await update.message.reply_text("Usage: /drive search <query>")
            return

        await update.message.reply_text(f"📁 Searching Drive: {query}")
        try:
            from actions.drive import search_files
            files = await search_files(query, max_results=8)
        except Exception as e:
            await update.message.reply_text(f"Drive search failed: {e}")
            return

        if not files:
            await update.message.reply_text("No files found.")
            return

        text = f"📁 Found {len(files)} files:\n\n"
        for f in files:
            text += f"📄 {f['name']}\n"
            text += f"   Modified: {f['modified']} | {f['link']}\n\n"

        await update.message.reply_text(text)

    elif subcommand == "recent":
        days = int(args[1]) if len(args) > 1 and args[1].isdigit() else 7
        await update.message.reply_text(f"📁 Files modified in last {days} days...")
        try:
            from actions.drive import list_recent
            files = await list_recent(days=days, max_results=10)
        except Exception as e:
            await update.message.reply_text(f"Drive query failed: {e}")
            return

        if not files:
            await update.message.reply_text("No recent files found.")
            return

        text = f"📁 {len(files)} files modified in last {days} days:\n\n"
        for f in files:
            text += f"📄 {f['name']} ({f['modified']})\n"

        await update.message.reply_text(text)

    else:
        await update.message.reply_text(
            "Unknown subcommand. Use: /drive search <query> or /drive recent"
        )


async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /remind command: /remind list, /remind cancel <id>, /remind <time> <text>, /remind recurring ..."""
    from actions.reminders import (
        _parse_relative_time, create_reminder, list_reminders, cancel_reminder,
        _parse_natural_cron, create_recurring, list_recurring, cancel_recurring,
    )

    args = context.args or []
    if not args:
        await update.message.reply_text(
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
            await update.message.reply_text(
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
                await update.message.reply_text("No active recurring reminders.")
                return
            text = f"🔄 {len(recurring)} recurring reminders:\n\n"
            for r in recurring:
                text += f"#{r['id']} — {r['text']}\n   Cron: {r['cron_expression']} | Next: {r['next_fire_at'][:16]}\n\n"
            await update.message.reply_text(text)

        elif recur_sub == "cancel":
            if len(args) < 3 or not args[2].isdigit():
                await update.message.reply_text("Usage: /remind recurring cancel <id>")
                return
            if cancel_recurring(int(args[2])):
                await update.message.reply_text(f"✅ Recurring reminder #{args[2]} cancelled.")
            else:
                await update.message.reply_text(f"Recurring #{args[2]} not found or already cancelled.")

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
                await update.message.reply_text(
                    "Couldn't parse schedule. Try:\n"
                    "  /remind recurring every monday 9am Review sprint\n"
                    "  /remind recurring every day Check email\n"
                    "  /remind recurring first of month Review RRSP"
                )
                return

            result = create_recurring(reminder_text, cron_expr)
            await update.message.reply_text(
                f"🔄 Recurring reminder set!\n\n"
                f"#{result['id']}: {result['text']}\n"
                f"Schedule: {result['cron_expression']}\n"
                f"Next: {result['next_fire_at'][:16]}"
            )
        return

    elif subcommand == "list":
        reminders = list_reminders()
        if not reminders:
            await update.message.reply_text("No active reminders.")
            return
        text = f"⏰ {len(reminders)} active reminders:\n\n"
        for r in reminders:
            text += f"#{r['id']} — {r['text']}\n   Due: {r['due_at']}\n\n"
        await update.message.reply_text(text)

    elif subcommand == "cancel":
        if len(args) < 2 or not args[1].isdigit():
            await update.message.reply_text("Usage: /remind cancel <id>")
            return
        if cancel_reminder(int(args[1])):
            await update.message.reply_text(f"✅ Reminder #{args[1]} cancelled.")
        else:
            await update.message.reply_text(f"Reminder #{args[1]} not found or already done.")

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
            await update.message.reply_text(
                "Couldn't parse time. Try:\n"
                "  /remind in 30 minutes Call dentist\n"
                "  /remind tomorrow 9am Review PR"
            )
            return

        result = create_reminder(reminder_text, due_at)
        await update.message.reply_text(
            f"⏰ Reminder set!\n\n"
            f"#{result['id']}: {result['text']}\n"
            f"Due: {result['due_at']}\n\n"
            f"Use /remind list to see all, /remind cancel {result['id']} to remove."
        )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_conversation(update.effective_chat.id)
    await update.message.reply_text("🧹 Conversation history cleared.")


async def cmd_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📧 Syncing emails...")
    try:
        from actions.gmail_sync import sync_new_emails
        result = await sync_new_emails()
        await update.message.reply_text(
            f"✅ Email sync complete: {result['fetched']} fetched, {result['indexed']} indexed."
        )
    except Exception as e:
        log.error("Email sync failed: %s", e)
        await update.message.reply_text(f"❌ Email sync failed: {e}")


async def cmd_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("💼 Checking for new job matches...")
    try:
        from actions.jobs import fetch_new_jobs, format_jobs_text
        jobs = await fetch_new_jobs()
        await update.message.reply_text(format_jobs_text(jobs))
    except Exception as e:
        log.error("Job scraper failed: %s", e)
        await update.message.reply_text(f"❌ Job scraper failed: {e}")


async def cmd_project(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /project command: view project status."""
    from actions.projects import resolve_project, get_project_status, list_projects, get_open_tasks

    args = context.args or []
    if not args:
        projects = list_projects()
        await update.message.reply_text(
            f"📋 Projects\n\n{projects}\n\n"
            "Usage: /project <name> — detailed status\n"
            "       /project <name> tasks — open tasks"
        )
        return

    name = args[0]
    key = resolve_project(name)
    if not key:
        await update.message.reply_text(
            f"Unknown project: {name}\n\nKnown: zia, tiny-grounds, bezier, khalil"
        )
        return

    subcommand = args[1].lower() if len(args) > 1 else ""

    if subcommand == "tasks":
        tasks = get_open_tasks(key)
        if not tasks:
            await update.message.reply_text(f"No open tasks for {key}.")
        else:
            text = f"📝 Open tasks for {key}:\n\n" + "\n".join(f"- [ ] {t}" for t in tasks)
            await update.message.reply_text(text)
    else:
        status = get_project_status(key)
        await update.message.reply_text(status)


async def cmd_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /calendar command: show today's or upcoming events."""
    args = context.args or []
    days = 1
    if args and args[0].isdigit():
        days = min(int(args[0]), 30)

    await update.message.reply_text(f"📅 Fetching calendar events ({days} day{'s' if days > 1 else ''})...")
    try:
        from actions.calendar import get_today_events, get_upcoming_events, format_events_text
        if days == 1:
            events = await get_today_events()
        else:
            events = await get_upcoming_events(days=days)
        await update.message.reply_text(format_events_text(events))
    except FileNotFoundError as e:
        await update.message.reply_text(f"⚠️ Calendar not configured: {e}")
    except Exception as e:
        log.error("Calendar fetch failed: %s", e)
        await update.message.reply_text(f"❌ Calendar fetch failed: {e}")


async def cmd_finance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /finance command: show financial dashboard or detailed views."""
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
        await update.message.reply_text(
            f"📅 Financial Deadlines\n\n{format_deadlines_text(deadlines)}"
        )

    elif subcommand == "portfolio":
        portfolio = get_portfolio_summary()
        if not portfolio:
            await update.message.reply_text("No portfolio data found.")
            return
        # Truncate for Telegram (4096 char limit)
        await update.message.reply_text(f"📊 Portfolio\n\n{portfolio[:3500]}")

    elif subcommand == "rsu":
        rsu = get_rsu_summary()
        if not rsu:
            await update.message.reply_text("No RSU data found.")
            return
        await update.message.reply_text(f"📈 RSU Summary\n\n{rsu[:3500]}")

    elif subcommand == "ask" and len(args) > 1:
        query = " ".join(args[1:])
        await update.message.reply_text(f"🔍 Analyzing: {query}")
        personal_context = get_relevant_context("finance investment rrsp tfsa rsu", max_chars=3000)
        results = await hybrid_search(query, limit=5, category="email:finance")
        archive_context = truncate_context(results) if results else ""
        full_context = f"{personal_context}\n\n{archive_context}"
        answer = await ask_claude(
            f"Answer Ahmed's finance question based on his financial records:\n\n{query}",
            full_context,
            system_extra=f"Today's date: {date.today().isoformat()}",
        )
        await update.message.reply_text(answer)

    else:
        dashboard = format_dashboard_text()
        await update.message.reply_text(
            f"💰 Financial Dashboard\n\n{dashboard}\n\n"
            "Sub-commands:\n"
            "  /finance deadlines — All deadlines\n"
            "  /finance portfolio — Full portfolio\n"
            "  /finance rsu — RSU/tax summary\n"
            "  /finance ask <question> — Ask about finances"
        )


async def cmd_work(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /work command: sprint dashboard, P0s, filter by theme/owner."""
    from actions.work import (
        get_sprint_summary, get_p0_epics, get_epics_by_theme,
        get_epics_by_owner, get_in_progress,
    )

    args = context.args or []
    if not args:
        await update.message.reply_text(get_sprint_summary())
        return

    subcommand = args[0].lower()

    if subcommand == "p0":
        await update.message.reply_text(get_p0_epics())

    elif subcommand == "progress":
        await update.message.reply_text(get_in_progress())

    elif subcommand == "theme" and len(args) > 1:
        theme = " ".join(args[1:])
        await update.message.reply_text(get_epics_by_theme(theme))

    elif subcommand == "owner" and len(args) > 1:
        name = " ".join(args[1:])
        await update.message.reply_text(get_epics_by_owner(name))

    elif subcommand == "ask" and len(args) > 1:
        query = " ".join(args[1:])
        await update.message.reply_text(f"🔍 Analyzing: {query}")
        work_context = get_sprint_summary() + "\n\n" + get_p0_epics()
        results = await hybrid_search(query, limit=5, category="work:planning")
        if results:
            work_context += "\n\n" + truncate_context(results)
        answer = await ask_claude(
            f"Answer Ahmed's work question based on sprint planning data:\n\n{query}",
            work_context,
            system_extra=f"Today's date: {date.today().isoformat()}",
        )
        await update.message.reply_text(answer)

    else:
        await update.message.reply_text(
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
    from actions.goals import (
        get_current_goals, get_all_goals, add_goal, complete_goal, get_goal_summary,
    )

    args = context.args or []
    if not args:
        await update.message.reply_text(get_current_goals())
        return

    subcommand = args[0].lower()

    if subcommand == "all":
        await update.message.reply_text(get_all_goals())

    elif subcommand == "add" and len(args) >= 3:
        category = args[1]
        text = " ".join(args[2:])
        await update.message.reply_text(add_goal(category, text))

    elif subcommand == "done" and len(args) >= 3:
        category = args[1]
        try:
            index = int(args[2])
        except ValueError:
            await update.message.reply_text("Usage: /goals done <category> <number>")
            return
        await update.message.reply_text(complete_goal(category, index))

    elif subcommand == "review":
        await update.message.reply_text("🔍 Reviewing goals...")
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
        await update.message.reply_text(answer)

    else:
        await update.message.reply_text(
            "Usage:\n"
            "  /goals — Current quarter goals\n"
            "  /goals all — All quarters\n"
            "  /goals add <category> <text> — Add a goal\n"
            "  /goals done <category> <number> — Mark done\n"
            "  /goals review — LLM-powered reflection\n"
            "\nCategories: career, health, learning, personal"
        )


async def cmd_nudge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manual trigger for proactive checks."""
    from scheduler.proactive import run_proactive_checks

    findings = run_proactive_checks()
    if not findings:
        await update.message.reply_text("✅ All clear — nothing needs attention.")
        return

    text = "🔔 Proactive Check — things that need attention:\n\n" + "\n\n".join(findings)
    await update.message.reply_text(text)


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Execute a shell command on the local machine."""
    cmd = " ".join(context.args) if context.args else ""
    if not cmd:
        await update.message.reply_text(
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
        await update.message.reply_text(f"🚫 Command blocked (dangerous):\n`{cmd}`", parse_mode="Markdown")
        return

    action_name = "shell_read" if classification == ActionType.READ else "shell_write"

    if autonomy.needs_approval(action_name):
        action_id = autonomy.create_pending_action(
            action_name,
            f"Run: {cmd}",
            {"command": cmd},
        )
        label = "safe" if classification == ActionType.READ else "risky"
        await update.message.reply_text(
            f"🖥 Shell command requires approval:\n\n`{cmd}`\n\n"
            f"Classification: {label}\n{autonomy.format_level()}",
            reply_markup=approve_deny_keyboard(),
            parse_mode="Markdown",
        )
        return

    # Auto-execute (safe command in GUIDED/AUTONOMOUS mode)
    autonomy.log_audit(action_name, f"Auto-run: {cmd}", result="executing")
    result = await execute_shell(cmd)
    autonomy.log_audit(action_name, f"Completed: {cmd}", result=f"exit={result['returncode']}")
    output = format_output(result, cmd)
    await update.message.reply_text(output)


async def cmd_audit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    entries = autonomy.get_audit_log(limit=10)
    if not entries:
        await update.message.reply_text("No audit log entries yet.")
        return
    text = f"📋 Last {len(entries)} actions:\n\n"
    for e in entries:
        text += f"#{e['id']} [{e['autonomy_level']}] {e['action_type']}\n"
        text += f"   {e['description'][:60]}\n"
        text += f"   Result: {e['result'] or '—'} | {e['timestamp'][:16]}\n\n"
    await update.message.reply_text(text)


async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show system health status."""
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

    await update.message.reply_text("\n".join(lines))


async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /backup command: export or list backups."""
    from actions.backup import export_backup, list_backups, format_backup_summary

    args = context.args or []
    subcommand = args[0].lower() if args else "export"

    if subcommand == "list":
        backups = list_backups()
        if not backups:
            await update.message.reply_text("No backups found.")
            return
        text = f"📦 {len(backups)} backup(s):\n\n"
        for b in backups[:10]:
            text += f"  {b['filename']} ({b['size_kb']} KB)\n  Created: {b['created']}\n\n"
        await update.message.reply_text(text)

    else:
        await update.message.reply_text("📦 Creating backup...")
        try:
            path = export_backup()
            summary = format_backup_summary(path)
            await update.message.reply_text(f"✅ Backup created!\n\n{summary}")
        except Exception as e:
            log.error("Backup failed: %s", e)
            await update.message.reply_text(f"❌ Backup failed: {e}")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = get_stats()
    text = f"📊 Knowledge Base\n\nTotal documents: {stats['total_documents']}\n\n"
    for cat, count in list(stats["by_category"].items())[:15]:
        text += f"  {cat}: {count}\n"
    text += f"\nMode: {autonomy.format_level()}"
    await update.message.reply_text(text)


async def cmd_learn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /learn command — view and manage self-improvement insights."""
    from learning import get_insights, list_preferences, apply_insight, dismiss_insight, reset_preferences

    args = context.args or []
    subcommand = args[0].lower() if args else ""

    if subcommand == "preferences":
        prefs = list_preferences()
        if not prefs:
            await update.message.reply_text("No learned preferences yet. Khalil will start learning from your interactions over time.")
            return
        text = f"🧠 {len(prefs)} Learned Preferences:\n\n"
        for p in prefs:
            conf_bar = "●" * int(p["confidence"] * 10) + "○" * (10 - int(p["confidence"] * 10))
            text += f"  {p['key']}: {p['value']}\n  Confidence: [{conf_bar}] {p['confidence']:.1f}\n\n"
        await update.message.reply_text(text)

    elif subcommand == "apply" and len(args) > 1 and args[1].isdigit():
        if apply_insight(int(args[1])):
            await update.message.reply_text(f"✅ Insight #{args[1]} applied.")
        else:
            await update.message.reply_text(f"Insight #{args[1]} not found or not pending.")

    elif subcommand == "dismiss" and len(args) > 1 and args[1].isdigit():
        if dismiss_insight(int(args[1])):
            await update.message.reply_text(f"❌ Insight #{args[1]} dismissed.")
        else:
            await update.message.reply_text(f"Insight #{args[1]} not found or not pending.")

    elif subcommand == "reset":
        reset_preferences()
        await update.message.reply_text("🧹 All learned preferences cleared.")

    elif subcommand == "history":
        insights = get_insights(limit=15)
        if not insights:
            await update.message.reply_text("No insights yet. Khalil generates insights from weekly reflection.")
            return
        text = f"🧠 Insight History ({len(insights)}):\n\n"
        for i in insights:
            status_icon = {"pending": "⏳", "applied": "✅", "dismissed": "❌", "superseded": "🔄"}.get(i["status"], "?")
            text += f"#{i['id']} {status_icon} [{i['category']}]\n  {i['summary']}\n  {i['recommendation'][:80]}\n\n"
        await update.message.reply_text(text)

    else:
        # Default: show last 5 insights
        insights = get_insights(limit=5)
        if not insights:
            await update.message.reply_text(
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
        await update.message.reply_text(text)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle free-text messages — the main conversational flow."""
    global OWNER_CHAT_ID
    if OWNER_CHAT_ID is None:
        OWNER_CHAT_ID = update.effective_chat.id
        _persist_owner_chat_id(OWNER_CHAT_ID)

    query = update.message.text
    if not query:
        return

    # Check for sensitive data in query
    if contains_sensitive_data(query):
        await update.message.reply_text(
            "⚠️ Your message appears to contain sensitive data. "
            "I'll proceed but won't include raw sensitive values in API calls."
        )

    # Save user message to conversation history
    chat_id = update.effective_chat.id
    save_message(chat_id, "user", query)

    # Track user corrections for self-healing
    _CORRECTION_PATTERNS = [
        r"^no[,.]?\s+i\s+(?:meant|want)", r"^that'?s\s+not\s+what",
        r"^wrong[,.]", r"^not\s+that", r"^try\s+again",
    ]
    if any(re.search(p, query.lower()) for p in _CORRECTION_PATTERNS):
        from learning import record_signal
        record_signal("user_correction", {"query": query[:200]})
        await _try_inline_healing(update)

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
        handled = await handle_action_intent(direct_intent, update)
        if handled:
            return
    # 2. LLM-based detection for ambiguous patterns
    if action_hint is None:
        action_hint = _looks_like_action(query)
    if action_hint:
        intent = await detect_intent(query)
        if intent:
            intent["user_query"] = query
            handled = await handle_action_intent(intent, update)
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
            await _try_inline_healing(update)

    # Show progress indicator
    progress_msg = await update.message.reply_text("🔍 Thinking...")

    # Search knowledge base
    results = await hybrid_search(query, limit=6)
    archive_context = truncate_context(results) if results else "No relevant archive data found."

    # Get relevant CONTEXT.md sections
    personal_context = get_relevant_context(query, max_chars=2000)

    # Get conversation history for multi-turn context
    conversation = get_conversation_history(chat_id)

    # Combine context
    full_context = f"Personal Profile:\n{personal_context}\n\nArchive Results:\n{archive_context}"
    if conversation:
        full_context = f"{conversation}\n\n{full_context}"

    # Ask LLM
    response = await ask_claude(query, full_context)

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
        await _try_inline_healing(update)
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
            handled = await handle_action_intent(intent, update)
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
                await progress_msg.edit_text(display_response)
            except Exception:
                await progress_msg.delete()
                await update.message.reply_text(display_response)
            await _handle_self_extend_with_spec(spec, update)
            return
    # 2. Fallback: phrase-based detection
    try:
        from actions.extend import detect_capability_gap, handle_self_extend
        if detect_capability_gap(display_response):
            record_signal("capability_gap_detected", {"query": query[:200]})
            await _try_inline_healing(update)
            await handle_self_extend(query, update, ask_claude)
    except Exception as e:
        log.debug("Capability gap detection failed: %s", e)

    # Replace progress message with response (tag already stripped)
    try:
        await progress_msg.edit_text(display_response)
    except Exception:
        # If edit fails (e.g., message too long), send as new message
        await progress_msg.delete()
        await update.message.reply_text(display_response)


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Unknown command. Send /help to see available commands."
    )


# --- Startup ---


def _wrap_extension_handler(handler_fn, extension_name: str):
    """Wrap extension handler to record failures for self-healing."""
    async def wrapper(update, context):
        try:
            return await handler_fn(update, context)
        except Exception as e:
            log.error("Extension %s failed: %s", extension_name, e)
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


def _load_extensions(application):
    """Dynamically register command handlers from extension manifests."""
    import importlib
    from config import EXTENSIONS_DIR

    if not EXTENSIONS_DIR.exists():
        return

    for manifest_path in sorted(EXTENSIONS_DIR.glob("*.json")):
        try:
            manifest = json.loads(manifest_path.read_text())
            module_name = manifest["action_module"]
            handler_name = manifest["handler_function"]
            command = manifest["command"]

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
    application.add_handler(CommandHandler("backup", cmd_backup))
    application.add_handler(CommandHandler("run", cmd_run))
    application.add_handler(CommandHandler("learn", cmd_learn))

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
        BotCommand("help", "Show help"),
    ])

    log.info("Telegram bot starting...")
    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)

    global telegram_app
    telegram_app = application

    return application


def _setup_scheduler():
    """Register scheduled jobs."""
    from scheduler.tasks import sync_emails, send_morning_brief, send_financial_alert, send_weekly_summary, send_career_alert, send_friday_reflection, run_reflection, run_micro_reflection

    def _can_send():
        return telegram_app and OWNER_CHAT_ID

    async def _morning_brief_job():
        if _can_send():
            await send_morning_brief(telegram_app.bot, OWNER_CHAT_ID, ask_claude)
        else:
            log.warning("Morning brief skipped: no Telegram bot or owner chat ID yet")

    async def _financial_alert_job():
        if _can_send():
            await send_financial_alert(telegram_app.bot, OWNER_CHAT_ID, ask_claude)

    async def _weekly_summary_job():
        if _can_send():
            await send_weekly_summary(telegram_app.bot, OWNER_CHAT_ID, ask_claude)

    async def _reminder_check_job():
        if not _can_send():
            return
        from actions.reminders import check_due_reminders, check_recurring_due
        # One-shot reminders
        fired = check_due_reminders()
        for r in fired:
            await telegram_app.bot.send_message(
                chat_id=OWNER_CHAT_ID,
                text=f"⏰ Reminder!\n\n{r['text']}",
            )
            log.info(f"Reminder #{r['id']} fired: {r['text']}")
        # Recurring reminders
        recurring_fired = check_recurring_due()
        for r in recurring_fired:
            await telegram_app.bot.send_message(
                chat_id=OWNER_CHAT_ID,
                text=f"🔄 Recurring Reminder!\n\n{r['text']}",
            )
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
            await send_career_alert(telegram_app.bot, OWNER_CHAT_ID)

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
            await send_friday_reflection(telegram_app.bot, OWNER_CHAT_ID, ask_claude)

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
            await telegram_app.bot.send_message(chat_id=OWNER_CHAT_ID, text=msg)
            log.warning("Self-check found issues — notified owner")

    scheduler.add_job(
        _self_check_job,
        CronTrigger(hour=20, minute=0, timezone=TIMEZONE),
        id="self_check",
        name="Daily Self-Check",
        replace_existing=True,
    )

    # Weekly reflection — Sunday 5 PM (before weekly summary at 6 PM)
    async def _weekly_reflection_job():
        if _can_send():
            await run_reflection(telegram_app.bot, OWNER_CHAT_ID, ask_claude)

    scheduler.add_job(
        _weekly_reflection_job,
        CronTrigger(day_of_week="sun", hour=17, minute=0, timezone=TIMEZONE),
        id="weekly_reflection",
        name="Weekly Reflection",
        replace_existing=True,
    )

    # Daily micro-reflection + self-healing check — 11 PM
    async def _micro_reflection_job():
        bot = telegram_app.bot if telegram_app else None
        await run_micro_reflection(ask_claude, bot=bot, chat_id=OWNER_CHAT_ID)

    scheduler.add_job(
        _micro_reflection_job,
        CronTrigger(hour=23, minute=0, timezone=TIMEZONE),
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
            await telegram_app.bot.send_message(chat_id=OWNER_CHAT_ID, text=text)
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
                await telegram_app.bot.send_message(chat_id=OWNER_CHAT_ID, text=msg)
        await proactive_token_refresh(notify_fn=_notify)

    scheduler.add_job(
        _oauth_refresh_job,
        CronTrigger(hour="*/6", minute=30, timezone=TIMEZONE),
        id="oauth_refresh",
        name="OAuth Token Refresh",
        replace_existing=True,
    )

    log.info("Scheduler jobs registered")


@app.on_event("startup")
async def startup():
    global db_conn, autonomy, claude

    log.info("Khalil starting up...")

    # Initialize database
    db_conn = init_db()
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

    # Start Telegram bot
    asyncio.create_task(start_telegram_bot())

    # Start scheduler
    _setup_scheduler()
    scheduler.start()
    log.info(f"Scheduler started with {len(scheduler.get_jobs())} jobs")

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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=8033, reload=False, log_level="info")
