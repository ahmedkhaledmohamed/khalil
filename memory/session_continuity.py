"""M4: Session continuity — inject prior session context for cross-session coherence.

At session start (gap >2h), automatically inject last session summary.
On continuation cues ("continue", "where were we"), inject last 2 summaries.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from config import DB_PATH

log = logging.getLogger("khalil.session_continuity")

# Phrases that indicate the user wants to continue a prior conversation
_CONTINUATION_CUES = re.compile(
    r"\b(continue|where were we|pick up where|last time|yesterday|earlier|"
    r"what were we|as we discussed|we were talking|resume|carry on)\b",
    re.IGNORECASE,
)

# Maximum tokens to inject (approximate: 1 token ~= 4 chars)
MAX_CONTEXT_CHARS = 1200  # ~300 tokens


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


def get_session_continuity(chat_id: int, query: str) -> str:
    """Get session continuity context to inject before LLM sees the query.

    Returns formatted context string, capped at MAX_CONTEXT_CHARS.
    Returns empty string if no relevant context.
    """
    try:
        conn = _get_conn()

        # Determine how many summaries to fetch
        limit = 2 if has_continuation_cue(query) else 1
        new_session = is_new_session(chat_id)

        if not new_session and not has_continuation_cue(query):
            conn.close()
            return ""

        rows = conn.execute(
            "SELECT summary, created_at FROM conversation_summaries "
            "WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
        conn.close()

        if not rows:
            return ""

        parts = ["[Prior session context]"]
        for summary, created_at in reversed(rows):
            # Truncate individual summaries to fit budget
            max_per = MAX_CONTEXT_CHARS // limit
            truncated = summary[:max_per] + "..." if len(summary) > max_per else summary
            parts.append(f"Session ({created_at[:10]}): {truncated}")

        result = "\n".join(parts)
        return result[:MAX_CONTEXT_CHARS]
    except Exception as e:
        log.debug("Session continuity failed: %s", e)
        return ""
