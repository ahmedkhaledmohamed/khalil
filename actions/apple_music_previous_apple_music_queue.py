"""Apple Music rewind + queue — go back N tracks then show what's up next.

Combines 'apple_music_previous' and 'apple_music_queue' into one /prevqueue
command. Common use: undo an accidental skip and confirm the queue looks right.

No API key needed. Uses AppleScript via asyncio subprocess (same as apple_music.py).
"""

import asyncio
import logging
import sqlite3

from config import DB_PATH, KEYRING_SERVICE, TIMEZONE

log = logging.getLogger("khalil.actions.apple_music_previous_apple_music_queue")

_tables_ensured = False


def ensure_tables(conn: sqlite3.Connection):
    """Create tables for tracking rewind+queue usage. Called once at startup."""
    global _tables_ensured
    if _tables_ensured:
        return
    conn.execute(
        "CREATE TABLE IF NOT EXISTS prevqueue_history ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  rewind_count INTEGER NOT NULL DEFAULT 1,"
        "  track_name TEXT,"
        "  artist TEXT,"
        "  queue_length INTEGER,"
        "  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    conn.commit()
    _tables_ensured = True


# --- Core async functions (AppleScript via subprocess) ---


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


async def _previous_track() -> bool:
    """Go to previous track."""
    _, rc = await _run_osascript('tell application "Music" to previous track')
    return rc == 0


async def _rewind(count: int = 1) -> tuple[bool, dict | None]:
    """Go back `count` tracks. Returns (success, now_playing)."""
    success = True
    for _ in range(count):
        if not await _previous_track():
            success = False
            break
        if count > 1:
            await asyncio.sleep(0.3)
    track = await _now_playing() if success else None
    return success, track


async def _get_queue(limit: int = 10) -> list[dict]:
    """Get upcoming tracks in the queue."""
    script = (
        'tell application "Music"\n'
        '  if player state is stopped then return "STOPPED"\n'
        '  set output to ""\n'
        '  set idx to index of current track\n'
        '  set pl to current playlist\n'
        f'  set maxCount to {limit}\n'
        '  set i to 0\n'
        '  repeat with t in (tracks (idx + 1) thru -1 of pl)\n'
        '    set i to i + 1\n'
        '    if i > maxCount then exit repeat\n'
        '    set output to output & name of t & "|||" & artist of t & "|||" & album of t & linefeed\n'
        '  end repeat\n'
        '  return output\n'
        'end tell'
    )
    stdout, rc = await _run_osascript(script)
    if rc != 0 or stdout in ("STOPPED", ""):
        return []

    results = []
    for line in stdout.split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split("|||", 2)
        results.append({
            "name": parts[0].strip() if parts else "",
            "artist": parts[1].strip() if len(parts) > 1 else "",
            "album": parts[2].strip() if len(parts) > 2 else "",
        })
    return results


def _log_action(rewind_count: int, track: dict | None, queue_length: int):
    """Record rewind+queue event in SQLite."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        ensure_tables(conn)
        conn.execute(
            "INSERT INTO prevqueue_history (rewind_count, track_name, artist, queue_length) "
            "VALUES (?, ?, ?, ?)",
            (rewind_count, track["name"] if track else None,
             track["artist"] if track else None, queue_length),
        )
        conn.commit()
    finally:
        conn.close()


def _get_stats() -> dict:
    """Get rewind+queue usage statistics."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        ensure_tables(conn)
        row = conn.execute(
            "SELECT COUNT(*) as total, SUM(rewind_count) as total_rewinds "
            "FROM prevqueue_history"
        ).fetchone()
        top_artist = conn.execute(
            "SELECT artist, COUNT(*) as cnt FROM prevqueue_history "
            "WHERE artist IS NOT NULL GROUP BY artist ORDER BY cnt DESC LIMIT 1"
        ).fetchone()
        return {
            "total": row[0] if row else 0,
            "total_rewinds": row[1] if row else 0,
            "top_rewound_artist": top_artist[0] if top_artist else None,
        }
    finally:
        conn.close()


# --- Telegram handler ---


async def handle_prevqueue(update, context):
    """Handle /prevqueue command.

    Subcommands:
        /prevqueue [N]        — go back N tracks (default 1) and show queue
        /prevqueue queue      — just show the queue (no rewind)
        /prevqueue stats      — usage statistics
        /prevqueue preview [N] — dry-run: show what would happen without rewinding
    """
    args = context.args or []

    # No args: rewind 1 and show queue
    if not args:
        args = ["1"]

    sub = args[0].lower()

    # Stats subcommand
    if sub == "stats":
        stats = await asyncio.to_thread(_get_stats)
        lines = [
            "📊 Rewind+Queue Stats\n",
            f"  Total uses: {stats['total']}",
            f"  Total tracks rewound: {stats['total_rewinds']}",
        ]
        if stats["top_rewound_artist"]:
            lines.append(f"  Most rewound to: {stats['top_rewound_artist']}")
        await update.message.reply_text("\n".join(lines))
        return

    # Queue-only subcommand
    if sub == "queue":
        tracks = await _get_queue()
        if not tracks:
            await update.message.reply_text("Queue is empty or nothing is playing.")
        else:
            lines = [f"🎵 Up Next ({len(tracks)} tracks):\n"]
            for i, t in enumerate(tracks, 1):
                lines.append(f"  {i}. **{t['name']}** — {t['artist']}")
            await update.message.reply_text("\n".join(lines))
        return

    # Preview (dry-run) subcommand
    if sub == "preview":
        count = 1
        if len(args) > 1:
            try:
                count = max(1, min(int(args[1]), 20))
            except ValueError:
                count = 1
        track = await _now_playing()
        queue = await _get_queue()
        lines = ["🔍 Preview (no changes made):\n"]
        if track:
            lines.append(f"  Current: **{track['name']}** — {track['artist']}")
        lines.append(f"  Would rewind: {count} track(s)")
        if queue:
            lines.append(f"\n🎵 Current queue ({len(queue)} tracks):")
            for i, t in enumerate(queue[:5], 1):
                lines.append(f"  {i}. **{t['name']}** — {t['artist']}")
            if len(queue) > 5:
                lines.append(f"  ...and {len(queue) - 5} more")
        await update.message.reply_text("\n".join(lines))
        return

    # Parse rewind count
    try:
        count = max(1, min(int(sub), 20))
    except ValueError:
        await update.message.reply_text(
            "Usage: /prevqueue [N]  — go back N tracks and show queue\n"
            "  /prevqueue queue     — just show queue\n"
            "  /prevqueue preview N — dry-run\n"
            "  /prevqueue stats     — usage stats"
        )
        return

    # Rewind then show queue
    success, track = await _rewind(count)

    if not success:
        await update.message.reply_text("❌ Could not go to previous track.")
        return

    queue = await _get_queue()

    # Log in background
    asyncio.create_task(asyncio.to_thread(_log_action, count, track, len(queue)))

    lines = []
    plural = f" ({count} tracks)" if count > 1 else ""
    if track:
        lines.append(f"⏮{plural} Now playing: **{track['name']}** — {track['artist']}")
    else:
        lines.append(f"⏮{plural} Rewound.")

    if queue:
        lines.append(f"\n🎵 Up Next ({len(queue)} tracks):")
        for i, t in enumerate(queue[:10], 1):
            lines.append(f"  {i}. **{t['name']}** — {t['artist']}")
        if len(queue) > 10:
            lines.append(f"  ...and {len(queue) - 10} more")
    else:
        lines.append("\nQueue is empty.")

    await update.message.reply_text("\n".join(lines))
