"""Notion API integration — search, read, create, and update pages.

Uses Notion API v1 via httpx (no notion-client dependency).
Auth: integration token stored in system keyring.
Setup: keyring.set_password('pharoclaw', 'notion-api-token', 'ntn_...')

All public functions are async.
"""

import logging

import httpx
import keyring

from config import KEYRING_SERVICE

log = logging.getLogger("pharoclaw.actions.notion")

_BASE_URL = "https://api.notion.com/v1"
_TOKEN_KEY = "notion-api-token"
_NOTION_VERSION = "2022-06-28"

SKILL = {
    "name": "notion",
    "description": "Notion — search pages, create pages, query databases",
    "category": "productivity",
    "patterns": [
        (r"\bsearch\s+(?:my\s+)?notion\b", "notion_search"),
        (r"\bfind\s+in\s+notion\b", "notion_search"),
        (r"\bnotion\s+search\b", "notion_search"),
        (r"\bcreate\s+(?:a\s+)?notion\s+page\b", "notion_create"),
    ],
    "actions": [
        {"type": "notion_search", "handler": "handle_intent", "keywords": "notion search find pages notes", "description": "Search Notion pages"},
        {"type": "notion_create", "handler": "handle_intent", "keywords": "notion create page new", "description": "Create a Notion page"},
    ],
    "examples": ["Search my Notion for project ideas", "Create a Notion page"],
}


def _get_token() -> str:
    """Retrieve Notion integration token from system keyring."""
    token = keyring.get_password(KEYRING_SERVICE, _TOKEN_KEY)
    if not token:
        raise RuntimeError(
            f"Notion API token not found in keyring. Set it with:\n"
            f"  python3 -c \"import keyring; keyring.set_password('{KEYRING_SERVICE}', '{_TOKEN_KEY}', 'ntn_...')\""
        )
    return token


def _headers() -> dict[str, str]:
    """Build request headers with auth and version."""
    return {
        "Authorization": f"Bearer {_get_token()}",
        "Notion-Version": _NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _extract_title(page: dict) -> str:
    """Extract plain-text title from a Notion page object."""
    props = page.get("properties", {})
    # Title can be under any property name; find the first "title" type
    for prop in props.values():
        if prop.get("type") == "title":
            title_parts = prop.get("title", [])
            return "".join(t.get("plain_text", "") for t in title_parts)
    return "(untitled)"


async def search_pages(query: str) -> list[dict]:
    """Search Notion workspace for pages matching query.

    Returns list of {id, title, url, last_edited}.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{_BASE_URL}/search",
            headers=_headers(),
            json={"query": query, "page_size": 20},
        )
        resp.raise_for_status()

    results = []
    for item in resp.json().get("results", []):
        if item.get("object") != "page":
            continue
        results.append({
            "id": item["id"],
            "title": _extract_title(item),
            "url": item.get("url", ""),
            "last_edited": item.get("last_edited_time", ""),
        })
    return results


async def get_page(page_id: str) -> dict:
    """Get a single page's title and properties.

    Returns {id, title, url, properties, last_edited}.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{_BASE_URL}/pages/{page_id}",
            headers=_headers(),
        )
        resp.raise_for_status()

    page = resp.json()
    return {
        "id": page["id"],
        "title": _extract_title(page),
        "url": page.get("url", ""),
        "properties": page.get("properties", {}),
        "last_edited": page.get("last_edited_time", ""),
    }


async def create_page(parent_id: str, title: str, content: str = "") -> str:
    """Create a new page under a parent page or database.

    Returns the URL of the created page.
    """
    # Build children blocks if content provided
    children = []
    if content:
        children.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": content}}],
            },
        })

    body: dict = {
        "parent": {"page_id": parent_id},
        "properties": {
            "title": {
                "title": [{"type": "text", "text": {"content": title}}],
            },
        },
    }
    if children:
        body["children"] = children

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{_BASE_URL}/pages",
            headers=_headers(),
            json=body,
        )
        resp.raise_for_status()

    page = resp.json()
    url = page.get("url", "")
    log.info("Created Notion page: %s (%s)", title, url)
    return url


async def query_database(db_id: str, filter: dict | None = None) -> list[dict]:
    """Query a Notion database, optionally with a filter.

    Returns list of {id, title, url, properties, last_edited}.
    """
    body: dict = {"page_size": 100}
    if filter is not None:
        body["filter"] = filter

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{_BASE_URL}/databases/{db_id}/query",
            headers=_headers(),
            json=body,
        )
        resp.raise_for_status()

    results = []
    for item in resp.json().get("results", []):
        results.append({
            "id": item["id"],
            "title": _extract_title(item),
            "url": item.get("url", ""),
            "properties": item.get("properties", {}),
            "last_edited": item.get("last_edited_time", ""),
        })
    return results


async def update_page(page_id: str, properties: dict) -> bool:
    """Update a page's properties.

    Args:
        page_id: The Notion page ID.
        properties: Dict of property names to Notion property values.

    Returns True on success.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.patch(
            f"{_BASE_URL}/pages/{page_id}",
            headers=_headers(),
            json={"properties": properties},
        )
        resp.raise_for_status()

    log.info("Updated Notion page %s", page_id)
    return True


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle a natural language intent. Returns True if handled."""
    if action == "notion_search":
        try:
            query = intent.get("query", intent.get("text", ""))
            if not query:
                await ctx.reply("What should I search for in Notion?")
                return True
            pages = await search_pages(query)
            if not pages:
                await ctx.reply(f'No Notion pages found for "{query}".')
            else:
                lines = [f'\U0001f4dd Notion results for "{query}":\n']
                for p in pages[:10]:
                    lines.append(f"  \u2022 {p.get('title', 'Untitled')}")
                    if p.get("url"):
                        lines.append(f"    {p['url']}")
                await ctx.reply("\n".join(lines))
        except Exception as e:
            await ctx.reply(f"\u274c Notion search failed: {e}")
        return True
    elif action == "notion_create":
        try:
            title = intent.get("title", intent.get("text", ""))
            parent_id = intent.get("parent_id", "")
            if not title:
                await ctx.reply("What should the page be titled?")
                return True
            if not parent_id:
                await ctx.reply("I need a parent page or database ID. "
                                "Example: create notion page titled 'Ideas' in <parent_id>")
                return True
            url = await create_page(parent_id, title)
            await ctx.reply(f"\u2705 Notion page created: {url}")
        except Exception as e:
            await ctx.reply(f"\u274c Notion page creation failed: {e}")
        return True
    return False
