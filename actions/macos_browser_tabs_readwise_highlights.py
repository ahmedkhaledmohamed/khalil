"""Cross-reference open browser tabs with Readwise highlights.

Fetches open Safari/Chrome tabs and searches Readwise for highlights
matching the articles currently being read. Uses the existing Readwise
API token (same as actions/readwise.py).

Setup:
    python3 -c "import keyring; keyring.set_password('khalil-assistant', 'readwise-api-token', 'YOUR_TOKEN')"
"""

import asyncio
import logging
import re
import sqlite3
from datetime import datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from config import DB_PATH, TIMEZONE

log = logging.getLogger("khalil.actions.macos_browser_tabs_readwise_highlights")

_tables_ensured = False

SKILL = {
    "name": "macos_browser_tabs_readwise_highlights",
    "description": "Cross-reference open browser tabs with Readwise highlights",
    "category": "reading",
    "patterns": [
        (r"\bhighlights?\s+(?:for|from)\s+(?:my\s+)?(?:open\s+)?tabs?\b", "macos_browser_tabs_readwise_highlights"),
        (r"\b(?:open\s+)?tabs?\b.*\breadwise\b", "macos_browser_tabs_readwise_highlights"),
        (r"\breadwise\b.*\b(?:open\s+)?tabs?\b", "macos_browser_tabs_readwise_highlights"),
        (r"\bwhat\s+(?:am\s+i|i'?m)\s+reading\b.*\bhighlights?\b", "macos_browser_tabs_readwise_highlights"),
    ],
    "actions": [
        {
            "type": "macos_browser_tabs_readwise_highlights",
            "handler": "handle_intent",
            "description": "Find Readwise highlights for articles open in browser tabs",
            "keywords": "browser tabs readwise highlights reading match open safari chrome articles",
        },
    ],
    "examples": ["Highlights for my open tabs", "Match my tabs to Readwise"],
}


