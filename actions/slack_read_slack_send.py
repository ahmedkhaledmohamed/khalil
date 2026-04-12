"""Combined Slack read+send — read channels and send/reply in one flow.

Bot token (xoxb-) via keyring. Does NOT support search.messages (needs xoxp-).
Setup: keyring.set_password('khalil-assistant', 'slack-bot-token', 'xoxb-...')
Required bot scopes: channels:history, channels:read, chat:write, users:read
"""

import asyncio
import logging
import re
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import keyring

from config import DB_PATH, KEYRING_SERVICE, TIMEZONE

log = logging.getLogger("khalil.actions.slack_read_slack_send")

SLACK_API = "https://slack.com/api"
_TOKEN_KEY = "slack-bot-token"
_tables_ensured = False

SKILL = {
    "name": "slack_read_slack_send",
    "description": "Read Slack channels and send messages — combined read+send workflow",
    "category": "communication",
    "patterns": [
        (r"\bread\b.*\bslack\b.*\b(?:and|then)\b.*\b(?:send|reply|respond)\b", "slack_read_send"),
        (r"\bslack\b.*\bread\b.*\b(?:send|reply)\b", "slack_read_send"),
        (r"\breply\b.*\bslack\b.*\bthread\b", "slack_reply"),
        (r"\bsend\b.*\bslack\b.*\bmessage\b", "slack_send_msg"),
        (r"\bpost\b.*\bto\b.*\bslack\b", "slack_send_msg"),
    ],
    "actions": [
        {"type": "slack_read_send", "handler": "handle_intent",
         "description": "Read a Slack channel then send a message",
         "keywords": "slack read send reply channel message"},
        {"type": "slack_send_msg", "handler": "handle_intent",
         "description": "Send a message to a Slack channel",
         "keywords": "slack send post message channel"},
        {"type": "slack_reply", "handler": "handle_intent",
         "description": "Reply to a Slack thread",
         "keywords": "slack reply thread respond"},
    ],
    "examples": ["Read #general and reply with an update", "Send a Slack message to #team-updates"],
}


def ensure_tables(conn: sqlite3.Connection):
    """Create send log table. Called once at startup."""
    global _tables_ensured
    if _tables_ensured:
        return
    conn.execute("""CREATE TABLE IF NOT EXISTS slack_send_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id TEXT NOT NULL,
        channel_name TEXT, thread_ts TEXT, message_text TEXT NOT NULL,
        message_ts TEXT, sent_at TEXT NOT NULL)""")
    conn.commit()
    _tables_ensured = True


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _get_token() -> str:
    token = keyring.get_password(KEYRING_SERVICE, _TOKEN_KEY)
    if not token:
        raise RuntimeError(f"Slack token not set. Run: keyring.set_password('{KEYRING_SERVICE}', '{_TOKEN_KEY}', 'xoxb-...')")
    return token


