"""Combined browser navigate + extract — one page load, both outputs.

Merges the frequently co-used 'browser_navigate' and 'browser_extract' actions
into a single command. Users who asked for both ended up paying two full page
loads; this module loads the page once and returns screenshot + text together.

No external API / token required — uses Playwright headless Chromium locally.
Financial domains remain hard-blocked via browser.is_financial_url.
"""
import asyncio
import logging
import re
import sqlite3
from datetime import datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from config import DB_PATH, KEYRING_SERVICE, TIMEZONE
from actions.browser import (
    is_financial_url,
    navigate_and_screenshot,
    extract_page_text,
)

log = logging.getLogger("khalil.actions.browser_extract_browser_navigate")

_TELEGRAM_LIMIT = 4096
_DEFAULT_MAX_CHARS = 3500
_HISTORY_LIMIT = 50
_tables_ensured = False

SKILL = {
    "name": "browser_extract_browser_navigate",
    "description": "Actions 'browser_extract' and 'browser_navigate' used together 22x — integration opportunity",
    "category": "extension",
    "patterns": [
        (r"\b(?:open|visit|browse|load)\b.+\band\s+(?:extract|read|scrape)\b", "browser_extract_browser_navigate"),
        (r"\b(?:navigate|go)\s+to\b.+\band\s+(?:extract|read|pull)\b", "browser_extract_browser_navigate"),
        (r"\bscreenshot\s+and\s+extract\b", "browser_extract_browser_navigate"),
        (r"\b(?:grab|fetch)\s+(?:the\s+)?page\s+and\s+(?:text|content)\b", "browser_extract_browser_navigate"),
        (r"\bpage\s+summary\s+for\b\s+https?://", "browser_extract_browser_navigate"),
    ],
    "actions": [
        {
            "type": "browser_extract_browser_navigate",
            "handler": "handle_",
            "description": "Actions 'browser_extract' and 'browser_navigate' used together 22x — integration opportunity",
            "keywords": "browser_extract_browser_navigate browser navigate extract screenshot page url",
        },
    ],
    "examples": [
        "Screenshot and extract text from https://example.com",
        "Open https://news.ycombinator.com and extract the headlines",
    ],
}


def ensure_tables(conn: sqlite3.Connection):
    """Create tables for logging combined browser ops. Called once at startup."""
    global _tables_ensured
    if _tables_ensured:
        return
    conn.execute(
        """CREATE TABLE IF NOT EXISTS browser_combined_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at TEXT NOT NULL,
            url TEXT NOT NULL,
            title TEXT,
            text_chars INTEGER,
            screenshot_path TEXT,
            blocked INTEGER DEFAULT 0
        )"""
    )
    conn.commit()
    _tables_ensured = True


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _log_run(url: str, title: str | None, text_chars: int, screenshot_path: str | None, blocked: bool):
    """Persist a run record. Isolated so failures don't break the user reply."""
    conn = _get_conn()
    try:
        ensure_tables(conn)
        now = datetime.now(ZoneInfo(TIMEZONE)).isoformat()
        conn.execute(
            "INSERT INTO browser_combined_log "
            "(fetched_at, url, title, text_chars, screenshot_path, blocked) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (now, url, title, text_chars, screenshot_path, 1 if blocked else 0),
        )
        conn.commit()
    except sqlite3.Error as e:
        log.warning("Failed to log browser combined run: %s", e)
    finally:
        conn.close()


