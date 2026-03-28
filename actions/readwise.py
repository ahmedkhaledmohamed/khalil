"""Readwise API integration — highlights, books, and daily review.

Uses Readwise API v2 (REST). Auth via API token stored in keyring.
All public functions are async.

Setup:
    python3 -c "import keyring; keyring.set_password('khalil-assistant', 'readwise-api-token', 'YOUR_TOKEN')"
"""

import logging

import httpx
import keyring

from config import KEYRING_SERVICE

log = logging.getLogger("khalil.actions.readwise")

BASE_URL = "https://readwise.io/api/v2"

SKILL = {
    "name": "readwise",
    "description": "Readwise — highlights, books, and daily review",
    "category": "reading",
    "patterns": [
        (r"\breadwise\b", "readwise_highlights"),
        (r"\bbook\s+highlights?\b", "readwise_highlights"),
        (r"\bmy\s+highlights?\b", "readwise_highlights"),
        (r"\bdaily\s+review\b", "readwise_review"),
    ],
    "actions": [
        {"type": "readwise_highlights", "handler": "handle_intent", "keywords": "readwise highlights books reading", "description": "Recent highlights"},
        {"type": "readwise_review", "handler": "handle_intent", "keywords": "readwise daily review", "description": "Daily review"},
    ],
    "examples": ["My Readwise highlights", "Daily review"],
}


def _get_token() -> str:
    """Read Readwise API token from keyring."""
    token = keyring.get_password(KEYRING_SERVICE, "readwise-api-token")
    if not token:
        raise ValueError(
            "Readwise API token not found in keyring. Set it with:\n"
            "  python3 -c \"import keyring; keyring.set_password('khalil-assistant', 'readwise-api-token', 'YOUR_TOKEN')\""
        )
    return token


def _headers() -> dict:
    return {"Authorization": f"Token {_get_token()}"}


async def get_highlights(limit: int = 20) -> list[dict]:
    """Fetch recent highlights. Returns [{title, text, author, highlighted_at}]."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{BASE_URL}/highlights/",
            headers=_headers(),
            params={"page_size": limit},
        )
        resp.raise_for_status()

    results = resp.json().get("results", [])
    return [
        {
            "title": h.get("title", ""),
            "text": h.get("text", ""),
            "author": h.get("author", ""),
            "highlighted_at": h.get("highlighted_at", ""),
        }
        for h in results
    ]


async def get_books() -> list[dict]:
    """Fetch full library with pagination. Returns [{title, author, category, num_highlights}]."""
    books = []
    url = f"{BASE_URL}/books/"
    headers = _headers()

    async with httpx.AsyncClient(timeout=10) as client:
        while url:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            for b in data.get("results", []):
                books.append({
                    "title": b.get("title", ""),
                    "author": b.get("author", ""),
                    "category": b.get("category", ""),
                    "num_highlights": b.get("num_highlights", 0),
                })
            url = data.get("next")

    log.info("Fetched %d books from Readwise", len(books))
    return books


async def search_highlights(query: str) -> list[dict]:
    """Search highlights by keyword. Returns [{title, text, author, highlighted_at}]."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{BASE_URL}/highlights/",
            headers=_headers(),
            params={"search": query},
        )
        resp.raise_for_status()

    results = resp.json().get("results", [])
    return [
        {
            "title": h.get("title", ""),
            "text": h.get("text", ""),
            "author": h.get("author", ""),
            "highlighted_at": h.get("highlighted_at", ""),
        }
        for h in results
    ]


async def get_daily_review() -> list[dict]:
    """Fetch today's daily review highlights."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{BASE_URL}/review/",
            headers=_headers(),
        )
        resp.raise_for_status()

    return resp.json().get("highlights", [])


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle a natural language intent. Returns True if handled."""
    if action == "readwise_highlights":
        try:
            highlights = await get_highlights(limit=10)
            if not highlights:
                await ctx.reply("No Readwise highlights found.")
            else:
                lines = ["\U0001f4da Recent Highlights:\n"]
                for h in highlights:
                    lines.append(f'  \u2022 "{h.get("text", "")[:100]}"')
                    if h.get("title"):
                        lines.append(f"    \u2014 {h['title']}")
                await ctx.reply("\n".join(lines))
        except Exception as e:
            await ctx.reply(f"\u274c Readwise failed: {e}")
        return True
    elif action == "readwise_review":
        try:
            highlights = await get_daily_review()
            if not highlights:
                await ctx.reply("No daily review highlights today.")
            else:
                lines = [f"\U0001f4d6 Daily Review ({len(highlights)} highlights):\n"]
                for h in highlights:
                    lines.append(f'  \u2022 "{h.get("text", "")[:120]}"')
                    if h.get("title"):
                        lines.append(f"    \u2014 {h['title']}")
                await ctx.reply("\n".join(lines))
        except Exception as e:
            await ctx.reply(f"\u274c Readwise failed: {e}")
        return True
    return False
