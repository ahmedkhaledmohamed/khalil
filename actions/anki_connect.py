"""Anki flashcard integration via AnkiConnect REST API.

Requires Anki desktop running with AnkiConnect plugin (code: 2055492159).
Default endpoint: http://localhost:8765
"""

import asyncio
import json
import logging
import re
from urllib.request import urlopen, Request

log = logging.getLogger("khalil.actions.anki_connect")

ANKI_URL = "http://localhost:8765"

SKILL = {
    "name": "anki_connect",
    "description": "Manage Anki flashcards — create, review, search, stats",
    "category": "learning",
    "patterns": [
        (r"\banki\b", "anki_status"),
        (r"\bcreate\s+(?:an?\s+)?(?:anki\s+)?(?:flash)?card\b", "anki_create"),
        (r"\badd\s+(?:an?\s+)?(?:anki\s+)?(?:flash)?card\b", "anki_create"),
        (r"\bnew\s+(?:anki\s+)?(?:flash)?card\b", "anki_create"),
        (r"\banki\s+(?:stats?|status|summary)\b", "anki_status"),
        (r"\bhow\s+many\s+(?:anki\s+)?cards?\s+(?:due|left)\b", "anki_status"),
        (r"\bcards?\s+due\b", "anki_status"),
        (r"\breview\s+(?:anki\s+)?cards?\b", "anki_review"),
        (r"\bsearch\s+(?:anki\s+)?(?:flash)?cards?\b", "anki_search"),
        (r"\bfind\s+(?:anki\s+)?(?:flash)?cards?\b", "anki_search"),
        (r"\banki\s+decks?\b", "anki_decks"),
        (r"\blist\s+(?:my\s+)?decks?\b", "anki_decks"),
    ],
    "actions": [
        {"type": "anki_create", "handler": "handle_intent", "keywords": "anki flashcard create add new card", "description": "Create a flashcard"},
        {"type": "anki_status", "handler": "handle_intent", "keywords": "anki stats status due cards review count", "description": "Anki review stats"},
        {"type": "anki_review", "handler": "handle_intent", "keywords": "anki review cards study flashcard", "description": "Start a review session"},
        {"type": "anki_search", "handler": "handle_intent", "keywords": "anki search find cards flashcard", "description": "Search flashcards"},
        {"type": "anki_decks", "handler": "handle_intent", "keywords": "anki decks list collection", "description": "List Anki decks"},
    ],
    "examples": [
        "Create an Anki card: Q: What is TCP? A: Transmission Control Protocol",
        "How many cards are due?",
        "List my Anki decks",
        "Search Anki cards for Python",
    ],
}


async def _anki_request(action: str, **params) -> dict:
    """Send a request to AnkiConnect."""
    payload = json.dumps({"action": action, "version": 6, "params": params}).encode()
    req = Request(ANKI_URL, data=payload, headers={"Content-Type": "application/json"})
    loop = asyncio.get_event_loop()
    try:
        response = await loop.run_in_executor(None, lambda: urlopen(req, timeout=5).read())
        result = json.loads(response)
        if result.get("error"):
            raise RuntimeError(result["error"])
        return result.get("result")
    except Exception as e:
        raise ConnectionError(f"AnkiConnect error: {e}") from e


async def is_available() -> bool:
    try:
        await _anki_request("version")
        return True
    except Exception:
        return False


async def get_deck_names() -> list[str]:
    return await _anki_request("deckNames")


async def get_deck_stats() -> dict:
    """Get review stats for all decks."""
    names = await get_deck_names()
    stats = {}
    for name in names:
        try:
            result = await _anki_request("getDeckStats", decks=[name])
            for deck_id, info in result.items():
                stats[name] = {
                    "new": info.get("new_count", 0),
                    "learn": info.get("learn_count", 0),
                    "review": info.get("review_count", 0),
                    "total_due": info.get("new_count", 0) + info.get("learn_count", 0) + info.get("review_count", 0),
                }
        except Exception:
            stats[name] = {"new": 0, "learn": 0, "review": 0, "total_due": 0}
    return stats


