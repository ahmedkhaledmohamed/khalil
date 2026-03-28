"""Read Slack messages via the Slack Web API.

Uses a Slack Bot token stored in the system keyring.
All public functions are async — sync HTTP calls run in asyncio.to_thread().
Setup: keyring.set_password('khalil-assistant', 'slack-bot-token', 'xoxb-...')

Note: search.messages requires a user token (xoxp-), not a bot token.
Search is not supported in this version — only channel reading and listing.
"""

import asyncio
import logging
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import keyring

from config import DB_PATH, KEYRING_SERVICE, TIMEZONE

log = logging.getLogger("khalil.actions.slack_reader")

SLACK_API = "https://slack.com/api"
_TOKEN_KEY = "slack-bot-token"

SKILL = {
    "name": "slack",
    "description": "Read Slack messages and send messages via Slack Web API",
    "category": "communication",
    "patterns": [
        (r"\bsend\s+(?:a\s+)?slack\s+message\b", "slack_send"),
        (r"\bpost\s+to\s+slack\b", "slack_send"),
        (r"\bmessage\s+on\s+slack\b", "slack_send"),
        (r"\bread\s+slack\b", "slack_read"),
        (r"\bslack\s+(?:messages?|channel)\b", "slack_read"),
    ],
    "actions": [
        {"type": "slack_send", "handler": None, "keywords": "send post slack message channel", "description": "Send a Slack message"},
        {"type": "slack_read", "handler": None, "keywords": "read slack messages channel", "description": "Read Slack messages"},
    ],
    "examples": ["Send a Slack message to #general", "Read Slack messages"],
}

# --- DB helpers ---

def ensure_tables(conn: sqlite3.Connection):
    """Create tables used by the Slack reader extension."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS slack_channels (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            is_member INTEGER DEFAULT 0,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS slack_users (
            id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.commit()


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


# --- Slack API helpers ---

def _get_token() -> str:
    token = keyring.get_password(KEYRING_SERVICE, _TOKEN_KEY)
    if not token:
        raise RuntimeError(f"Slack token not found. Set via keyring: '{KEYRING_SERVICE}' / '{_TOKEN_KEY}'")
    return token


def _slack_get_sync(endpoint: str, params: dict | None = None) -> dict:
    """Synchronous Slack API GET — called via asyncio.to_thread()."""
    with httpx.Client(timeout=15) as client:
        resp = client.get(
            f"{SLACK_API}/{endpoint}",
            headers={"Authorization": f"Bearer {_get_token()}"},
            params=params or {},
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Slack API error: {data.get('error', 'unknown')}")
        return data


# --- User resolution ---

def _resolve_user_sync(user_id: str) -> str:
    """Resolve a Slack user ID to a display name, with caching."""
    if not user_id or not user_id.startswith("U"):
        return user_id or "bot"

    conn = _get_conn()
    row = conn.execute(
        "SELECT display_name FROM slack_users WHERE id = ?", (user_id,)
    ).fetchone()
    conn.close()

    if row:
        return row["display_name"]

    # Cache miss — fetch from API
    try:
        data = _slack_get_sync("users.info", {"user": user_id})
        user = data.get("user", {})
        profile = user.get("profile", {})
        name = (
            profile.get("display_name")
            or profile.get("real_name")
            or user.get("real_name")
            or user_id
        )
        tz = ZoneInfo(TIMEZONE)
        now = datetime.now(tz).isoformat()
        conn = _get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO slack_users (id, display_name, updated_at) VALUES (?, ?, ?)",
            (user_id, name, now),
        )
        conn.commit()
        conn.close()
        return name
    except Exception as e:
        log.debug("Failed to resolve user %s: %s", user_id, e)
        return user_id


# --- Channel helpers ---

def _refresh_channels_sync() -> list[dict]:
    """Fetch and cache the channel list."""
    channels = []
    cursor = None
    while True:
        params = {"types": "public_channel,private_channel", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = _slack_get_sync("conversations.list", params)
        channels.extend(data.get("channels", []))
        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz).isoformat()
    conn = _get_conn()
    for ch in channels:
        conn.execute(
            "INSERT OR REPLACE INTO slack_channels (id, name, is_member, updated_at) VALUES (?, ?, ?, ?)",
            (ch["id"], ch["name"], int(ch.get("is_member", False)), now),
        )
    conn.commit()
    conn.close()
    log.info("Cached %d Slack channels", len(channels))
    return channels


def _resolve_channel_sync(name_or_id: str) -> str | None:
    """Resolve a channel name or ID to a channel ID."""
    name_or_id = name_or_id.lstrip("#")
    if name_or_id.startswith("C") and len(name_or_id) >= 9:
        return name_or_id

    conn = _get_conn()
    row = conn.execute(
        "SELECT id FROM slack_channels WHERE name = ?", (name_or_id,)
    ).fetchone()
    conn.close()

    if row:
        return row["id"]

    # Cache miss — refresh and retry
    _refresh_channels_sync()
    conn = _get_conn()
    row = conn.execute(
        "SELECT id FROM slack_channels WHERE name = ?", (name_or_id,)
    ).fetchone()
    conn.close()
    return row["id"] if row else None


# --- Core operations ---

def _read_channel_sync(channel: str, count: int = 20) -> list[dict]:
    """Read recent messages from a channel."""
    channel_id = _resolve_channel_sync(channel)
    if not channel_id:
        raise ValueError(f"Channel not found: {channel}")

    data = _slack_get_sync("conversations.history", {"channel": channel_id, "limit": count})
    messages = data.get("messages", [])
    tz = ZoneInfo(TIMEZONE)
    result = []
    for msg in messages:
        ts = float(msg.get("ts", 0))
        dt = datetime.fromtimestamp(ts, tz=tz)
        user_id = msg.get("user", msg.get("username", "bot"))
        result.append({
            "user": _resolve_user_sync(user_id),
            "text": msg.get("text", ""),
            "ts": msg["ts"],
            "time": dt.strftime("%Y-%m-%d %H:%M"),
        })
    return list(reversed(result))


def _list_channels_sync() -> list[dict]:
    """List channels the bot is a member of."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, name FROM slack_channels WHERE is_member = 1 ORDER BY name"
    ).fetchall()
    conn.close()
    if not rows:
        _refresh_channels_sync()
        conn = _get_conn()
        rows = conn.execute(
            "SELECT id, name FROM slack_channels WHERE is_member = 1 ORDER BY name"
        ).fetchall()
        conn.close()
    return [dict(r) for r in rows]


