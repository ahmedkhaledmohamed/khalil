"""Read-only access to the macOS iMessage database (~/Library/Messages/chat.db).

Requires Full Disk Access for the terminal running Khalil.
All public functions are async — sync SQLite calls run in asyncio.to_thread().
"""

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from config import TIMEZONE

log = logging.getLogger("khalil.actions.imessage")

CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"
APPLE_EPOCH = datetime(2001, 1, 1)


def _apple_ts_to_dt(ns: int | None) -> datetime | None:
    """Convert Apple CoreData timestamp (nanoseconds since 2001-01-01) to datetime."""
    if not ns:
        return None
    try:
        tz = ZoneInfo(TIMEZONE)
        return (APPLE_EPOCH + timedelta(seconds=ns / 1e9)).replace(tzinfo=tz)
    except (OverflowError, ValueError):
        return None


def _get_conn() -> sqlite3.Connection | None:
    """Open a read-only connection to chat.db, or None on failure."""
    if not CHAT_DB.exists():
        log.warning("chat.db not found — iMessage not available")
        return None
    try:
        conn = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.OperationalError as e:
        log.warning("Cannot open chat.db (grant Full Disk Access): %s", e)
        return None


def _row_to_dict(row: sqlite3.Row) -> dict:
    dt = _apple_ts_to_dt(row["date"])
    return {
        "sender": "me" if row["is_from_me"] else (row["handle_id"] or "unknown"),
        "text": row["text"] or "",
        "date": dt.isoformat() if dt else "",
        "is_from_me": bool(row["is_from_me"]),
        "chat_name": row["display_name"] or row["chat_identifier"] or "",
    }


# --- Sync implementations ---

_MSG_QUERY = """
    SELECT m.text, m.date, m.is_from_me,
           h.id AS handle_id,
           c.chat_identifier, c.display_name
    FROM message m
    LEFT JOIN handle h ON m.handle_id = h.ROWID
    LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
    LEFT JOIN chat c ON c.ROWID = cmj.chat_id
"""


def _get_recent_sync(contact: str | None, limit: int) -> list[dict]:
    conn = _get_conn()
    if not conn:
        return []
    try:
        if contact:
            sql = _MSG_QUERY + " WHERE h.id LIKE ? ORDER BY m.date DESC LIMIT ?"
            rows = conn.execute(sql, (f"%{contact}%", limit)).fetchall()
        else:
            sql = _MSG_QUERY + " ORDER BY m.date DESC LIMIT ?"
            rows = conn.execute(sql, (limit,)).fetchall()
        return [_row_to_dict(r) for r in rows]
    except sqlite3.OperationalError as e:
        log.warning("iMessage query failed: %s", e)
        return []
    finally:
        conn.close()


def _search_sync(query: str, limit: int) -> list[dict]:
    conn = _get_conn()
    if not conn:
        return []
    try:
        sql = _MSG_QUERY + " WHERE m.text LIKE ? ORDER BY m.date DESC LIMIT ?"
        rows = conn.execute(sql, (f"%{query}%", limit)).fetchall()
        return [_row_to_dict(r) for r in rows]
    except sqlite3.OperationalError as e:
        log.warning("iMessage search failed: %s", e)
        return []
    finally:
        conn.close()


def _recent_contacts_sync(limit: int) -> list[str]:
    conn = _get_conn()
    if not conn:
        return []
    try:
        sql = """
            SELECT h.id, MAX(m.date) AS last_date
            FROM message m
            JOIN handle h ON m.handle_id = h.ROWID
            WHERE m.is_from_me = 0
            GROUP BY h.id
            ORDER BY last_date DESC
            LIMIT ?
        """
        rows = conn.execute(sql, (limit,)).fetchall()
        return [r["id"] for r in rows]
    except sqlite3.OperationalError as e:
        log.warning("iMessage contacts query failed: %s", e)
        return []
    finally:
        conn.close()


# --- Async wrappers ---

async def get_recent_messages(contact: str | None = None, limit: int = 20) -> list[dict]:
    """Get recent iMessages, optionally filtered by contact (fuzzy match on handle)."""
    return await asyncio.to_thread(_get_recent_sync, contact, limit)


async def search_messages(query: str, limit: int = 10) -> list[dict]:
    """Search iMessage text using LIKE matching."""
    return await asyncio.to_thread(_search_sync, query, limit)


async def get_recent_contacts(limit: int = 10) -> list[str]:
    """Return phone numbers/emails of recent message contacts."""
    return await asyncio.to_thread(_recent_contacts_sync, limit)


def format_messages(messages: list[dict]) -> str:
    """Format messages for Telegram display."""
    if not messages:
        return "No messages found."
    lines = []
    for m in messages:
        time_str = m["date"][:16] if m["date"] else "?"
        sender = m["sender"]
        text = m["text"][:200] or "(attachment)"
        lines.append(f"[{time_str}] {sender}: {text}")
    return "\n".join(lines)
