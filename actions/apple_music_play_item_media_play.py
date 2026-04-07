"""Unified play command — searches Apple Music library first, falls back to local files.

Combines apple_music_play_item and media_play (used together 32x) into a single
/play command that resolves the user's intent across both sources.

No API key or token needed — uses AppleScript for Apple Music and afplay/open
for local files, same as the underlying modules.
"""

import asyncio
import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from config import DB_PATH, TIMEZONE

log = logging.getLogger("khalil.actions.apple_music_play_item_media_play")

_tables_ensured = False

_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".aiff"}
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm"}
_ALL_MEDIA = _AUDIO_EXTENSIONS | _VIDEO_EXTENSIONS


def ensure_tables(conn: sqlite3.Connection):
    """Create play-log table for tracking unified play history. Called once at startup."""
    global _tables_ensured
    if _tables_ensured:
        return
    conn.execute(
        """CREATE TABLE IF NOT EXISTS unified_play_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            query TEXT,
            track_name TEXT,
            artist TEXT,
            file_path TEXT,
            played_at TEXT NOT NULL
        )"""
    )
    conn.commit()
    _tables_ensured = True


# --- Core sync functions (called via asyncio.to_thread) ---


def _log_play(source: str, query: str | None, track_name: str | None,
              artist: str | None, file_path: str | None):
    """Log a play action to the database."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        ensure_tables(conn)
        now = datetime.now(ZoneInfo(TIMEZONE)).isoformat()
        conn.execute(
            "INSERT INTO unified_play_log (source, query, track_name, artist, file_path, played_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (source, query, track_name, artist, file_path, now),
        )
        conn.commit()
    finally:
        conn.close()


def _get_play_history(limit: int = 15) -> list[dict]:
    """Get recent play history across both sources."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        ensure_tables(conn)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT source, query, track_name, artist, file_path, played_at "
            "FROM unified_play_log ORDER BY played_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _search_local_files(query: str, limit: int = 10) -> list[dict]:
    """Search local media directories for files matching query."""
    search_dirs = [
        Path.home() / "Music",
        Path.home() / "Downloads",
        Path.home() / "Desktop",
    ]
    escaped = re.escape(query)
    results = []
    for search_dir in search_dirs:
        if not search_dir.exists():
            continue
        for f in search_dir.rglob("*"):
            if f.suffix.lower() in _ALL_MEDIA and re.search(escaped, f.stem, re.IGNORECASE):
                results.append({
                    "name": f.name,
                    "path": str(f),
                    "size_mb": round(f.stat().st_size / 1048576, 1),
                    "type": "audio" if f.suffix.lower() in _AUDIO_EXTENSIONS else "video",
                    "directory": str(f.parent),
                })
                if len(results) >= limit:
                    return results
    return results


# --- Async wrappers ---


async def _try_apple_music(query: str) -> dict | None:
    """Try to play via Apple Music. Returns track info or None."""
    from actions.apple_music import play_item, now_playing

    success = await play_item(query)
    if not success:
        return None

    await asyncio.sleep(0.3)
    track = await now_playing()
    if track:
        await asyncio.to_thread(
            _log_play, "apple_music", query, track["name"], track["artist"], None
        )
    return track


async def _try_local_file(query: str) -> dict | None:
    """Try to find and play a local file matching query. Returns file info or None."""
    from actions.media_player import play_file

    matches = await asyncio.to_thread(_search_local_files, query, 1)
    if not matches:
        return None

    best = matches[0]
    success = await play_file(best["path"])
    if not success:
        return None

    await asyncio.to_thread(
        _log_play, "local_file", query, best["name"], None, best["path"]
    )
    return best


async def _search_both(query: str) -> dict:
    """Search both Apple Music library and local files without playing. Returns results."""
    from actions.apple_music import _run_osascript

    # Search Apple Music library
    safe_query = query.replace('"', '\\"')
    script = (
        'tell application "Music"\n'
        f'  set searchResults to search playlist "Library" for "{safe_query}"\n'
        '  set output to ""\n'
        '  set i to 0\n'
        '  repeat with t in searchResults\n'
        '    set i to i + 1\n'
        '    if i > 5 then exit repeat\n'
        '    set output to output & name of t & "|||" & artist of t & "|||" & album of t & linefeed\n'
        '  end repeat\n'
        '  return output\n'
        'end tell'
    )
    stdout, rc = await _run_osascript(script, timeout=10)

    apple_results = []
    if rc == 0 and stdout:
        for line in stdout.split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = line.split("|||", 2)
            apple_results.append({
                "name": parts[0].strip() if parts else "",
                "artist": parts[1].strip() if len(parts) > 1 else "",
                "album": parts[2].strip() if len(parts) > 2 else "",
            })

    local_results = await asyncio.to_thread(_search_local_files, query, 5)

    return {"apple_music": apple_results, "local": local_results}


