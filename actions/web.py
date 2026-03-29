"""Web search and page fetching — no API keys required.

Uses duckduckgo-search if available, falls back to raw HTTP + regex parsing.
All public functions are async.
"""

import asyncio
import logging
import re
from urllib.parse import quote_plus

import httpx

log = logging.getLogger("khalil.actions.web")

SKILL = {
    "name": "web",
    "description": "Web search and page fetching via DuckDuckGo",
    "category": "information",
    "patterns": [
        (r"\bsearch\s+(?:the\s+)?(?:web|internet|online)\b", "web_search"),
        (r"\bgoogle\s+(?!doc|sheet|spreadsheet|slide|drive|form)", "web_search"),
        (r"\blook\s+up\b", "web_search"),
    ],
    "actions": [
        {"type": "web_search", "handler": "handle_intent", "keywords": "search web internet google look up find", "description": "Search the web"},
    ],
    "examples": ["Search the web for Python best practices", "Look up Toronto weather"],
}

# Try importing duckduckgo-search; gracefully degrade if missing
try:
    from duckduckgo_search import DDGS
    _HAS_DDGS = True
except ImportError:
    _HAS_DDGS = False
    log.info("duckduckgo-search not installed — using fallback HTTP scraper")


def _search_ddgs(query: str, max_results: int) -> list[dict]:
    """Search via duckduckgo-search library (synchronous)."""
    with DDGS() as ddgs:
        raw = list(ddgs.text(query, max_results=max_results))
    return [{"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")} for r in raw]


def _search_fallback(query: str, max_results: int) -> list[dict]:
    """Fallback: scrape DuckDuckGo HTML endpoint with regex."""
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    resp = httpx.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    results = []
    for m in re.finditer(
        r'<a rel="nofollow" class="result__a" href="([^"]+)"[^>]*>(.*?)</a>.*?'
        r'<a class="result__snippet"[^>]*>(.*?)</a>',
        resp.text, re.DOTALL,
    ):
        if len(results) >= max_results:
            break
        results.append({
            "title": re.sub(r"<[^>]+>", "", m.group(2)).strip(),
            "url": m.group(1),
            "snippet": re.sub(r"<[^>]+>", "", m.group(3)).strip(),
        })
    return results


async def web_search(query: str, max_results: int = 5) -> list[dict]:
    """Search the web. Returns [{title, url, snippet}]."""
    fn = _search_ddgs if _HAS_DDGS else _search_fallback
    try:
        return await asyncio.to_thread(fn, query, max_results)
    except Exception as e:
        log.error("Web search failed: %s", e)
        return [{"title": "Search error", "url": "", "snippet": str(e)}]


async def web_fetch(url: str, max_chars: int = 5000) -> str:
    """Fetch a URL and return cleaned text content, truncated to max_chars."""
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
        # Strip HTML tags, collapse whitespace
        text = re.sub(r"<script[^>]*>.*?</script>", " ", resp.text, flags=re.DOTALL)
        text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars]
    except Exception as e:
        log.error("web_fetch failed for %s: %s", url, e)
        return f"Error fetching {url}: {e}"


def format_search_results(results: list[dict]) -> str:
    """Format search results for Telegram display."""
    if not results:
        return "No results found."
    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "No title")
        url = r.get("url", "")
        snippet = r.get("snippet", "")
        lines.append(f"{i}. <b>{title}</b>\n   {snippet}\n   <a href=\"{url}\">Link</a>")
    return "\n\n".join(lines)


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle a natural language intent. Returns True if handled."""
    if action == "web_search":
        query = intent.get("query", "")
        if not query:
            raw = intent.get("user_query", "")
            query = re.sub(r"\b(?:search|google|look\s+up|find|web\s+search)\b", "", raw, flags=re.IGNORECASE)
            query = re.sub(r"\b(?:the|for|about|on)\b", "", query, flags=re.IGNORECASE)
            query = query.strip()
        if not query:
            return False
        await ctx.reply(f"\U0001f50d Searching: {query}...")
        results = await web_search(query)
        await ctx.reply(
            format_search_results(results),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return True
    return False