def ensure_tables(conn: sqlite3.Connection):
    """Create tables for logging tab-highlight matches."""
    global _tables_ensured
    if _tables_ensured:
        return
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tab_highlight_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checked_at TEXT NOT NULL,
            browser TEXT,
            tab_count INTEGER,
            matched_count INTEGER,
            highlight_count INTEGER
        )"""
    )
    conn.commit()
    _tables_ensured = True


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


_SKIP_DOMAINS = frozenset({
    "google.com", "google.ca", "youtube.com", "github.com",
    "stackoverflow.com", "reddit.com", "twitter.com", "x.com",
    "facebook.com", "instagram.com", "linkedin.com",
    "mail.google.com", "docs.google.com", "drive.google.com", "localhost",
})


def _extract_search_terms(tab: dict) -> str | None:
    """Extract search terms from a tab. Returns None for non-article pages."""
    url = tab.get("url", "")
    title = tab.get("title", "")
    if not url.startswith("http") or not title:
        return None
    try:
        domain = urlparse(url).hostname or ""
    except Exception:
        return None
    if domain.startswith("www."):
        domain = domain[4:]
    if domain in _SKIP_DOMAINS:
        return None
    if title.lower() in ("new tab", "start page", "favorites", "untitled"):
        return None
    words = re.findall(r"\b[a-zA-Z]{3,}\b", title)
    if len(words) < 2:
        return None
    return " ".join(words[:5])


def _log_match(browser: str, tab_count: int, matched: int, highlights: int):
    """Log a match run to the DB."""
    conn = _get_conn()
    try:
        ensure_tables(conn)
        conn.execute(
            "INSERT INTO tab_highlight_log "
            "(checked_at, browser, tab_count, matched_count, highlight_count) "
            "VALUES (?, ?, ?, ?, ?)",
            (datetime.now(ZoneInfo(TIMEZONE)).isoformat(),
             browser, tab_count, matched, highlights),
        )
        conn.commit()
    finally:
        conn.close()


def _get_history(limit: int = 10) -> list[dict]:
    """Fetch recent match run history."""
    conn = _get_conn()
    try:
        ensure_tables(conn)
        rows = conn.execute(
            "SELECT checked_at, browser, tab_count, matched_count, highlight_count "
            "FROM tab_highlight_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


async def match_tabs_to_highlights(browser: str = "Safari") -> dict:
    """Fetch open tabs and search Readwise for matching highlights.

    Returns {tabs, matches: [{tab, highlights}], unmatched}.
    """
    from actions.macos import get_browser_tabs
    from actions.readwise import search_highlights

    tabs = await get_browser_tabs(browser)
    if not tabs:
        return {"tabs": [], "matches": [], "unmatched": []}

    matches, unmatched, total_hl = [], [], 0
    for tab in tabs:
        query = _extract_search_terms(tab)
        if not query:
            continue
        try:
            highlights = await search_highlights(query)
        except Exception as e:
            log.warning("Readwise search failed for %r: %s", query, e)
            continue
        if highlights:
            matches.append({"tab": tab, "highlights": highlights[:5]})
            total_hl += len(highlights[:5])
        else:
            unmatched.append(tab)

    await asyncio.to_thread(_log_match, browser, len(tabs), len(matches), total_hl)
    return {"tabs": tabs, "matches": matches, "unmatched": unmatched}


# --- Formatting ---

def _format_matches(result: dict, browser: str) -> str:
    tabs, matches = result["tabs"], result["matches"]
    if not tabs:
        return f"\U0001f310 No tabs in {browser} (or not running)."
    if not matches:
        return f"\U0001f310 {len(tabs)} tabs open, none matched Readwise highlights."
    lines = [f"\U0001f4da Tabs \u2194 Readwise ({len(matches)}/{len(tabs)} matched)", ""]
    for m in matches[:10]:
        tab, hl = m["tab"], m["highlights"]
        lines.append(f"\U0001f4d6 {tab['title'][:60]}")
        lines.append(f"   {tab['url'][:70]}")
        for h in hl[:3]:
            lines.append(f'   \u2022 "{h.get("text", "")[:80]}"')
            if h.get("title"):
                lines.append(f"     \u2014 {h['title']}")
        lines.append("")
    if len(matches) > 10:
        lines.append(f"  ...and {len(matches) - 10} more")
    return "\n".join(lines)


def _format_preview(tabs: list[dict]) -> str:
    searchable = [(t, q) for t in tabs if (q := _extract_search_terms(t))]
    skipped = [t for t in tabs if not _extract_search_terms(t)]
    lines = [f"\U0001f50d Preview: {len(searchable)} searchable / {len(skipped)} skipped", ""]
    for tab, query in searchable[:15]:
        lines.append(f"  \u2022 {tab['title'][:50]}  \u2192 \"{query}\"")
    if skipped:
        lines.append(f"\nSkipped ({len(skipped)}):")
        for tab in skipped[:10]:
            lines.append(f"  \u2022 {tab.get('title', '(untitled)')[:50]}")
    return "\n".join(lines)


def _format_history(records: list[dict]) -> str:
    if not records:
        return "No match history yet."
    lines = ["\U0001f4ca Recent Matches", ""]
    for r in records:
        lines.append(
            f"  {r['checked_at'][:16]}  {r['browser']}  "
            f"tabs={r['tab_count']}  matched={r['matched_count']}  hl={r['highlight_count']}"
        )
    return "\n".join(lines)


# --- Telegram handler ---

async def handle_readtabs(update, context):
    """Handle /readtabs [preview|history [N]] [chrome]."""
    args = context.args or []
    browser = "Safari"
    if "chrome" in [a.lower() for a in args]:
        browser = "Google Chrome"
        args = [a for a in args if a.lower() != "chrome"]
    sub = args[0].lower() if args else ""

    try:
        if sub == "preview":
            from actions.macos import get_browser_tabs
            tabs = await get_browser_tabs(browser)
            await update.message.reply_text(_format_preview(tabs)[:4096])
        elif sub == "history":
            limit = 10
            if len(args) > 1:
                try:
                    limit = min(int(args[1]), 50)
                except ValueError:
                    await update.message.reply_text("Usage: /readtabs history [N]")
                    return
            records = await asyncio.to_thread(_get_history, limit)
            await update.message.reply_text(_format_history(records))
        else:
            result = await match_tabs_to_highlights(browser=browser)
            await update.message.reply_text(_format_matches(result, browser)[:4096])
    except Exception as e:
        log.exception("readtabs error")
        await update.message.reply_text(f"\u274c Error: {e}")


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle natural language intent. Returns True if handled."""
    if action != "macos_browser_tabs_readwise_highlights":
        return False
    try:
        browser = intent.get("browser", "Safari")
        result = await match_tabs_to_highlights(browser=browser)
        await ctx.reply(_format_matches(result, browser)[:4096])
        return True
    except Exception as e:
        from resilience import format_user_error
        await ctx.reply(format_user_error(e, skill_name="Tabs \u2194 Readwise"))
        return True
