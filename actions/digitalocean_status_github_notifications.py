"""Infrastructure & dev dashboard — DigitalOcean droplet status + GitHub notifications.

Auth:
- DigitalOcean: keyring.set_password('khalil-assistant', 'digitalocean-api-token', 'dop_v1_...')
- GitHub: keyring.set_password('khalil-assistant', 'github-pat', 'ghp_...')
"""

import asyncio
import logging
import re
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from config import DB_PATH, KEYRING_SERVICE, TIMEZONE

log = logging.getLogger("khalil.actions.digitalocean_status_github_notifications")

SKILL = {
    "name": "digitalocean_status_github_notifications",
    "description": "Infrastructure & dev dashboard — DigitalOcean status + GitHub notifications",
    "category": "extension",
    "patterns": [
        (r"\b(?:infra(?:structure)?|ops)\s*(?:dashboard|status|check)\b", "infra_dashboard"),
        (r"\bdigitalocean\b.*\bgithub\b", "infra_dashboard"),
        (r"\bgithub\b.*\bdigitalocean\b", "infra_dashboard"),
        (r"\b(?:server|droplet)s?\s+(?:and|&)\s+(?:notifications?|github)\b", "infra_dashboard"),
        (r"\bdev\s*ops\s*(?:dashboard|status|check)\b", "infra_dashboard"),
    ],
    "actions": [
        {
            "type": "infra_dashboard",
            "handler": "handle_infra",
            "description": "Show DigitalOcean droplet status + GitHub notifications",
            "keywords": "infra infrastructure devops dashboard digitalocean droplet server github notifications status",
        },
    ],
    "examples": ["Show my infra dashboard", "DigitalOcean and GitHub status", "DevOps check"],
}

_tables_ensured = False


def ensure_tables(conn: sqlite3.Connection):
    """Create table for logging dashboard checks. Called once at startup."""
    global _tables_ensured
    if _tables_ensured:
        return
    conn.execute(
        """CREATE TABLE IF NOT EXISTS infra_dashboard_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checked_at TEXT NOT NULL,
            droplet_count INTEGER,
            active_count INTEGER,
            gh_notification_count INTEGER,
            notes TEXT
        )"""
    )
    conn.commit()
    _tables_ensured = True


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


# --- Core functions (reuse from existing action modules) ---


async def _fetch_droplets() -> list[dict]:
    """Fetch DigitalOcean droplets via existing action module."""
    try:
        from actions.digitalocean import get_droplets
        return await get_droplets()
    except Exception as e:
        log.warning("Failed to fetch droplets: %s", e)
        return []


async def _fetch_github_notifications() -> list[dict]:
    """Fetch GitHub notifications via existing action module."""
    try:
        from actions.github_api import get_notifications
        return await get_notifications(unread_only=True)
    except Exception as e:
        log.warning("Failed to fetch GitHub notifications: %s", e)
        return []


async def _fetch_all() -> dict:
    """Fetch both sources in parallel."""
    droplets, gh_notifs = await asyncio.gather(
        _fetch_droplets(), _fetch_github_notifications(),
    )
    return {"droplets": droplets, "github_notifications": gh_notifs}


