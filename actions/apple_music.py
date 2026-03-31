"""Apple Music control via AppleScript — play, pause, skip, queue, now playing.

No API key or external library required. Uses asyncio.create_subprocess_exec
for non-blocking osascript calls. Same pattern as apple_reminders.py.
"""

import asyncio
import logging
import re

log = logging.getLogger("khalil.actions.apple_music")

SKILL = {
    "name": "apple_music",
    "description": "Control Apple Music — play, pause, skip, now playing, queue",
    "category": "media",
    "patterns": [
        (r"\bnow\s+playing\b", "apple_music_now_playing"),
        (r"\bwhat(?:'s|\s+is)\s+playing\b", "apple_music_now_playing"),
        (r"\bcurrent\s+(?:song|track)\b", "apple_music_now_playing"),
        (r"\bpause\s+(?:the\s+)?music\b", "apple_music_pause"),
        (r"\bstop\s+(?:the\s+)?music\b", "apple_music_pause"),
        (r"\bresume\s+(?:the\s+)?music\b", "apple_music_play"),
        (r"\bplay\s+music\b", "apple_music_play"),
        (r"\bunpause\b", "apple_music_play"),
        (r"\bskip\s+(?:this\s+)?(?:song|track)\b", "apple_music_skip"),
        (r"\bnext\s+(?:song|track)\b", "apple_music_skip"),
        (r"\bprevious\s+(?:song|track)\b", "apple_music_previous"),
        (r"\bgo\s+back\s+(?:a\s+)?(?:song|track)\b", "apple_music_previous"),
        (r"\bplay\s+(?:the\s+)?(?:album|playlist|song|track)\s+", "apple_music_play_item"),
        (r"\bqueue\b.*\bmusic\b", "apple_music_queue"),
        (r"\bmusic\b.*\bqueue\b", "apple_music_queue"),
        (r"\bup\s+next\b", "apple_music_queue"),
        (r"\brecently\s+played\b", "apple_music_recent"),
        (r"\brecent\s+(?:songs|tracks|music)\b", "apple_music_recent"),
    ],
    "actions": [
        {"type": "apple_music_now_playing", "handler": "handle_intent", "keywords": "music now playing current song track", "description": "Show currently playing track"},
        {"type": "apple_music_play", "handler": "handle_intent", "keywords": "music play resume unpause", "description": "Resume playback"},
        {"type": "apple_music_pause", "handler": "handle_intent", "keywords": "music pause stop", "description": "Pause playback"},
        {"type": "apple_music_skip", "handler": "handle_intent", "keywords": "music skip next song track", "description": "Skip to next track"},
        {"type": "apple_music_previous", "handler": "handle_intent", "keywords": "music previous back song track", "description": "Go to previous track"},
        {"type": "apple_music_play_item", "handler": "handle_intent", "keywords": "music play album playlist song artist", "description": "Play a specific album, playlist, or song"},
        {"type": "apple_music_queue", "handler": "handle_intent", "keywords": "music queue up next", "description": "Show playback queue"},
        {"type": "apple_music_recent", "handler": "handle_intent", "keywords": "music recently played recent songs tracks", "description": "Show recently played tracks"},
    ],
    "examples": [
        "What's playing?",
        "Pause the music",
        "Skip this song",
        "Play the album Midnights",
        "Show my queue",
    ],
}


# ---------------------------------------------------------------------------
# AppleScript runner
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

async def now_playing() -> dict | None:
    """Get the currently playing track. Returns {name, artist, album, position, duration, state}."""
    script = (
        'tell application "Music"\n'
        '  if player state is not stopped then\n'
        '    set trackName to name of current track\n'
        '    set trackArtist to artist of current track\n'
        '    set trackAlbum to album of current track\n'
        '    set trackDuration to duration of current track\n'
        '    set trackPosition to player position\n'
        '    set pState to player state as string\n'
        '    return trackName & "|||" & trackArtist & "|||" & trackAlbum & "|||" & trackPosition & "|||" & trackDuration & "|||" & pState\n'
        '  else\n'
        '    return "STOPPED"\n'
        '  end if\n'
        'end tell'
    )
    stdout, rc = await _run_osascript(script)
    if rc != 0 or stdout == "STOPPED":
        return None

    parts = stdout.split("|||")
    if len(parts) < 6:
        return None

    return {
        "name": parts[0].strip(),
        "artist": parts[1].strip(),
        "album": parts[2].strip(),
        "position": float(parts[3].strip()) if parts[3].strip() else 0,
        "duration": float(parts[4].strip()) if parts[4].strip() else 0,
        "state": parts[5].strip(),
    }


async def play() -> bool:
    """Resume playback."""
    _, rc = await _run_osascript('tell application "Music" to play')
    return rc == 0


async def pause() -> bool:
    """Pause playback."""
    _, rc = await _run_osascript('tell application "Music" to pause')
    return rc == 0


async def skip() -> bool:
    """Skip to next track."""
    _, rc = await _run_osascript('tell application "Music" to next track')
    return rc == 0


async def previous() -> bool:
    """Go to previous track."""
    _, rc = await _run_osascript('tell application "Music" to previous track')
    return rc == 0


