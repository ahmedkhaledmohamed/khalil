"""Combined tmux kill+list workflows — kill with list context, bulk cleanup, session history.

Pairs the two most co-used tmux actions (28x together) into single commands:
  /tmux list       — list all sessions
  /tmux kill <name> — kill a session, show remaining sessions after
  /tmux killall    — preview all sessions, then kill all on confirm
  /tmux cleanup    — list sessions + kill stale/idle ones (preview first)
  /tmux history    — show recent kill/cleanup activity

No external API — delegates to actions.tmux_control for tmux CLI calls.
"""

import asyncio
import logging
import re
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from config import DB_PATH, TIMEZONE

log = logging.getLogger("khalil.actions.tmux_kill_tmux_list")

_tables_ensured = False


def ensure_tables(conn: sqlite3.Connection):
    """Create session-kill log table. Called once at startup."""
    global _tables_ensured
    if _tables_ensured:
        return
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tmux_kill_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            session_name TEXT,
            sessions_before INTEGER,
            sessions_after INTEGER,
            performed_at TEXT NOT NULL
        )"""
    )
    conn.commit()
    _tables_ensured = True


# --- Core sync functions (called via asyncio.to_thread) ---


def _log_action(action: str, session_name: str | None, before: int, after: int):
    """Log a kill/cleanup action to the database."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        ensure_tables(conn)
        now = datetime.now(ZoneInfo(TIMEZONE)).isoformat()
        conn.execute(
            "INSERT INTO tmux_kill_log (action, session_name, sessions_before, sessions_after, performed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (action, session_name, before, after, now),
        )
        conn.commit()
    finally:
        conn.close()