def _slack_post_sync(endpoint: str, payload: dict) -> dict:
    """Synchronous Slack API POST — called via asyncio.to_thread()."""
    with httpx.Client(timeout=15) as client:
        resp = client.post(
            f"{SLACK_API}/{endpoint}",
            headers={"Authorization": f"Bearer {_get_token()}"},
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Slack API error: {data.get('error', 'unknown')}")
        return data


# --- Core send operations ---

def _send_message_sync(channel_id: str, text: str, thread_ts: str | None = None) -> dict:
    """Send a message to a Slack channel (or thread). Logs to DB."""
    payload = {"channel": channel_id, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts

    data = _slack_post_sync("chat.postMessage", payload)
    msg = data.get("message", {})

    conn = _get_conn()
    try:
        ensure_tables(conn)
        conn.execute(
            "INSERT INTO slack_send_log (channel_id, channel_name, thread_ts, message_text, message_ts, sent_at) VALUES (?, ?, ?, ?, ?, ?)",
            (channel_id, None, thread_ts, text[:500], msg.get("ts"), datetime.now(ZoneInfo(TIMEZONE)).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ts": msg.get("ts"), "channel": channel_id, "text": text}


def _get_send_history(limit: int = 10) -> list[dict]:
    """Get recent send log entries."""
    conn = _get_conn()
    try:
        ensure_tables(conn)
        rows = conn.execute(
            "SELECT channel_id, channel_name, thread_ts, message_text, message_ts, sent_at "
            "FROM slack_send_log ORDER BY sent_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --- Async wrappers ---

async def send_message(channel: str, text: str, thread_ts: str | None = None) -> dict:
    """Resolve channel name and send a message."""
    from actions.slack_reader import _resolve_channel_sync

    channel_id = await asyncio.to_thread(_resolve_channel_sync, channel.lstrip("#"))
    if not channel_id:
        raise ValueError(f"Channel not found: {channel}")
    return await asyncio.to_thread(_send_message_sync, channel_id, text, thread_ts)


# --- Telegram command handler ---

USAGE = ("Usage:\n"
    "  /slackchat read <#ch> [n]  — Read messages\n"
    "  /slackchat send <#ch> <msg>  — Preview\n"
    "  /slackchat send <#ch> confirm <msg>  — Send\n"
    "  /slackchat reply <#ch> <ts> [confirm] <msg>\n"
    "  /slackchat history  — Recent sends")


async def handle_slackchat(update, context):
    """Handle /slackchat command \u2014 combined Slack read+send."""
    args = context.args or []
    if not args:
        await update.message.reply_text(USAGE)
        return

    sub = args[0].lower()

    if sub == "read":
        from actions.slack_reader import read_channel

        if len(args) < 2:
            await update.message.reply_text("Usage: /slackchat read <#channel> [count]")
            return
        channel = args[1].lstrip("#")
        count = 20
        if len(args) >= 3:
            try:
                count = min(int(args[2]), 100)
            except ValueError:
                pass
        try:
            messages = await read_channel(channel, count)
        except Exception as e:
            await update.message.reply_text(f"\u274c Read failed: {e}")
            return
        if not messages:
            await update.message.reply_text(f"No messages in #{channel}.")
            return
        lines = [f"\U0001f4ac #{channel} ({len(messages)} messages):\n"]
        for m in messages[-count:]:
            lines.append(f"[{m['time']}] {m['user']}: {m['text']}")
        text = "\n".join(lines)
        if len(text) > 4000:
            text = text[-4000:]
        await update.message.reply_text(text)

    elif sub == "send":
        if len(args) < 3:
            await update.message.reply_text("Usage: /slackchat send <#channel> [confirm] <message>")
            return
        channel = args[1].lstrip("#")
        confirmed = args[2].lower() == "confirm" and len(args) > 3
        message = " ".join(args[3:] if confirmed else args[2:])
        if confirmed:
            try:
                await send_message(channel, message)
                await update.message.reply_text(f"\u2705 Sent to #{channel}:\n{message[:200]}")
            except Exception as e:
                await update.message.reply_text(f"\u274c Send failed: {e}")
        else:
            await update.message.reply_text(
                f"\U0001f50d Dry-run \u2014 would send to #{channel}:\n\n{message[:500]}\n\n"
                f"Run `/slackchat send #{channel} confirm {message}` to send.")

    elif sub == "reply":
        if len(args) < 4:
            await update.message.reply_text("Usage: /slackchat reply <#channel> <ts> [confirm] <message>")
            return
        channel = args[1].lstrip("#")
        thread_ts = args[2]
        if not re.match(r"\d+\.\d+", thread_ts):
            await update.message.reply_text(f"Invalid thread ts: {thread_ts} (expect e.g. 1712345678.123456)")
            return
        confirmed = args[3].lower() == "confirm" and len(args) > 4
        message = " ".join(args[4:] if confirmed else args[3:])
        if confirmed:
            try:
                await send_message(channel, message, thread_ts=thread_ts)
                await update.message.reply_text(f"\u2705 Replied in #{channel} thread:\n{message[:200]}")
            except Exception as e:
                await update.message.reply_text(f"\u274c Reply failed: {e}")
        else:
            await update.message.reply_text(
                f"\U0001f50d Dry-run \u2014 reply in #{channel} thread {thread_ts}:\n\n{message[:500]}\n\n"
                f"Run `/slackchat reply #{channel} {thread_ts} confirm {message}` to send.")

    elif sub == "history":
        history = await asyncio.to_thread(_get_send_history, 10)
        if not history:
            await update.message.reply_text("No messages sent yet.")
        else:
            lines = ["\U0001f4ca Recent Slack sends:\n"]
            for h in history:
                ts = h["sent_at"][:16].replace("T", " ")
                ch = h["channel_name"] or h["channel_id"]
                thread = " (thread)" if h["thread_ts"] else ""
                lines.append(f"  {ts} \u2192 {ch}{thread}: {(h['message_text'] or '')[:60]}")
            await update.message.reply_text("\n".join(lines))

    else:
        await update.message.reply_text(USAGE)


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle a natural language intent. Returns True if handled."""
    if action == "slack_send_msg":
        channel = intent.get("channel", "")
        message = intent.get("text", intent.get("message", ""))
        if not channel:
            await ctx.reply("Which channel? E.g., 'send a Slack message to #general saying hello'")
            return True
        if not message:
            await ctx.reply(f"What should I say in #{channel.lstrip('#')}?")
            return True
        await ctx.reply(
            f"\U0001f50d Preview \u2014 would send to #{channel.lstrip('#')}:\n\n"
            f"{message[:500]}\n\nSay 'confirm' to send, or 'cancel' to abort.")
        return True

    if action == "slack_reply":
        await ctx.reply("To reply in a thread: /slackchat reply <#channel> <thread_ts> <message>")
        return True

    if action == "slack_read_send":
        channel = intent.get("channel", "")
        if not channel:
            await ctx.reply("Which channel? E.g., 'read #general and reply with an update'")
            return True
        try:
            from actions.slack_reader import read_channel
            messages = await read_channel(channel.lstrip("#"), count=10)
            if not messages:
                await ctx.reply(f"No messages in #{channel.lstrip('#')}.")
            else:
                lines = [f"\U0001f4ac #{channel.lstrip('#')} ({len(messages)} messages):\n"]
                for m in messages[-10:]:
                    lines.append(f"  [{m['time']}] {m['user']}: {m['text'][:100]}")
                lines.append("\nWhat would you like to say? I'll preview before sending.")
                await ctx.reply("\n".join(lines)[:4000])
        except Exception as e:
            await ctx.reply(f"\u274c Slack read failed: {e}")
        return True

    return False
