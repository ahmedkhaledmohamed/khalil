"""Inspect Cursor IDE integrated terminal tabs — titles, directories, active files.

Queries the Khalil Terminal Bridge VS Code extension running at http://127.0.0.1:8034.
No token needed — the bridge runs locally and requires no authentication.

Setup: Install the khalil-terminal-bridge extension in Cursor, which starts an HTTP
server on port 8034 exposing terminal and workspace metadata.
"""

import asyncio
import json
import logging
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from config import DB_PATH, TIMEZONE

log = logging.getLogger("khalil.actions.cursor_inspector")

BRIDGE_URL = "http://127.0.0.1:8034"

_tables_ensured = False


def ensure_tables(conn: sqlite3.Connection):
    """Create tables for terminal snapshots. Called once at startup."""
    global _tables_ensured
    if _tables_ensured:
        return
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cursor_terminal_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_json TEXT NOT NULL,
            terminal_count INTEGER NOT NULL DEFAULT 0,
            workspace_name TEXT,
            captured_at TEXT NOT NULL
        )
    """)
    conn.commit()
    _tables_ensured = True


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


# --- Core sync functions ---


def _save_snapshot_sync(terminals: list[dict], workspace: dict | None) -> int:
    """Persist a terminal snapshot. Returns the snapshot ID."""
    conn = _get_conn()
    try:
        ensure_tables(conn)
        now = datetime.now(ZoneInfo(TIMEZONE)).isoformat()
        ws_name = None
        if workspace and workspace.get("folders"):
            ws_name = workspace["folders"][0].get("name")
        cursor = conn.execute(
            "INSERT INTO cursor_terminal_snapshots "
            "(snapshot_json, terminal_count, workspace_name, captured_at) "
            "VALUES (?, ?, ?, ?)",
            (
                json.dumps({"terminals": terminals, "workspace": workspace}),
                len(terminals),
                ws_name,
                now,
            ),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def _list_snapshots_sync(limit: int = 10) -> list[dict]:
    """Fetch recent terminal snapshots."""
    conn = _get_conn()
    try:
        ensure_tables(conn)
        rows = conn.execute(
            "SELECT id, terminal_count, workspace_name, captured_at "
            "FROM cursor_terminal_snapshots ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _get_snapshot_sync(snapshot_id: int) -> dict | None:
    """Fetch a single snapshot by ID."""
    conn = _get_conn()
    try:
        ensure_tables(conn)
        row = conn.execute(
            "SELECT id, snapshot_json, terminal_count, workspace_name, captured_at "
            "FROM cursor_terminal_snapshots WHERE id = ?",
            (snapshot_id,),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["data"] = json.loads(result.pop("snapshot_json"))
        return result
    finally:
        conn.close()


# --- Bridge HTTP helpers ---


async def _bridge_get(path: str, timeout: int = 10) -> dict:
    """GET request to the Cursor Terminal Bridge."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{BRIDGE_URL}{path}")
            data = resp.json()
            if resp.status_code >= 400:
                return {"error": data.get("error", f"HTTP {resp.status_code}")}
            return data
    except httpx.ConnectError:
        return {"error": "Bridge not running. Install khalil-terminal-bridge in Cursor."}
    except httpx.TimeoutException:
        return {"error": "Bridge request timed out."}
    except Exception as e:
        return {"error": str(e)}


async def fetch_terminals() -> list[dict]:
    """Fetch terminal list from the bridge."""
    result = await _bridge_get("/terminals")
    if result.get("error"):
        log.warning("Bridge /terminals error: %s", result["error"])
        return []
    return result.get("terminals", [])


async def fetch_workspace() -> dict | None:
    """Fetch workspace metadata from the bridge."""
    result = await _bridge_get("/workspace")
    if result.get("error"):
        log.warning("Bridge /workspace error: %s", result["error"])
        return None
    return result


async def inspect_and_snapshot() -> dict:
    """Fetch current terminals + workspace and save a snapshot.

    Returns: {terminals, workspace, snapshot_id, error}
    """
    status = await _bridge_get("/status")
    if status.get("error"):
        return {"terminals": [], "workspace": None, "snapshot_id": None, "error": status["error"]}

    terminals, workspace = await asyncio.gather(
        fetch_terminals(),
        fetch_workspace(),
    )
    snapshot_id = await asyncio.to_thread(_save_snapshot_sync, terminals, workspace)
    return {
        "terminals": terminals,
        "workspace": workspace,
        "snapshot_id": snapshot_id,
        "error": None,
    }


