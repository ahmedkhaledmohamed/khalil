"""macOS Focus modes and Shortcuts — toggle DnD, run Shortcuts by name.

Uses AppleScript and the `shortcuts` CLI for non-blocking execution.
Same async osascript pattern as apple_reminders.py.
"""

import asyncio
import logging
import re

log = logging.getLogger("khalil.actions.macos_focus")

SKILL = {
    "name": "macos_focus",
    "description": "Toggle macOS Focus modes and run Shortcuts",
    "category": "system",
    "patterns": [
        (r"\bfocus\s+mode\b", "macos_focus_status"),
        # Specific on/off patterns must come before generic dnd toggle
        (r"\bturn\s+on\s+(?:do\s+not\s+disturb|dnd|focus)\b", "macos_focus_dnd_on"),
        (r"\benable\s+(?:do\s+not\s+disturb|dnd|focus)\b", "macos_focus_dnd_on"),
        (r"\bturn\s+off\s+(?:do\s+not\s+disturb|dnd|focus)\b", "macos_focus_dnd_off"),
        (r"\bdisable\s+(?:do\s+not\s+disturb|dnd|focus)\b", "macos_focus_dnd_off"),
        # Generic toggle (fallback)
        (r"\bdo\s+not\s+disturb\b", "macos_focus_dnd"),
        (r"\bdnd\b", "macos_focus_dnd"),
        (r"\blist\s+(?:my\s+)?shortcuts?\b", "macos_shortcuts_list"),
        (r"\bshow\s+(?:my\s+)?shortcuts?\b", "macos_shortcuts_list"),
        (r"\brun\s+(?:the\s+)?shortcut\b", "macos_shortcuts_run"),
        (r"\bexecute\s+(?:the\s+)?shortcut\b", "macos_shortcuts_run"),
        (r"\bshortcut\s+(?:called|named)\b", "macos_shortcuts_run"),
    ],
    "actions": [
        {"type": "macos_focus_status", "handler": "handle_intent", "keywords": "focus mode status current", "description": "Check current Focus mode status"},
        {"type": "macos_focus_dnd", "handler": "handle_intent", "keywords": "do not disturb dnd focus toggle", "description": "Toggle Do Not Disturb"},
        {"type": "macos_focus_dnd_on", "handler": "handle_intent", "keywords": "do not disturb dnd focus enable turn on", "description": "Enable Do Not Disturb"},
        {"type": "macos_focus_dnd_off", "handler": "handle_intent", "keywords": "do not disturb dnd focus disable turn off", "description": "Disable Do Not Disturb"},
        {"type": "macos_shortcuts_list", "handler": "handle_intent", "keywords": "shortcuts list show all", "description": "List available Shortcuts"},
        {"type": "macos_shortcuts_run", "handler": "handle_intent", "keywords": "shortcuts run execute trigger", "description": "Run a Shortcut by name"},
    ],
    "examples": [
        "What focus mode is on?",
        "Turn on Do Not Disturb",
        "Disable DND",
        "List my shortcuts",
        "Run shortcut Morning Routine",
    ],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


async def _run_cmd(args: list[str], timeout: float = 15) -> tuple[str, int]:
    """Run a subprocess command and return (stdout, returncode)."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    if proc.returncode != 0:
        log.warning("Command %s failed (rc=%d): %s", args[0], proc.returncode, stderr.decode().strip()[:200])
    return stdout.decode().strip(), proc.returncode


# ---------------------------------------------------------------------------
# Focus mode functions
# ---------------------------------------------------------------------------

async def get_focus_status() -> dict:
    """Get current Focus mode status. Returns {dnd_active, focus_name}."""
    # Check DND via plutil — Focus state is in com.apple.controlcenter plist
    script = (
        'do shell script "defaults read com.apple.controlcenter \\"NSStatusItem Visible FocusModes\\" 2>/dev/null || echo 0"'
    )
    stdout, rc = await _run_osascript(script)

    # Also check via assertion lookup (more reliable on recent macOS)
    check_script = (
        'do shell script "plutil -extract dnd.userPref.enabled raw '
        '-o - ~/Library/DoNotDisturb/DB/Assertions/DND.json 2>/dev/null || echo unknown"'
    )
    dnd_stdout, _ = await _run_osascript(check_script)

    dnd_active = dnd_stdout.strip().lower() == "true"

    return {
        "dnd_active": dnd_active,
        "focus_name": "Do Not Disturb" if dnd_active else "None",
    }


async def toggle_dnd(enable: bool) -> bool:
    """Toggle Do Not Disturb on or off.

    Uses Shortcuts to toggle Focus since direct DND toggle requires
    accessibility permissions that AppleScript can't easily obtain.
    """
    # Method 1: Try via Shortcuts (if user has a "Toggle DND" shortcut)
    action = "Turn On" if enable else "Turn Off"

    # Method 2: Use AppleScript to open Focus settings and simulate
    # For reliability, we use the Control Center approach
    script = (
        'tell application "System Events"\n'
        '  tell process "ControlCenter"\n'
        '    -- Click the Focus button in menu bar\n'
        '    set focusItem to menu bar item "Focus" of menu bar 1\n'
        '    click focusItem\n'
        '    delay 0.5\n'
        f'    -- Look for Do Not Disturb toggle\n'
        '    set foundDND to false\n'
        '    repeat with elem in (entire contents of window 1)\n'
        '      try\n'
        '        if description of elem contains "Do Not Disturb" then\n'
        '          click elem\n'
        '          set foundDND to true\n'
        '          exit repeat\n'
        '        end if\n'
        '      end try\n'
        '    end repeat\n'
        '    -- Close Control Center\n'
        '    key code 53\n'
        '    if foundDND then\n'
        '      return "OK"\n'
        '    else\n'
        '      return "NOT_FOUND"\n'
        '    end if\n'
        '  end tell\n'
        'end tell'
    )
    stdout, rc = await _run_osascript(script, timeout=15)
    if rc == 0 and stdout == "OK":
        log.info("DND toggled: %s", "on" if enable else "off")
        return True

    # Fallback: try running a shortcut named "DND On" or "DND Off"
    shortcut_name = "DND On" if enable else "DND Off"
    success = await run_shortcut(shortcut_name)
    if success:
        return True

    log.warning("Could not toggle DND — create Shortcuts named 'DND On' and 'DND Off' for reliable control")
    return False


# ---------------------------------------------------------------------------
# Shortcuts functions
# ---------------------------------------------------------------------------

async def list_shortcuts() -> list[str]:
    """List all available Shortcuts. Returns list of shortcut names."""
    stdout, rc = await _run_cmd(["shortcuts", "list"])
    if rc != 0:
        return []
    return [line.strip() for line in stdout.split("\n") if line.strip()]


async def run_shortcut(name: str, input_text: str | None = None) -> bool:
    """Run a Shortcut by name. Returns True on success."""
    args = ["shortcuts", "run", name]
    if input_text:
        args.extend(["--input-type", "text", "--input", input_text])

    _, rc = await _run_cmd(args, timeout=30)
    if rc == 0:
        log.info("Ran shortcut: %s", name)
    return rc == 0


# ---------------------------------------------------------------------------
# Intent handler
# ---------------------------------------------------------------------------

async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle a natural language intent. Returns True if handled."""
    query = intent.get("query", "") or intent.get("user_query", "")

    if action == "macos_focus_status":
        status = await get_focus_status()
        if status["dnd_active"]:
            await ctx.reply("🔕 Do Not Disturb is **ON**")
        else:
            await ctx.reply("🔔 No Focus mode is active.")
        return True

    elif action == "macos_focus_dnd":
        # Toggle — check current state first
        status = await get_focus_status()
        enable = not status["dnd_active"]
        success = await toggle_dnd(enable)
        if success:
            state = "ON" if enable else "OFF"
            await ctx.reply(f"🔕 Do Not Disturb is now **{state}**")
        else:
            await ctx.reply(
                "❌ Could not toggle DND. Create Shortcuts named 'DND On' and 'DND Off' for reliable control."
            )
        return True

    elif action == "macos_focus_dnd_on":
        success = await toggle_dnd(True)
        if success:
            await ctx.reply("🔕 Do Not Disturb is now **ON**")
        else:
            await ctx.reply("❌ Could not enable DND. Create a Shortcut named 'DND On' for reliable control.")
        return True

    elif action == "macos_focus_dnd_off":
        success = await toggle_dnd(False)
        if success:
            await ctx.reply("🔔 Do Not Disturb is now **OFF**")
        else:
            await ctx.reply("❌ Could not disable DND. Create a Shortcut named 'DND Off' for reliable control.")
        return True

    elif action == "macos_shortcuts_list":
        shortcuts = await list_shortcuts()
        if not shortcuts:
            await ctx.reply("No Shortcuts found (or `shortcuts` CLI unavailable).")
        else:
            lines = [f"⚡ Shortcuts ({len(shortcuts)}):\n"]
            for s in shortcuts[:30]:  # Cap display
                lines.append(f"  • {s}")
            if len(shortcuts) > 30:
                lines.append(f"  ... and {len(shortcuts) - 30} more")
            await ctx.reply("\n".join(lines))
        return True

    elif action == "macos_shortcuts_run":
        # Extract shortcut name from query
        text = re.sub(
            r"\b(?:run|execute|trigger)\s+(?:the\s+)?shortcut\s*(?:called|named)?\s*",
            "", query, flags=re.IGNORECASE
        )
        text = text.strip().strip('"\'')
        if not text:
            await ctx.reply("Which shortcut should I run? Try: \"run shortcut Morning Routine\"")
            return True

        # Check if shortcut exists
        shortcuts = await list_shortcuts()
        # Case-insensitive match
        match = None
        for s in shortcuts:
            if s.lower() == text.lower():
                match = s
                break
        if not match:
            # Partial match
            for s in shortcuts:
                if text.lower() in s.lower():
                    match = s
                    break

        if not match:
            await ctx.reply(f"❌ No shortcut found matching \"{text}\". Use \"list shortcuts\" to see available ones.")
            return True

        success = await run_shortcut(match)
        if success:
            await ctx.reply(f"⚡ Ran shortcut: **{match}**")
        else:
            await ctx.reply(f"❌ Shortcut \"{match}\" failed to run.")
        return True

    return False
