"""Combined Anki dashboard — create flashcards and see review status in one command.

Merges the frequently co-used 'anki_create' and 'anki_status' actions into a
single /anki command with subcommands.  Creating a card automatically shows
updated deck stats afterward (the pattern behind 26 co-uses).

Requires Anki desktop running with AnkiConnect plugin (code: 2055492159).
No external API token needed — AnkiConnect listens on localhost:8765.
"""

import asyncio
import logging
import re
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from config import DB_PATH, TIMEZONE

log = logging.getLogger("khalil.actions.anki_create_anki_status")

_tables_ensured = False

SKILL = {
    "name": "anki_create_anki_status",
    "description": "Create Anki flashcards and view review status in a single command",
    "category": "learning",
    "patterns": [
        (r"\b(?:create|add|new)\s+(?:anki\s+)?(?:flash)?card\b.*\b(?:status|stats?|due)\b", "anki_create_anki_status"),
        (r"\banki\s+(?:create|add)\b", "anki_create_anki_status"),
        (r"\banki\s+(?:dashboard|overview)\b", "anki_create_anki_status"),
        (r"\b(?:flash)?card\s+and\s+(?:status|stats)\b", "anki_create_anki_status"),
    ],
    "actions": [
        {
            "type": "anki_create_anki_status",
            "handler": "handle_anki",
            "description": "Create flashcards and view review status in one command",
            "keywords": "anki flashcard create add status stats due cards review dashboard",
        },
    ],
    "examples": [
        "Create an Anki card and show me my stats",
        "Anki dashboard",
        "/anki create Q: What is TCP? A: Transmission Control Protocol",
        "/anki status",
    ],
}


def ensure_tables(conn: sqlite3.Connection):
    """Create tables for logging combined anki operations. Called once at startup."""
    global _tables_ensured
    if _tables_ensured:
        return
    conn.execute(
        """CREATE TABLE IF NOT EXISTS anki_dashboard_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            detail TEXT,
            deck TEXT,
            total_due INTEGER,
            created_at TEXT NOT NULL
        )"""
    )
    conn.commit()
    _tables_ensured = True


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _log_action(action: str, detail: str | None = None,
                deck: str | None = None, total_due: int | None = None):
    """Log a dashboard action to the DB."""
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz).isoformat()
    conn = _get_conn()
    try:
        ensure_tables(conn)
        conn.execute(
            "INSERT INTO anki_dashboard_log (action, detail, deck, total_due, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (action, detail, deck, total_due, now),
        )
        conn.commit()
    finally:
        conn.close()


# --- Core functions — imported from existing anki_connect module ---

from actions.anki_connect import (
    create_card,
    get_deck_names,
    get_deck_stats,
    is_available,
    search_cards,
)


def _format_status(stats: dict) -> str:
    """Format deck stats for Telegram."""
    total_due = sum(s["total_due"] for s in stats.values())
    lines = [f"\U0001f4da Anki — {total_due} cards due", ""]
    for deck, s in stats.items():
        if s["total_due"] > 0:
            lines.append(
                f"  \u2022 {deck}: {s['new']} new, "
                f"{s['learn']} learning, {s['review']} review"
            )
    if total_due == 0:
        lines.append("  All caught up!")
    return "\n".join(lines)


