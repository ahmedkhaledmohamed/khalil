"""Combined blog feed check and list — single command for feed status.

Integrates the 'blog_check' and 'blog_list' actions that were frequently
used together (34x). Provides a unified /blogfeed command that shows
subscriptions and new posts in one view.

No external API token needed — uses the existing blogwatcher feed storage.
"""

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone

from config import DB_PATH, TIMEZONE

log = logging.getLogger("khalil.actions.blog_check_blog_list")

_tables_ensured = False


def ensure_tables(conn: sqlite3.Connection):
    """Create tables if needed. Called once at startup."""
    global _tables_ensured
    if _tables_ensured:
        return
    conn.execute(
        "CREATE TABLE IF NOT EXISTS blog_feed_checks ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  checked_at TEXT NOT NULL,"
        "  feeds_count INTEGER DEFAULT 0,"
        "  new_posts_count INTEGER DEFAULT 0"
        ")"
    )
    conn.commit()
    _tables_ensured = True


def _log_check(feeds_count: int, new_posts_count: int):
    """Record a feed check in the audit log."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        ensure_tables(conn)
        conn.execute(
            "INSERT INTO blog_feed_checks (checked_at, feeds_count, new_posts_count) VALUES (?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), feeds_count, new_posts_count),
        )
        conn.commit()
    finally:
        conn.close()


# --- Core sync functions (called via asyncio.to_thread) ---


def _get_feeds_and_posts() -> dict:
    """Fetch feed list and check for new posts in one pass.

    Returns {feeds: [...], new_posts: [...], errors: [...]}.
    Delegates to blogwatcher internals for feed parsing.
    """
    from actions.blogwatcher import _load_feeds, _parse_feed, _save_feeds

    feeds = _load_feeds()
    if not feeds:
        return {"feeds": [], "new_posts": [], "errors": []}

    new_posts = []
    errors = []

    for feed in feeds:
        try:
            parsed = _parse_feed(feed["url"])
            if not parsed or not parsed["entries"]:
                errors.append(feed.get("title", feed["url"]))
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
        except Exception as e:
            log.warning("Error checking feed %s: %s", feed.get("url"), e)
            errors.append(feed.get("title", feed["url"]))

    _save_feeds(feeds)
    return {"feeds": feeds, "new_posts": new_posts, "errors": errors}


# --- Async wrappers ---


async def combined_feed_status() -> str:
    """Check all feeds and return a combined status + subscription list."""
    result = await asyncio.to_thread(_get_feeds_and_posts)

    feeds = result["feeds"]
    new_posts = result["new_posts"]
    errors = result["errors"]

    if not feeds:
        return "No feed subscriptions. Use 'add feed <url>' to subscribe."

    # Log the check
    total_new = sum(len(g["posts"]) for g in new_posts)
    _log_check(len(feeds), total_new)

    lines = []

    # Section 1: New posts
    if new_posts:
        lines.append(f"**New posts** ({total_new} across {len(new_posts)} feeds)\n")
        for group in new_posts:
            lines.append(f"  **{group['feed_title']}**")
            for post in group["posts"]:
                title = post["title"][:80] or "(untitled)"
                lines.append(f"    - {title}")
                if post.get("link"):
                    lines.append(f"      {post['link']}")
        lines.append("")
    else:
        lines.append("No new posts.\n")

    # Section 2: Subscriptions
    lines.append(f"**Subscriptions** ({len(feeds)})")
    for i, f in enumerate(feeds, 1):
        title = f.get("title", f["url"])
        checked = f.get("last_checked", "never")
        if checked != "never":
            checked = checked[:16].replace("T", " ")
        lines.append(f"  {i}. {title} (checked: {checked})")

    if errors:
        lines.append(f"\n_Failed to fetch: {', '.join(errors)}_")

    # Respect Telegram 4096 char limit
    output = "\n".join(lines)
    if len(output) > 4000:
        output = output[:3990] + "\n...(truncated)"

    return output


async def feed_stats() -> str:
    """Show check history stats."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        ensure_tables(conn)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT checked_at, feeds_count, new_posts_count "
            "FROM blog_feed_checks ORDER BY id DESC LIMIT 10"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return "No feed check history yet."

    lines = ["**Recent feed checks**\n"]
    for r in rows:
        ts = r["checked_at"][:16].replace("T", " ")
        lines.append(f"  {ts} — {r['feeds_count']} feeds, {r['new_posts_count']} new posts")
    return "\n".join(lines)


async def handle_blogfeed(update, context):
    """Handle /blogfeed command.

    Subcommands:
      /blogfeed        — check feeds + show subscriptions
      /blogfeed stats  — show recent check history
    """
    args = context.args or []
    subcommand = args[0].lower() if args else ""

    if subcommand == "stats":
        reply = await feed_stats()
    else:
        await update.message.reply_text("Checking feeds...")
        reply = await combined_feed_status()

    await update.message.reply_text(reply, parse_mode="Markdown")
