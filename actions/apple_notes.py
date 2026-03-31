"""Apple Notes via AppleScript — search, read, create, and append notes.

No API key or external library required. Uses asyncio.create_subprocess_exec
for non-blocking osascript calls. Same pattern as apple_reminders.py.
"""

import asyncio
import logging
import re

log = logging.getLogger("khalil.actions.apple_notes")

SKILL = {
    "name": "apple_notes",
    "description": "Search, read, create, and append to Apple Notes",
    "category": "productivity",
    "patterns": [
        (r"\bapple\s+notes?\b", "apple_notes_search"),
        (r"\bnotes?\s+app\b", "apple_notes_search"),
        (r"\bsearch\s+(?:my\s+)?notes?\b", "apple_notes_search"),
        (r"\bfind\s+(?:in\s+)?(?:my\s+)?notes?\b", "apple_notes_search"),
        (r"\bshow\s+(?:my\s+)?(?:recent\s+)?notes?\b", "apple_notes_list"),
        (r"\blist\s+(?:my\s+)?notes?\b", "apple_notes_list"),
        (r"\bjot\s+down\b", "apple_notes_create"),
        (r"\bcreate\s+(?:a\s+)?note\b", "apple_notes_create"),
        (r"\bmake\s+(?:a\s+)?note\b", "apple_notes_create"),
        (r"\bnew\s+note\b", "apple_notes_create"),
        (r"\badd\s+to\s+(?:my\s+)?note\b", "apple_notes_append"),
        (r"\bappend\s+(?:to\s+)?(?:my\s+)?note\b", "apple_notes_append"),
    ],
    "actions": [
        {"type": "apple_notes_search", "handler": "handle_intent", "keywords": "apple notes search find note", "description": "Search Apple Notes"},
        {"type": "apple_notes_list", "handler": "handle_intent", "keywords": "apple notes list recent show", "description": "List recent Apple Notes"},
        {"type": "apple_notes_create", "handler": "handle_intent", "keywords": "apple notes create new jot note write", "description": "Create a new Apple Note"},
        {"type": "apple_notes_append", "handler": "handle_intent", "keywords": "apple notes append add update", "description": "Append to an existing Apple Note"},
    ],
    "examples": [
        "Search my notes for meeting ideas",
        "Show my recent notes",
        "Create a note called Shopping List",
        "Jot down: remember to buy milk",
    ],
}


# ---------------------------------------------------------------------------
# AppleScript runner (reusable pattern)
# ---------------------------------------------------------------------------

async def _run_osascript(script: str, timeout: float = 15) -> tuple[str, int]:
    """Run an AppleScript snippet and return (stdout, returncode)."""
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    if proc.returncode != 0:
        log.warning("osascript failed (rc=%d): %s", proc.returncode, stderr.decode().strip()[:200])
    return stdout.decode().strip(), proc.returncode


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

async def search_notes(query: str, limit: int = 10) -> list[dict]:
    """Search Apple Notes by text content. Returns list of {name, folder, snippet}."""
    safe_query = query.replace('"', '\\"')
    script = (
        'tell application "Notes"\n'
        f'  set matchingNotes to notes whose body contains "{safe_query}"\n'
        '  set output to ""\n'
        f'  set maxCount to {limit}\n'
        '  set i to 0\n'
        '  repeat with n in matchingNotes\n'
        '    set i to i + 1\n'
        '    if i > maxCount then exit repeat\n'
        '    set folderName to name of container of n\n'
        '    set noteBody to plaintext of n\n'
        '    if length of noteBody > 150 then\n'
        '      set noteBody to text 1 thru 150 of noteBody\n'
        '    end if\n'
        '    set output to output & name of n & "|||" & folderName & "|||" & noteBody & linefeed\n'
        '  end repeat\n'
        '  return output\n'
        'end tell'
    )
    stdout, rc = await _run_osascript(script)
    if rc != 0:
        return []

    results = []
    for line in stdout.split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split("|||", 2)
        results.append({
            "name": parts[0].strip() if parts else "",
            "folder": parts[1].strip() if len(parts) > 1 else "",
            "snippet": parts[2].strip() if len(parts) > 2 else "",
        })
    return results


async def list_recent_notes(limit: int = 10) -> list[dict]:
    """List most recent Apple Notes. Returns list of {name, folder, modified}."""
    script = (
        'tell application "Notes"\n'
        '  set output to ""\n'
        f'  set maxCount to {limit}\n'
        '  set i to 0\n'
        '  repeat with n in notes\n'
        '    set i to i + 1\n'
        '    if i > maxCount then exit repeat\n'
        '    set folderName to name of container of n\n'
        '    set modDate to modification date of n as string\n'
        '    set output to output & name of n & "|||" & folderName & "|||" & modDate & linefeed\n'
        '  end repeat\n'
        '  return output\n'
        'end tell'
    )
    stdout, rc = await _run_osascript(script)
    if rc != 0:
        return []

    results = []
    for line in stdout.split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split("|||", 2)
        results.append({
            "name": parts[0].strip() if parts else "",
            "folder": parts[1].strip() if len(parts) > 1 else "",
            "modified": parts[2].strip() if len(parts) > 2 else "",
        })
    return results


