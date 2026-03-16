"""Check Cursor IDE instances and window names via the Cursor CLI.

Uses `cursor --status` (via actions.terminal) to list running instances.
Snapshots are stored in SQLite for historical tracking.

No external API keys needed — relies on the locally installed Cursor CLI.
"""

import asyncio
import logging
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from config import DB_PATH, TIMEZONE

log = logging.getLogger("khalil.actions.cursor_checker")

_tables_created = False


def ensure_tables(conn: sqlite3.Connection):
    """Create snapshot tables. Called once at startup."""
    global _tables_created
    if _tables_created:
        return
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cursor_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            captured_at TEXT NOT NULL,
            window_count INTEGER NOT NULL,
            version TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cursor_snapshot_windows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id INTEGER NOT NULL,
            window_name TEXT NOT NULL,
            project TEXT,
            pid INTEGER,
            cpu_pct REAL,
            mem_mb REAL,
            FOREIGN KEY (snapshot_id) REFERENCES cursor_snapshots(id)
        )
    """)
    conn.commit()
    _tables_created = True


# --- Core sync functions (called via asyncio.to_thread) ---


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def save_snapshot(status: dict) -> int | None:
    """Persist a Cursor status snapshot. Returns snapshot ID or None on error."""
    if status.get("error"):
        return None
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz).isoformat()
    conn = _get_conn()
    try:
        ensure_tables(conn)
        cur = conn.execute(
            "INSERT INTO cursor_snapshots (captured_at, window_count, version) VALUES (?, ?, ?)",
            (now, len(status.get("windows", [])), status.get("version")),
        )
        snap_id = cur.lastrowid
        for w in status.get("windows", []):
            conn.execute(
                "INSERT INTO cursor_snapshot_windows "
                "(snapshot_id, window_name, project, pid, cpu_pct, mem_mb) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    snap_id,
                    w.get("name", ""),
                    w.get("project"),
                    w.get("pid"),
                    w.get("cpu_pct", 0),
                    w.get("mem_mb", 0),
                ),
            )
        conn.commit()
        return snap_id
    finally:
        conn.close()


def get_recent_snapshots(limit: int = 10) -> list[dict]:
    """Fetch recent snapshots with their windows."""
    conn = _get_conn()
    try:
        ensure_tables(conn)
        rows = conn.execute(
            "SELECT id, captured_at, window_count, version "
            "FROM cursor_snapshots ORDER BY captured_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        snapshots = []
        for r in rows:
            windows = conn.execute(
                "SELECT window_name, project, pid, cpu_pct, mem_mb "
                "FROM cursor_snapshot_windows WHERE snapshot_id = ?",
                (r["id"],),
            ).fetchall()
            snapshots.append({
                **dict(r),
                "windows": [dict(w) for w in windows],
            })
        return snapshots
    finally:
        conn.close()


def search_snapshots(query: str, limit: int = 20) -> list[dict]:
    """Search snapshot windows by name/project using case-insensitive matching."""
    query_lower = query.lower()
    conn = _get_conn()
    try:
        ensure_tables(conn)
        rows = conn.execute(
            "SELECT sw.window_name, sw.project, sw.pid, sw.cpu_pct, sw.mem_mb, "
            "s.captured_at, s.version "
            "FROM cursor_snapshot_windows sw "
            "JOIN cursor_snapshots s ON s.id = sw.snapshot_id "
            "ORDER BY s.captured_at DESC LIMIT 200"
        ).fetchall()
        matches = []
        for r in rows:
            text = f"{r['window_name']} {r['project'] or ''}".lower()
            if query_lower in text:
                matches.append(dict(r))
                if len(matches) >= limit:
                    break
        return matches
    finally:
        conn.close()


# --- Async wrappers ---


async def async_get_cursor_status() -> dict:
    """Get live Cursor status via terminal module."""
    from actions.terminal import get_cursor_status
    return await get_cursor_status()


async def async_save_snapshot(status: dict) -> int | None:
    return await asyncio.to_thread(save_snapshot, status)


async def async_get_recent_snapshots(limit: int = 10) -> list[dict]:
    return await asyncio.to_thread(get_recent_snapshots, limit)


async def async_search_snapshots(query: str) -> list[dict]:
    return await asyncio.to_thread(search_snapshots, query)


# --- Formatting ---


def format_live_status(status: dict) -> str:
    """Format live Cursor status for Telegram."""
    if status.get("error"):
        return f"⚠️ Cursor: {status['error']}"

    windows = status.get("windows", [])
    if not windows:
        return "No Cursor instances running."

    lines = [f"🖥 Cursor — {len(windows)} window(s)"]
    if status.get("version"):
        lines[0] += f" (v{status['version']})"

    for i, w in enumerate(windows, 1):
        name = w.get("name", "unnamed")
        project = w.get("project", "")
        pid = w.get("pid", "?")
        cpu = w.get("cpu_pct", 0)
        mem = w.get("mem_mb", 0)
        line = f"  {i}. {name}"
        if project and project != name:
            line += f" [{project}]"
        line += f" (PID {pid}, {cpu:.0f}% CPU, {mem:.0f}MB)"
        lines.append(line)

    return "\n".join(lines)


def format_history(snapshots: list[dict]) -> str:
    """Format snapshot history for Telegram."""
    if not snapshots:
        return "No snapshots recorded yet. Run /cursor to capture one."

    lines = ["📊 Recent Cursor snapshots:"]
    for s in snapshots:
        ts = s["captured_at"][:16].replace("T", " ")
        wc = s["window_count"]
        ver = f" v{s['version']}" if s.get("version") else ""
        projects = [w["project"] for w in s.get("windows", []) if w.get("project")]
        proj_str = ", ".join(projects) if projects else "no projects"
        lines.append(f"  • {ts}{ver} — {wc} window(s): {proj_str}")

    return "\n".join(lines)


# --- Telegram handler ---


async def handle_cursor(update, context):
    """Handle /cursor command.

    Subcommands:
        /cursor          — show live Cursor instances (and save snapshot)
        /cursor history  — show recent snapshots
        /cursor search <query> — search snapshot history by project/window name
    """
    args = context.args or []
    subcommand = args[0].lower() if args else ""

    if subcommand == "history":
        limit = 10
        if len(args) > 1 and args[1].isdigit():
            limit = min(int(args[1]), 50)
        snapshots = await async_get_recent_snapshots(limit)
        text = format_history(snapshots)
        await update.message.reply_text(text[:4096])
        return

    if subcommand == "search":
        query = " ".join(args[1:]).strip()
        if not query:
            await update.message.reply_text("Usage: /cursor search <query>")
            return
        matches = await async_search_snapshots(query)
        if not matches:
            await update.message.reply_text(f"No windows matching '{query}' in history.")
            return
        lines = [f"🔍 Windows matching '{query}':"]
        for m in matches:
            ts = m["captured_at"][:16].replace("T", " ")
            lines.append(f"  • {m['window_name']} [{m.get('project', '?')}] at {ts}")
        await update.message.reply_text("\n".join(lines)[:4096])
        return

    # Default: live status + snapshot
    status = await async_get_cursor_status()
    text = format_live_status(status)
    await update.message.reply_text(text[:4096])

    # Save snapshot in background
    if not status.get("error"):
        snap_id = await async_save_snapshot(status)
        if snap_id:
            log.info("Cursor snapshot #%d saved (%d windows)", snap_id, len(status.get("windows", [])))
