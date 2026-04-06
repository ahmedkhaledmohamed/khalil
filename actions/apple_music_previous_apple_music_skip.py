"""Apple Music track navigation — skip forward or go back with a single command.

Combines 'apple_music_previous' and 'apple_music_skip' into one unified
/track command. Supports skipping multiple tracks and shows what's now playing.

No API key needed. Uses AppleScript via asyncio subprocess (same as apple_music.py).
"""

import asyncio
import logging
import sqlite3

from config import DB_PATH, KEYRING_SERVICE, TIMEZONE

log = logging.getLogger("khalil.actions.apple_music_previous_apple_music_skip")

_tables_ensured = False


def ensure_tables(conn: sqlite3.Connection):
    """Create tables for tracking skip history. Called once at startup."""
    global _tables_ensured
    if _tables_ensured:
        return
    conn.execute(
        "CREATE TABLE IF NOT EXISTS track_nav_history ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  direction TEXT NOT NULL,"
        "  count INTEGER NOT NULL DEFAULT 1,"
        "  track_name TEXT,"
        "  artist TEXT,"
        "  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    conn.commit()
    _tables_ensured = True


# --- Core async functions (reuse apple_music AppleScript runner) ---


async def _run_osascript(script: str, timeout: float = 10) -> tuple[str, int]:
    """Run an AppleScript snippet and return (stdout, returncode)."""
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    if proc.returncode != 0:
        log.warning("osascript failed (rc=%d): %s", proc.returncode, stderr.decode().strip()[:200])
    return stdout.decode().strip(), proc.returncode


async def _now_playing() -> dict | None:
    """Get currently playing track info."""
    script = (
        'tell application "Music"\n'
        '  if player state is not stopped then\n'
        '    set trackName to name of current track\n'
        '    set trackArtist to artist of current track\n'
        '    set trackAlbum to album of current track\n'
        '    set pState to player state as string\n'
        '    return trackName & "|||" & trackArtist & "|||" & trackAlbum & "|||" & pState\n'
        '  else\n'
        '    return "STOPPED"\n'
        '  end if\n'
        'end tell'
    )
    stdout, rc = await _run_osascript(script)
    if rc != 0 or stdout == "STOPPED":
        return None
    parts = stdout.split("|||")
    if len(parts) < 4:
        return None
    return {
        "name": parts[0].strip(),
        "artist": parts[1].strip(),
        "album": parts[2].strip(),
        "state": parts[3].strip(),
    }


async def _skip_track() -> bool:
    """Skip to next track."""
    _, rc = await _run_osascript('tell application "Music" to next track')
    return rc == 0


async def _previous_track() -> bool:
    """Go to previous track."""
    _, rc = await _run_osascript('tell application "Music" to previous track')
    return rc == 0


async def _navigate(direction: str, count: int = 1) -> tuple[bool, dict | None]:
    """Skip or go back `count` tracks. Returns (success, now_playing)."""
    fn = _skip_track if direction == "next" else _previous_track
    success = True
    for _ in range(count):
        if not await fn():
            success = False
            break
        # Small delay between rapid skips so Music.app keeps up
        if count > 1:
            await asyncio.sleep(0.3)

    track = await _now_playing() if success else None
    return success, track


def _log_navigation(direction: str, count: int, track: dict | None):
    """Record navigation event in SQLite."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        ensure_tables(conn)
        conn.execute(
            "INSERT INTO track_nav_history (direction, count, track_name, artist) VALUES (?, ?, ?, ?)",
            (direction, count, track["name"] if track else None, track["artist"] if track else None),
        )
        conn.commit()
    finally:
        conn.close()


def _get_nav_stats() -> dict:
    """Get track navigation statistics."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        ensure_tables(conn)
        row = conn.execute(
            "SELECT "
            "  COUNT(*) as total, "
            "  SUM(CASE WHEN direction = 'next' THEN count ELSE 0 END) as skips, "
            "  SUM(CASE WHEN direction = 'prev' THEN count ELSE 0 END) as backs "
            "FROM track_nav_history"
        ).fetchone()
        # Most skipped artist
        top_artist = conn.execute(
            "SELECT artist, COUNT(*) as cnt FROM track_nav_history "
            "WHERE direction = 'next' AND artist IS NOT NULL "
            "GROUP BY artist ORDER BY cnt DESC LIMIT 1"
        ).fetchone()
        return {
            "total": row[0] if row else 0,
            "skips": row[1] if row else 0,
            "backs": row[2] if row else 0,
            "top_skipped_artist": top_artist[0] if top_artist else None,
        }
    finally:
        conn.close()


# --- Async wrappers ---


async def handle_track(update, context):
    """Handle /track command.

    Subcommands:
        /track next [N]   — skip forward N tracks (default 1)
        /track prev [N]   — go back N tracks (default 1)
        /track skip [N]   — alias for next
        /track back [N]   — alias for prev
        /track stats      — show navigation statistics
        /track            — show what's currently playing
    """
    args = context.args or []

    # No args: show now playing
    if not args:
        track = await _now_playing()
        if track:
            state = "▶️" if track["state"] == "playing" else "⏸"
            await update.message.reply_text(
                f"{state} {track['name']}\n  {track['artist']} — {track['album']}"
            )
        else:
            await update.message.reply_text("Nothing is playing.")
        return

    sub = args[0].lower()

    # Stats subcommand
    if sub == "stats":
        stats = await asyncio.to_thread(_get_nav_stats)
        lines = [
            "📊 Track Navigation Stats\n",
            f"  Total actions: {stats['total']}",
            f"  Tracks skipped: {stats['skips']}",
            f"  Tracks rewound: {stats['backs']}",
        ]
        if stats["top_skipped_artist"]:
            lines.append(f"  Most skipped artist: {stats['top_skipped_artist']}")
        await update.message.reply_text("\n".join(lines))
        return

    # Determine direction and count
    if sub in ("next", "skip", "n"):
        direction = "next"
    elif sub in ("prev", "previous", "back", "b"):
        direction = "prev"
    else:
        await update.message.reply_text(
            "Usage: /track [next|prev|skip|back] [N]\n"
            "  /track next 3  — skip 3 tracks\n"
            "  /track prev    — go back 1 track\n"
            "  /track stats   — navigation stats"
        )
        return

    # Parse optional count
    count = 1
    if len(args) > 1:
        try:
            count = max(1, min(int(args[1]), 20))  # Cap at 20
        except ValueError:
            count = 1

    success, track = await _navigate(direction, count)

    # Log in background
    asyncio.create_task(asyncio.to_thread(_log_navigation, direction, count, track))

    if success and track:
        icon = "⏭" if direction == "next" else "⏮"
        plural = f" ({count} tracks)" if count > 1 else ""
        await update.message.reply_text(
            f"{icon}{plural} Now playing: **{track['name']}** — {track['artist']}"
        )
    elif success:
        icon = "⏭" if direction == "next" else "⏮"
        await update.message.reply_text(f"{icon} Done.")
    else:
        await update.message.reply_text("❌ Could not navigate tracks.")
