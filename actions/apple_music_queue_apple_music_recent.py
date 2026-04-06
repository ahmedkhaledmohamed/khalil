"""Apple Music queue + recently played — combined view.

Shows the current queue (up next) alongside recently played tracks in a
single response.  Delegates to the existing AppleScript helpers in
actions.apple_music so there is no extra osascript overhead.

No API key or external token required — uses local AppleScript via the
Music.app on macOS.
"""

import asyncio
import logging
import sqlite3

from config import DB_PATH, KEYRING_SERVICE, TIMEZONE

log = logging.getLogger("khalil.actions.apple_music_queue_apple_music_recent")

# ---------------------------------------------------------------------------
# DB table for tracking "pinned" queue snapshots (optional bookmarking)
# ---------------------------------------------------------------------------

_tables_ready = False


def ensure_tables(conn: sqlite3.Connection):
    """Create tables. Called once at startup."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS apple_music_snapshots ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  snapshot_type TEXT NOT NULL,"  # 'queue' or 'recent'
        "  data TEXT NOT NULL,"
        "  created_at TEXT DEFAULT (datetime('now'))"
        ")"
    )
    conn.commit()


def _init_tables():
    global _tables_ready
    if _tables_ready:
        return
    conn = sqlite3.connect(str(DB_PATH))
    try:
        ensure_tables(conn)
    finally:
        conn.close()
    _tables_ready = True


# ---------------------------------------------------------------------------
# Core sync functions (called via asyncio.to_thread)
# ---------------------------------------------------------------------------

def save_snapshot(snapshot_type: str, data: str) -> int:
    """Save a queue or recent snapshot to DB. Returns row id."""
    _init_tables()
    conn = sqlite3.connect(str(DB_PATH))
    try:
        cur = conn.execute(
            "INSERT INTO apple_music_snapshots (snapshot_type, data) VALUES (?, ?)",
            (snapshot_type, data),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_snapshots(limit: int = 10) -> list[dict]:
    """List recent snapshots."""
    _init_tables()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, snapshot_type, data, created_at "
            "FROM apple_music_snapshots ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Async wrappers
# ---------------------------------------------------------------------------

async def _fetch_combined(queue_limit: int = 10, recent_limit: int = 10):
    """Fetch queue and recently played in parallel."""
    from actions.apple_music import get_queue, recently_played, now_playing

    current, queue, recent = await asyncio.gather(
        now_playing(),
        get_queue(limit=queue_limit),
        recently_played(limit=recent_limit),
    )
    return current, queue, recent


def _format_combined(current, queue, recent) -> str:
    """Format the combined view into a single Telegram-friendly message."""
    lines = []

    # Now playing
    if current:
        state = "\u25b6\ufe0f" if current["state"] == "playing" else "\u23f8"
        lines.append(f"{state} **Now Playing**")
        lines.append(f"  {current['name']} \u2014 {current['artist']}")
        lines.append(f"  {current['album']}")
        lines.append("")

    # Queue
    if queue:
        lines.append(f"\U0001f3b5 **Up Next** ({len(queue)})")
        for i, t in enumerate(queue, 1):
            lines.append(f"  {i}. {t['name']} \u2014 {t['artist']}")
        lines.append("")
    else:
        lines.append("\U0001f3b5 **Up Next**: empty")
        lines.append("")

    # Recently played
    if recent:
        lines.append(f"\U0001f553 **Recently Played** ({len(recent)})")
        for t in recent:
            line = f"  \u2022 {t['name']} \u2014 {t['artist']}"
            if t.get("played_date"):
                line += f" ({t['played_date']})"
            lines.append(line)
    else:
        lines.append("\U0001f553 **Recently Played**: none")

    msg = "\n".join(lines)
    # Respect Telegram 4096 char limit
    if len(msg) > 4000:
        msg = msg[:3997] + "..."
    return msg


# ---------------------------------------------------------------------------
# Command handler
# ---------------------------------------------------------------------------

async def handle_music_overview(update, context):
    """Handle /musicoverview command.

    Subcommands:
        /musicoverview          — show queue + recent + now playing
        /musicoverview queue    — queue only
        /musicoverview recent   — recently played only
        /musicoverview save     — save current queue+recent as a snapshot
        /musicoverview history  — show saved snapshots
    """
    args = context.args or []
    sub = args[0].lower() if args else ""

    try:
        if sub == "queue":
            from actions.apple_music import get_queue
            tracks = await get_queue()
            if not tracks:
                await update.message.reply_text("Queue is empty or nothing is playing.")
                return
            lines = [f"\U0001f3b5 Up Next ({len(tracks)}):\n"]
            for i, t in enumerate(tracks, 1):
                lines.append(f"  {i}. {t['name']} \u2014 {t['artist']}")
            await update.message.reply_text("\n".join(lines))

        elif sub == "recent":
            from actions.apple_music import recently_played
            tracks = await recently_played()
            if not tracks:
                await update.message.reply_text("No recently played tracks found.")
                return
            lines = [f"\U0001f553 Recently Played ({len(tracks)}):\n"]
            for t in tracks:
                line = f"  \u2022 {t['name']} \u2014 {t['artist']}"
                if t.get("played_date"):
                    line += f" ({t['played_date']})"
                lines.append(line)
            await update.message.reply_text("\n".join(lines))

        elif sub == "save":
            current, queue, recent = await _fetch_combined()
            import json
            data = json.dumps({
                "current": current,
                "queue": queue,
                "recent": recent,
            })
            row_id = await asyncio.to_thread(save_snapshot, "combined", data)
            await update.message.reply_text(f"\U0001f4be Snapshot saved (#{row_id}).")

        elif sub == "history":
            snapshots = await asyncio.to_thread(list_snapshots)
            if not snapshots:
                await update.message.reply_text("No saved snapshots.")
                return
            lines = [f"\U0001f4cb Saved snapshots ({len(snapshots)}):\n"]
            for s in snapshots:
                lines.append(f"  #{s['id']} [{s['snapshot_type']}] {s['created_at']}")
            await update.message.reply_text("\n".join(lines))

        else:
            # Default: combined view
            current, queue, recent = await _fetch_combined()
            msg = _format_combined(current, queue, recent)
            await update.message.reply_text(msg)

    except Exception as exc:
        log.exception("musicoverview command failed")
        await update.message.reply_text(f"\u274c Error: {exc}")