def _get_history(limit: int = 10) -> list[dict]:
    """Get recent kill/cleanup actions."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        ensure_tables(conn)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT action, session_name, sessions_before, sessions_after, performed_at "
            "FROM tmux_kill_log ORDER BY performed_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --- Helpers ---


def _parse_session_names(sessions_text: str) -> list[str]:
    """Extract session names from list_sessions() output."""
    names = []
    for line in sessions_text.strip().split("\n"):
        m = re.match(r"(\S+?):", line.strip())
        if m and not line.startswith("\U0001f4df"):
            names.append(m.group(1))
    return names


def _count_sessions(sessions_text: str) -> int:
    """Count sessions from list_sessions() output."""
    if "No tmux sessions" in sessions_text:
        return 0
    return len(_parse_session_names(sessions_text))


async def _is_idle(name: str) -> bool:
    """Check if a session looks idle (empty pane or minimal output)."""
    from actions.tmux_control import read_output
    output = await read_output(name, lines=5)
    content = output.replace(f"\U0001f4df Output from **{name}**:", "").strip().strip("`").strip()
    return not content or content == f"Session '{name}' pane is empty." or len(content) < 10


# --- Async wrappers ---


async def _list_and_kill(session_name: str) -> str:
    """Kill a named session, then list remaining sessions."""
    from actions.tmux_control import kill_session, list_sessions

    before_count = _count_sessions(await list_sessions())
    result = await kill_session(session_name)
    after_text = await list_sessions()
    await asyncio.to_thread(_log_action, "kill", session_name, before_count, _count_sessions(after_text))
    return f"{result}\n\n{after_text}"


async def _preview_killall() -> str:
    """Dry-run: show what killall would do."""
    from actions.tmux_control import list_sessions

    sessions_text = await list_sessions()
    if "No tmux sessions" in sessions_text:
        return sessions_text
    count = len(_parse_session_names(sessions_text))
    return (
        f"\U0001f50d **Dry-run — killall would terminate {count} session(s):**\n\n"
        f"{sessions_text}\n\n"
        "Run `/tmux killall confirm` to proceed."
    )


async def _killall() -> str:
    """Kill all tmux sessions."""
    from actions.tmux_control import list_sessions, kill_session

    sessions_text = await list_sessions()
    if "No tmux sessions" in sessions_text:
        return "No tmux sessions to kill."

    names = _parse_session_names(sessions_text)
    if not names:
        return "No tmux sessions found to kill."

    before_count = len(names)
    killed, failed = [], []
    for name in names:
        result = await kill_session(name)
        (killed if "Killed" in result else failed).append(name if "Killed" in result else f"{name}: {result}")

    after_text = await list_sessions()
    await asyncio.to_thread(_log_action, "killall", None, before_count, _count_sessions(after_text))

    lines = [f"\U0001f480 Killed {len(killed)}/{before_count} session(s)"]
    if killed:
        lines.append("  Killed: " + ", ".join(killed))
    if failed:
        lines.append("  Failed: " + "; ".join(failed))
    lines.append(f"\n{after_text}")
    return "\n".join(lines)


async def _cleanup_preview() -> str:
    """Preview which sessions look idle/stale for cleanup."""
    from actions.tmux_control import list_sessions

    sessions_text = await list_sessions()
    if "No tmux sessions" in sessions_text:
        return sessions_text

    names = _parse_session_names(sessions_text)
    if not names:
        return "No sessions to analyze."

    idle, active = [], []
    for name in names[:20]:
        (idle if await _is_idle(name) else active).append(name)

    lines = [f"\U0001f50d **Cleanup preview — {len(names)} session(s):**\n"]
    if idle:
        lines.append(f"  \U0001f4a4 Idle ({len(idle)}): " + ", ".join(idle))
    if active:
        lines.append(f"  \u2705 Active ({len(active)}): " + ", ".join(active))
    if idle:
        lines.append(f"\nRun `/tmux cleanup confirm` to kill {len(idle)} idle session(s).")
    else:
        lines.append("\nNo idle sessions found — nothing to clean up.")
    return "\n".join(lines)


async def _cleanup_confirm() -> str:
    """Kill idle sessions identified by cleanup heuristic."""
    from actions.tmux_control import list_sessions, kill_session

    sessions_text = await list_sessions()
    if "No tmux sessions" in sessions_text:
        return "No tmux sessions to clean up."

    names = _parse_session_names(sessions_text)
    before_count = len(names)
    killed = []
    for name in names[:20]:
        if await _is_idle(name):
            result = await kill_session(name)
            if "Killed" in result:
                killed.append(name)

    after_text = await list_sessions()
    await asyncio.to_thread(_log_action, "cleanup", ",".join(killed) if killed else None, before_count, _count_sessions(after_text))

    if not killed:
        return "No idle sessions found to clean up."
    lines = [f"\U0001f9f9 Cleaned up {len(killed)} idle session(s): " + ", ".join(killed)]
    lines.append(f"\n{after_text}")
    return "\n".join(lines)


async def handle_tmux(update, context):
    """Handle /tmux command.

    Subcommands:
      /tmux list              — list all sessions
      /tmux kill <name>       — kill session, show remaining
      /tmux killall [confirm] — kill all (preview first)
      /tmux cleanup [confirm] — kill idle sessions (preview first)
      /tmux history           — recent kill/cleanup activity
    """
    args = context.args or []
    sub = args[0].lower() if args else "list"

    if sub == "list":
        from actions.tmux_control import list_sessions
        result = await list_sessions()
        await update.message.reply_text(result)

    elif sub == "kill":
        if len(args) < 2:
            # No name given — show list so user can pick
            from actions.tmux_control import list_sessions
            sessions = await list_sessions()
            await update.message.reply_text(
                f"{sessions}\n\nUsage: `/tmux kill <session_name>`",
                parse_mode="Markdown",
            )
            return
        name = args[1]
        result = await _list_and_kill(name)
        await update.message.reply_text(result)

    elif sub == "killall":
        confirm = len(args) > 1 and args[1].lower() == "confirm"
        if confirm:
            result = await _killall()
        else:
            result = await _preview_killall()
        await update.message.reply_text(result)

    elif sub == "cleanup":
        confirm = len(args) > 1 and args[1].lower() == "confirm"
        if confirm:
            result = await _cleanup_confirm()
        else:
            result = await _cleanup_preview()
        await update.message.reply_text(result)

    elif sub == "history":
        history = await asyncio.to_thread(_get_history, 10)
        if not history:
            await update.message.reply_text("No tmux kill/cleanup history yet.")
        else:
            lines = ["📊 **Recent tmux kill activity**\n"]
            for h in history:
                ts = h["performed_at"][:16].replace("T", " ")
                name = h["session_name"] or "all"
                lines.append(f"  {ts} — {h['action']} `{name}` ({h['sessions_before']}→{h['sessions_after']})")
            await update.message.reply_text("\n".join(lines))

    else:
        await update.message.reply_text(
            "Usage: /tmux [list|kill <name>|killall|cleanup|history]\n\n"
            "  list — show all sessions\n"
            "  kill <name> — kill session + show remaining\n"
            "  killall — kill all sessions (preview first)\n"
            "  cleanup — kill idle sessions (preview first)\n"
            "  history — recent kill/cleanup log"
        )
