"""Live source indexers — fetch data from APIs and index into knowledge base.

Each indexer fetches from an external service (Notion, Readwise, Google Tasks, Gmail)
and stores documents via the existing index_source() pipeline. Tracks last sync
timestamps in the settings table to avoid re-indexing.
"""

import asyncio
import logging
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone

from knowledge.indexer import index_source

log = logging.getLogger("khalil.live_sources")


def _get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _set_setting(conn: sqlite3.Connection, key: str, value: str):
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value)
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Notion
# ---------------------------------------------------------------------------

async def index_notion(conn: sqlite3.Connection) -> int:
    """Fetch accessible Notion pages and index their content."""
    import httpx
    from actions.notion import _get_token, _BASE_URL, _NOTION_VERSION

    headers = {
        "Authorization": f"Bearer {_get_token()}",
        "Notion-Version": _NOTION_VERSION,
        "Content-Type": "application/json",
    }

    total = 0
    async with httpx.AsyncClient(timeout=30) as client:
        # Search all pages
        has_more = True
        start_cursor = None
        while has_more:
            body = {"page_size": 100}
            if start_cursor:
                body["start_cursor"] = start_cursor
            resp = await client.post(f"{_BASE_URL}/search", headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()

            for item in data.get("results", []):
                if item.get("object") != "page":
                    continue
                page_id = item["id"]
                # Extract title
                title = "(untitled)"
                for prop in item.get("properties", {}).values():
                    if prop.get("type") == "title":
                        title = "".join(t.get("plain_text", "") for t in prop.get("title", []))
                        break

                # Fetch block content
                try:
                    blocks_resp = await client.get(
                        f"{_BASE_URL}/blocks/{page_id}/children",
                        headers=headers,
                        params={"page_size": 100},
                    )
                    blocks_resp.raise_for_status()
                    blocks = blocks_resp.json().get("results", [])
                except Exception:
                    blocks = []

                # Extract text from blocks
                text_parts = []
                for block in blocks:
                    btype = block.get("type", "")
                    block_data = block.get(btype, {})
                    rich_text = block_data.get("rich_text", [])
                    text = "".join(rt.get("plain_text", "") for rt in rich_text)
                    if text.strip():
                        text_parts.append(text.strip())

                if not text_parts and not title:
                    continue

                content = "\n".join(text_parts) if text_parts else title
                entries = [{"title": title, "content": content, "metadata": f"notion_id={page_id}"}]
                n = await index_source(conn, "notion", "notion:pages", entries)
                total += n

            has_more = data.get("has_more", False)
            start_cursor = data.get("next_cursor")

    _set_setting(conn, "last_notion_sync", datetime.now(timezone.utc).isoformat())
    log.info("Indexed %d Notion pages", total)
    return total


# ---------------------------------------------------------------------------
# Readwise
# ---------------------------------------------------------------------------

async def index_readwise(conn: sqlite3.Connection) -> int:
    """Fetch Readwise highlights and index grouped by book/source."""
    import httpx
    from actions.readwise import _get_token, BASE_URL

    headers = {"Authorization": f"Token {_get_token()}"}

    # Fetch all highlights with pagination
    highlights = []
    url = f"{BASE_URL}/highlights/"
    async with httpx.AsyncClient(timeout=15) as client:
        while url:
            resp = await client.get(url, headers=headers, params={"page_size": 100})
            resp.raise_for_status()
            data = resp.json()
            highlights.extend(data.get("results", []))
            url = data.get("next")

    if not highlights:
        return 0

    # Group highlights by book
    by_book = defaultdict(list)
    for h in highlights:
        book_title = h.get("title") or h.get("book_title") or "Unknown"
        by_book[book_title].append(h)

    # Index each book's highlights as one document
    total = 0
    for book_title, book_highlights in by_book.items():
        author = book_highlights[0].get("author", "Unknown") if book_highlights else ""
        texts = [h.get("text", "") for h in book_highlights if h.get("text")]
        if not texts:
            continue
        content = f"Book: {book_title}\nAuthor: {author}\n\nHighlights:\n" + "\n---\n".join(texts)
        entries = [{
            "title": f"Readwise: {book_title}",
            "content": content,
            "metadata": f"author={author}; highlight_count={len(texts)}",
        }]
        n = await index_source(conn, "readwise", f"readwise:{book_title[:50]}", entries)
        total += n

    _set_setting(conn, "last_readwise_sync", datetime.now(timezone.utc).isoformat())
    log.info("Indexed %d Readwise books (%d highlights)", len(by_book), len(highlights))
    return total


# ---------------------------------------------------------------------------
# Google Tasks
# ---------------------------------------------------------------------------

async def index_google_tasks(conn: sqlite3.Connection) -> int:
    """Index Google Tasks (pending + completed) into knowledge base."""
    from state.tasks_provider import get_all_tasks

    tasks = await get_all_tasks()
    if not tasks:
        return 0

    # Group by list
    by_list = defaultdict(list)
    for t in tasks:
        by_list[t.get("list_name", "Tasks")].append(t)

    total = 0
    for list_name, list_tasks in by_list.items():
        lines = []
        for t in list_tasks:
            status = "✓" if t.get("status") == "completed" else "○"
            due = f" (due: {t['due']})" if t.get("due") else ""
            notes = f"\n  {t['notes']}" if t.get("notes") else ""
            lines.append(f"{status} {t['title']}{due}{notes}")
        content = f"Task list: {list_name}\n\n" + "\n".join(lines)
        entries = [{"title": f"Google Tasks: {list_name}", "content": content}]
        n = await index_source(conn, "google_tasks", f"tasks:{list_name}", entries)
        total += n

    _set_setting(conn, "last_tasks_sync", datetime.now(timezone.utc).isoformat())
    log.info("Indexed %d task lists (%d tasks)", len(by_list), len(tasks))
    return total


# ---------------------------------------------------------------------------
# Work Email
# ---------------------------------------------------------------------------

async def index_work_email(conn: sqlite3.Connection) -> int:
    """Sync work Gmail account into knowledge base."""
    from config import TOKEN_FILE_WORK
    from actions.gmail_sync import sync_new_emails

    last_sync = _get_setting(conn, "last_work_email_sync")
    n = await sync_new_emails(
        conn,
        after_timestamp=last_sync,
        token_file=TOKEN_FILE_WORK,
        source_prefix="gmail_sync_work",
        category_prefix="email:work",
    )
    _set_setting(conn, "last_work_email_sync", datetime.now(timezone.utc).isoformat())
    log.info("Indexed %d work emails", n)
    return n


# ---------------------------------------------------------------------------
# Second Personal Email
# ---------------------------------------------------------------------------

async def index_personal_email2(conn: sqlite3.Connection) -> int:
    """Sync second personal Gmail account into knowledge base."""
    from config import TOKEN_FILE_PERSONAL2
    from actions.gmail_sync import sync_new_emails

    if not TOKEN_FILE_PERSONAL2.exists():
        log.debug("Second personal email token not configured, skipping")
        return 0

    last_sync = _get_setting(conn, "last_personal2_email_sync")
    n = await sync_new_emails(
        conn,
        after_timestamp=last_sync,
        token_file=TOKEN_FILE_PERSONAL2,
        source_prefix="gmail_sync_personal2",
        category_prefix="email:personal2",
    )
    _set_setting(conn, "last_personal2_email_sync", datetime.now(timezone.utc).isoformat())
    log.info("Indexed %d second personal emails", n)
    return n


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

async def index_all_live_sources(conn: sqlite3.Connection) -> dict[str, int]:
    """Run all live source indexers. Returns {source: count} dict."""
    results = {}
    for name, fn in [
        ("notion", index_notion),
        ("readwise", index_readwise),
        ("google_tasks", index_google_tasks),
        ("work_email", index_work_email),
        ("personal_email2", index_personal_email2),
    ]:
        try:
            n = await fn(conn)
            results[name] = n
        except Exception as e:
            log.warning("Live source %s failed: %s", name, e)
            results[name] = 0
    return results
