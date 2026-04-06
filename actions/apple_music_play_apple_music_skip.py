"""Combined Apple Music play+skip workflows — skip-and-play, multi-skip, restart track.

These actions pair play and skip (used together 41x) into single commands:
  /music next       — skip current track, resume playback, show what's now playing
  /music skip N     — skip N tracks forward, keep playing
  /music restart    — restart current track from the beginning
  /music fresh      — skip to next + show now playing (alias for next)
  /music preview    — dry-run: show what track is next without skipping

No API key needed — uses AppleScript via the existing apple_music module.
"""

import asyncio
import logging
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from config import DB_PATH, TIMEZONE

log = logging.getLogger("khalil.actions.apple_music_play_apple_music_skip")

_tables_ensured = False


def ensure_tables(conn: sqlite3.Connection):
    """Create skip-log table for tracking usage. Called once at startup."""
    global _tables_ensured
    if _tables_ensured:
        return
    conn.execute(
        """CREATE TABLE IF NOT EXISTS apple_music_skip_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            track_name TEXT,
            artist TEXT,
            skipped_at TEXT NOT NULL
        )"""
    )
    conn.commit()
    _tables_ensured = True


# --- Core sync functions (called via asyncio.to_thread) ---


def _log_skip(action: str, track_name: str | None, artist: str | None):
    """Log a skip/play action to the database."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        ensure_tables(conn)
        now = datetime.now(ZoneInfo(TIMEZONE)).isoformat()
        conn.execute(
            "INSERT INTO apple_music_skip_log (action, track_name, artist, skipped_at) VALUES (?, ?, ?, ?)",
            (action, track_name, artist, now),
        )
        conn.commit()
    finally:
        conn.close()


def _get_skip_stats(limit: int = 10) -> list[dict]:
    """Get most-skipped tracks."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        ensure_tables(conn)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT track_name, artist, COUNT(*) as skip_count "
            "FROM apple_music_skip_log WHERE action = 'skip' AND track_name IS NOT NULL "
            "GROUP BY track_name, artist ORDER BY skip_count DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --- Async wrappers ---


async def _skip_and_play() -> dict | None:
    """Skip current track, ensure playback, return new track info."""
    from actions.apple_music import skip, play, now_playing

    # Grab current track before skipping (for logging)
    current = await now_playing()
    if current:
        await asyncio.to_thread(_log_skip, "skip", current["name"], current["artist"])

    success = await skip()
    if not success:
        return None

    # Ensure playback is active after skip
    await play()
    # Small delay for Apple Music to update state
    await asyncio.sleep(0.3)
    return await now_playing()


async def _multi_skip(count: int) -> dict | None:
    """Skip N tracks forward, return final track info."""
    from actions.apple_music import skip, play, now_playing

    for i in range(count):
        current = await now_playing()
        if current:
            await asyncio.to_thread(_log_skip, "skip", current["name"], current["artist"])
        success = await skip()
        if not success:
            log.warning("Multi-skip failed at track %d/%d", i + 1, count)
            break
        if i < count - 1:
            await asyncio.sleep(0.2)

    await play()
    await asyncio.sleep(0.3)
    return await now_playing()


async def _restart_track() -> dict | None:
    """Restart the current track from the beginning."""
    from actions.apple_music import _run_osascript, now_playing

    _, rc = await _run_osascript('tell application "Music" to set player position to 0')
    if rc != 0:
        return None
    return await now_playing()


async def _peek_next() -> dict | None:
    """Preview the next track without skipping."""
    from actions.apple_music import get_queue

    queue = await get_queue(limit=1)
    return queue[0] if queue else None


def _format_track(track: dict) -> str:
    """Format track info for display."""
    return f"**{track.get('name', 'Unknown')}** — {track.get('artist', 'Unknown')}"


async def handle_music(update, context):
    """Handle /music command.

    Subcommands:
      /music next      — skip + play + show now playing
      /music skip [N]  — skip N tracks (default 1)
      /music restart   — restart current track
      /music fresh     — alias for next
      /music preview   — show next track without skipping
      /music stats     — show most-skipped tracks
    """
    args = context.args or []
    sub = args[0].lower() if args else "next"

    if sub in ("next", "fresh"):
        track = await _skip_and_play()
        if track:
            await update.message.reply_text(f"⏭ Now playing: {_format_track(track)}")
        else:
            await update.message.reply_text("❌ Could not skip — nothing playing or Music not open.")

    elif sub == "skip":
        count = 1
        if len(args) > 1:
            try:
                count = int(args[1])
                count = max(1, min(count, 20))  # bound to 1-20
            except ValueError:
                await update.message.reply_text("Usage: /music skip [N] — N must be a number (1-20)")
                return

        if count == 1:
            track = await _skip_and_play()
            if track:
                await update.message.reply_text(f"⏭ Now playing: {_format_track(track)}")
            else:
                await update.message.reply_text("❌ Could not skip.")
        else:
            await update.message.reply_text(f"⏭ Skipping {count} tracks...")
            track = await _multi_skip(count)
            if track:
                await update.message.reply_text(f"⏭ Skipped {count}. Now playing: {_format_track(track)}")
            else:
                await update.message.reply_text(f"Skipped {count} tracks but can't read current track.")

    elif sub == "restart":
        track = await _restart_track()
        if track:
            await update.message.reply_text(f"🔄 Restarted: {_format_track(track)}")
        else:
            await update.message.reply_text("❌ Could not restart — nothing playing.")

    elif sub == "preview":
        track = await _peek_next()
        if track:
            await update.message.reply_text(f"👀 Up next: {_format_track(track)}")
        else:
            await update.message.reply_text("No upcoming track found.")

    elif sub == "stats":
        stats = await asyncio.to_thread(_get_skip_stats, 10)
        if not stats:
            await update.message.reply_text("No skip history yet.")
        else:
            lines = ["📊 **Most Skipped Tracks**\n"]
            for i, s in enumerate(stats, 1):
                lines.append(f"  {i}. {s['track_name']} — {s['artist']} ({s['skip_count']}x)")
            await update.message.reply_text("\n".join(lines))

    else:
        await update.message.reply_text(
            "Usage: /music [next|skip N|restart|preview|stats]\n\n"
            "  next/fresh — skip + play + show track\n"
            "  skip N — skip N tracks\n"
            "  restart — restart current track\n"
            "  preview — peek at next track\n"
            "  stats — most-skipped tracks"
        )