def _parse_card(text: str) -> tuple[str | None, str | None, str | None]:
    """Parse card creation input.

    Returns (front, back, deck) or (None, None, None) if unparseable.
    Accepted formats:
      Q: What is X? A: It is Y
      front: What is X? back: It is Y
      question: ... answer: ...
    Optional: deck:MyDeck anywhere in the string.
    """
    m = re.search(
        r"(?:Q|front|question):\s*(.+?)\s*(?:A|back|answer):\s*(.+)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return None, None, None

    front = m.group(1).strip()
    back = m.group(2).strip()

    deck_match = re.search(r"\bdeck:\s*(\S+)", back, re.IGNORECASE)
    if deck_match:
        back = back[: deck_match.start()].strip()
    if not deck_match:
        deck_match = re.search(r"\bdeck:\s*(\S+)", text, re.IGNORECASE)

    deck = deck_match.group(1) if deck_match else None
    return front, back, deck


def _format_history(records: list[dict]) -> str:
    """Format dashboard history for Telegram."""
    if not records:
        return "No Anki dashboard history yet."
    lines = ["\U0001f4ca Recent Anki Activity", ""]
    for r in records:
        ts = r["created_at"][:16]
        due_str = f"  due={r['total_due']}" if r["total_due"] is not None else ""
        deck_str = f"  [{r['deck']}]" if r["deck"] else ""
        detail = f"  {r['detail'][:60]}" if r["detail"] else ""
        lines.append(f"  {ts}  {r['action']}{deck_str}{due_str}{detail}")
    return "\n".join(lines)


async def _get_history(limit: int = 10) -> list[dict]:
    """Fetch recent dashboard history."""
    def _query():
        conn = _get_conn()
        try:
            ensure_tables(conn)
            rows = conn.execute(
                "SELECT action, detail, deck, total_due, created_at "
                "FROM anki_dashboard_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    return await asyncio.to_thread(_query)


# --- Telegram handler ---


async def handle_anki(update, context):
    """Handle /anki command — combined Anki create + status dashboard.

    Subcommands:
        /anki                       — full status (all decks)
        /anki create Q: ... A: ...  — create card, then show updated status
        /anki preview Q: ... A: ... — dry-run: show what would be created
        /anki status                — detailed deck stats
        /anki history [N]           — recent dashboard activity (default 10, max 50)
    """
    args = context.args or []
    sub = args[0].lower() if args else ""
    rest = " ".join(args[1:]) if len(args) > 1 else ""

    # Check AnkiConnect availability upfront
    if not await is_available():
        await update.message.reply_text(
            "\u274c Anki not running or AnkiConnect plugin not installed.\n"
            "Install plugin code 2055492159 in Anki."
        )
        return

    try:
        if sub in ("preview", "create"):
            front, back, deck = _parse_card(rest)
            if not front:
                await update.message.reply_text(
                    f"Usage: /anki {sub} Q: What is X? A: It is Y [deck:MyDeck]"
                )
                return
            decks = await get_deck_names()
            if deck and deck not in decks:
                await update.message.reply_text(
                    f"\u26a0\ufe0f Deck \"{deck}\" not found.\n"
                    f"Available: {', '.join(decks)}"
                )
                return
            target_deck = deck or (decks[0] if decks else "Default")

            if sub == "preview":
                await update.message.reply_text(
                    f"\U0001f50d Preview (not created yet):\n\n"
                    f"  Deck: {target_deck}\n"
                    f"  Q: {front}\n  A: {back}\n\n"
                    f"Run /anki create ... to actually create it."
                )
                return

            note_id = await create_card(target_deck, front, back)
            # Fetch updated stats immediately after creation
            stats = await get_deck_stats()
            total_due = sum(s["total_due"] for s in stats.values())

            status_text = _format_status(stats)
            await update.message.reply_text(
                f"\u2705 Card created in {target_deck}\n"
                f"  Q: {front}\n"
                f"  A: {back}\n\n"
                f"{status_text}"
            )
            await asyncio.to_thread(
                _log_action, "create", front[:100], target_deck, total_due
            )

        elif sub == "history":
            limit = 10
            if rest:
                try:
                    limit = min(int(rest.split()[0]), 50)
                except ValueError:
                    await update.message.reply_text("Usage: /anki history [N]")
                    return
            records = await _get_history(limit)
            await update.message.reply_text(_format_history(records))

        elif sub in ("status", "stats"):
            stats = await get_deck_stats()
            total_due = sum(s["total_due"] for s in stats.values())
            text = _format_status(stats)
            await update.message.reply_text(text)
            await asyncio.to_thread(_log_action, "status", None, None, total_due)

        else:
            # Default: show full status
            stats = await get_deck_stats()
            total_due = sum(s["total_due"] for s in stats.values())
            text = _format_status(stats)
            help_hint = (
                "\n\nCommands:\n"
                "  /anki create Q: ... A: ...\n"
                "  /anki preview Q: ... A: ...\n"
                "  /anki status\n"
                "  /anki history [N]"
            )
            await update.message.reply_text(text + help_hint)
            await asyncio.to_thread(_log_action, "status", None, None, total_due)

    except ConnectionError as e:
        await update.message.reply_text(f"\u274c {e}")
    except Exception as e:
        log.exception("Anki dashboard error")
        await update.message.reply_text(f"\u274c Error: {e}")
