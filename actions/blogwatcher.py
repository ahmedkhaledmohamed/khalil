"""RSS/Blog feed watcher — subscribe, poll, and digest.

Stores feed subscriptions and last-seen timestamps in SQLite.
Includes a background sensor for proactive new-post notifications.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("khalil.actions.blogwatcher")

SKILL = {
    "name": "blogwatcher",
    "description": "Subscribe to RSS/blog feeds, check for new posts, and get digests",
    "category": "information",
    "patterns": [
        (r"\badd\s+(?:this\s+)?(?:rss|feed|blog)\b", "blog_add"),
        (r"\bsubscribe\s+(?:to\s+)?(?:this\s+)?(?:rss|feed|blog)\b", "blog_add"),
        (r"\bfollow\s+(?:this\s+)?(?:blog|feed)\b", "blog_add"),
        (r"\bcheck\s+(?:my\s+)?feeds?\b", "blog_check"),
        (r"\bnew\s+(?:blog\s+)?posts?\b", "blog_check"),
        (r"\bany\s+new\s+(?:articles?|posts?)\b", "blog_check"),
        (r"\blist\s+(?:my\s+)?(?:feeds?|subscriptions?|blogs?)\b", "blog_list"),
        (r"\bmy\s+(?:rss\s+)?feeds?\b", "blog_list"),
        (r"\b(?:remove|unsubscribe|delete)\s+(?:this\s+)?(?:feed|blog|subscription)\b", "blog_remove"),
    ],
    "actions": [
        {"type": "blog_add", "handler": "handle_intent", "keywords": "add subscribe follow rss feed blog", "description": "Subscribe to an RSS feed"},
        {"type": "blog_check", "handler": "handle_intent", "keywords": "check new posts articles feeds blog", "description": "Check feeds for new posts"},
        {"type": "blog_list", "handler": "handle_intent", "keywords": "list feeds subscriptions blogs rss", "description": "List feed subscriptions"},
        {"type": "blog_remove", "handler": "handle_intent", "keywords": "remove unsubscribe delete feed blog", "description": "Unsubscribe from a feed"},
    ],
    "examples": [
        "Add this RSS feed: https://blog.example.com/feed",
        "Check my feeds",
        "Any new blog posts?",
        "List my subscriptions",
    ],
    "sensor": {"function": "sense_feeds", "interval_min": 60},
    "voice": {"response_style": "brief"},
}

_DATA_DIR = Path(__file__).parent.parent / "data"
_FEEDS_FILE = _DATA_DIR / "feeds.json"


def _load_feeds() -> list[dict]:
    """Load feed subscriptions from JSON."""
    if _FEEDS_FILE.exists():
        return json.loads(_FEEDS_FILE.read_text())
    return []


def _save_feeds(feeds: list[dict]) -> None:
    """Save feed subscriptions to JSON."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _FEEDS_FILE.write_text(json.dumps(feeds, indent=2, default=str))


def _parse_feed(url: str) -> dict | None:
    """Parse an RSS/Atom feed and return feed metadata + entries."""
    try:
        import feedparser
        d = feedparser.parse(url)
        if d.bozo and not d.entries:
            return None
        return {
            "title": d.feed.get("title", url),
            "entries": [
                {
                    "title": e.get("title", ""),
                    "link": e.get("link", ""),
                    "published": e.get("published", ""),
                    "summary": (e.get("summary", "") or "")[:200],
                }
                for e in d.entries[:20]
            ],
        }
    except ImportError:
        log.info("feedparser not installed — run: pip install feedparser")
        return None
    except Exception as e:
        log.error("Feed parse failed for %s: %s", url, e)
        return None


async def add_feed(url: str) -> str:
    """Subscribe to an RSS feed."""
    import re
    url_match = re.search(r"https?://\S+", url)
    if not url_match:
        return "Please provide a valid URL."
    feed_url = url_match.group(0).rstrip(".,;:)")

    feeds = _load_feeds()
    if any(f["url"] == feed_url for f in feeds):
        return f"Already subscribed to {feed_url}"

    parsed = await asyncio.to_thread(_parse_feed, feed_url)
    if not parsed:
        return f"Could not parse feed at {feed_url}. Is it a valid RSS/Atom feed?"

    feeds.append({
        "url": feed_url,
        "title": parsed["title"],
        "added": datetime.now(timezone.utc).isoformat(),
        "last_checked": None,
        "last_entry_link": None,
    })
    _save_feeds(feeds)
    return f"Subscribed to **{parsed['title']}** ({len(parsed['entries'])} entries)"


