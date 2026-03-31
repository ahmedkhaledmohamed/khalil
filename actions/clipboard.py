"""Clipboard history — log and search clipboard entries via pbpaste.

Stores clipboard entries in SQLite for persistent history.
Uses periodic polling (called from agent_loop or scheduler) to capture
new clipboard content.
"""

import asyncio
import hashlib
import logging
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from config import DB_PATH, TIMEZONE

log = logging.getLogger("khalil.actions.clipboard")

SKILL = {
    "name": "clipboard",
    "description": "Clipboard history — search and recall recent copies",
    "category": "productivity",
    "patterns": [
        (r"\bclipboard\s+history\b", "clipboard_history"),
        (r"\brecent\s+(?:clipboard|copies|copied)\b", "clipboard_history"),
        (r"\bwhat\s+(?:did\s+I|have\s+I)\s+cop(?:y|ied)\b", "clipboard_history"),
        (r"\bpaste\s+history\b", "clipboard_history"),
        (r"\bsearch\s+(?:my\s+)?clipboard\b", "clipboard_search"),
        (r"\bfind\s+(?:in\s+)?(?:my\s+)?clipboard\b", "clipboard_search"),
        (r"\bclipboard\b.*\bsearch\b", "clipboard_search"),
        (r"\blast\s+(?:thing\s+)?(?:I\s+)?copied\b", "clipboard_last"),
        (r"\bcurrent\s+clipboard\b", "clipboard_last"),
        (r"\bwhat(?:'s|\s+is)\s+(?:on\s+)?(?:my\s+)?clipboard\b", "clipboard_last"),
    ],
    "actions": [
        {"type": "clipboard_history", "handler": "handle_intent", "keywords": "clipboard history recent copies copied paste", "description": "Show recent clipboard entries"},
        {"type": "clipboard_search", "handler": "handle_intent", "keywords": "clipboard search find copied text", "description": "Search clipboard history"},
        {"type": "clipboard_last", "handler": "handle_intent", "keywords": "clipboard last current copied", "description": "Show last copied item"},
    ],
    "examples": [
        "Show clipboard history",
        "What did I copy recently?",
        "Search clipboard for URL",
        "What's on my clipboard?",
    ],
}


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def _ensure_table():
    """Create clipboard_history table if it doesn't exist."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS clipboard_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            content_type TEXT DEFAULT 'text'
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_clipboard_hash ON clipboard_history(content_hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_clipboard_created ON clipboard_history(created_at DESC)")
    conn.commit()
    conn.close()


def record_clipboard(content: str, content_type: str = "text") -> bool:
    """Record a clipboard entry. Returns True if new (not duplicate)."""
    if not content or not content.strip():
        return False

    content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
    now = datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S")

    _ensure_table()
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(
            "INSERT OR IGNORE INTO clipboard_history (content, content_hash, created_at, content_type) VALUES (?, ?, ?, ?)",
            (content[:10000] if len(content) > 10000 else content, content_hash, now, content_type),
        )
        conn.commit()
        changed = conn.total_changes > 0
        return changed
    except Exception as e:
        log.debug("Clipboard record failed: %s", e)
        return False
    finally:
        conn.close()


def get_recent(limit: int = 20) -> list[dict]:
    """Get recent clipboard entries."""
    _ensure_table()
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute(
        "SELECT content, created_at, content_type FROM clipboard_history ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [{"content": r[0], "created_at": r[1], "type": r[2]} for r in rows]


def search_clipboard(query: str, limit: int = 10) -> list[dict]:
    """Search clipboard history by content."""
    _ensure_table()
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute(
        "SELECT content, created_at, content_type FROM clipboard_history WHERE content LIKE ? ORDER BY created_at DESC LIMIT ?",
        (f"%{query}%", limit),
    ).fetchall()
    conn.close()
    return [{"content": r[0], "created_at": r[1], "type": r[2]} for r in rows]


def get_last() -> dict | None:
    """Get the most recent clipboard entry."""
    entries = get_recent(1)
    return entries[0] if entries else None


# ---------------------------------------------------------------------------
# Clipboard polling (for agent_loop integration)
# ---------------------------------------------------------------------------

_last_hash: str | None = None


async def poll_clipboard() -> bool:
    """Check current clipboard and record if new. Returns True if new entry captured."""
    global _last_hash

    try:
        proc = await asyncio.create_subprocess_exec(
            "pbpaste",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        content = stdout.decode(errors="replace").strip()
    except Exception:
        return False

    if not content:
        return False

    content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
    if content_hash == _last_hash:
        return False

    _last_hash = content_hash
    return record_clipboard(content)


# ---------------------------------------------------------------------------
# Intent handler
# ---------------------------------------------------------------------------

async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle clipboard-related intents."""
    query = intent.get("query", "") or intent.get("user_query", "")

    if action == "clipboard_last":
        # Also poll current clipboard first
        await poll_clipboard()
        entry = get_last()
        if not entry:
            await ctx.reply("Clipboard history is empty.")
        else:
            content = entry["content"]
            if len(content) > 500:
                content = content[:500] + "..."
            await ctx.reply(
                f"📋 **Last Copied** ({entry['created_at']}):\n\n```\n{content}\n```"
            )
        return True

    elif action == "clipboard_history":
        await poll_clipboard()
        entries = get_recent(15)
        if not entries:
            await ctx.reply("No clipboard history recorded yet.")
        else:
            lines = [f"📋 **Clipboard History** ({len(entries)} entries):\n"]
            for i, e in enumerate(entries, 1):
                preview = e["content"][:80].replace("\n", " ")
                if len(e["content"]) > 80:
                    preview += "..."
                lines.append(f"  {i}. `{preview}` — {e['created_at']}")
            await ctx.reply("\n".join(lines))
        return True

    elif action == "clipboard_search":
        import re as _re
        search_term = _re.sub(
            r"\b(?:search|find|look\s+for)\b", "", query, flags=_re.IGNORECASE
        )
        search_term = _re.sub(
            r"\b(?:my|in|the|clipboard|history|for)\b", "", search_term, flags=_re.IGNORECASE
        )
        search_term = search_term.strip()
        if not search_term:
            await ctx.reply("What should I search for in clipboard history?")
            return True

        results = search_clipboard(search_term)
        if not results:
            await ctx.reply(f"No clipboard entries matching \"{search_term}\".")
        else:
            lines = [f"📋 Found {len(results)} clipboard entries matching \"{search_term}\":\n"]
            for e in results:
                preview = e["content"][:100].replace("\n", " ")
                if len(e["content"]) > 100:
                    preview += "..."
                lines.append(f"  • `{preview}` — {e['created_at']}")
            await ctx.reply("\n".join(lines))
        return True

    return False
