"""Obsidian vault integration — search, read, create, and manage notes.

Direct filesystem access to the vault directory (markdown files).
No API or plugin required. Configure OBSIDIAN_VAULT_PATH in config or
via KHALIL_OBSIDIAN_VAULT env var.
"""

import logging
import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from config import TIMEZONE

log = logging.getLogger("khalil.actions.obsidian")

VAULT_PATH = Path(os.environ.get(
    "KHALIL_OBSIDIAN_VAULT",
    str(Path.home() / "Documents" / "Obsidian"),
))

SKILL = {
    "name": "obsidian",
    "description": "Search, read, create, and manage Obsidian vault notes",
    "category": "knowledge",
    "patterns": [
        # Specific actions before generic patterns
        (r"\bcreate\s+(?:an?\s+)?(?:obsidian\s+)?(?:vault\s+)?note\b", "obsidian_create"),
        (r"\bnew\s+vault\s+note\b", "obsidian_create"),
        (r"\blist\s+(?:my\s+)?(?:vault\s+)?notes?\b", "obsidian_list"),
        (r"\brecent\s+(?:vault\s+)?notes?\b", "obsidian_list"),
        (r"\bbacklinks?\s+(?:for|to)\b", "obsidian_backlinks"),
        (r"\bwhat\s+links\s+to\b", "obsidian_backlinks"),
        (r"\bvault\b.*\b(?:search|find|look)\b", "obsidian_search"),
        (r"\bsearch\s+(?:my\s+)?vault\b", "obsidian_search"),
        (r"\bfind\s+(?:in\s+)?(?:my\s+)?vault\b", "obsidian_search"),
        # Read/show (specific)
        (r"\bread\s+(?:my\s+)?(?:obsidian\s+)?note\b", "obsidian_read"),
        (r"\bshow\s+(?:my\s+)?(?:obsidian\s+)?note\b", "obsidian_read"),
        (r"\bvault\s+note\b", "obsidian_read"),
        # Generic obsidian mention → search
        (r"\bobsidian\b", "obsidian_search"),
    ],
    "actions": [
        {"type": "obsidian_search", "handler": "handle_intent", "keywords": "obsidian vault search find notes markdown", "description": "Search Obsidian vault"},
        {"type": "obsidian_read", "handler": "handle_intent", "keywords": "obsidian vault read show note content", "description": "Read an Obsidian note"},
        {"type": "obsidian_list", "handler": "handle_intent", "keywords": "obsidian vault list recent notes", "description": "List recent vault notes"},
        {"type": "obsidian_create", "handler": "handle_intent", "keywords": "obsidian vault create new note write", "description": "Create a new vault note"},
        {"type": "obsidian_backlinks", "handler": "handle_intent", "keywords": "obsidian vault backlinks links references", "description": "Find backlinks to a note"},
    ],
    "examples": [
        "Search my vault for project ideas",
        "Show my note on meeting notes",
        "List recent vault notes",
        "Create a vault note called Daily Log",
        "What links to my Projects note?",
    ],
}


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

def _vault_exists() -> bool:
    return VAULT_PATH.exists() and VAULT_PATH.is_dir()


def _get_all_notes() -> list[Path]:
    """Get all markdown files in the vault, sorted by mtime desc."""
    if not _vault_exists():
        return []
    notes = list(VAULT_PATH.rglob("*.md"))
    notes.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return notes


def search_notes(query: str, limit: int = 10) -> list[dict]:
    """Search notes by filename and content. Returns list of {title, path, snippet}."""
    if not _vault_exists():
        return []

    query_lower = query.lower()
    results = []

    for note in _get_all_notes():
        if len(results) >= limit:
            break

        title = note.stem
        rel_path = note.relative_to(VAULT_PATH)

        # Check filename match
        name_match = query_lower in title.lower()

        # Check content match
        try:
            content = note.read_text(errors="replace")
            content_match = query_lower in content.lower()
        except Exception:
            content = ""
            content_match = False

        if name_match or content_match:
            # Extract snippet around match
            snippet = ""
            if content_match and content:
                idx = content.lower().find(query_lower)
                start = max(0, idx - 50)
                end = min(len(content), idx + len(query) + 100)
                snippet = content[start:end].replace("\n", " ").strip()
                if start > 0:
                    snippet = "..." + snippet
                if end < len(content):
                    snippet += "..."

            results.append({
                "title": title,
                "path": str(rel_path),
                "snippet": snippet,
            })

    return results


def read_note(title: str) -> str | None:
    """Read a note by title (stem name). Returns content or None."""
    if not _vault_exists():
        return None

    # Exact match first
    for note in _get_all_notes():
        if note.stem.lower() == title.lower():
            return note.read_text(errors="replace")

    # Partial match
    for note in _get_all_notes():
        if title.lower() in note.stem.lower():
            return note.read_text(errors="replace")

    return None


def list_recent(limit: int = 15) -> list[dict]:
    """List recent notes. Returns list of {title, path, modified}."""
    results = []
    for note in _get_all_notes()[:limit]:
        mtime = datetime.fromtimestamp(note.stat().st_mtime, tz=ZoneInfo(TIMEZONE))
        results.append({
            "title": note.stem,
            "path": str(note.relative_to(VAULT_PATH)),
            "modified": mtime.strftime("%Y-%m-%d %H:%M"),
        })
    return results