# --- Formatting ---


def _format_terminals(terminals: list[dict], workspace: dict | None) -> str:
    """Format terminal metadata for Telegram."""
    lines = []

    # Workspace header
    if workspace and workspace.get("folders"):
        folders = ", ".join(f["name"] for f in workspace["folders"])
        lines.append(f"Workspace: {folders}")
    if workspace and workspace.get("activeFile"):
        af = workspace["activeFile"]
        lines.append(f"Editing: {af.get('path', '?')} (L{af.get('line', '?')})")

    if not terminals:
        lines.append("\nNo terminals open.")
        return "\n".join(lines)

    lines.append(f"\nTerminals ({len(terminals)}):")
    for t in terminals:
        active = " <" if t.get("isActive") else ""
        pid_str = f"  PID {t['pid']}" if t.get("pid") else ""
        name = t.get("name", "?")
        cwd = t.get("cwd", "")
        cwd_str = f"\n    dir: {cwd}" if cwd else ""
        lines.append(f"  [{t.get('id', '?')}] {name}{pid_str}{active}{cwd_str}")

    return "\n".join(lines)


def _format_snapshot_list(snapshots: list[dict]) -> str:
    """Format snapshot history for Telegram."""
    if not snapshots:
        return "No snapshots recorded yet."
    lines = ["Recent snapshots:"]
    for s in snapshots:
        ws = s["workspace_name"] or "unknown"
        lines.append(f"  #{s['id']}  {s['terminal_count']} terminals  ({ws})  {s['captured_at']}")
    return "\n".join(lines)


def _format_snapshot_detail(snap: dict) -> str:
    """Format a single snapshot detail for Telegram."""
    data = snap["data"]
    header = f"Snapshot #{snap['id']} — {snap['captured_at']}"
    body = _format_terminals(data.get("terminals", []), data.get("workspace"))
    return f"{header}\n\n{body}"


# --- Telegram handler ---


async def handle_cursor(update, context):
    """Handle /cursor command.

    Subcommands:
      /cursor           — show current terminals and workspace
      /cursor history   — list recent snapshots
      /cursor snap <id> — show details of a specific snapshot
      /cursor find <q>  — filter terminals by name/directory
    """
    args = context.args or []
    sub = args[0].lower() if args else "list"

    if sub in ("list", "ls", "status"):
        result = await inspect_and_snapshot()
        if result["error"]:
            await update.message.reply_text(f"Error: {result['error']}")
            return
        text = _format_terminals(result["terminals"], result["workspace"])
        text += f"\n\n(snapshot #{result['snapshot_id']})"
        await update.message.reply_text(text[:4096])

    elif sub == "history":
        limit = 10
        if len(args) > 1 and args[1].isdigit():
            limit = min(int(args[1]), 50)
        snapshots = await asyncio.to_thread(_list_snapshots_sync, limit)
        await update.message.reply_text(_format_snapshot_list(snapshots)[:4096])

    elif sub in ("snap", "snapshot"):
        if len(args) < 2 or not args[1].isdigit():
            await update.message.reply_text("Usage: /cursor snap <id>")
            return
        snap = await asyncio.to_thread(_get_snapshot_sync, int(args[1]))
        if not snap:
            await update.message.reply_text(f"Snapshot #{args[1]} not found.")
            return
        await update.message.reply_text(_format_snapshot_detail(snap)[:4096])

    elif sub == "find":
        if len(args) < 2:
            await update.message.reply_text("Usage: /cursor find <query>")
            return
        query = " ".join(args[1:]).lower()
        terminals = await fetch_terminals()
        matched = [
            t for t in terminals
            if query in t.get("name", "").lower() or query in t.get("cwd", "").lower()
        ]
        if not matched:
            await update.message.reply_text(f"No terminals matching '{query}'.")
            return
        text = _format_terminals(matched, None)
        await update.message.reply_text(text[:4096])

    else:
        await update.message.reply_text(
            "Usage:\n"
            "  /cursor — list terminals\n"
            "  /cursor history [n] — recent snapshots\n"
            "  /cursor snap <id> — snapshot detail\n"
            "  /cursor find <query> — filter by name/dir"
        )
