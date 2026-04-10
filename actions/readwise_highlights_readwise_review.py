"""Combined Readwise dashboard — highlights + daily review in one /readwise command.

API: Readwise REST API v2. Auth: API token in keyring.
Setup: keyring.set_password('khalil-assistant', 'readwise-api-token', 'YOUR_TOKEN')
"""

import asyncio
import logging
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from config import DB_PATH, KEYRING_SERVICE, TIMEZONE

log = logging.getLogger("khalil.actions.readwise_highlights_readwise_review")

# Re-use core functions from existing readwise module — no reimplementation
from actions.readwise import get_highlights, get_daily_review, search_highlights, get_books

SKILL = {
    "name": "readwise_highlights_readwise_review",
    "description": "Combined Readwise dashboard — highlights and daily review in one command",
    "category": "reading",
    "patterns": [
        (r"\breadwise\b.*\b(?:review|highlights?)\b", "readwise_highlights_readwise_review"),
        (r"\b(?:daily\s+)?review\b.*\bhighlights?\b", "readwise_highlights_readwise_review"),
        (r"\breadwise\s+(?:dashboard|summary|overview)\b", "readwise_highlights_readwise_review"),
        (r"\bmy\s+reading\s+(?:dashboard|summary)\b", "readwise_highlights_readwise_review"),
        (r"\breadwise\s+search\b", "readwise_search"),
    ],
    "actions": [
        {
            "type": "readwise_highlights_readwise_review",
            "handler": "handle_readwise",
            "description": "Combined Readwise dashboard — highlights + daily review",
            "keywords": "readwise highlights review daily reading dashboard summary",
        },
        {
            "type": "readwise_search",
            "handler": "handle_readwise",
            "description": "Search Readwise highlights by keyword",
            "keywords": "readwise search find highlights query",
        },
    ],
    "examples": [
        "Show my Readwise dashboard",
        "Readwise highlights and review",
        "Search readwise for leadership",
        "My reading summary",
    ],
}

_tables_ensured = False


def ensure_tables(conn: sqlite3.Connection):
    """Create tables for logging Readwise dashboard checks. Called once at startup."""
    global _tables_ensured
    if _tables_ensured:
        return
    conn.execute(
        """CREATE TABLE IF NOT EXISTS readwise_dashboard_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checked_at TEXT NOT NULL,
            highlight_count INTEGER,
            review_count INTEGER,
            search_query TEXT
        )"""
    )
    conn.commit()
    _tables_ensured = True


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _log_check(highlight_count: int = 0, review_count: int = 0, search_query: str | None = None):
    """Log a dashboard check to the DB."""
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz).isoformat()
    conn = _get_conn()
    try:
        ensure_tables(conn)
        conn.execute(
            "INSERT INTO readwise_dashboard_log (checked_at, highlight_count, review_count, search_query) "
            "VALUES (?, ?, ?, ?)",
            (now, highlight_count, review_count, search_query),
        )
        conn.commit()
    finally:
        conn.close()