async def play_item(query: str) -> bool:
    """Search and play a track, album, or playlist by name."""
    safe_query = query.replace('"', '\\"')
    # Search in library first, then play the first match
    script = (
        'tell application "Music"\n'
        f'  set searchResults to search playlist "Library" for "{safe_query}"\n'
        '  if (count of searchResults) > 0 then\n'
        '    play item 1 of searchResults\n'
        '    return "OK"\n'
        '  else\n'
        '    return "NOT_FOUND"\n'
        '  end if\n'
        'end tell'
    )
    stdout, rc = await _run_osascript(script)
    return rc == 0 and stdout == "OK"


async def get_queue(limit: int = 10) -> list[dict]:
    """Get upcoming tracks in the queue. Returns list of {name, artist, album}."""
    # Apple Music doesn't expose queue directly via AppleScript.
    # We can get the current playlist's upcoming tracks instead.
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


async def recently_played(limit: int = 10) -> list[dict]:
    """Get recently played tracks. Returns list of {name, artist, album, played_date}."""
    script = (
        'tell application "Music"\n'
        '  set output to ""\n'
        f'  set maxCount to {limit}\n'
        '  set i to 0\n'
        '  set recentTracks to (every track of playlist "Recently Played")\n'
        '  repeat with t in recentTracks\n'
        '    set i to i + 1\n'
        '    if i > maxCount then exit repeat\n'
        '    set pDate to ""\n'
        '    try\n'
        '      set pDate to played date of t as string\n'
        '    end try\n'
        '    set output to output & name of t & "|||" & artist of t & "|||" & album of t & "|||" & pDate & linefeed\n'
        '  end repeat\n'
        '  return output\n'
        'end tell'
    )
    stdout, rc = await _run_osascript(script)
    if rc != 0:
        return []

    results = []
    for line in stdout.split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split("|||", 3)
        results.append({
            "name": parts[0].strip() if parts else "",
            "artist": parts[1].strip() if len(parts) > 1 else "",
            "album": parts[2].strip() if len(parts) > 2 else "",
            "played_date": parts[3].strip() if len(parts) > 3 else "",
        })
    return results


def _format_time(seconds: float) -> str:
    """Format seconds as m:ss."""
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


# ---------------------------------------------------------------------------
# Intent handler
# ---------------------------------------------------------------------------

async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle a natural language intent. Returns True if handled."""
    query = intent.get("query", "") or intent.get("user_query", "")

    if action == "apple_music_now_playing":
        track = await now_playing()
        if not track:
            await ctx.reply("Nothing is playing right now.")
        else:
            pos = _format_time(track["position"])
            dur = _format_time(track["duration"])
            state = "▶️" if track["state"] == "playing" else "⏸"
            await ctx.reply(
                f"{state} **{track['name']}**\n"
                f"  {track['artist']} — {track['album']}\n"
                f"  {pos} / {dur}"
            )
        return True

    elif action == "apple_music_play":
        success = await play()
        await ctx.reply("▶️ Playing." if success else "❌ Could not resume playback.")
        return True

    elif action == "apple_music_pause":
        success = await pause()
        await ctx.reply("⏸ Paused." if success else "❌ Could not pause.")
        return True

    elif action == "apple_music_skip":
        success = await skip()
        if success:
            track = await now_playing()
            if track:
                await ctx.reply(f"⏭ Now playing: **{track['name']}** — {track['artist']}")
            else:
                await ctx.reply("⏭ Skipped.")
        else:
            await ctx.reply("❌ Could not skip track.")
        return True

    elif action == "apple_music_previous":
        success = await previous()
        if success:
            track = await now_playing()
            if track:
                await ctx.reply(f"⏮ Now playing: **{track['name']}** — {track['artist']}")
            else:
                await ctx.reply("⏮ Previous track.")
        else:
            await ctx.reply("❌ Could not go to previous track.")
        return True

    elif action == "apple_music_play_item":
        # Extract what to play
        text = re.sub(r"\bplay\s+(?:the\s+)?(?:album|playlist|song|track|artist)?\s*", "", query, flags=re.IGNORECASE)
        text = text.strip()
        if not text:
            await ctx.reply("What should I play?")
            return True
        success = await play_item(text)
        if success:
            track = await now_playing()
            if track:
                await ctx.reply(f"▶️ Playing: **{track['name']}** — {track['artist']}")
            else:
                await ctx.reply(f"▶️ Playing \"{text}\"")
        else:
            await ctx.reply(f"❌ Could not find \"{text}\" in your library.")
        return True

    elif action == "apple_music_queue":
        tracks = await get_queue()
        if not tracks:
            await ctx.reply("Queue is empty or nothing is playing.")
        else:
            lines = [f"🎵 Up Next ({len(tracks)} tracks):\n"]
            for i, t in enumerate(tracks, 1):
                lines.append(f"  {i}. **{t['name']}** — {t['artist']}")
            await ctx.reply("\n".join(lines))
        return True

    elif action == "apple_music_recent":
        tracks = await recently_played()
        if not tracks:
            await ctx.reply("No recently played tracks found.")
        else:
            lines = [f"🎵 Recently Played ({len(tracks)}):\n"]
            for t in tracks:
                line = f"  • **{t['name']}** — {t['artist']}"
                if t.get("played_date"):
                    line += f" ({t['played_date']})"
                lines.append(line)
            await ctx.reply("\n".join(lines))
        return True

    return False