# --- Async wrappers ---

async def read_channel(channel: str, count: int = 20) -> list[dict]:
    """Read recent messages from a Slack channel."""
    return await asyncio.to_thread(_read_channel_sync, channel, count)


async def list_channels() -> list[dict]:
    """List channels the bot is a member of."""
    return await asyncio.to_thread(_list_channels_sync)


# --- Telegram command handler ---

USAGE = (
    "Usage:\n"
    "  /slack read <#channel> [count] — Recent messages\n"
    "  /slack channels — List joined channels"
)


async def handle_slack(update, context):
    """Handle the /slack Telegram command."""
    args = context.args or []
    if not args:
        await update.message.reply_text(USAGE)
        return

    subcommand = args[0].lower()

    if subcommand == "read":
        if len(args) < 2:
            await update.message.reply_text("Usage: /slack read <#channel> [count]")
            return
        channel = args[1]
        count = 20
        if len(args) >= 3:
            try:
                count = min(int(args[2]), 100)
            except ValueError:
                pass

        await update.message.reply_text(f"Reading #{channel.lstrip('#')}...")
        try:
            messages = await read_channel(channel, count)
        except Exception as e:
            log.error("Slack read failed: %s", e)
            await update.message.reply_text(f"Failed: {e}")
            return

        if not messages:
            await update.message.reply_text("No messages found.")
            return

        lines = []
        for m in messages[-count:]:
            lines.append(f"[{m['time']}] {m['user']}: {m['text']}")
        text = "\n".join(lines)
        if len(text) > 4000:
            text = text[-4000:]
        await update.message.reply_text(text)

    elif subcommand == "channels":
        await update.message.reply_text("Fetching channels...")
        try:
            channels = await list_channels()
        except Exception as e:
            log.error("Slack channels failed: %s", e)
            await update.message.reply_text(f"Failed: {e}")
            return

        if not channels:
            await update.message.reply_text("No channels found. Is the bot added to any channels?")
            return

        text = "Joined channels:\n" + "\n".join(f"  #{ch['name']}" for ch in channels)
        if len(text) > 4000:
            text = text[:4000]
        await update.message.reply_text(text)

    else:
        await update.message.reply_text(USAGE)