async def get_note(title: str) -> str | None:
    """Get the full plaintext content of a note by title. Returns None if not found."""
    safe_title = title.replace('"', '\\"')
    script = (
        'tell application "Notes"\n'
        f'  set matchingNotes to notes whose name is "{safe_title}"\n'
        '  if (count of matchingNotes) > 0 then\n'
        '    return plaintext of item 1 of matchingNotes\n'
        '  else\n'
        '    return "NOT_FOUND"\n'
        '  end if\n'
        'end tell'
    )
    stdout, rc = await _run_osascript(script)
    if rc != 0 or stdout == "NOT_FOUND":
        return None
    return stdout


async def create_note(title: str, body: str, folder: str = "Notes") -> bool:
    """Create a new note in Apple Notes. Returns True on success."""
    safe_title = title.replace('"', '\\"')
    safe_body = body.replace('"', '\\"').replace("\n", "\\n")
    # Notes.app uses HTML for body content
    html_body = f"<h1>{safe_title}</h1><br>{safe_body}"
    script = (
        'tell application "Notes"\n'
        f'  tell folder "{folder}"\n'
        f'    make new note with properties {{name:"{safe_title}", body:"{html_body}"}}\n'
        '  end tell\n'
        'end tell'
    )
    _, rc = await _run_osascript(script)
    if rc == 0:
        log.info("Created Apple Note: %s", title)
    return rc == 0


async def append_to_note(title: str, text: str) -> bool:
    """Append text to an existing note. Returns True on success."""
    safe_title = title.replace('"', '\\"')
    safe_text = text.replace('"', '\\"').replace("\n", "<br>")
    script = (
        'tell application "Notes"\n'
        f'  set matchingNotes to notes whose name is "{safe_title}"\n'
        '  if (count of matchingNotes) > 0 then\n'
        '    set targetNote to item 1 of matchingNotes\n'
        '    set existingBody to body of targetNote\n'
        f'    set body of targetNote to existingBody & "<br>" & "{safe_text}"\n'
        '    return "OK"\n'
        '  else\n'
        '    return "NOT_FOUND"\n'
        '  end if\n'
        'end tell'
    )
    stdout, rc = await _run_osascript(script)
    if rc == 0 and stdout == "OK":
        log.info("Appended to Apple Note: %s", title)
        return True
    return False


# ---------------------------------------------------------------------------
# Intent handler
# ---------------------------------------------------------------------------

async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle a natural language intent. Returns True if handled."""
    query = intent.get("query", "") or intent.get("user_query", "")

    if action == "apple_notes_search":
        # Extract search term
        search_term = query
        search_term = re.sub(r"\b(?:search|find|look\s+for|look\s+up)\b", "", search_term, flags=re.IGNORECASE)
        search_term = re.sub(r"\b(?:my|in|the|apple|notes?|app|for)\b", "", search_term, flags=re.IGNORECASE)
        search_term = search_term.strip()
        if not search_term:
            await ctx.reply("What should I search for in your notes?")
            return True
        results = await search_notes(search_term)
        if not results:
            await ctx.reply(f"No notes found matching \"{search_term}\".")
        else:
            lines = [f"📝 Found {len(results)} note(s) matching \"{search_term}\":\n"]
            for r in results:
                lines.append(f"  • **{r['name']}** ({r['folder']})")
                if r.get("snippet"):
                    lines.append(f"    {r['snippet'][:100]}...")
            await ctx.reply("\n".join(lines))
        return True

    elif action == "apple_notes_list":
        results = await list_recent_notes()
        if not results:
            await ctx.reply("No notes found in Apple Notes.")
        else:
            lines = [f"📝 Recent Notes ({len(results)}):\n"]
            for r in results:
                lines.append(f"  • **{r['name']}** ({r['folder']}) — {r.get('modified', '')}")
            await ctx.reply("\n".join(lines))
        return True

    elif action == "apple_notes_create":
        # Extract title and body from query
        text = re.sub(r"\b(?:create|make|new|jot\s+down|write)\b", "", query, flags=re.IGNORECASE)
        text = re.sub(r"\b(?:a|an|the|note|called|titled|about)\b", "", text, flags=re.IGNORECASE)
        text = text.strip().strip(":")
        if not text:
            await ctx.reply("What should the note say?")
            return True
        # Use first line as title, rest as body
        parts = text.split("\n", 1)
        title = parts[0].strip()[:100]
        body = parts[1].strip() if len(parts) > 1 else ""
        success = await create_note(title, body)
        if success:
            await ctx.reply(f"✅ Created note: **{title}**")
        else:
            await ctx.reply("❌ Failed to create note in Apple Notes.")
        return True

    elif action == "apple_notes_append":
        # Need to extract note title and text to append
        text = re.sub(r"\b(?:add|append)\s+(?:to\s+)?(?:my\s+)?note\b", "", query, flags=re.IGNORECASE)
        text = text.strip().strip(":")
        if not text:
            await ctx.reply("What should I add, and to which note?")
            return True
        # Try to find a quoted title or "titled X" pattern
        m = re.search(r'(?:titled?|called?|named?)\s+"?([^"]+)"?', text, re.IGNORECASE)
        if m:
            title = m.group(1).strip()
            append_text = text[:m.start()].strip() + text[m.end():].strip()
        else:
            await ctx.reply("Which note should I append to? Try: \"add to note titled Meeting Notes: new item\"")
            return True
        success = await append_to_note(title, append_text.strip())
        if success:
            await ctx.reply(f"✅ Added to **{title}**")
        else:
            await ctx.reply(f"❌ Could not find note titled \"{title}\" or append failed.")
        return True

    return False
