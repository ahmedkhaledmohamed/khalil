"""Apple Reminders sync via osascript (AppleScript).

Creates, reads, and syncs reminders to/from Apple Reminders.app
using a dedicated list (default: "Khalil"). No API key or external
library required — uses asyncio.create_subprocess_exec for non-blocking calls.
"""

import asyncio
import logging

log = logging.getLogger("khalil.actions.apple_reminders")

SKILL = {
    "name": "apple_reminders",
    "description": "Read and sync reminders with Apple Reminders.app",
    "category": "productivity",
    "patterns": [
        (r"\badd\s+(?:to\s+)?(?:apple|icloud)\s+reminder", "icloud_reminder"),
        (r"\b(?:apple|icloud)\s+reminder", "icloud_reminder"),
        (r"\breminders?\s+app\b", "icloud_reminder"),
        (r"\bshow\s+(?:my\s+)?(?:apple|icloud)\s+reminders?\b", "icloud_reminder"),
        (r"\bget\s+(?:my\s+)?(?:apple|icloud)\s+reminders?\b", "icloud_reminder"),
        (r"\b(?:apple|icloud)\s+reminders?\s+due\s+today\b", "icloud_reminder"),
        (r"\b(?:what|list)\s+(?:apple|icloud)\s+reminders?\b", "icloud_reminder"),
        (r"\bsync\s+(?:reminders?\s+)?(?:to\s+)?apple\b", "apple_reminders_sync"),
    ],
    "actions": [
        {"type": "icloud_reminder", "handler": "handle_intent", "keywords": "apple icloud iphone reminders list add show native", "description": "Apple/iCloud Reminders operations"},
        {"type": "apple_reminders_sync", "handler": "handle_intent", "keywords": "sync reminders apple iphone", "description": "Sync a reminder to Apple Reminders"},
    ],
    "examples": ["Show Apple Reminders", "Sync reminder to Apple"],
}


async def _run_osascript(script: str, timeout: float = 10) -> tuple[str, int]:
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


async def _ensure_list(list_name: str) -> None:
    """Create the target list in Reminders.app if it doesn't exist."""
    script = (
        f'tell application "Reminders"\n'
        f'  if not (exists list "{list_name}") then\n'
        f'    make new list with properties {{name:"{list_name}"}}\n'
        f'  end if\n'
        f'end tell'
    )
    await _run_osascript(script)


async def sync_to_apple(text: str, due_date: str | None = None, list_name: str = "Khalil") -> bool:
    """Create a reminder in Apple Reminders.app.

    Args:
        text: Reminder body text.
        due_date: Optional due date string (e.g. "2026-03-24 09:00").
        list_name: Target Reminders list (created if missing).

    Returns:
        True on success, False on failure.
    """
    await _ensure_list(list_name)

    # Escape double quotes in user text
    safe_text = text.replace('"', '\\"')

    if due_date:
        script = (
            f'tell application "Reminders"\n'
            f'  set d to date "{due_date}"\n'
            f'  tell list "{list_name}"\n'
            f'    make new reminder with properties {{name:"{safe_text}", due date:d}}\n'
            f'  end tell\n'
            f'end tell'
        )
    else:
        script = (
            f'tell application "Reminders"\n'
            f'  tell list "{list_name}"\n'
            f'    make new reminder with properties {{name:"{safe_text}"}}\n'
            f'  end tell\n'
            f'end tell'
        )

    stdout, rc = await _run_osascript(script)
    if rc != 0:
        log.error("Failed to create Apple reminder: %s", text[:80])
        return False

    log.info("Apple reminder created in '%s': %s", list_name, text[:80])
    return True


async def get_apple_reminders(list_name: str = "Khalil") -> list[dict]:
    """Get incomplete reminders from a Reminders.app list.

    Returns list of dicts with keys: name, due_date.
    """
    await _ensure_list(list_name)

    script = (
        f'tell application "Reminders"\n'
        f'  set output to ""\n'
        f'  repeat with r in (reminders of list "{list_name}" whose completed is false)\n'
        f'    set dStr to ""\n'
        f'    try\n'
        f'      set dStr to (due date of r as string)\n'
        f'    end try\n'
        f'    set output to output & name of r & "|||" & dStr & linefeed\n'
        f'  end repeat\n'
        f'  return output\n'
        f'end tell'
    )

    stdout, rc = await _run_osascript(script)
    if rc != 0:
        return []

    reminders = []
    for line in stdout.split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split("|||", 1)
        name = parts[0].strip()
        due_date = parts[1].strip() if len(parts) > 1 else ""
        reminders.append({"name": name, "due_date": due_date})

    log.info("Fetched %d incomplete reminders from '%s'", len(reminders), list_name)
    return reminders


async def sync_from_apple(list_name: str = "Khalil") -> list[dict]:
    """Get recently completed reminders from a Reminders.app list.

    Returns list of dicts with keys: name, due_date, completion_date.
    """
    await _ensure_list(list_name)

    script = (
        f'tell application "Reminders"\n'
        f'  set output to ""\n'
        f'  repeat with r in (reminders of list "{list_name}" whose completed is true)\n'
        f'    set dStr to ""\n'
        f'    set cStr to ""\n'
        f'    try\n'
        f'      set dStr to (due date of r as string)\n'
        f'    end try\n'
        f'    try\n'
        f'      set cStr to (completion date of r as string)\n'
        f'    end try\n'
        f'    set output to output & name of r & "|||" & dStr & "|||" & cStr & linefeed\n'
        f'  end repeat\n'
        f'  return output\n'
        f'end tell'
    )

    stdout, rc = await _run_osascript(script)
    if rc != 0:
        return []

    completed = []
    for line in stdout.split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split("|||")
        name = parts[0].strip()
        due_date = parts[1].strip() if len(parts) > 1 else ""
        completion_date = parts[2].strip() if len(parts) > 2 else ""
        completed.append({"name": name, "due_date": due_date, "completion_date": completion_date})

    log.info("Fetched %d completed reminders from '%s'", len(completed), list_name)
    return completed


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle a natural language intent. Returns True if handled."""
    if action == "icloud_reminder":
        try:
            reminders = await get_apple_reminders()
            if not reminders:
                await ctx.reply("No incomplete Apple Reminders found.")
            else:
                lines = [f"🍎 Apple Reminders ({len(reminders)}):\n"]
                for r in reminders:
                    lines.append(f"  • {r.get('name', '?')}"
                                 + (f" (due: {r['due_date']})" if r.get('due_date') else ""))
                await ctx.reply("\n".join(lines))
        except Exception as e:
            await ctx.reply(f"❌ Apple Reminders failed: {e}")
        return True
    elif action == "apple_reminders_sync":
        try:
            text = intent.get("text", "")
            if not text:
                await ctx.reply("What reminder should I sync to Apple Reminders?")
                return True
            success = await sync_to_apple(text)
            if success:
                await ctx.reply(f"✅ Synced to Apple Reminders: {text}")
            else:
                await ctx.reply("❌ Failed to sync to Apple Reminders.")
        except Exception as e:
            await ctx.reply(f"❌ Apple Reminders sync failed: {e}")
        return True
    return False
