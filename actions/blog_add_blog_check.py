"""Integrated blog/feed manager — subscribe, check, and manage RSS feeds.

Combines blog_add and blog_check into a single /blog command with subcommands:
  /blog add <url>      — subscribe to an RSS/Atom feed
  /blog check          — check all feeds for new posts
  /blog list           — list current subscriptions
  /blog remove <query> — unsubscribe by title or URL
  /blog preview <url>  — dry-run: show feed info without subscribing

No external API key required. Feeds are fetched directly via HTTP.
"""

import asyncio
import logging
import re
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import httpx

from config import DB_PATH

log = logging.getLogger("khalil.actions.blog_add_blog_check")

_tables_ready = False


def ensure_tables(conn: sqlite3.Connection):
    """Create tables. Called once at startup."""
    global _tables_ready
    if _tables_ready:
        return
    conn.execute("""
        CREATE TABLE IF NOT EXISTS blog_feeds (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            url         TEXT    NOT NULL UNIQUE,
            title       TEXT    NOT NULL DEFAULT '',
            added_at    TEXT    NOT NULL,
            last_checked_at TEXT,
            last_entry_link TEXT
        )
    """)
    conn.commit()
    _tables_ready = True


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# RSS/Atom parsing (stdlib XML — no feedparser dependency)
# ---------------------------------------------------------------------------

_ATOM_NS = "{http://www.w3.org/2005/Atom}"


