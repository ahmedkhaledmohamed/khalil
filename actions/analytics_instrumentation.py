"""Track OS permission outcomes for all flows.

Local analytics instrumentation for logging OS permission prompts
(notifications, location, contacts, camera, etc.) and their outcomes
(granted, denied, not_determined) across different app flows.

No external API required — all data stored locally in SQLite.
Useful for auditing permission request patterns and identifying
flows with low grant rates.
"""

import asyncio
import logging
import re
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from config import DB_PATH, TIMEZONE

log = logging.getLogger("khalil.actions.analytics_instrumentation")

_tables_ensured = False


def ensure_tables(conn: sqlite3.Connection):
    """Create tables. Called once at startup."""
    global _tables_ensured
    if _tables_ensured:
        return
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ubi_permission_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            flow_name TEXT NOT NULL,
            permission_type TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK(outcome IN ('granted', 'denied', 'dismissed', 'not_determined', 'restricted')),
            platform TEXT DEFAULT 'ios',
            app_version TEXT,
            notes TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_ubi_flow
        ON ubi_permission_events(flow_name, permission_type)
    """)
    conn.commit()
    _tables_ensured = True


VALID_OUTCOMES = {"granted", "denied", "dismissed", "not_determined", "restricted"}
VALID_PERMISSIONS = {
    "notifications", "location", "location_always", "contacts",
    "camera", "microphone", "photos", "tracking", "bluetooth",
    "health", "calendar", "reminders", "motion", "siri",
}


# --- Core sync functions (called via asyncio.to_thread) ---


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _log_event_sync(flow_name: str, permission_type: str, outcome: str,
                    platform: str = "ios", app_version: str | None = None,
                    notes: str | None = None) -> dict:
    """Record a single permission event."""
    conn = _get_conn()
    try:
        ensure_tables(conn)
        conn.execute(
            "INSERT INTO ubi_permission_events (flow_name, permission_type, outcome, platform, app_version, notes) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (flow_name, permission_type, outcome, platform, app_version, notes),
        )
        conn.commit()
        event_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        log.info("Logged permission event #%d: %s/%s → %s", event_id, flow_name, permission_type, outcome)
        return {"id": event_id, "flow_name": flow_name, "permission_type": permission_type, "outcome": outcome}
    finally:
        conn.close()


def _query_events_sync(flow_name: str | None = None, permission_type: str | None = None,
                       limit: int = 50) -> list[dict]:
    """Query permission events with optional filters."""
    conn = _get_conn()
    try:
        ensure_tables(conn)
        clauses = []
        params: list = []
        if flow_name:
            clauses.append("flow_name = ?")
            params.append(flow_name)
        if permission_type:
            clauses.append("permission_type = ?")
            params.append(permission_type)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT id, flow_name, permission_type, outcome, platform, app_version, notes, created_at "
            f"FROM ubi_permission_events {where} ORDER BY created_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _summary_sync(flow_name: str | None = None) -> list[dict]:
    """Aggregate grant rates by flow and permission type."""
    conn = _get_conn()
    try:
        ensure_tables(conn)
        params: list = []
        where = ""
        if flow_name:
            where = "WHERE flow_name = ?"
            params.append(flow_name)

        rows = conn.execute(
            f"SELECT flow_name, permission_type, outcome, COUNT(*) as cnt "
            f"FROM ubi_permission_events {where} "
            f"GROUP BY flow_name, permission_type, outcome "
            f"ORDER BY flow_name, permission_type, outcome",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _delete_events_sync(flow_name: str | None = None, before_date: str | None = None) -> int:
    """Delete events matching filters. Returns count deleted."""
    conn = _get_conn()
    try:
        ensure_tables(conn)
        clauses = []
        params: list = []
        if flow_name:
            clauses.append("flow_name = ?")
            params.append(flow_name)
        if before_date:
            clauses.append("created_at < ?")
            params.append(before_date)

        if not clauses:
            return 0  # Safety: refuse to delete everything without filters

        where = f"WHERE {' AND '.join(clauses)}"
        result = conn.execute(f"DELETE FROM ubi_permission_events {where}", params)
        conn.commit()
        return result.rowcount
    finally:
        conn.close()


def _list_flows_sync() -> list[dict]:
    """List distinct flows with event counts."""
    conn = _get_conn()
    try:
        ensure_tables(conn)
        rows = conn.execute(
            "SELECT flow_name, COUNT(*) as event_count, MAX(created_at) as last_event "
            "FROM ubi_permission_events GROUP BY flow_name ORDER BY event_count DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --- Async wrappers ---


async def log_event(flow_name: str, permission_type: str, outcome: str, **kwargs) -> dict:
    return await asyncio.to_thread(_log_event_sync, flow_name, permission_type, outcome, **kwargs)


async def query_events(flow_name: str | None = None, permission_type: str | None = None,
                       limit: int = 50) -> list[dict]:
    return await asyncio.to_thread(_query_events_sync, flow_name, permission_type, limit)


async def get_summary(flow_name: str | None = None) -> list[dict]:
    return await asyncio.to_thread(_summary_sync, flow_name)


async def delete_events(flow_name: str | None = None, before_date: str | None = None) -> int:
    return await asyncio.to_thread(_delete_events_sync, flow_name, before_date)


async def list_flows() -> list[dict]:
    return await asyncio.to_thread(_list_flows_sync)


# --- Formatting helpers ---


def _format_summary(rows: list[dict]) -> str:
    """Format summary rows into a readable grant-rate report."""
    if not rows:
        return "No permission events recorded yet."

    # Group by flow → permission → {outcome: count}
    flows: dict[str, dict[str, dict[str, int]]] = {}
    for r in rows:
        flows.setdefault(r["flow_name"], {}).setdefault(r["permission_type"], {})[r["outcome"]] = r["cnt"]

    lines = ["📊 *Permission Grant Rate Summary*\n"]
    for flow, perms in flows.items():
        lines.append(f"*{flow}*")
        for perm, outcomes in perms.items():
            total = sum(outcomes.values())
            granted = outcomes.get("granted", 0)
            rate = (granted / total * 100) if total > 0 else 0
            lines.append(f"  `{perm}`: {rate:.0f}% granted ({granted}/{total})")
        lines.append("")

    return "\n".join(lines)


def _format_events(events: list[dict]) -> str:
    """Format event list for display."""
    if not events:
        return "No events found."

    lines = [f"📋 *Recent Permission Events* ({len(events)} shown)\n"]
    for e in events:
        icon = "✅" if e["outcome"] == "granted" else "❌" if e["outcome"] == "denied" else "⏸"
        line = f"{icon} `#{e['id']}` {e['flow_name']}/{e['permission_type']} → *{e['outcome']}*"
        if e.get("app_version"):
            line += f" (v{e['app_version']})"
        if e.get("notes"):
            line += f" — {e['notes']}"
        lines.append(line)

    return "\n".join(lines)


# --- Telegram command handler ---


async def handle_ubi_logging(update, context):
    """Handle /ubi_logging command.

    Subcommands:
        /ubi_logging log <flow> <permission> <outcome> [version] [notes...]
        /ubi_logging list [flow]
        /ubi_logging summary [flow]
        /ubi_logging flows
        /ubi_logging delete preview <flow> [before:YYYY-MM-DD]
        /ubi_logging delete confirm <flow> [before:YYYY-MM-DD]
    """
    args = context.args or []

    if not args:
        await update.message.reply_text(
            "*UBI Permission Logging*\n\n"
            "Usage:\n"
            "`/ubi_logging log <flow> <perm> <outcome> [version] [notes]`\n"
            "`/ubi_logging list [flow]` — recent events\n"
            "`/ubi_logging summary [flow]` — grant rates\n"
            "`/ubi_logging flows` — list tracked flows\n"
            "`/ubi_logging delete preview <flow>` — dry-run\n"
            "`/ubi_logging delete confirm <flow>` — execute\n\n"
            f"Valid permissions: {', '.join(sorted(VALID_PERMISSIONS))}\n"
            f"Valid outcomes: {', '.join(sorted(VALID_OUTCOMES))}",
            parse_mode="Markdown",
        )
        return

    sub = args[0].lower()

    if sub == "log":
        if len(args) < 4:
            await update.message.reply_text("Usage: `/ubi_logging log <flow> <permission> <outcome> [version] [notes...]`", parse_mode="Markdown")
            return

        flow_name = args[1]
        permission_type = args[2].lower()
        outcome = args[3].lower()

        if permission_type not in VALID_PERMISSIONS:
            await update.message.reply_text(f"Unknown permission `{permission_type}`.\nValid: {', '.join(sorted(VALID_PERMISSIONS))}", parse_mode="Markdown")
            return
        if outcome not in VALID_OUTCOMES:
            await update.message.reply_text(f"Unknown outcome `{outcome}`.\nValid: {', '.join(sorted(VALID_OUTCOMES))}", parse_mode="Markdown")
            return

        app_version = args[4] if len(args) > 4 and re.match(r"^\d+\.\d+", args[4]) else None
        notes_start = 5 if app_version else 4
        notes = " ".join(args[notes_start:]) if len(args) > notes_start else None

        result = await log_event(flow_name, permission_type, outcome, app_version=app_version, notes=notes)
        await update.message.reply_text(f"✅ Logged event `#{result['id']}`: {flow_name}/{permission_type} → *{outcome}*", parse_mode="Markdown")

    elif sub == "list":
        flow = args[1] if len(args) > 1 else None
        events = await query_events(flow_name=flow, limit=50)
        text = _format_events(events)
        if len(text) > 4096:
            text = text[:4090] + "\n…"
        await update.message.reply_text(text, parse_mode="Markdown")

    elif sub == "summary":
        flow = args[1] if len(args) > 1 else None
        rows = await get_summary(flow_name=flow)
        text = _format_summary(rows)
        if len(text) > 4096:
            text = text[:4090] + "\n…"
        await update.message.reply_text(text, parse_mode="Markdown")

    elif sub == "flows":
        flows = await list_flows()
        if not flows:
            await update.message.reply_text("No flows tracked yet.")
            return
        lines = ["*Tracked Flows*\n"]
        for f in flows:
            lines.append(f"• `{f['flow_name']}` — {f['event_count']} events (last: {f['last_event']})")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    elif sub == "delete":
        if len(args) < 3:
            await update.message.reply_text("Usage: `/ubi_logging delete preview|confirm <flow> [before:YYYY-MM-DD]`", parse_mode="Markdown")
            return

        mode = args[1].lower()
        flow = args[2]
        before_date = None
        for a in args[3:]:
            if a.startswith("before:"):
                before_date = a.split(":", 1)[1]

        if mode == "preview":
            events = await query_events(flow_name=flow, limit=50)
            if before_date:
                events = [e for e in events if e["created_at"] < before_date]
            await update.message.reply_text(
                f"🔍 *Dry-run*: would delete {len(events)} events for flow `{flow}`"
                + (f" before {before_date}" if before_date else "")
                + "\n\nUse `delete confirm` to execute.",
                parse_mode="Markdown",
            )
        elif mode == "confirm":
            count = await delete_events(flow_name=flow, before_date=before_date)
            await update.message.reply_text(f"🗑 Deleted {count} events for flow `{flow}`.", parse_mode="Markdown")
        else:
            await update.message.reply_text("Use `delete preview` or `delete confirm`.", parse_mode="Markdown")

    else:
        await update.message.reply_text(f"Unknown subcommand `{sub}`. Use `/ubi_logging` for help.", parse_mode="Markdown")
