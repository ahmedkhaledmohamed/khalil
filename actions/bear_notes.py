"""Bear Notes integration via x-callback-url and SQLite.

Bear stores notes in a SQLite database. We read directly for search/list
and use x-callback-url via `open` command for create/append (triggers Bear).
"""

import logging
import os
import re
import sqlite3
from pathlib import Path

log = logging.getLogger("khalil.actions.bear_notes")

# Bear's SQLite database location
BEAR_DB = Path.home() / "Library" / "Group Containers" / "9K33E3U3T4.net.shinyfrog.bear" / "Application Data" / "database.sqlite"

SKILL = {
    "name": "bear_notes",
    "description": "Search, read, and create Bear notes",
    "category": "knowledge",
    "patterns": [
        (r"\bbear\s+note", "bear_search"),
        (r"\bsearch\s+bear\b", "bear_search"),
        (r"\bfind\s+(?:in\s+)?bear\b", "bear_search"),
        (r"\bcreate\s+(?:a\s+)?bear\s+note\b", "bear_create"),
        (r"\bnew\s+bear\s+note\b", "bear_create"),
        (r"\brecent\s+bear\s+notes?\b", "bear_list"),
        (r"\blist\s+bear\s+notes?\b", "bear_list"),
        (r"\bread\s+bear\s+note\b", "bear_read"),
        (r"\bshow\s+bear\s+note\b", "bear_read"),
        (r"\bbear\s+tags?\b", "bear_tags"),
    ],
    "actions": [
        {"type": "bear_search", "handler": "handle_intent", "keywords": "bear notes search find note", "description": "Search Bear notes"},
        {"type": "bear_list", "handler": "handle_intent", "keywords": "bear notes list recent", "description": "List recent Bear notes"},
        {"type": "bear_create", "handler": "handle_intent", "keywords": "bear notes create new write", "description": "Create a Bear note"},
        {"type": "bear_read", "handler": "handle_intent", "keywords": "bear notes read show content", "description": "Read a Bear note"},
        {"type": "bear_tags", "handler": "handle_intent", "keywords": "bear notes tags categories", "description": "List Bear tags"},
    ],
    "examples": [
        "Search Bear notes for project ideas",
        "List recent Bear notes",
        "Create a Bear note called Meeting Notes",
        "Show Bear tags",
    ],
}


def _db_available() -> bool:
    return BEAR_DB.exists()


def _query_db(sql: str, params: tuple = ()) -> list:
    if not _db_available():
        return []
    try:
        conn = sqlite3.connect(f"file:{BEAR_DB}?mode=ro", uri=True)
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return rows
    except Exception as e:
        log.warning("Bear DB error: %s", e)
        return []


def search_notes(query: str, limit: int = 10) -> list[dict]:
    rows = _query_db(
        "SELECT ZTITLE, ZSUBTITLE, ZUNIQUEIDENTIFIER FROM ZSFNOTE "
        "WHERE ZTRASHED = 0 AND (ZTITLE LIKE ? OR ZTEXT LIKE ?) "
        "ORDER BY ZMODIFICATIONDATE DESC LIMIT ?",
        (f"%{query}%", f"%{query}%", limit),
    )
    return [{"title": r[0] or "", "subtitle": r[1] or "", "id": r[2]} for r in rows]


def list_recent(limit: int = 15) -> list[dict]:
    rows = _query_db(
        "SELECT ZTITLE, ZSUBTITLE, ZUNIQUEIDENTIFIER, ZMODIFICATIONDATE FROM ZSFNOTE "
        "WHERE ZTRASHED = 0 ORDER BY ZMODIFICATIONDATE DESC LIMIT ?",
        (limit,),
    )
    return [{"title": r[0] or "", "subtitle": r[1] or "", "id": r[2]} for r in rows]


def read_note(title: str) -> str | None:
    rows = _query_db(
        "SELECT ZTEXT FROM ZSFNOTE WHERE ZTRASHED = 0 AND ZTITLE LIKE ? LIMIT 1",
        (f"%{title}%",),
    )
    return rows[0][0] if rows else None


def get_tags() -> list[str]:
    rows = _query_db("SELECT ZTITLE FROM ZSFNOTETAG ORDER BY ZTITLE")
    return [r[0] for r in rows if r[0]]


async def create_note_via_url(title: str, body: str = "", tags: str = "") -> bool:
    """Create a note via Bear's x-callback-url scheme."""
    import asyncio
    from urllib.parse import quote
    url = f"bear://x-callback-url/create?title={quote(title)}"
    if body:
        url += f"&text={quote(body)}"
    if tags:
        url += f"&tags={quote(tags)}"
    try:
        proc = await asyncio.create_subprocess_exec("open", url)
        await proc.wait()
        return proc.returncode == 0
    except Exception as e:
        log.warning("Bear create failed: %s", e)
        return False


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    query = intent.get("query", "") or intent.get("user_query", "")

    if not _db_available():
        await ctx.reply("❌ Bear not installed or database not found.")
        return True

    if action == "bear_search":
        text = re.sub(r"\b(?:search|find)\s+(?:in\s+)?bear\s*(?:notes?)?\s*(?:for)?\s*", "", query, flags=re.IGNORECASE)
        text = text.strip()
        if not text:
            await ctx.reply("What should I search for in Bear?")
            return True
        results = search_notes(text)
        if not results:
            await ctx.reply(f"No Bear notes found matching \"{text}\".")
        else:
            lines = [f"🐻 Found {len(results)} note(s) matching \"{text}\":\n"]
            for r in results:
                sub = f" — {r['subtitle'][:60]}" if r.get("subtitle") else ""
                lines.append(f"  • **{r['title']}**{sub}")
            await ctx.reply("\n".join(lines))
        return True

    elif action == "bear_list":
        results = list_recent()
        if not results:
            await ctx.reply("No Bear notes found.")
        else:
            lines = [f"🐻 Recent Bear Notes ({len(results)}):\n"]
            for r in results:
                lines.append(f"  • **{r['title']}**")
            await ctx.reply("\n".join(lines))
        return True

    elif action == "bear_create":
        text = re.sub(r"\b(?:create|make|new)\s+(?:a\s+)?bear\s+note\s*(?:called|titled)?\s*", "", query, flags=re.IGNORECASE)
        title = text.strip().strip('"\'')
        if not title:
            await ctx.reply("What should the Bear note be called?")
            return True
        ok = await create_note_via_url(title)
        await ctx.reply(f"✅ Created Bear note: **{title}**" if ok else "❌ Failed to create Bear note.")
        return True

    elif action == "bear_read":
        text = re.sub(r"\b(?:read|show|open)\s+bear\s+note\s*(?:called|titled)?\s*", "", query, flags=re.IGNORECASE)
        title = text.strip().strip('"\'')
        if not title:
            await ctx.reply("Which Bear note should I read?")
            return True
        content = read_note(title)
        if not content:
            await ctx.reply(f"Note \"{title}\" not found in Bear.")
        else:
            if len(content) > 3000:
                content = content[:3000] + "\n\n... (truncated)"
            await ctx.reply(f"🐻 **{title}**\n\n{content}")
        return True

    elif action == "bear_tags":
        tags = get_tags()
        if not tags:
            await ctx.reply("No tags found in Bear.")
        else:
            await ctx.reply(f"🐻 **Bear Tags** ({len(tags)}):\n  " + ", ".join(f"#{t}" for t in tags))
        return True

    return False
