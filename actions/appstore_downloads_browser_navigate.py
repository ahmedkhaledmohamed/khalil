"""App health dashboard — fetch App Store download stats and screenshot the listing page.

Combines appstore_downloads (App Store Connect API) and browser_navigate (Playwright)
into a single command that returns download metrics alongside a visual snapshot of the
app's store listing.

API credentials (App Store Connect JWT):
- appstore-key-id, appstore-issuer-id, appstore-private-key stored in keyring
- Setup: keyring.set_password('khalil-assistant', 'appstore-key-id', '...')
"""

import asyncio
import logging
import re
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from config import DB_PATH, KEYRING_SERVICE, TIMEZONE

log = logging.getLogger("khalil.actions.appstore_downloads_browser_navigate")

APP_STORE_LISTING_URL = "https://apps.apple.com/app/id{app_id}"
_tables_ensured = False

SKILL = {
    "name": "appstore_downloads_browser_navigate",
    "description": "App health dashboard — download stats + App Store page screenshot",
    "category": "extension",
    "patterns": [
        (r"\bapp\s+health\b", "appstore_downloads_browser_navigate"),
        (r"\bapp\s+(?:store\s+)?dashboard\b", "appstore_downloads_browser_navigate"),
        (r"\bzia\s+(?:health|dashboard|overview)\b", "appstore_downloads_browser_navigate"),
        (r"\b(?:check|show)\s+(?:my\s+)?app\b.*\b(?:screenshot|page|listing)\b", "appstore_downloads_browser_navigate"),
        (r"\bdownloads?\b.*\bscreenshot\b", "appstore_downloads_browser_navigate"),
    ],
    "actions": [
        {
            "type": "appstore_downloads_browser_navigate",
            "handler": "handle_apphealth",
            "description": "Fetch download stats and screenshot the App Store listing",
            "keywords": "app store downloads stats screenshot health dashboard zia listing",
        },
    ],
    "examples": [
        "Show me the app health dashboard",
        "Zia health check with screenshot",
        "App downloads and store page screenshot",
    ],
}


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_tables(conn: sqlite3.Connection):
    """Create lookup log table. Called once at startup."""
    global _tables_ensured
    if _tables_ensured:
        return
    conn.execute("""CREATE TABLE IF NOT EXISTS appstore_health_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        app_id TEXT NOT NULL,
        downloads_summary TEXT,
        screenshot_path TEXT,
        page_title TEXT,
        checked_at TEXT NOT NULL
    )""")
    conn.commit()
    _tables_ensured = True


