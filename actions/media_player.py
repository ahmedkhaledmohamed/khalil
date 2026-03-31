"""Local media player — play audio/video files via macOS native tools.

Uses afplay for audio and open for video. No external dependencies.
"""

import asyncio
import logging
import os
import re
from pathlib import Path

log = logging.getLogger("khalil.actions.media_player")

SKILL = {
    "name": "media_player",
    "description": "Play local audio and video files",
    "category": "media",
    "patterns": [
        (r"\bplay\s+(?:the\s+)?(?:file|audio|video|mp3|wav|mp4|m4a)\b", "media_play"),
        (r"\bplay\s+(?:the\s+)?(?:song|track|music)\s+(?:file|from)\b", "media_play"),
        (r"\bopen\s+(?:the\s+)?(?:video|movie|mp4)\b", "media_play"),
        (r"\bstop\s+(?:the\s+)?(?:audio|playback|media)\b", "media_stop"),
        (r"\bstop\s+playing\b", "media_stop"),
        (r"\blist\s+(?:audio|music|media)\s+files?\b", "media_list"),
        (r"\bwhat\s+(?:audio|music|media)\s+files?\b", "media_list"),
    ],
    "actions": [
        {"type": "media_play", "handler": "handle_intent", "keywords": "play audio video file mp3 wav mp4 song music media", "description": "Play a media file"},
        {"type": "media_stop", "handler": "handle_intent", "keywords": "stop audio playback media", "description": "Stop playback"},
        {"type": "media_list", "handler": "handle_intent", "keywords": "list audio music media files", "description": "List media files"},
    ],
    "examples": [
        "Play the file music.mp3",
        "Stop playback",
        "List audio files in Downloads",
    ],
}

_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".aiff"}
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm"}
_ALL_MEDIA = _AUDIO_EXTENSIONS | _VIDEO_EXTENSIONS

_active_process: asyncio.subprocess.Process | None = None


async def play_file(path: str) -> bool:
    """Play an audio or video file."""
    global _active_process

    filepath = Path(path).expanduser()
    if not filepath.exists():
        return False

    ext = filepath.suffix.lower()

    # Stop any active playback first
    await stop_playback()

    if ext in _AUDIO_EXTENSIONS:
        # Use afplay for audio (macOS native)
        _active_process = await asyncio.create_subprocess_exec(
            "afplay", str(filepath),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        log.info("Playing audio: %s", filepath)
        return True
    elif ext in _VIDEO_EXTENSIONS:
        # Use open for video (opens in default player)
        proc = await asyncio.create_subprocess_exec("open", str(filepath))
        await proc.wait()
        log.info("Opened video: %s", filepath)
        return True

    return False


async def stop_playback() -> bool:
    """Stop current audio playback."""
    global _active_process
    if _active_process:
        try:
            _active_process.terminate()
            await _active_process.wait()
        except Exception:
            pass
        _active_process = None
        return True

    # Also try to kill any afplay processes
    try:
        proc = await asyncio.create_subprocess_exec(
            "pkill", "-f", "afplay",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        return True
    except Exception:
        return False


def list_media_files(directory: str | None = None, limit: int = 20) -> list[dict]:
    """List media files in a directory."""
    if directory:
        search_dir = Path(directory).expanduser()
    else:
        search_dir = Path.home() / "Music"

    if not search_dir.exists():
        return []

    files = []
    for f in sorted(search_dir.rglob("*")):
        if f.suffix.lower() in _ALL_MEDIA:
            files.append({
                "name": f.name,
                "path": str(f),
                "size_mb": round(f.stat().st_size / 1048576, 1),
                "type": "audio" if f.suffix.lower() in _AUDIO_EXTENSIONS else "video",
            })
            if len(files) >= limit:
                break
    return files


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    query = intent.get("query", "") or intent.get("user_query", "")

    if action == "media_play":
        # Extract file path from query
        # Look for quoted path or path-like string
        path_match = re.search(r'["\']([^"\']+)["\']', query)
        if not path_match:
            path_match = re.search(r'(?:play|open)\s+(?:the\s+)?(?:file\s+)?(.+\.(?:mp3|wav|m4a|mp4|mov|mkv|avi|flac))', query, re.IGNORECASE)
        if not path_match:
            # Try to find a filename-like string
            path_match = re.search(r'(\S+\.(?:mp3|wav|m4a|mp4|mov|mkv|avi|flac))', query, re.IGNORECASE)

        if not path_match:
            await ctx.reply("Which file should I play? Provide a file path or name.")
            return True

        filepath = path_match.group(1).strip()

        # If it's just a filename, search common directories
        if not os.path.sep in filepath and not filepath.startswith("~"):
            for search_dir in [Path.home() / "Music", Path.home() / "Downloads", Path.home() / "Desktop"]:
                candidate = search_dir / filepath
                if candidate.exists():
                    filepath = str(candidate)
                    break

        ok = await play_file(filepath)
        if ok:
            name = Path(filepath).name
            await ctx.reply(f"▶️ Playing: **{name}**")
        else:
            await ctx.reply(f"❌ File not found: {filepath}")
        return True

    elif action == "media_stop":
        ok = await stop_playback()
        await ctx.reply("⏹ Playback stopped." if ok else "Nothing is playing.")
        return True

    elif action == "media_list":
        # Extract directory from query
        dir_match = re.search(r'\bin\s+(\S+)', query, re.IGNORECASE)
        directory = dir_match.group(1) if dir_match else None

        files = list_media_files(directory)
        if not files:
            await ctx.reply("No media files found.")
        else:
            lines = [f"🎵 **Media Files** ({len(files)}):\n"]
            for f in files:
                emoji = "🎵" if f["type"] == "audio" else "🎬"
                lines.append(f"  {emoji} **{f['name']}** ({f['size_mb']} MB)")
            await ctx.reply("\n".join(lines))
        return True

    return False