def _get_history(limit: int = 10) -> list[dict]:
    """Fetch recent dashboard check history."""
    conn = _get_conn()
    try:
        ensure_tables(conn)
        rows = conn.execute(
            "SELECT checked_at, highlight_count, review_count, search_query "
            "FROM readwise_dashboard_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --- Formatting ---


def _format_highlights(highlights: list[dict]) -> str:
    if not highlights:
        return "No recent highlights."
    lines = [f"\U0001f4da Recent Highlights ({len(highlights)})\n"]
    for h in highlights:
        lines.append(f'  \u2022 "{h.get("text", "")[:120]}"')
        title, author = h.get("title"), h.get("author")
        if title:
            lines.append(f"    \u2014 {title}" + (f" by {author}" if author else ""))
    return "\n".join(lines)


def _format_review(highlights: list[dict]) -> str:
    if not highlights:
        return "No daily review highlights today."
    lines = [f"\U0001f4d6 Daily Review ({len(highlights)} highlights)\n"]
    for h in highlights:
        lines.append(f'  \u2022 "{h.get("text", "")[:120]}"')
        if h.get("title"):
            lines.append(f"    \u2014 {h['title']}")
    return "\n".join(lines)


def _format_search(query: str, results: list[dict]) -> str:
    if not results:
        return f'No highlights matching "{query}".'
    lines = [f'\U0001f50d Search: "{query}" ({len(results)} results)\n']
    for h in results[:20]:
        lines.append(f'  \u2022 "{h.get("text", "")[:120]}"')
        if h.get("title"):
            lines.append(f"    \u2014 {h['title']}")
    if len(results) > 20:
        lines.append(f"  ... and {len(results) - 20} more")
    return "\n".join(lines)


def _format_dashboard(highlights: list[dict], review: list[dict]) -> str:
    return "\n\n".join([_format_review(review), _format_highlights(highlights)])


def _format_history(records: list[dict]) -> str:
    if not records:
        return "No Readwise check history yet."
    lines = ["\U0001f4ca Recent Readwise Checks\n"]
    for r in records:
        ts = r["checked_at"][:16]
        parts = [f"  {ts}"]
        if r["highlight_count"]:
            parts.append(f"highlights={r['highlight_count']}")
        if r["review_count"]:
            parts.append(f"review={r['review_count']}")
        if r["search_query"]:
            parts.append(f'search="{r["search_query"]}"')
        lines.append("  ".join(parts))
    return "\n".join(lines)


def _truncate(text: str, limit: int = 4096) -> str:
    return text if len(text) <= limit else text[: limit - 4] + "\n..."


# --- Telegram handler ---


async def handle_readwise(update, context):
    """Handle /readwise — subcommands: highlights [N], review, search <q>, books, history [N]."""
    args = context.args or []
    sub = args[0].lower() if args else ""

    try:
        if sub == "highlights":
            limit = 20
            if len(args) > 1:
                try:
                    limit = min(int(args[1]), 50)
                except ValueError:
                    pass
            highlights = await get_highlights(limit=limit)
            await asyncio.to_thread(_log_check, highlight_count=len(highlights))
            await update.message.reply_text(_truncate(_format_highlights(highlights)))

        elif sub == "review":
            review = await get_daily_review()
            await asyncio.to_thread(_log_check, review_count=len(review))
            await update.message.reply_text(_truncate(_format_review(review)))

        elif sub == "search":
            query = " ".join(args[1:])
            if not query:
                await update.message.reply_text("Usage: /readwise search <query>")
                return
            results = await search_highlights(query)
            await asyncio.to_thread(_log_check, highlight_count=len(results), search_query=query)
            await update.message.reply_text(_truncate(_format_search(query, results)))

        elif sub == "books":
            await update.message.reply_text("Fetching library...")
            books = await get_books()
            if not books:
                await update.message.reply_text("No books found in Readwise.")
                return
            lines = [f"\U0001f4da Library ({len(books)} books)\n"]
            for b in books[:30]:
                hl = b.get("num_highlights", 0)
                cat = b.get("category", "")
                author = b.get("author", "")
                entry = f"  \u2022 {b['title']}"
                if author:
                    entry += f" — {author}"
                entry += f" ({hl} highlights"
                if cat:
                    entry += f", {cat}"
                entry += ")"
                lines.append(entry)
            if len(books) > 30:
                lines.append(f"  ... and {len(books) - 30} more")
            await update.message.reply_text(_truncate("\n".join(lines)))

        elif sub == "history":
            limit = 10
            if len(args) > 1:
                try:
                    limit = min(int(args[1]), 50)
                except ValueError:
                    await update.message.reply_text("Usage: /readwise history [N]")
                    return
            records = await asyncio.to_thread(_get_history, limit)
            await update.message.reply_text(_format_history(records))

        else:
            # Default: combined dashboard (review + highlights)
            await update.message.reply_text("Fetching Readwise dashboard...")
            highlights, review = await asyncio.gather(
                get_highlights(limit=10),
                get_daily_review(),
            )
            await asyncio.to_thread(
                _log_check, highlight_count=len(highlights), review_count=len(review)
            )
            await update.message.reply_text(_truncate(_format_dashboard(highlights, review)))

    except ValueError as e:
        await update.message.reply_text(f"\u26a0\ufe0f {e}")
    except Exception as e:
        log.exception("Readwise dashboard error")
        await update.message.reply_text(f"\u274c Readwise error: {e}")


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle natural language intents. Returns True if handled."""
    try:
        if action == "readwise_highlights_readwise_review":
            highlights, review = await asyncio.gather(
                get_highlights(limit=10),
                get_daily_review(),
            )
            await asyncio.to_thread(
                _log_check, highlight_count=len(highlights), review_count=len(review)
            )
            await ctx.reply(_truncate(_format_dashboard(highlights, review)))
            return True
        elif action == "readwise_search":
            query = intent.get("query", intent.get("text", ""))
            if not query:
                await ctx.reply("What would you like to search for in Readwise?")
                return True
            results = await search_highlights(query)
            await asyncio.to_thread(
                _log_check, highlight_count=len(results), search_query=query
            )
            await ctx.reply(_truncate(_format_search(query, results)))
            return True
    except ValueError as e:
        await ctx.reply(f"\u26a0\ufe0f {e}")
        return True
    except Exception as e:
        await ctx.reply(f"\u274c Readwise error: {e}")
        return True
    return False