def _parse_feed_xml(xml_text: str) -> dict | None:
    """Parse RSS 2.0 or Atom XML into {title, entries: [{title, link, published, summary}]}."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    # Atom feed
    if root.tag == f"{_ATOM_NS}feed" or root.tag == "feed":
        ns = _ATOM_NS if root.tag.startswith("{") else ""
        title_el = root.find(f"{ns}title")
        feed_title = title_el.text.strip() if title_el is not None and title_el.text else ""
        entries = []
        for entry in root.findall(f"{ns}entry")[:50]:
            e_title = entry.findtext(f"{ns}title", "").strip()
            link_el = entry.find(f"{ns}link")
            e_link = link_el.get("href", "") if link_el is not None else ""
            e_published = entry.findtext(f"{ns}published", "") or entry.findtext(f"{ns}updated", "")
            e_summary = (entry.findtext(f"{ns}summary", "") or "")[:200]
            entries.append({"title": e_title, "link": e_link, "published": e_published.strip(), "summary": e_summary.strip()})
        return {"title": feed_title, "entries": entries}

    # RSS 2.0
    channel = root.find("channel")
    if channel is None:
        return None
    feed_title = (channel.findtext("title") or "").strip()
    entries = []
    for item in channel.findall("item")[:50]:
        e_title = (item.findtext("title") or "").strip()
        e_link = (item.findtext("link") or "").strip()
        e_published = (item.findtext("pubDate") or "").strip()
        e_summary = (item.findtext("description") or "")[:200].strip()
        entries.append({"title": e_title, "link": e_link, "published": e_published, "summary": e_summary})
    return {"title": feed_title, "entries": entries}


async def _fetch_feed(url: str) -> dict | None:
    """Fetch and parse an RSS/Atom feed URL. Returns parsed dict or None."""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            resp = await client.get(url, headers={"User-Agent": "Khalil/1.0 (RSS reader)"})
            resp.raise_for_status()
        return _parse_feed_xml(resp.text)
    except Exception as e:
        log.warning("Failed to fetch feed %s: %s", url, e)
        return None


# ---------------------------------------------------------------------------
# Core sync functions (called via asyncio.to_thread where needed)
# ---------------------------------------------------------------------------

def _add_feed_sync(url: str, title: str) -> dict:
    """Insert a feed subscription. Returns the row as dict."""
    now = datetime.now(timezone.utc).isoformat()
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO blog_feeds (url, title, added_at) VALUES (?, ?, ?)",
            (url, title, now),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM blog_feeds WHERE url = ?", (url,)).fetchone()
        return dict(row)
    finally:
        conn.close()


def _list_feeds_sync() -> list[dict]:
    """Return all feed subscriptions."""
    conn = _get_conn()
    try:
        rows = conn.execute("SELECT * FROM blog_feeds ORDER BY added_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _remove_feed_sync(query: str) -> dict | None:
    """Remove a feed matching query (by title or URL substring). Returns removed row or None."""
    conn = _get_conn()
    try:
        rows = conn.execute("SELECT * FROM blog_feeds").fetchall()
        query_lower = query.lower()
        matches = [dict(r) for r in rows if query_lower in r["url"].lower() or query_lower in (r["title"] or "").lower()]
        if len(matches) != 1:
            return None if not matches else {"ambiguous": [m["title"] or m["url"] for m in matches]}
        feed = matches[0]
        conn.execute("DELETE FROM blog_feeds WHERE id = ?", (feed["id"],))
        conn.commit()
        return feed
    finally:
        conn.close()


def _update_last_checked_sync(feed_id: int, last_entry_link: str | None) -> None:
    """Update last_checked_at and optionally last_entry_link."""
    now = datetime.now(timezone.utc).isoformat()
    conn = _get_conn()
    try:
        if last_entry_link:
            conn.execute(
                "UPDATE blog_feeds SET last_checked_at = ?, last_entry_link = ? WHERE id = ?",
                (now, last_entry_link, feed_id),
            )
        else:
            conn.execute(
                "UPDATE blog_feeds SET last_checked_at = ? WHERE id = ?",
                (now, feed_id),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def _extract_url(text: str) -> str | None:
    """Extract the first HTTP(S) URL from text."""
    m = re.search(r"https?://\S+", text)
    if not m:
        return None
    return m.group(0).rstrip(".,;:)")


async def _do_add(args: list[str], reply) -> None:
    url = _extract_url(" ".join(args))
    if not url:
        await reply("Usage: /blog add <feed-url>")
        return

    # Check duplicate
    existing = await asyncio.to_thread(_list_feeds_sync)
    if any(f["url"] == url for f in existing):
        await reply(f"Already subscribed to {url}")
        return

    parsed = await _fetch_feed(url)
    if not parsed:
        await reply(f"Could not parse feed at {url}. Is it a valid RSS/Atom feed?")
        return

    await asyncio.to_thread(_add_feed_sync, url, parsed["title"])
    count = len(parsed["entries"])
    await reply(f"Subscribed to **{parsed['title']}** ({count} entries)\n{url}")


async def _do_preview(args: list[str], reply) -> None:
    """Dry-run: show feed info without subscribing."""
    url = _extract_url(" ".join(args))
    if not url:
        await reply("Usage: /blog preview <feed-url>")
        return

    parsed = await _fetch_feed(url)
    if not parsed:
        await reply(f"Could not parse feed at {url}.")
        return

    lines = [f"**Preview** — {parsed['title']} ({len(parsed['entries'])} entries)"]
    for entry in parsed["entries"][:5]:
        lines.append(f"  - {entry['title']}")
        if entry["link"]:
            lines.append(f"    {entry['link']}")
    if len(parsed["entries"]) > 5:
        lines.append(f"  ...and {len(parsed['entries']) - 5} more")
    lines.append("\nUse `/blog add <url>` to subscribe.")
    await reply("\n".join(lines))


async def _do_check(reply) -> None:
    feeds = await asyncio.to_thread(_list_feeds_sync)
    if not feeds:
        await reply("No feeds. Use `/blog add <url>` to subscribe.")
        return

    new_posts: list[tuple[str, list[dict]]] = []
    for feed in feeds:
        parsed = await _fetch_feed(feed["url"])
        if not parsed or not parsed["entries"]:
            continue

        last_link = feed.get("last_entry_link")
        new_for_feed = []
        for entry in parsed["entries"]:
            if entry["link"] == last_link:
                break
            new_for_feed.append(entry)

        if new_for_feed:
            new_posts.append((parsed["title"], new_for_feed[:5]))

        # Update bookmark
        top_link = parsed["entries"][0]["link"] if parsed["entries"] else None
        await asyncio.to_thread(_update_last_checked_sync, feed["id"], top_link)

    if not new_posts:
        await reply("No new posts across your feeds.")
        return

    total = sum(len(posts) for _, posts in new_posts)
    lines = [f"**New posts** ({total} across {len(new_posts)} feeds)\n"]
    for title, posts in new_posts:
        lines.append(f"**{title}**")
        for p in posts:
            lines.append(f"  - {p['title']}")
            if p["link"]:
                lines.append(f"    {p['link']}")
    # Respect Telegram 4096 char limit
    msg = "\n".join(lines)
    if len(msg) > 4000:
        msg = msg[:3997] + "..."
    await reply(msg)


async def _do_list(reply) -> None:
    feeds = await asyncio.to_thread(_list_feeds_sync)
    if not feeds:
        await reply("No feeds. Use `/blog add <url>` to subscribe.")
        return

    lines = [f"**Your feeds** ({len(feeds)})\n"]
    for i, f in enumerate(feeds, 1):
        title = f["title"] or f["url"]
        checked = f["last_checked_at"] or "never"
        lines.append(f"  {i}. {title}\n     {f['url']}\n     Last checked: {checked}")
    msg = "\n".join(lines)
    if len(msg) > 4000:
        msg = msg[:3997] + "..."
    await reply(msg)


async def _do_remove(args: list[str], reply) -> None:
    query = " ".join(args).strip()
    if not query:
        await reply("Usage: /blog remove <title-or-url>")
        return

    result = await asyncio.to_thread(_remove_feed_sync, query)
    if result is None:
        await reply(f"No feed matching '{query}'.")
    elif "ambiguous" in result:
        names = ", ".join(result["ambiguous"])
        await reply(f"Multiple matches: {names}. Be more specific.")
    else:
        await reply(f"Unsubscribed from **{result.get('title') or result['url']}**")


# ---------------------------------------------------------------------------
# Telegram command handler
# ---------------------------------------------------------------------------

async def handle_blog(update, context):
    """Handle /blog command."""
    args = context.args or []
    reply = update.effective_message.reply_text

    if not args:
        await reply(
            "Usage:\n"
            "  /blog add <url> — subscribe to a feed\n"
            "  /blog check — check for new posts\n"
            "  /blog list — list subscriptions\n"
            "  /blog remove <query> — unsubscribe\n"
            "  /blog preview <url> — preview without subscribing"
        )
        return

    sub = args[0].lower()
    rest = args[1:]

    if sub == "add":
        await _do_add(rest, reply)
    elif sub == "check":
        await _do_check(reply)
    elif sub == "list":
        await _do_list(reply)
    elif sub == "remove":
        await _do_remove(rest, reply)
    elif sub == "preview":
        await _do_preview(rest, reply)
    else:
        # Treat unknown subcommand as URL for add
        if re.search(r"https?://", sub):
            await _do_add(args, reply)
        else:
            await reply(f"Unknown subcommand '{sub}'. Try /blog for usage.")
