"""App Store ratings dashboard — API ratings + browser screenshots in one flow.

Combines App Store Connect API (ratings, reviews) with headless browser
to screenshot the public listing and track rating trends over time.

Credentials (App Store Connect API — stored in keyring):
- appstore-key-id, appstore-issuer-id, appstore-private-key (.p8 contents)
Setup: keyring.set_password('khalil-assistant', 'appstore-key-id', '...')
"""

import asyncio
import logging
import re
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from config import DB_PATH, TIMEZONE

log = logging.getLogger("khalil.actions.appstore_ratings_browser_navigate")

APP_STORE_WEB_URL = "https://apps.apple.com/app/id{app_id}"
_tables_ensured = False

SKILL = {
    "name": "appstore_ratings_browser_navigate",
    "description": "App Store ratings dashboard — fetch reviews via API and screenshot the listing page",
    "category": "extension",
    "patterns": [
        (r"\bapp\s+store\b.*\b(?:screenshot|listing|page)\b", "appstore_ratings_browser_navigate"),
        (r"\b(?:zia|app)\s+(?:rating|review)s?\b.*\b(?:screenshot|page|trend)\b", "appstore_ratings_browser_navigate"),
        (r"\b(?:screenshot|capture)\b.*\bapp\s+store\b", "appstore_ratings_browser_navigate"),
        (r"\brating\s+trend\b", "appstore_rating_trends"),
        (r"\bapp\s+store\b.*\btrend\b", "appstore_rating_trends"),
    ],
    "actions": [
        {"type": "appstore_ratings_browser_navigate", "handler": "handle_appreviews",
         "description": "Fetch App Store ratings and screenshot the listing page",
         "keywords": "app store ratings reviews screenshot listing page zia"},
        {"type": "appstore_rating_trends", "handler": "handle_appreviews",
         "description": "Show rating trend over time from stored snapshots",
         "keywords": "app store rating trend history snapshot"},
    ],
    "examples": ["Screenshot the App Store listing for Zia", "Show app rating trends"],
}


def ensure_tables(conn: sqlite3.Connection):
    """Create rating snapshot table. Called once at startup."""
    global _tables_ensured
    if _tables_ensured:
        return
    conn.execute("""CREATE TABLE IF NOT EXISTS appstore_rating_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        app_id TEXT NOT NULL, avg_rating REAL,
        review_count INTEGER, snapshot_at TEXT NOT NULL)""")
    conn.commit()
    _tables_ensured = True


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_db():
    global _tables_ensured
    if _tables_ensured:
        return
    conn = _get_conn()
    try:
        ensure_tables(conn)
    finally:
        conn.close()


# --- Core functions (delegate to existing action modules) ---

async def fetch_ratings(app_id: str) -> dict:
    """Fetch ratings via the existing appstore module."""
    from actions.appstore import get_app_ratings
    return await get_app_ratings(app_id)


async def screenshot_listing(app_id: str) -> tuple[str | None, str]:
    """Screenshot the public App Store listing page."""
    from actions.browser import navigate_and_screenshot
    return await navigate_and_screenshot(APP_STORE_WEB_URL.format(app_id=app_id))


async def fetch_and_screenshot(app_id: str) -> dict:
    """Fetch API ratings and screenshot the listing concurrently."""
    ratings, (path, title) = await asyncio.gather(
        fetch_ratings(app_id), screenshot_listing(app_id)
    )
    return {"ratings": ratings, "screenshot_path": path, "page_title": title}