def _format_track(track: dict) -> str:
    """Format track info for display."""
    name = track.get("name", "Unknown")
    artist = track.get("artist", "")
    if artist:
        return f"**{name}** — {artist}"
    return f"**{name}**"


async def handle_play(update, context):
    """Handle /play command.

    Subcommands:
      /play <query>       — search Apple Music first, fall back to local files
      /play local <path>  — play a local file directly
      /play search <query> — preview: show matches from both sources without playing
      /play history       — show recent play history
    """
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: /play <query|local|search|history>\n\n"
            "  /play <song or artist> — Apple Music first, local fallback\n"
            "  /play local <file path> — play a specific file\n"
            "  /play search <query> — preview matches without playing\n"
            "  /play history — recent play log"
        )
        return

    sub = args[0].lower()

    if sub == "local":
        # Direct local file playback
        if len(args) < 2:
            await update.message.reply_text("Usage: /play local <file path>")
            return

        filepath = " ".join(args[1:])
        from actions.media_player import play_file

        success = await play_file(filepath)
        if success:
            name = Path(filepath).name
            await asyncio.to_thread(_log_play, "local_file", None, name, None, filepath)
            await update.message.reply_text(f"▶️ Playing: **{name}**")
        else:
            await update.message.reply_text(f"❌ File not found or unsupported: {filepath}")
        return

    if sub == "search":
        # Dry-run: show matches from both sources
        query = " ".join(args[1:])
        if not query:
            await update.message.reply_text("Usage: /play search <query>")
            return

        results = await _search_both(query)
        lines = [f"🔍 Search results for \"{query}\":\n"]

        if results["apple_music"]:
            lines.append("**Apple Music Library:**")
            for i, t in enumerate(results["apple_music"], 1):
                lines.append(f"  {i}. {_format_track(t)} ({t.get('album', '')})")
        else:
            lines.append("**Apple Music Library:** No matches")

        lines.append("")

        if results["local"]:
            lines.append("**Local Files:**")
            for i, f in enumerate(results["local"], 1):
                emoji = "🎵" if f["type"] == "audio" else "🎬"
                lines.append(f"  {i}. {emoji} {f['name']} ({f['size_mb']} MB)")
        else:
            lines.append("**Local Files:** No matches")

        text = "\n".join(lines)
        if len(text) > 4096:
            text = text[:4090] + "\n..."
        await update.message.reply_text(text)
        return

    if sub == "history":
        history = await asyncio.to_thread(_get_play_history, 15)
        if not history:
            await update.message.reply_text("No play history yet.")
            return

        lines = ["📜 **Recent Play History**\n"]
        for h in history:
            source_icon = "🎵" if h["source"] == "apple_music" else "📁"
            name = h.get("track_name") or h.get("file_path", "").split("/")[-1] or "Unknown"
            artist = h.get("artist") or ""
            time_str = h["played_at"][:16].replace("T", " ")
            entry = f"  {source_icon} {name}"
            if artist:
                entry += f" — {artist}"
            entry += f" ({time_str})"
            lines.append(entry)

        text = "\n".join(lines)
        if len(text) > 4096:
            text = text[:4090] + "\n..."
        await update.message.reply_text(text)
        return

    # Default: unified play — Apple Music first, then local files
    query = " ".join(args)
    await update.message.reply_text(f"🔍 Searching for \"{query}\"...")

    # Try Apple Music first
    track = await _try_apple_music(query)
    if track:
        await update.message.reply_text(f"▶️ Playing from Apple Music: {_format_track(track)}")
        return

    # Fall back to local files
    result = await _try_local_file(query)
    if result:
        await update.message.reply_text(f"▶️ Playing local file: **{result['name']}**")
        return

    await update.message.reply_text(
        f"❌ No results for \"{query}\" in Apple Music library or local files.\n"
        "Try /play search <query> to see partial matches."
    )
