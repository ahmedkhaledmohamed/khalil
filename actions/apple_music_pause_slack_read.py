"""Pause Apple Music and read Slack — a focus-mode combo action.

Pauses music playback via AppleScript and reads recent messages from a
Slack channel in a single command. Useful for switching from listening
to catching up on team messages.

Requires:
- macOS with Apple Music.app (no token needed for pause)
- Slack bot token (xoxb-) for channel reading
  Setup: keyring.set_password('khalil-assistant', 'slack-bot-token', 'xoxb-...')
  Required scopes: channels:history, channels:read, users:read
"""

import asyncio
import logging
import re
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from config import DB_PATH, KEYRING_SERVICE, TIMEZONE

log = logging.getLogger("khalil.actions.apple_music_pause_slack_read")

_tables_ensured = False

SKILL = {
    "name": "apple_music_pause_slack_read",
    "description": "Pause music and read Slack — focus-mode combo for catching up on messages",
    "category": "extension",
    "patterns": [
        (r"\bpause\b.*\b(?:music|apple\s*music)\b.*\bread\b.*\bslack\b", "apple_music_pause_slack_read"),
        (r"\bstop\b.*\b(?:music|playing)\b.*\b(?:check|read)\b.*\bslack\b", "apple_music_pause_slack_read"),
        (r"\bfocus\s+mode\b.*\bslack\b", "apple_music_pause_slack_read"),
        (r"\bslack\b.*\b(?:pause|stop)\b.*\bmusic\b", "apple_music_pause_slack_read"),
        (r"\bmute\b.*\bread\b.*\bslack\b", "apple_music_pause_slack_read"),
    ],
    "actions": [
        {
            "type": "apple_music_pause_slack_read",
            "handler": "handle_intent",
            "description": "Pause Apple Music and read recent Slack messages",
            "keywords": "pause music stop slack read messages focus channel",
        },
    ],
    "examples": [
        "Pause music and read Slack #general",
        "Stop the music and check Slack",
        "Focus mode — read #team-updates",
    ],
}


def ensure_tables(conn: sqlite3.Connection):
    """Create usage log table. Called once at startup."""
    global _tables_ensured
    if _tables_ensured:
        return
    conn.execute("""CREATE TABLE IF NOT EXISTS focus_mode_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_name TEXT,
        message_count INTEGER,
        music_was_playing INTEGER,
        created_at TEXT NOT NULL
    )""")
    conn.commit()
    _tables_ensured = True


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _log_usage(channel: str | None, message_count: int, music_was_playing: bool):
    """Record a focus-mode activation for usage tracking."""
    conn = _get_conn()
    try:
        ensure_tables(conn)
        conn.execute(
            "INSERT INTO focus_mode_log (channel_name, message_count, music_was_playing, created_at) "
            "VALUES (?, ?, ?, ?)",
            (channel, message_count, int(music_was_playing), datetime.now(ZoneInfo(TIMEZONE)).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


async def pause_and_read(channel: str, count: int = 20) -> dict:
    """Pause Apple Music and read recent Slack messages from a channel.

    Returns {music_paused, was_playing, track, messages, channel}.
    """
    from actions.apple_music import now_playing, pause
    from actions.slack_reader import read_channel

    # Run pause + now_playing concurrently, then read Slack
    track_info, pause_ok = await asyncio.gather(
        now_playing(),
        pause(),
    )
    was_playing = track_info is not None and track_info.get("state") == "playing"

    messages = await read_channel(channel.lstrip("#"), count=count)

    _log_usage(channel.lstrip("#"), len(messages), was_playing)

    return {
        "music_paused": pause_ok,
        "was_playing": was_playing,
        "track": track_info,
        "messages": messages,
        "channel": channel.lstrip("#"),
    }


def _format_result(result: dict) -> str:
    """Format the combined result for Telegram (respects 4096 char limit)."""
    lines = []

    # Music status
    if result["was_playing"] and result["track"]:
        t = result["track"]
        lines.append(f"Paused: **{t['name']}** — {t['artist']}")
    elif result["music_paused"]:
        lines.append("Music paused.")
    else:
        lines.append("Music was not playing.")

    lines.append("")

    # Slack messages
    channel = result["channel"]
    messages = result["messages"]
    if not messages:
        lines.append(f"No messages in #{channel}.")
    else:
        lines.append(f"#{channel} ({len(messages)} messages):")
        for m in messages[-20:]:
            text_preview = m["text"][:100]
            lines.append(f"  [{m['time']}] {m['user']}: {text_preview}")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n... (truncated)"
    return text


# --- Telegram command handler ---

USAGE = (
    "Usage:\n"
    "  /focusslack <#channel> [count] — Pause music + read channel\n"
    "  /focusslack status — Check music state without pausing"
)


async def handle_focusslack(update, context):
    """Handle /focusslack command."""
    args = context.args or []
    if not args:
        await update.message.reply_text(USAGE)
        return

    sub = args[0].lower()

    if sub == "status":
        from actions.apple_music import now_playing
        track = await now_playing()
        if not track:
            await update.message.reply_text("Nothing playing right now.")
        else:
            from actions.apple_music import _format_time
            pos = _format_time(track["position"])
            dur = _format_time(track["duration"])
            state = "Playing" if track["state"] == "playing" else "Paused"
            await update.message.reply_text(
                f"{state}: **{track['name']}** — {track['artist']}\n"
                f"  {track['album']} ({pos}/{dur})"
            )
        return

    channel = sub.lstrip("#")
    count = 20
    if len(args) >= 2:
        try:
            count = min(int(args[1]), 50)
        except ValueError:
            pass

    await update.message.reply_text(f"Pausing music and reading #{channel}...")
    try:
        result = await pause_and_read(channel, count)
        await update.message.reply_text(_format_result(result))
    except Exception as e:
        log.error("Focus-slack failed: %s", e)
        await update.message.reply_text(f"Failed: {e}")


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle a natural language intent. Returns True if handled."""
    if action != "apple_music_pause_slack_read":
        return False

    channel = intent.get("channel", intent.get("query", ""))
    if not channel:
        # No channel specified — pause music and list available channels
        from actions.apple_music import now_playing, pause
        track_info = await now_playing()
        was_playing = track_info and track_info.get("state") == "playing"
        if was_playing:
            await pause()

        from actions.slack_reader import list_channels
        try:
            channels = await list_channels()
            lines = []
            if was_playing and track_info:
                lines.append(f"Paused: **{track_info['name']}** — {track_info['artist']}\n")
            elif was_playing:
                lines.append("Music paused.\n")
            if channels:
                lines.append("Which channel? Joined channels:")
                for ch in channels[:15]:
                    lines.append(f"  #{ch['name']}")
            else:
                lines.append("No Slack channels found. Is the bot added to any?")
            await ctx.reply("\n".join(lines)[:4000])
        except Exception as e:
            await ctx.reply(f"Music paused, but Slack failed: {e}")
        return True

    try:
        result = await pause_and_read(channel, count=20)
        await ctx.reply(_format_result(result))
    except Exception as e:
        await ctx.reply(f"Failed: {e}")
    return True