def save_snapshot(app_id: str, avg_rating: float | None, review_count: int) -> int:
    """Store a rating snapshot. Returns the snapshot ID."""
    _ensure_db()
    now = datetime.now(ZoneInfo(TIMEZONE)).isoformat()
    conn = _get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO appstore_rating_snapshots (app_id, avg_rating, review_count, snapshot_at) "
            "VALUES (?, ?, ?, ?)", (app_id, avg_rating, review_count, now))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_trends(app_id: str, limit: int = 30) -> list[dict]:
    """Retrieve recent rating snapshots for trend display."""
    _ensure_db()
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT avg_rating, review_count, snapshot_at FROM appstore_rating_snapshots "
            "WHERE app_id = ? ORDER BY snapshot_at DESC LIMIT ?", (app_id, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _format_trends(trends: list[dict]) -> str:
    if not trends:
        return "No snapshots yet. Use `/appreviews snapshot` to start tracking."
    lines = ["**Rating Trend** (newest first)\n"]
    for t in trends:
        r = f"{t['avg_rating']:.2f}" if t["avg_rating"] is not None else "N/A"
        lines.append(f"  {t['snapshot_at'][:10]}  {r}\u2605  ({t['review_count']} reviews)")
    return "\n".join(lines)


def _format_ratings(ratings: dict) -> str:
    if "error" in ratings:
        return f"API error: {ratings['error']}"
    avg = ratings.get("rating")
    count = ratings.get("review_count", 0)
    header = f"\u2b50 **{avg}\u2605** ({count} reviews)" if avg else "No ratings yet"
    reviews = ratings.get("reviews", [])
    if not reviews:
        return header
    lines = [header, ""]
    for r in reviews[:5]:
        stars = "\u2605" * r["rating"] + "\u2606" * (5 - r["rating"])
        body = r.get("body", "")[:120]
        lines.append(f"  {stars}  **{r.get('title', '')}**")
        if body:
            lines.append(f"    {body}")
    if len(reviews) > 5:
        lines.append(f"  ...and {len(reviews) - 5} more")
    return "\n".join(lines)


def _get_app_id(args: list[str]) -> str | None:
    """Extract app_id from args, falling back to config."""
    for arg in args:
        if re.match(r"^\d{5,}$", arg):
            return arg
    from config import ZIA_APP_ID
    return ZIA_APP_ID or None


async def _reply_screenshot(msg, path: str | None, title: str, url: str):
    """Send screenshot photo if available, else text fallback."""
    if path:
        with open(path, "rb") as f:
            await msg.reply_photo(photo=f, caption=f"{title}\n{url}")
    else:
        await msg.reply_text(f"Navigation result: {title}")


# --- Telegram handler ---

async def handle_appreviews(update, context):
    """Handle /appreviews command.

    Subcommands: (none) | snapshot | trends | page <url> | preview
    """
    args = context.args or []
    sub = args[0].lower() if args else ""
    msg = update.message

    if sub == "preview":
        app_id = _get_app_id(args[1:])
        if not app_id:
            await msg.reply_text("No app ID. Set ZIA_APP_ID in config or pass an ID.")
            return
        url = APP_STORE_WEB_URL.format(app_id=app_id)
        await msg.reply_text(
            f"**Preview** (dry-run)\n\nWould fetch ratings for app `{app_id}`\n"
            f"Would screenshot: {url}\nNo data written.")
        return

    if sub == "trends":
        app_id = _get_app_id(args[1:])
        if not app_id:
            await msg.reply_text("No app ID configured.")
            return
        await msg.reply_text(_format_trends(get_trends(app_id)))
        return

    if sub == "snapshot":
        app_id = _get_app_id(args[1:])
        if not app_id:
            await msg.reply_text("No app ID configured.")
            return
        await msg.reply_text("Fetching ratings for snapshot...")
        try:
            ratings = await fetch_ratings(app_id)
            if "error" in ratings:
                await msg.reply_text(f"API error: {ratings['error']}")
                return
            sid = save_snapshot(app_id, ratings.get("rating"), ratings.get("review_count", 0))
            avg = ratings.get("rating")
            r_str = f"{avg:.2f}\u2605" if avg else "N/A"
            await msg.reply_text(f"Snapshot #{sid} saved: {r_str} ({ratings.get('review_count', 0)} reviews)")
        except Exception as e:
            log.error("Snapshot failed: %s", e)
            await msg.reply_text(f"Snapshot failed: {e}")
        return

    if sub == "page":
        url = args[1] if len(args) > 1 else None
        if not url or not url.startswith("http"):
            await msg.reply_text("Usage: `/appreviews page <url>`")
            return
        from actions.browser import is_financial_url, navigate_and_screenshot
        if is_financial_url(url):
            await msg.reply_text("Blocked: cannot automate financial sites.")
            return
        await msg.reply_text("Navigating...")
        try:
            path, title = await navigate_and_screenshot(url)
            await _reply_screenshot(msg, path, title, url)
        except Exception as e:
            log.error("Page screenshot failed: %s", e)
            await msg.reply_text(f"Screenshot failed: {e}")
        return

    # Default: fetch ratings + screenshot concurrently
    app_id = _get_app_id(args)
    if not app_id:
        await msg.reply_text("No app ID configured. Set ZIA_APP_ID or pass one:\n`/appreviews <app_id>`")
        return
    await msg.reply_text("Fetching ratings and screenshotting listing...")
    try:
        result = await fetch_and_screenshot(app_id)
        await msg.reply_text(_format_ratings(result["ratings"])[:4096])
        if result["screenshot_path"]:
            url = APP_STORE_WEB_URL.format(app_id=app_id)
            await _reply_screenshot(msg, result["screenshot_path"], result["page_title"], url)
    except Exception as e:
        log.error("App review dashboard failed: %s", e)
        await msg.reply_text(f"Failed: {e}")


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle natural language intent. Returns True if handled."""
    from config import ZIA_APP_ID

    if action == "appstore_rating_trends":
        app_id = intent.get("app_id", ZIA_APP_ID)
        if not app_id:
            await ctx.reply("No app ID configured.")
            return True
        await ctx.reply(_format_trends(get_trends(app_id)))
        return True

    if action == "appstore_ratings_browser_navigate":
        app_id = intent.get("app_id", ZIA_APP_ID)
        if not app_id:
            await ctx.reply("No app ID configured.")
            return True
        try:
            result = await fetch_and_screenshot(app_id)
            await ctx.reply(_format_ratings(result["ratings"])[:4096])
            if result["screenshot_path"]:
                await ctx.reply_photo(result["screenshot_path"], caption=result["page_title"])
        except Exception as e:
            await ctx.reply(f"App review fetch failed: {e}")
        return True

    return False
