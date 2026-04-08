"""M4: Session continuity — inject prior session context for cross-session coherence.

At session start (gap >2h), automatically inject last session summary.
On continuation cues ("continue", "where were we"), inject last 2 summaries.
After process restart, inject last tool-use burst so in-flight work isn't lost.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from config import DB_PATH

log = logging.getLogger("khalil.session_continuity")

# Phrases that indicate the user wants to continue a prior conversation
_CONTINUATION_CUES = re.compile(
    r"\b(continue|where were we|pick up where|last time|yesterday|earlier|"
    r"what were we|as we discussed|we were talking|resume|carry on|"
    r"you were working on|what happened to|continue the|finish the|"
    r"status of|how did .+ go|what were you doing|"
    r"what'?s the status|any update|any progress|how'?s it going|"
    r"is it (?:done|ready|finished)|did you (?:finish|complete)|where are we)\b",
    re.IGNORECASE,
)

# Maximum tokens to inject (approximate: 1 token ~= 4 chars)
MAX_CONTEXT_CHARS = 1200  # ~300 tokens
MAX_CONTEXT_CHARS_CONTINUATION = 2400  # ~600 tokens when resuming work


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def is_new_session(chat_id: int, gap_hours: float = 2.0) -> bool:
    """Check if this is a new session (gap > gap_hours since last message)."""
    try:
        conn = _get_conn()
        row = conn.execute(
            "SELECT timestamp FROM conversations WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        conn.close()
        if not row:
            return True
        last_ts = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
        gap = datetime.now(timezone.utc) - last_ts
        return gap > timedelta(hours=gap_hours)
    except Exception as e:
        log.debug("Session gap check failed: %s", e)
        return False


def has_continuation_cue(query: str) -> bool:
    """Check if query contains continuation cues."""
    return bool(_CONTINUATION_CUES.search(query))


def is_post_restart() -> bool:
    """Check if this is the first query after a process restart.

    Returns True if `previous_boot_time` exists in settings — set by startup()
    when it detects a prior boot timestamp before overwriting it.
    Consumed once, then cleared by get_session_continuity().
    """
    try:
        conn = _get_conn()
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'previous_boot_time'"
        ).fetchone()
        conn.close()
        return bool(row and row[0])
    except Exception:
        return False


def get_last_tool_use_context(chat_id: int, max_chars: int = 2000) -> str:
    """Reconstruct the last tool-use burst from saved conversation messages.

    Walks backward from newest messages to find the most recent user query
    and its associated tool_call/tool_result chain. Returns a formatted
    summary of what was in progress.
    """
    try:
        conn = _get_conn()
        rows = conn.execute(
            """SELECT role, content, message_type, metadata
               FROM conversations
               WHERE chat_id = ?
               ORDER BY id DESC LIMIT 40""",
            (chat_id,),
        ).fetchall()
        conn.close()
    except Exception as e:
        log.debug("Failed to load tool-use context: %s", e)
        return ""

    if not rows:
        return ""

    # Walk backward: collect tool_call/tool_result until we hit the user query
    burst = []
    user_query = ""
    found_user = False
    for role, content, msg_type, metadata in rows:
        if msg_type in ("tool_call", "tool_result"):
            burst.append((role, content, msg_type, metadata))
        elif role == "user" and msg_type == "text":
            user_query = content
            found_user = True
            break
        elif role == "assistant" and msg_type == "text" and burst:
            # Hit a completed text response before the burst — no in-flight work
            break

    if not burst or not found_user:
        return ""

    parts = [f"[Last in-progress task]\nUser asked: {user_query[:300]}"]
    for role, content, msg_type, metadata in reversed(burst):
        meta = json.loads(metadata) if metadata else {}
        tool_name = meta.get("tool_name", "unknown")
        if msg_type == "tool_call":
            parts.append(f"→ Called {tool_name}: {content[:200]}")
        elif msg_type == "tool_result":
            parts.append(f"← Result from {tool_name}: {content[:400]}")

    result = "\n".join(parts)
    return result[:max_chars]


def _clear_restart_flag() -> None:
    """Clear the previous_boot_time flag so restart context is injected only once."""
    try:
        conn = _get_conn()
        conn.execute("DELETE FROM settings WHERE key = 'previous_boot_time'")
        conn.commit()
        conn.close()
    except Exception:
        pass


def get_session_continuity(chat_id: int, query: str) -> str:
    """Get session continuity context to inject before LLM sees the query.

    Returns formatted context string, capped at MAX_CONTEXT_CHARS.
    Returns empty string if no relevant context.
    """
    try:
        post_restart = is_post_restart()
        conn = _get_conn()

        # Determine how many summaries to fetch
        is_continuation = has_continuation_cue(query)
        limit = 2 if is_continuation else 1
        new_session = is_new_session(chat_id)

        if not new_session and not is_continuation and not post_restart:
            conn.close()
            return ""

        rows = conn.execute(
            "SELECT summary, created_at FROM conversation_summaries "
            "WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
        conn.close()

        # Use higher budget for continuation or restart
        use_high_budget = is_continuation or post_restart
        char_budget = MAX_CONTEXT_CHARS_CONTINUATION if use_high_budget else MAX_CONTEXT_CHARS

        parts = ["[Prior session context]"]

        if rows:
            for summary, created_at in reversed(rows):
                max_per = char_budget // limit
                truncated = summary[:max_per] + "..." if len(summary) > max_per else summary
                parts.append(f"Session ({created_at[:10]}): {truncated}")

        # Check for incomplete tasks from prior session
        try:
            pending_row = conn.execute(
                "SELECT value FROM settings WHERE key = ?",
                (f"pending_task_{chat_id}",),
            ).fetchone()
            if pending_row:
                task = json.loads(pending_row[0])
                parts.append(
                    f"[Incomplete task from prior session]\n"
                    f"User asked: {task['query']}\n"
                    f"Tools used: {', '.join(task.get('tools_used', []))}\n"
                    f"Status: May not have completed successfully"
                )
        except Exception:
            pass

        # If continuation cue detected, also inject active task plans
        if is_continuation:
            try:
                from orchestrator import get_active_plans_for_chat, format_plan_summary, ensure_table
                ensure_table()
                active_plans = get_active_plans_for_chat(chat_id)
                for plan in active_plans[:2]:
                    parts.append(f"[Active plan]\n{format_plan_summary(plan)}")
            except Exception:
                pass

        # After restart, inject last tool-use burst so in-flight work is visible
        if post_restart:
            tool_ctx = get_last_tool_use_context(chat_id)
            if tool_ctx:
                parts.append(tool_ctx)
            _clear_restart_flag()
            log.info("Post-restart context injected for chat %d", chat_id)

        result = "\n".join(parts)
        if len(parts) <= 1:
            # Only header, no actual context
            return ""
        return result[:char_budget]
    except Exception as e:
        log.debug("Session continuity failed: %s", e)
        return ""
