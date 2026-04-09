"""Unified communications dashboard — GitHub notifications + recent iMessages.

Fetches unread GitHub notifications and recent iMessage conversations in parallel,
presenting a combined view of what needs attention.

Auth:
- GitHub: Personal Access Token stored in keyring as 'github-pat'
  Setup: python3 -c "import keyring; keyring.set_password('khalil-assistant', 'github-pat', 'ghp_...')"
- iMessage: Requires Full Disk Access for the terminal running Khalil (macOS only)
"""

import asyncio
import logging
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from config import DB_PATH, TIMEZONE

log = logging.getLogger("khalil.actions.github_notifications_imessage_recent")

SKILL = {
    "name": "github_notifications_imessage_recent",
    "description": "Unified communications dashboard — GitHub notifications + recent iMessages",
    "category": "extension",
    "patterns": [
        (r"\b(?:comms|communications)\s*(?:dashboard)?", "comms_all"),
        (r"\bshow\s+(?:my\s+)?(?:notifications|comms)", "comms_all"),
        (r"\b(?:github\s+notifications?\s+and\s+(?:i?messages?|texts?))", "comms_all"),
        (r"\bwhat(?:'s| is)\s+(?:new|unread)\s+(?:in\s+)?(?:comms|messages)", "comms_all"),
    ],
    "actions": [
        {"type": "comms_all", "handler": "handle_comms", "description": "Show GitHub notifications + recent iMessages", "keywords": "comms communications notifications messages imessage github dashboard"},
    ],
    "examples": ["Show my comms", "What's new in notifications and messages?", "Communications dashboard"],
}

_tables_ensured = False


def ensure_tables(conn: sqlite3.Connection):
    """Create cache table for combined comms results. Called once at startup."""
    global _tables_ensured
    if _tables_ensured:
        return
    conn.execute("""
        CREATE TABLE IF NOT EXISTS comms_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            title TEXT NOT NULL,
            detail TEXT DEFAULT '',
            fetched_at TEXT NOT NULL
        )
    """)
    conn.commit()
    _tables_ensured = True


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


# --- Core sync functions (called via asyncio.to_thread) ---


def _cache_results(items: list[dict], source: str):
    """Cache fetched results for quick re-display."""
    conn = _get_conn()
    try:
        now = datetime.now(ZoneInfo(TIMEZONE)).isoformat()
        conn.execute("DELETE FROM comms_cache WHERE source = ?", (source,))
        for item in items[:50]:
            conn.execute(
                "INSERT INTO comms_cache (source, title, detail, fetched_at) VALUES (?, ?, ?, ?)",
                (source, item.get("title", ""), item.get("detail", ""), now),
            )
        conn.commit()
    finally:
        conn.close()


def _format_github_notifications(notifs: list[dict]) -> list[str]:
    """Format GitHub notifications for display."""
    if not notifs:
        return ["  No unread notifications."]
    lines = []
    for n in notifs[:20]:
        emoji = {"PullRequest": "\U0001f4cb", "Issue": "\U0001f41b"}.get(n.get("type", ""), "\U0001f4cc")
        repo = n.get("repo", "?")
        title = n.get("title", "?")
        reason = n.get("reason", "")
        line = f"  {emoji} {repo}: {title}"
        if reason:
            line += f" ({reason})"
        lines.append(line)
    if len(notifs) > 20:
        lines.append(f"  ...and {len(notifs) - 20} more")
    return lines


def _format_imessage(contacts: list[str], messages: list[dict]) -> list[str]:
    """Format iMessage recent contacts and latest messages for display."""
    if not contacts and not messages:
        return ["  No recent messages (check Full Disk Access)."]
    lines = []
    if contacts:
        for c in contacts[:15]:
            lines.append(f"  \u2022 {c}")
        if len(contacts) > 15:
            lines.append(f"  ...and {len(contacts) - 15} more")
    if messages:
        lines.append("")
        for m in messages[:10]:
            time_str = m.get("date", "")[:16] if m.get("date") else "?"
            sender = m.get("sender", "?")
            chat = m.get("chat_name", "")
            display = chat if chat and chat != sender else sender
            text = (m.get("text", "") or "(attachment)")[:120]
            lines.append(f"  [{time_str}] {display}: {text}")
    return lines


def _filter_by_keyword(items: list[dict], keyword: str, fields: list[str]) -> list[dict]:
    """Filter items using case-insensitive keyword match on specified fields."""
    kw_lower = keyword.lower()
    filtered = []
    for item in items:
        for field in fields:
            val = item.get(field, "")
            if val and kw_lower in str(val).lower():
                filtered.append(item)
                break
    return filtered