def _log_health_check(app_id: str, downloads_summary: str,
                      screenshot_path: str | None, page_title: str):
    """Persist a health check to the log table."""
    conn = _get_conn()
    try:
        ensure_tables(conn)
        conn.execute(
            "INSERT INTO appstore_health_log "
            "(app_id, downloads_summary, screenshot_path, page_title, checked_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (app_id, downloads_summary, screenshot_path, page_title,
             datetime.now(ZoneInfo(TIMEZONE)).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def _get_recent_checks(limit: int = 10) -> list[dict]:
    """Return recent health check log entries."""
    conn = _get_conn()
    try:
        ensure_tables(conn)
        rows = conn.execute(
            "SELECT app_id, downloads_summary, page_title, checked_at "
            "FROM appstore_health_log ORDER BY checked_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


async def app_health_check(app_id: str, days: int = 7) -> dict:
    """Run a combined health check: downloads + store page screenshot.

    Returns dict with downloads data, screenshot path, and page title.
    """
    from actions.appstore import get_app_downloads, get_app_ratings
    from actions.browser import navigate_and_screenshot

    # Run API call and browser screenshot concurrently
    downloads_task = asyncio.create_task(get_app_downloads(app_id, days=days))
    ratings_task = asyncio.create_task(get_app_ratings(app_id))
    listing_url = APP_STORE_LISTING_URL.format(app_id=app_id)
    screenshot_task = asyncio.create_task(navigate_and_screenshot(listing_url))

    downloads = await downloads_task
    ratings = await ratings_task
    screenshot_path, page_title = await screenshot_task

    summary = (
        f"downloads={downloads.get('total', '?')}, "
        f"daily_avg={downloads.get('daily_avg', '?')}, "
        f"rating={ratings.get('rating', '?')}, "
        f"reviews={ratings.get('review_count', '?')}"
    )
    _log_health_check(app_id, summary, screenshot_path, page_title)

    return {
        "app_id": app_id,
        "downloads": downloads,
        "ratings": ratings,
        "screenshot_path": screenshot_path,
        "page_title": page_title,
        "listing_url": listing_url,
    }


def _format_health_text(result: dict, days: int) -> str:
    """Format health check result into a Telegram-friendly message."""
    dl = result["downloads"]
    rt = result["ratings"]
    lines = [f"App Health Dashboard ({days}d)\n"]

    # Downloads section
    if dl.get("error"):
        lines.append(f"  Downloads: error — {dl['error']}")
    else:
        lines.append(f"  Downloads (total): {dl.get('total', '?')}")
        lines.append(f"  Daily avg: {dl.get('daily_avg', '?')}")

    # Ratings section
    if rt.get("error"):
        lines.append(f"  Ratings: error — {rt['error']}")
    else:
        avg = rt.get("rating")
        count = rt.get("review_count", 0)
        stars = f"{avg:.1f}" if avg else "?"
        lines.append(f"  Rating: {stars} ({count} reviews)")
        for review in rt.get("reviews", [])[:3]:
            lines.append(f"    {review['rating']}: {review.get('title', '')[:60]}")

    # Page info
    lines.append(f"\n  Page: {result.get('page_title', '?')}")
    lines.append(f"  URL: {result.get('listing_url', '?')}")
    return "\n".join(lines)


USAGE = (
    "Usage:\n"
    "  /apphealth [app_id] [days]  — Full health check\n"
    "  /apphealth history  — Recent checks"
)


async def handle_apphealth(update, context):
    """Handle /apphealth command — combined downloads + screenshot."""
    args = context.args or []
    if not args:
        # Default: use ZIA_APP_ID
        from config import ZIA_APP_ID
        app_id = ZIA_APP_ID
        if not app_id:
            await update.message.reply_text(
                "No app ID configured. Set ZIA_APP_ID in config.py or pass one:\n" + USAGE
            )
            return
        days = 7
    elif args[0].lower() == "history":
        checks = await asyncio.to_thread(_get_recent_checks, 10)
        if not checks:
            await update.message.reply_text("No health checks recorded yet.")
            return
        lines = ["Recent app health checks:\n"]
        for c in checks:
            ts = c["checked_at"][:16].replace("T", " ")
            lines.append(f"  {ts}  {c['app_id']}  {c['downloads_summary']}")
        await update.message.reply_text("\n".join(lines)[:4000])
        return
    else:
        app_id = args[0]
        days = 7
        if len(args) >= 2:
            try:
                days = min(int(args[1]), 90)
            except ValueError:
                pass

    await update.message.reply_text(f"Running health check for {app_id} ({days}d)...")

    try:
        result = await app_health_check(app_id, days=days)
    except Exception as e:
        log.error("Health check failed for %s: %s", app_id, e)
        await update.message.reply_text(f"Health check failed: {e}")
        return

    text = _format_health_text(result, days)
    screenshot = result.get("screenshot_path")

    if screenshot:
        try:
            with open(screenshot, "rb") as f:
                await update.message.reply_photo(photo=f, caption=text[:1024])
        except Exception as e:
            log.warning("Failed to send screenshot: %s", e)
            await update.message.reply_text(text[:4000])
    else:
        await update.message.reply_text(text[:4000])


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle a natural language intent. Returns True if handled."""
    if action == "appstore_downloads_browser_navigate":
        from config import ZIA_APP_ID

        app_id = intent.get("app_id", ZIA_APP_ID)
        if not app_id:
            await ctx.reply("No app ID configured. Set ZIA_APP_ID in config.py.")
            return True

        days = 7
        days_str = intent.get("days", "")
        if days_str:
            try:
                days = min(int(days_str), 90)
            except (ValueError, TypeError):
                pass

        try:
            result = await app_health_check(app_id, days=days)
            text = _format_health_text(result, days)
            screenshot = result.get("screenshot_path")
            if screenshot:
                await ctx.reply_photo(screenshot, caption=text[:1024])
            else:
                await ctx.reply(text[:4000])
        except Exception as e:
            from resilience import format_user_error
            await ctx.reply(format_user_error(e, skill_name="App Health"))
        return True
    return False