async def list_feeds() -> str:
    """List all feed subscriptions."""
    feeds = _load_feeds()
    if not feeds:
        return "No feed subscriptions. Use 'add feed <url>' to subscribe."
    lines = ["📰 Your feeds:"]
    for i, f in enumerate(feeds, 1):
        title = f.get("title", f["url"])
        checked = f.get("last_checked", "never")
        lines.append(f"  {i}. {title}\n     {f['url']}\n     Last checked: {checked}")
    return "\n".join(lines)


async def check_feeds() -> str:
    """Check all feeds for new posts since last check."""
    feeds = _load_feeds()
    if not feeds:
        return "No feeds to check. Use 'add feed <url>' to subscribe."

    new_posts = []
    for feed in feeds:
        parsed = await asyncio.to_thread(_parse_feed, feed["url"])
        if not parsed or not parsed["entries"]:
            continue

        last_link = feed.get("last_entry_link")
        new_for_feed = []
        for entry in parsed["entries"]:
            if entry["link"] == last_link:
                break
            new_for_feed.append(entry)

        if new_for_feed:
            new_posts.append({
                "feed_title": parsed["title"],
                "posts": new_for_feed[:5],
            })

        # Update last-seen
        if parsed["entries"]:
            feed["last_entry_link"] = parsed["entries"][0]["link"]
        feed["last_checked"] = datetime.now(timezone.utc).isoformat()

    _save_feeds(feeds)

    if not new_posts:
        return "No new posts across your feeds."

    lines = ["📰 New posts:"]
    total = 0
    for group in new_posts:
        lines.append(f"\n**{group['feed_title']}**")
        for post in group["posts"]:
            lines.append(f"  • {post['title']}")
            if post["link"]:
                lines.append(f"    {post['link']}")
            total += 1
    lines.insert(1, f"({total} new across {len(new_posts)} feeds)\n")
    return "\n".join(lines)


async def remove_feed(query: str) -> str:
    """Remove a feed by title or URL substring."""
    feeds = _load_feeds()
    query_lower = query.lower()
    matches = [
        f for f in feeds
        if query_lower in f["url"].lower() or query_lower in f.get("title", "").lower()
    ]
    if not matches:
        return f"No feed matching '{query}' found."
    if len(matches) > 1:
        names = ", ".join(f.get("title", f["url"]) for f in matches)
        return f"Multiple matches: {names}. Be more specific."
    removed = matches[0]
    feeds.remove(removed)
    _save_feeds(feeds)
    return f"Unsubscribed from **{removed.get('title', removed['url'])}**"


async def sense_feeds() -> dict:
    """Background sensor: poll feeds for new posts (called by agent loop)."""
    feeds = _load_feeds()
    if not feeds:
        return {"new_posts": 0}

    total_new = 0
    for feed in feeds:
        try:
            parsed = await asyncio.to_thread(_parse_feed, feed["url"])
            if not parsed or not parsed["entries"]:
                continue
            last_link = feed.get("last_entry_link")
            if last_link and parsed["entries"][0]["link"] != last_link:
                total_new += 1
        except Exception:
            continue

    return {"feeds_count": len(feeds), "new_posts": total_new}


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle blogwatcher intents."""
    import re
    query = intent.get("query", "") or intent.get("user_query", "")

    if action == "blog_add":
        result = await add_feed(query)
        await ctx.reply(result)
        return True

    if action == "blog_list":
        result = await list_feeds()
        await ctx.reply(result)
        return True

    if action == "blog_check":
        await ctx.reply("📰 Checking feeds...")
        result = await check_feeds()
        await ctx.reply(result)
        return True

    if action == "blog_remove":
        # Extract feed identifier from query
        keyword = re.sub(
            r"\b(?:remove|unsubscribe|delete|from|the|this|feed|blog|subscription)\b",
            "", query, flags=re.IGNORECASE,
        ).strip()
        if not keyword:
            await ctx.reply("Which feed should I remove? Give me a name or URL.")
            return True
        result = await remove_feed(keyword)
        await ctx.reply(result)
        return True

    return False