def create_note(title: str, content: str = "", folder: str = "") -> Path | None:
    """Create a new note in the vault. Returns path on success."""
    if not _vault_exists():
        return None

    # Sanitize title for filename
    safe_title = re.sub(r'[<>:"/\\|?*]', "", title).strip()
    if not safe_title:
        return None

    target_dir = VAULT_PATH / folder if folder else VAULT_PATH
    target_dir.mkdir(parents=True, exist_ok=True)

    note_path = target_dir / f"{safe_title}.md"
    if note_path.exists():
        log.warning("Note already exists: %s", note_path)
        return None

    # Add frontmatter
    now = datetime.now(ZoneInfo(TIMEZONE))
    frontmatter = f"---\ncreated: {now.strftime('%Y-%m-%d %H:%M')}\n---\n\n"
    note_path.write_text(frontmatter + content)
    log.info("Created vault note: %s", note_path)
    return note_path


def find_backlinks(title: str) -> list[dict]:
    """Find notes that link to the given title. Returns list of {title, path}."""
    if not _vault_exists():
        return []

    # Obsidian wikilinks: [[Title]] or [[Title|alias]]
    pattern = re.compile(rf"\[\[{re.escape(title)}(?:\|[^\]]+)?\]\]", re.IGNORECASE)

    results = []
    for note in _get_all_notes():
        if note.stem.lower() == title.lower():
            continue  # Skip the note itself
        try:
            content = note.read_text(errors="replace")
            if pattern.search(content):
                results.append({
                    "title": note.stem,
                    "path": str(note.relative_to(VAULT_PATH)),
                })
        except Exception:
            continue

    return results


# ---------------------------------------------------------------------------
# Intent handler
# ---------------------------------------------------------------------------

async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle Obsidian vault intents."""
    query = intent.get("query", "") or intent.get("user_query", "")

    if not _vault_exists():
        await ctx.reply(
            f"Obsidian vault not found at `{VAULT_PATH}`.\n"
            "Set `KHALIL_OBSIDIAN_VAULT` env var to your vault path."
        )
        return True

    if action == "obsidian_search":
        search_term = re.sub(
            r"\b(?:search|find|look\s+for|look\s+up)\b", "", query, flags=re.IGNORECASE
        )
        search_term = re.sub(
            r"\b(?:my|in|the|obsidian|vault|notes?|for)\b", "", search_term, flags=re.IGNORECASE
        )
        search_term = search_term.strip()
        if not search_term:
            await ctx.reply("What should I search for in your vault?")
            return True

        results = search_notes(search_term)
        if not results:
            await ctx.reply(f"No vault notes found matching \"{search_term}\".")
        else:
            lines = [f"📓 Found {len(results)} note(s) matching \"{search_term}\":\n"]
            for r in results:
                lines.append(f"  • **{r['title']}** ({r['path']})")
                if r.get("snippet"):
                    lines.append(f"    {r['snippet'][:120]}")
            await ctx.reply("\n".join(lines))
        return True

    elif action == "obsidian_read":
        # Extract note title
        text = re.sub(r"\b(?:read|show|open|get)\b", "", query, flags=re.IGNORECASE)
        text = re.sub(r"\b(?:my|the|obsidian|vault|note)\b", "", text, flags=re.IGNORECASE)
        title = text.strip().strip('"\'')
        if not title:
            await ctx.reply("Which note should I read?")
            return True

        content = read_note(title)
        if not content:
            await ctx.reply(f"Note \"{title}\" not found in vault.")
        else:
            # Truncate long notes
            if len(content) > 3000:
                content = content[:3000] + "\n\n... (truncated)"
            await ctx.reply(f"📓 **{title}**\n\n{content}")
        return True

    elif action == "obsidian_list":
        notes = list_recent()
        if not notes:
            await ctx.reply("No notes found in vault.")
        else:
            lines = [f"📓 Recent Vault Notes ({len(notes)}):\n"]
            for n in notes:
                lines.append(f"  • **{n['title']}** — {n['modified']}")
            await ctx.reply("\n".join(lines))
        return True

    elif action == "obsidian_create":
        text = re.sub(r"\b(?:create|make|new)\b", "", query, flags=re.IGNORECASE)
        text = re.sub(r"\b(?:an?|the|obsidian|vault|note|called|titled)\b", "", text, flags=re.IGNORECASE)
        title = text.strip().strip('"\'')
        if not title:
            await ctx.reply("What should the note be called?")
            return True

        path = create_note(title)
        if path:
            await ctx.reply(f"✅ Created vault note: **{title}**")
        else:
            await ctx.reply(f"❌ Could not create note \"{title}\" (may already exist).")
        return True

    elif action == "obsidian_backlinks":
        text = re.sub(r"\b(?:backlinks?|links?)\s+(?:for|to)\b", "", query, flags=re.IGNORECASE)
        text = re.sub(r"\b(?:what|find|show|my|the)\b", "", text, flags=re.IGNORECASE)
        title = text.strip().strip('"\'')
        if not title:
            await ctx.reply("Which note should I find backlinks for?")
            return True

        links = find_backlinks(title)
        if not links:
            await ctx.reply(f"No notes link to \"{title}\".")
        else:
            lines = [f"🔗 **{len(links)} note(s)** link to \"{title}\":\n"]
            for l in links:
                lines.append(f"  • **{l['title']}** ({l['path']})")
            await ctx.reply("\n".join(lines))
        return True

    return False
