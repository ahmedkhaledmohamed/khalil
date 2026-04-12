"""Combined macOS dashboard — running apps + system info in one /sysapps command.

No external API or tokens required — reads local macOS state via actions.macos.
"""

import asyncio
import logging
import re
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from config import DB_PATH, TIMEZONE

log = logging.getLogger("khalil.actions.macos_apps_system_info")

_tables_ensured = False

SKILL = {
    "name": "macos_apps_system_info",
    "description": "Combined macOS dashboard — running apps and system info in one view",
    "category": "system",
    "patterns": [
        (r"\b(?:system|mac)\s+(?:dashboard|overview|summary)\b", "macos_apps_system_info"),
        (r"\bhow'?s?\s+my\s+(?:mac|machine|computer)\b", "macos_apps_system_info"),
        (r"\bmachine\s+(?:status|health|state)\b", "macos_apps_system_info"),
        (r"\b(?:apps?\s+and\s+system|system\s+and\s+apps?)\b", "macos_apps_system_info"),
    ],
    "actions": [
        {
            "type": "macos_apps_system_info",
            "handler": "handle_intent",
            "description": "Full macOS dashboard — running apps + battery, CPU, memory, storage",
            "keywords": "system dashboard overview apps running battery cpu memory storage mac machine",
        },
    ],
    "examples": [
        "How's my Mac doing?",
        "Machine status",
        "System dashboard",
    ],
}


def ensure_tables(conn: sqlite3.Connection):
    """Create tables for logging dashboard snapshots. Called once at startup."""
    global _tables_ensured
    if _tables_ensured:
        return
    conn.execute(
        """CREATE TABLE IF NOT EXISTS macos_dashboard_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checked_at TEXT NOT NULL,
            app_count INTEGER,
            battery_percent INTEGER,
            storage_available TEXT,
            memory_total_gb REAL
        )"""
    )
    conn.commit()
    _tables_ensured = True


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


# --- Core functions (reuse from actions.macos) ---


async def _fetch_dashboard() -> tuple[list[str], dict]:
    """Fetch running apps and system info concurrently."""
    from actions.macos import get_running_apps, get_system_info

    apps, info = await asyncio.gather(
        get_running_apps(),
        get_system_info(),
    )
    return apps, info