def _log_check(droplets: list[dict], gh_notifs: list[dict]):
    """Log a dashboard check to the DB."""
    conn = _get_conn()
    try:
        ensure_tables(conn)
        now = datetime.now(ZoneInfo(TIMEZONE)).isoformat()
        active = sum(1 for d in droplets if d.get("status") == "active")
        conn.execute(
            "INSERT INTO infra_dashboard_log "
            "(checked_at, droplet_count, active_count, gh_notification_count) "
            "VALUES (?, ?, ?, ?)",
            (now, len(droplets), active, len(gh_notifs)),
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
            "SELECT checked_at, droplet_count, active_count, gh_notification_count "
            "FROM infra_dashboard_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --- Formatting ---


def _format_droplets(droplets: list[dict]) -> list[str]:
    """Format droplet status section."""
    if not droplets:
        return ["  No droplets found."]
    lines = []
    for d in droplets:
        icon = "\u2705" if d.get("status") == "active" else "\u26a0\ufe0f"
        lines.append(
            f"  {icon} {d.get('name', '?')} \u2014 {d.get('status', '?')} "
            f"({d.get('vcpus', '?')}vCPU, {d.get('memory', '?')}MB, {d.get('region', '?')})"
        )
        if d.get("ip"):
            lines.append(f"      IP: {d['ip']}")
    return lines


def _format_github(notifs: list[dict]) -> list[str]:
    """Format GitHub notifications section."""
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


def _filter_by_keyword(items: list[dict], keyword: str, fields: list[str]) -> list[dict]:
    """Filter items using word-boundary regex match on specified fields."""
    pat = r"\b" + re.escape(keyword) + r"\b"
    filtered = []
    for item in items:
        for field in fields:
            val = item.get(field, "")
            if val and re.search(pat, str(val), re.IGNORECASE):
                filtered.append(item)
                break
    return filtered


def _build_output(data: dict, keyword: str | None = None) -> str:
    """Build the combined output string, respecting Telegram's 4096 char limit."""
    now = datetime.now(ZoneInfo(TIMEZONE)).strftime("%H:%M")
    droplets = data["droplets"]
    gh_notifs = data["github_notifications"]

    if keyword:
        droplets = _filter_by_keyword(droplets, keyword, ["name", "status", "region"])
        gh_notifs = _filter_by_keyword(gh_notifs, keyword, ["title", "repo", "reason"])

    sections: list[str] = []

    active = sum(1 for d in droplets if d.get("status") == "active")
    sections.append(f"\U0001f4e1 Droplets ({active}/{len(droplets)} active)")
    sections.extend(_format_droplets(droplets))
    sections.append("")
    sections.append(f"\U0001f514 GitHub Notifications ({len(gh_notifs)})")
    sections.extend(_format_github(gh_notifs))
    sections.append("")
    filter_note = f" | filter: '{keyword}'" if keyword else ""
    sections.append(f"\U0001f6e0\ufe0f Infra dashboard @ {now}{filter_note}")

    output = "\n".join(sections)
    if len(output) > 4000:
        output = output[:3990] + "\n\u2026(truncated)"
    return output


def _format_history(records: list[dict]) -> str:
    """Format dashboard history for Telegram."""
    if not records:
        return "No infra dashboard check history yet."
    lines = ["\U0001f4ca Recent Infra Checks", ""]
    for r in records:
        ts = r["checked_at"][:16]
        lines.append(
            f"  {ts}  droplets={r['droplet_count']} "
            f"(active={r['active_count']})  "
            f"gh_notifs={r['gh_notification_count']}"
        )
    return "\n".join(lines)


# --- Telegram command handler ---


async def handle_infra(update, context):
    """Handle /infra command — combined infrastructure & dev dashboard.

    Subcommands:
        /infra              — full dashboard (droplets + GitHub notifications)
        /infra droplets     — DigitalOcean droplets only
        /infra github       — GitHub notifications only
        /infra filter <kw>  — filter both sources by keyword
        /infra history      — recent dashboard check history
        /infra history N    — last N checks (default 10, max 50)
    """
    args = context.args or []
    sub = args[0].lower() if args else ""

    try:
        if sub == "droplets":
            droplets = await _fetch_droplets()
            active = sum(1 for d in droplets if d.get("status") == "active")
            lines = [f"\U0001f4e1 Droplets ({active}/{len(droplets)} active)"]
            lines.extend(_format_droplets(droplets))
            await update.message.reply_text("\n".join(lines))

        elif sub == "github":
            gh_notifs = await _fetch_github_notifications()
            lines = [f"\U0001f514 GitHub Notifications ({len(gh_notifs)})"]
            lines.extend(_format_github(gh_notifs))
            await update.message.reply_text("\n".join(lines))

        elif sub == "filter":
            keyword = " ".join(args[1:]) if len(args) > 1 else ""
            if not keyword:
                await update.message.reply_text("Usage: /infra filter <keyword>")
                return
            data = await _fetch_all()
            output = _build_output(data, keyword=keyword)
            await update.message.reply_text(output)

        elif sub == "history":
            limit = 10
            if len(args) > 1:
                try:
                    limit = min(int(args[1]), 50)
                except ValueError:
                    await update.message.reply_text("Usage: /infra history [N]")
                    return
            records = await asyncio.to_thread(_get_history, limit)
            await update.message.reply_text(_format_history(records))

        else:
            # Default: full dashboard
            data = await _fetch_all()
            output = _build_output(data)

            # Log check in background
            asyncio.create_task(
                asyncio.to_thread(
                    _log_check, data["droplets"], data["github_notifications"]
                )
            )

            await update.message.reply_text(output)

    except Exception as e:
        log.error("Infra dashboard failed: %s", e, exc_info=True)
        await update.message.reply_text(f"\u274c Infra dashboard error: {e}")