def _get_history(limit: int = 10) -> list[dict]:
    """Fetch recent combined-run records."""
    conn = _get_conn()
    try:
        ensure_tables(conn)
        rows = conn.execute(
            "SELECT fetched_at, url, title, text_chars, blocked "
            "FROM browser_combined_log ORDER BY id DESC LIMIT ?",
            (min(limit, _HISTORY_LIMIT),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


_URL_PATTERN = r"https?://\S+"


def _extract_url(args: list[str]) -> str | None:
    """Find the first URL-looking token in args."""
    for tok in args:
        if re.match(_URL_PATTERN, tok, re.IGNORECASE):
            return tok.rstrip(").,;")
    return None


def _valid_url(url: str) -> bool:
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


async def navigate_and_extract(
    url: str,
    selector: str | None = None,
    max_chars: int = _DEFAULT_MAX_CHARS,
) -> dict:
    """Combined op: navigate, screenshot, extract text — runs both fetches in parallel.

    Returns {url, title, screenshot_path, text, blocked, error}.
    """
    if is_financial_url(url):
        _log_run(url, None, 0, None, blocked=True)
        return {"url": url, "blocked": True, "error": "Financial site — blocked by guardrail"}

    try:
        (nav_res, text) = await asyncio.gather(
            navigate_and_screenshot(url),
            extract_page_text(url, selector),
            return_exceptions=True,
        )
    except Exception as e:
        log.exception("navigate_and_extract gather failed")
        return {"url": url, "blocked": False, "error": str(e)}

    screenshot_path, title = (None, None)
    if isinstance(nav_res, tuple):
        screenshot_path, title = nav_res
    elif isinstance(nav_res, Exception):
        log.warning("Screenshot failed for %s: %s", url, nav_res)
        title = None

    if isinstance(text, Exception):
        log.warning("Text extract failed for %s: %s", url, text)
        text_out = f"(extraction failed: {text})"
    else:
        text_out = text or ""

    truncated = text_out[:max_chars]
    _log_run(url, title, len(text_out), screenshot_path, blocked=False)
    return {
        "url": url,
        "title": title,
        "screenshot_path": screenshot_path,
        "text": truncated,
        "text_total_chars": len(text_out),
        "blocked": False,
        "error": None,
    }


def _format_result(result: dict) -> str:
    if result.get("blocked"):
        return f"🚫 Blocked: {result.get('error')}"
    if result.get("error") and not result.get("text"):
        return f"❌ Error: {result['error']}"

    title = result.get("title") or "(untitled)"
    url = result["url"]
    total = result.get("text_total_chars", 0)
    shown = len(result.get("text", ""))
    header = f"🌐 {title}\n{url}\n"
    if total > shown:
        header += f"(showing {shown}/{total} chars)\n"
    body = result.get("text", "")
    out = header + "\n" + body
    if len(out) > _TELEGRAM_LIMIT:
        out = out[: _TELEGRAM_LIMIT - 20] + "\n…(truncated)"
    return out


def _format_history(records: list[dict]) -> str:
    if not records:
        return "No combined browser runs logged yet."
    lines = [f"📜 Recent browser navigate+extract runs ({len(records)})", ""]
    for r in records:
        ts = (r.get("fetched_at") or "")[:16]
        mark = "🚫" if r.get("blocked") else "✅"
        title = (r.get("title") or "")[:40]
        lines.append(f"  {mark} {ts}  {r.get('text_chars', 0)}ch  {title}")
        lines.append(f"      {r.get('url', '')}")
    return "\n".join(lines)


async def handle_(update, context):
    """Handle / command — combined navigate + extract.

    Subcommands:
        / <url> [selector]        — fetch page: screenshot + extracted text
        / preview <url>           — dry-run: validate URL + guardrails, no fetch
        / history [N]             — show last N runs (default 10, max 50)
    """
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage:\n"
            "  / <url> [css-selector]   — screenshot + extract\n"
            "  / preview <url>          — dry-run check, no fetch\n"
            "  / history [N]            — recent runs"
        )
        return

    sub = args[0].lower()

    if sub == "history":
        limit = 10
        if len(args) > 1:
            try:
                limit = min(int(args[1]), _HISTORY_LIMIT)
            except ValueError:
                await update.message.reply_text("Usage: / history [N]")
                return
        records = await asyncio.to_thread(_get_history, limit)
        await update.message.reply_text(_format_history(records))
        return

    if sub == "preview":
        if len(args) < 2:
            await update.message.reply_text("Usage: / preview <url>")
            return
        url = args[1]
        if not _valid_url(url):
            await update.message.reply_text(f"❌ Not a valid http(s) URL: {url}")
            return
        if is_financial_url(url):
            await update.message.reply_text(
                f"🚫 Would block: {url}\nReason: financial domain guardrail."
            )
            return
        await update.message.reply_text(
            f"✅ Preview OK — {url}\nWould: load page, screenshot, extract body text."
        )
        return

    url = _extract_url(args) or args[0]
    if not _valid_url(url):
        await update.message.reply_text(f"❌ Not a valid http(s) URL: {url}")
        return

    selector = None
    if len(args) > 1 and args[-1] != url:
        candidate = args[-1]
        if not re.match(_URL_PATTERN, candidate, re.IGNORECASE):
            selector = candidate

    await update.message.reply_text(f"Fetching {url}…")
    try:
        result = await navigate_and_extract(url, selector=selector)
    except Exception as e:
        log.exception("navigate_and_extract failed")
        await update.message.reply_text(f"❌ Error: {e}")
        return

    if result.get("screenshot_path") and not result.get("blocked"):
        try:
            with open(result["screenshot_path"], "rb") as fh:
                await update.message.reply_photo(
                    fh, caption=(result.get("title") or url)[:1024]
                )
        except Exception as e:
            log.warning("Failed to send screenshot: %s", e)

    await update.message.reply_text(_format_result(result))