async def create_card(deck: str, front: str, back: str) -> int | None:
    """Create a basic card. Returns note ID."""
    note = {
        "deckName": deck,
        "modelName": "Basic",
        "fields": {"Front": front, "Back": back},
        "options": {"allowDuplicate": False},
    }
    return await _anki_request("addNote", note=note)


async def search_cards(query: str, limit: int = 10) -> list[dict]:
    """Search cards by query string."""
    note_ids = await _anki_request("findNotes", query=query)
    if not note_ids:
        return []
    note_ids = note_ids[:limit]
    notes = await _anki_request("notesInfo", notes=note_ids)
    results = []
    for note in notes:
        fields = note.get("fields", {})
        results.append({
            "id": note.get("noteId"),
            "front": fields.get("Front", {}).get("value", ""),
            "back": fields.get("Back", {}).get("value", ""),
            "deck": note.get("deckName", ""),
        })
    return results


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    query = intent.get("query", "") or intent.get("user_query", "")

    if not await is_available():
        await ctx.reply("❌ Anki not running or AnkiConnect plugin not installed.\nInstall plugin code **2055492159** in Anki.")
        return True

    if action == "anki_create":
        # Parse "Q: ... A: ..." or "front: ... back: ..."
        m = re.search(r"(?:Q|front|question):\s*(.+?)\s*(?:A|back|answer):\s*(.+)", query, re.IGNORECASE | re.DOTALL)
        if not m:
            await ctx.reply("Format: \"Create card Q: What is X? A: It is Y\"")
            return True
        front = m.group(1).strip()
        back = m.group(2).strip()

        # Default deck
        decks = await get_deck_names()
        deck = decks[0] if decks else "Default"

        # Check for deck specification
        deck_match = re.search(r"\bdeck:\s*([^\s,]+)", query, re.IGNORECASE)
        if deck_match and deck_match.group(1) in decks:
            deck = deck_match.group(1)

        try:
            note_id = await create_card(deck, front, back)
            await ctx.reply(f"✅ Card created in **{deck}**\n  Q: {front}\n  A: {back}")
        except Exception as e:
            await ctx.reply(f"❌ Failed to create card: {e}")
        return True

    elif action == "anki_status":
        stats = await get_deck_stats()
        total_due = sum(s["total_due"] for s in stats.values())
        lines = [f"📚 **Anki** — {total_due} cards due\n"]
        for deck, s in stats.items():
            if s["total_due"] > 0:
                lines.append(f"  • **{deck}**: {s['new']} new, {s['learn']} learning, {s['review']} review")
        if total_due == 0:
            lines.append("  All caught up! 🎉")
        await ctx.reply("\n".join(lines))
        return True

    elif action == "anki_review":
        stats = await get_deck_stats()
        total_due = sum(s["total_due"] for s in stats.values())
        if total_due == 0:
            await ctx.reply("No cards due for review! 🎉")
        else:
            await ctx.reply(f"📚 You have **{total_due}** cards to review. Open Anki to start studying.")
        return True

    elif action == "anki_search":
        text = re.sub(r"\b(?:search|find)\s+(?:anki\s+)?(?:flash)?cards?\s*(?:for|about)?\s*", "", query, flags=re.IGNORECASE)
        text = text.strip()
        if not text:
            await ctx.reply("What should I search for?")
            return True
        results = await search_cards(text)
        if not results:
            await ctx.reply(f"No cards found matching \"{text}\".")
        else:
            lines = [f"📚 Found {len(results)} card(s) for \"{text}\":\n"]
            for c in results:
                front = re.sub(r"<[^>]+>", "", c["front"])[:80]
                back = re.sub(r"<[^>]+>", "", c["back"])[:80]
                lines.append(f"  • **Q:** {front}\n    **A:** {back}")
            await ctx.reply("\n".join(lines))
        return True

    elif action == "anki_decks":
        decks = await get_deck_names()
        if not decks:
            await ctx.reply("No Anki decks found.")
        else:
            stats = await get_deck_stats()
            lines = [f"📚 **Anki Decks** ({len(decks)}):\n"]
            for d in decks:
                s = stats.get(d, {})
                due = s.get("total_due", 0)
                lines.append(f"  • **{d}**" + (f" — {due} due" if due else ""))
            await ctx.reply("\n".join(lines))
        return True

    return False
