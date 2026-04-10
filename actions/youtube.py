"""YouTube Data API v3 integration — liked videos, search, subscriptions, channel stats.

Uses separate OAuth token (TOKEN_FILE_YOUTUBE) with youtube.readonly scope.

NOTE: The YouTube Data API does not expose watch history. get_watch_history()
returns liked videos as a proxy. For actual watch history, use Google Takeout.

All public functions are async — sync Google API calls run in asyncio.to_thread().
"""

import asyncio
import logging

from googleapiclient.discovery import build

from config import TOKEN_FILE_YOUTUBE

log = logging.getLogger("khalil.actions.youtube")

SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]

SKILL = {
    "name": "youtube",
    "description": "YouTube — search videos, liked videos, subscriptions",
    "category": "media",
    "patterns": [
        (r"\bsearch\s+(?:on\s+)?youtube\b", "youtube_search"),
        (r"\bfind\s+(?:a\s+)?video\b", "youtube_search"),
        (r"\byoutube\s+search\b", "youtube_search"),
        (r"\bliked\s+videos?\b", "youtube_liked"),
        (r"\byoutube\s+(?:history|liked)\b", "youtube_liked"),
        (r"\bsearch\s+youtube\s+for\b", "youtube_search"),
        (r"\bliked\s+videos?\s+on\s+(?:yt|youtube)\b", "youtube_liked"),
        (r"\bfind\s+(?:a\s+)?(?:tutorial|video)\s+on\s+youtube\b", "youtube_search"),
    ],
    "actions": [
        {"type": "youtube_search", "handler": "handle_intent", "keywords": "youtube search video find", "description": "Search YouTube"},
        {"type": "youtube_liked", "handler": "handle_intent", "keywords": "youtube liked videos history", "description": "Liked videos"},
    ],
    "examples": ["Search YouTube for Python tutorials", "My liked videos"],
}


def _get_credentials():
    """Get or refresh OAuth credentials for YouTube readonly."""
    from oauth_utils import load_credentials
    return load_credentials(TOKEN_FILE_YOUTUBE, SCOPES)


def _get_youtube_service():
    """Get YouTube Data API v3 service."""
    creds = _get_credentials()
    return build("youtube", "v3", credentials=creds)


def _get_liked_videos_sync(limit: int = 20) -> list[dict]:
    """Fetch liked videos from the 'LL' playlist. Runs in thread."""
    service = _get_youtube_service()
    response = service.playlistItems().list(
        playlistId="LL",
        part="snippet",
        maxResults=min(limit, 50),
    ).execute()

    return [
        {
            "title": item["snippet"]["title"],
            "channel": item["snippet"].get("videoOwnerChannelTitle", ""),
            "video_id": item["snippet"]["resourceId"]["videoId"],
            "published_at": item["snippet"].get("publishedAt", ""),
            "url": f"https://youtube.com/watch?v={item['snippet']['resourceId']['videoId']}",
        }
        for item in response.get("items", [])
    ]


def _search_videos_sync(query: str, limit: int = 5) -> list[dict]:
    """Search YouTube videos. Runs in thread."""
    service = _get_youtube_service()
    response = service.search().list(
        q=query,
        part="snippet",
        type="video",
        maxResults=min(limit, 50),
    ).execute()

    return [
        {
            "title": item["snippet"]["title"],
            "channel": item["snippet"]["channelTitle"],
            "video_id": item["id"]["videoId"],
            "published_at": item["snippet"].get("publishedAt", ""),
            "description": (item["snippet"].get("description") or "")[:200],
            "url": f"https://youtube.com/watch?v={item['id']['videoId']}",
        }
        for item in response.get("items", [])
    ]


def _get_channel_stats_sync(channel_id: str) -> dict:
    """Fetch channel statistics. Runs in thread."""
    service = _get_youtube_service()
    response = service.channels().list(
        id=channel_id,
        part="snippet,statistics",
    ).execute()

    items = response.get("items", [])
    if not items:
        return {"error": f"Channel {channel_id} not found"}

    ch = items[0]
    stats = ch.get("statistics", {})
    return {
        "title": ch["snippet"]["title"],
        "description": (ch["snippet"].get("description") or "")[:200],
        "subscribers": stats.get("subscriberCount", "hidden"),
        "views": stats.get("viewCount", "0"),
        "videos": stats.get("videoCount", "0"),
        "url": f"https://youtube.com/channel/{channel_id}",
    }


def _get_subscriptions_sync(limit: int = 20) -> list[dict]:
    """Fetch authenticated user's subscriptions. Runs in thread."""
    service = _get_youtube_service()
    response = service.subscriptions().list(
        mine=True,
        part="snippet",
        maxResults=min(limit, 50),
        order="alphabetical",
    ).execute()

    return [
        {
            "title": item["snippet"]["title"],
            "channel_id": item["snippet"]["resourceId"]["channelId"],
            "description": (item["snippet"].get("description") or "")[:200],
            "url": f"https://youtube.com/channel/{item['snippet']['resourceId']['channelId']}",
        }
        for item in response.get("items", [])
    ]


async def get_watch_history(limit: int = 20) -> list[dict]:
    """Get recent video activity.

    NOTE: YouTube Data API does not expose watch history. This returns
    liked videos as a proxy. For full watch history, use Google Takeout.
    """
    log.info("Watch history not available via API; returning liked videos as proxy")
    return await asyncio.to_thread(_get_liked_videos_sync, limit)


async def get_liked_videos(limit: int = 20) -> list[dict]:
    """Get liked videos from the authenticated user's 'Liked videos' playlist."""
    return await asyncio.to_thread(_get_liked_videos_sync, limit)


async def search_videos(query: str, limit: int = 5) -> list[dict]:
    """Search YouTube videos by query string."""
    return await asyncio.to_thread(_search_videos_sync, query, limit)


async def get_channel_stats(channel_id: str) -> dict:
    """Get statistics for a YouTube channel."""
    return await asyncio.to_thread(_get_channel_stats_sync, channel_id)


async def get_subscriptions(limit: int = 20) -> list[dict]:
    """Get the authenticated user's YouTube subscriptions."""
    return await asyncio.to_thread(_get_subscriptions_sync, limit)


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle a natural language intent. Returns True if handled."""
    if action == "youtube_search":
        try:
            query = intent.get("query", intent.get("text", ""))
            if not query:
                await ctx.reply("What should I search for on YouTube?")
                return True
            videos = await search_videos(query, limit=5)
            if not videos:
                await ctx.reply(f'No YouTube results for "{query}".')
            else:
                lines = [f'\u25b6\ufe0f YouTube \u2014 "{query}":\n']
                for v in videos:
                    lines.append(f"  \u2022 {v.get('title', '?')}")
                    if v.get("url"):
                        lines.append(f"    {v['url']}")
                await ctx.reply("\n".join(lines))
        except Exception as e:
            await ctx.reply(f"\u274c YouTube search failed: {e}")
        return True
    elif action == "youtube_liked":
        try:
            videos = await get_liked_videos(limit=10)
            if not videos:
                await ctx.reply("No liked videos found.")
            else:
                lines = ["\U0001f44d Liked Videos:\n"]
                for v in videos:
                    lines.append(f"  \u2022 {v.get('title', '?')}")
                await ctx.reply("\n".join(lines))
        except Exception as e:
            await ctx.reply(f"\u274c YouTube failed: {e}")
        return True
    return False