# --- Async wrappers ---


async def _fetch_github() -> list[dict]:
    """Fetch GitHub notifications, gracefully handle failures."""
    try:
        from actions.github_api import get_notifications  # lazy: optional dependency
        return await get_notifications(unread_only=True)
    except ImportError:
        log.warning("github_api module not available")
        return []
    except Exception as e:
        log.warning("Failed to fetch GitHub notifications: %s", e)
        return []


async def _fetch_imessage() -> tuple[list[str], list[dict]]:
    """Fetch recent iMessage contacts and messages, gracefully handle failures."""
    try:
        from actions.imessage import get_recent_contacts, get_recent_messages  # lazy: optional dependency
        contacts, messages = await asyncio.gather(
            get_recent_contacts(limit=15),
            get_recent_messages(limit=10),
        )
        return contacts, messages
    except ImportError:
        log.warning("imessage module not available")
        return [], []
    except Exception as e:
        log.warning("Failed to fetch iMessages: %s", e)
        return [], []


async def _fetch_all() -> dict:
    """Fetch both sources in parallel."""
    gh_notifs, (im_contacts, im_messages) = await asyncio.gather(
        _fetch_github(),
        _fetch_imessage(),
    )
    return {
        "github_notifications": gh_notifs,
        "imessage_contacts": im_contacts,
        "imessage_messages": im_messages,
    }


def _build_output(data: dict, keyword: str | None = None) -> str:
    """Build the combined output string, respecting Telegram's 4096 char limit."""
    now = datetime.now(ZoneInfo(TIMEZONE)).strftime("%H:%M")

    gh_notifs = data["github_notifications"]
    im_contacts = data["imessage_contacts"]
    im_messages = data["imessage_messages"]

    if keyword:
        gh_notifs = _filter_by_keyword(gh_notifs, keyword, ["title", "repo", "reason"])
        im_messages = _filter_by_keyword(im_messages, keyword, ["text", "sender", "chat_name"])
        kw_lower = keyword.lower()
        im_contacts = [c for c in im_contacts if kw_lower in c.lower()]

    sections: list[str] = []

    # GitHub section
    sections.append(f"\U0001f514 GitHub Notifications ({len(gh_notifs)})")
    sections.extend(_format_github_notifications(gh_notifs))

    # iMessage section
    sections.append("")
    sections.append("\U0001f4ac Recent iMessages")
    sections.extend(_format_imessage(im_contacts, im_messages))

    # Footer
    sections.append("")
    filter_note = f" | filter: '{keyword}'" if keyword else ""
    sections.append(f"\U0001f4e1 Comms dashboard @ {now}{filter_note}")

    output = "\n".join(sections)
    if len(output) > 4000:
        output = output[:3990] + "\n\u2026(truncated)"
    return output


# --- Telegram command handler ---


async def handle_comms(update, context):
    """Handle /comms command.

    Usage:
        /comms              — show GitHub notifications + recent iMessages
        /comms github       — GitHub notifications only
        /comms imessage     — recent iMessages only
        /comms filter <kw>  — filter both sources by keyword
    """
    args = context.args or []
    subcommand = args[0].lower() if args else "all"

    try:
        if subcommand == "github":
            gh_notifs = await _fetch_github()
            lines = [f"\U0001f514 GitHub Notifications ({len(gh_notifs)})"]
            lines.extend(_format_github_notifications(gh_notifs))
            await update.message.reply_text("\n".join(lines))

        elif subcommand in ("imessage", "messages", "texts"):
            im_contacts, im_messages = await _fetch_imessage()
            lines = ["\U0001f4ac Recent iMessages"]
            lines.extend(_format_imessage(im_contacts, im_messages))
            await update.message.reply_text("\n".join(lines))

        elif subcommand == "filter":
            keyword = " ".join(args[1:]) if len(args) > 1 else ""
            if not keyword:
                await update.message.reply_text("Usage: /comms filter <keyword>")
                return
            data = await _fetch_all()
            output = _build_output(data, keyword=keyword)
            await update.message.reply_text(output)

        else:
            data = await _fetch_all()
            output = _build_output(data)

            # Cache GitHub results in background
            cache_items = [
                {"title": n.get("title", ""), "detail": n.get("repo", "")}
                for n in data["github_notifications"]
            ]
            asyncio.create_task(
                asyncio.to_thread(_cache_results, cache_items, "github")
            )

            await update.message.reply_text(output)

    except Exception as e:
        log.error("Comms dashboard failed: %s", e, exc_info=True)
        await update.message.reply_text(f"\u274c Comms dashboard error: {e}")