def _log_snapshot(apps: list[str], info: dict):
    """Log a dashboard snapshot to the DB."""
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz).isoformat()
    conn = _get_conn()
    try:
        ensure_tables(conn)
        conn.execute(
            "INSERT INTO macos_dashboard_log "
            "(checked_at, app_count, battery_percent, storage_available, memory_total_gb) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                now,
                len(apps),
                info.get("battery_percent"),
                info.get("storage_available"),
                info.get("memory_total_gb"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _get_history(limit: int = 10) -> list[dict]:
    """Fetch recent dashboard snapshot history."""
    conn = _get_conn()
    try:
        ensure_tables(conn)
        rows = conn.execute(
            "SELECT checked_at, app_count, battery_percent, storage_available, memory_total_gb "
            "FROM macos_dashboard_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _find_apps(apps: list[str], pattern: str) -> list[str]:
    """Filter apps by word-boundary regex pattern."""
    try:
        pat = r"\b" + pattern + r"\b"
        return [a for a in apps if re.search(pat, a, re.IGNORECASE)]
    except re.error:
        # Fall back to literal match if pattern is invalid regex
        pat = re.escape(pattern)
        return [a for a in apps if re.search(pat, a, re.IGNORECASE)]


# --- Formatting ---


def _format_dashboard(apps: list[str], info: dict) -> str:
    """Format combined apps + system info for Telegram."""
    lines = ["\U0001f4bb macOS Dashboard", ""]

    # System info
    lines.append("\u2699\ufe0f System")
    if "battery_percent" in info:
        charge = "\u26a1 charging" if info.get("battery_charging") else "\U0001f50b"
        lines.append(f"  Battery: {info['battery_percent']}% {charge}")
    if "storage_available" in info:
        lines.append(
            f"  Storage: {info['storage_used']} used / "
            f"{info['storage_total']} total ({info['storage_available']} free)"
        )
    if "memory_total_gb" in info:
        lines.append(f"  Memory: {info['memory_total_gb']} GB")
    if "cpu_brand" in info:
        lines.append(f"  CPU: {info['cpu_brand']}")

    # Running apps
    lines.append("")
    if apps:
        sorted_apps = sorted(apps)
        lines.append(f"\U0001f5a5 Running Apps ({len(apps)})")
        for a in sorted_apps[:30]:
            lines.append(f"  \u2022 {a}")
        if len(apps) > 30:
            lines.append(f"  ...and {len(apps) - 30} more")
    else:
        lines.append("\u26a0\ufe0f Could not retrieve running apps.")

    return "\n".join(lines)


def _format_apps_only(apps: list[str]) -> str:
    """Format running apps list only."""
    if not apps:
        return "\u26a0\ufe0f Could not retrieve running apps."
    sorted_apps = sorted(apps)
    lines = [f"\U0001f5a5 Running Apps ({len(apps)})", ""]
    for a in sorted_apps:
        lines.append(f"  \u2022 {a}")
    return "\n".join(lines)


def _format_info_only(info: dict) -> str:
    """Format system info only."""
    lines = ["\u2699\ufe0f System Info", ""]
    if "battery_percent" in info:
        charge = "\u26a1 charging" if info.get("battery_charging") else "\U0001f50b"
        lines.append(f"  Battery: {info['battery_percent']}% {charge}")
    if "storage_available" in info:
        lines.append(
            f"  Storage: {info['storage_used']} used / "
            f"{info['storage_total']} total ({info['storage_available']} free)"
        )
    if "memory_total_gb" in info:
        lines.append(f"  Memory: {info['memory_total_gb']} GB")
    if "cpu_brand" in info:
        lines.append(f"  CPU: {info['cpu_brand']}")
    if len(lines) <= 2:
        return "\u26a0\ufe0f Could not retrieve system info."
    return "\n".join(lines)


def _format_history(records: list[dict]) -> str:
    """Format dashboard history for Telegram."""
    if not records:
        return "No dashboard history yet."
    lines = ["\U0001f4ca Recent Snapshots", ""]
    for r in records:
        ts = r["checked_at"][:16]
        batt = f"  batt={r['battery_percent']}%" if r["battery_percent"] is not None else ""
        lines.append(f"  {ts}  apps={r['app_count']}{batt}  free={r['storage_available'] or '?'}")
    return "\n".join(lines)


# --- Telegram handler ---


async def handle_sysapps(update, context):
    """Handle /sysapps — full dashboard, or: apps, info, find <X>, history [N]."""
    args = context.args or []
    sub = args[0].lower() if args else ""

    try:
        if sub == "apps":
            from actions.macos import get_running_apps

            apps = await get_running_apps()
            text = _format_apps_only(apps)
            if len(text) > 4096:
                text = text[:4090] + "\n..."
            await update.message.reply_text(text)

        elif sub == "info":
            from actions.macos import get_system_info

            info = await get_system_info()
            await update.message.reply_text(_format_info_only(info))

        elif sub == "find":
            if len(args) < 2:
                await update.message.reply_text("Usage: /sysapps find <pattern>")
                return
            pattern = " ".join(args[1:])
            from actions.macos import get_running_apps

            apps = await get_running_apps()
            matched = _find_apps(apps, pattern)
            if matched:
                lines = [f"\U0001f50d Apps matching \"{pattern}\" ({len(matched)}):"]
                for a in sorted(matched):
                    lines.append(f"  \u2022 {a}")
                await update.message.reply_text("\n".join(lines))
            else:
                await update.message.reply_text(f"\U0001f50d No running apps match \"{pattern}\".")

        elif sub == "history":
            limit = 10
            if len(args) > 1:
                try:
                    limit = min(int(args[1]), 50)
                except ValueError:
                    await update.message.reply_text("Usage: /sysapps history [N]")
                    return
            records = await asyncio.to_thread(_get_history, limit)
            await update.message.reply_text(_format_history(records))

        else:
            # Default: full dashboard
            apps, info = await _fetch_dashboard()
            await asyncio.to_thread(_log_snapshot, apps, info)
            text = _format_dashboard(apps, info)
            if len(text) > 4096:
                text = text[:4090] + "\n..."
            await update.message.reply_text(text)

    except Exception as e:
        log.exception("macOS dashboard error")
        await update.message.reply_text(f"\u274c Error: {e}")


# --- NL intent handler ---


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle natural language intent. Returns True if handled."""
    if action != "macos_apps_system_info":
        return False

    try:
        apps, info = await _fetch_dashboard()
        await asyncio.to_thread(_log_snapshot, apps, info)
        text = _format_dashboard(apps, info)
        if len(text) > 4096:
            text = text[:4090] + "\n..."
        await ctx.reply(text)
        return True
    except Exception as e:
        from resilience import format_user_error

        await ctx.reply(format_user_error(e, skill_name="macOS Dashboard"))
        return True
