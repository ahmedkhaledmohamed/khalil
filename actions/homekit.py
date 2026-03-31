"""HomeKit smart home control via Shortcuts and AppleScript.

Controls HomeKit devices and scenes through macOS Shortcuts.
Requires pre-configured Shortcuts for each device type.
Also supports Siri-style voice commands via osascript.

Setup: Create Shortcuts like "Lights On", "Lights Off", "Set Thermostat 72",
"Run Scene Movie Night", etc. Khalil will invoke them by name.
"""

import asyncio
import json
import logging
import re

log = logging.getLogger("khalil.actions.homekit")

SKILL = {
    "name": "homekit",
    "description": "Control smart home devices — lights, thermostat, scenes",
    "category": "home",
    "patterns": [
        # Lights
        (r"\bturn\s+(?:on|off)\s+(?:the\s+)?lights?\b", "homekit_lights"),
        (r"\blights?\s+(?:on|off)\b", "homekit_lights"),
        (r"\bdim\s+(?:the\s+)?lights?\b", "homekit_lights"),
        (r"\bbright(?:en|ness)\s+(?:the\s+)?lights?\b", "homekit_lights"),
        (r"\bset\s+(?:the\s+)?lights?\s+(?:to\s+)?\d+", "homekit_lights"),
        # Thermostat
        (r"\bthermostat\b", "homekit_thermostat"),
        (r"\bset\s+(?:the\s+)?temperature\s+(?:to\s+)?\d+", "homekit_thermostat"),
        (r"\bheat(?:ing)?\s+(?:to|at)\s+\d+", "homekit_thermostat"),
        (r"\bcool(?:ing)?\s+(?:to|at)\s+\d+", "homekit_thermostat"),
        (r"\bwhat(?:'s|\s+is)\s+the\s+temperature\b", "homekit_thermostat"),
        # Scenes
        (r"\brun\s+(?:the\s+)?scene\b", "homekit_scene"),
        (r"\bactivate\s+(?:the\s+)?scene\b", "homekit_scene"),
        (r"\bscene\s+(?:called|named)\b", "homekit_scene"),
        (r"\bgood\s+(?:morning|night|evening)\s+(?:mode|scene)\b", "homekit_scene"),
        (r"\bmovie\s+(?:mode|night|scene)\b", "homekit_scene"),
        # Device status
        (r"\bhome\s+(?:status|devices?)\b", "homekit_status"),
        (r"\bsmart\s+home\s+(?:status|devices?)\b", "homekit_status"),
        (r"\bwhat(?:'s|\s+is)\s+(?:on|running)\s+at\s+home\b", "homekit_status"),
        # Locks
        (r"\block\s+(?:the\s+)?(?:front\s+)?door\b", "homekit_lock"),
        (r"\bunlock\s+(?:the\s+)?(?:front\s+)?door\b", "homekit_lock"),
        (r"\bis\s+(?:the\s+)?door\s+locked\b", "homekit_lock"),
    ],
    "actions": [
        {"type": "homekit_lights", "handler": "handle_intent", "keywords": "home lights on off dim brightness smart", "description": "Control smart lights"},
        {"type": "homekit_thermostat", "handler": "handle_intent", "keywords": "home thermostat temperature heat cool set", "description": "Control thermostat"},
        {"type": "homekit_scene", "handler": "handle_intent", "keywords": "home scene activate run movie morning night", "description": "Activate a HomeKit scene"},
        {"type": "homekit_status", "handler": "handle_intent", "keywords": "home status devices smart what running", "description": "Check device status"},
        {"type": "homekit_lock", "handler": "handle_intent", "keywords": "home lock unlock door front", "description": "Control door locks"},
    ],
    "examples": [
        "Turn on the lights",
        "Set the temperature to 72",
        "Run the Movie Night scene",
        "What's the home status?",
        "Lock the front door",
    ],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _run_shortcut(name: str, input_text: str | None = None) -> tuple[str, bool]:
    """Run a Shortcut. Returns (output, success)."""
    args = ["shortcuts", "run", name]
    if input_text:
        args.extend(["--input-type", "text", "--input", input_text])

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        output = stdout.decode().strip()
        if proc.returncode != 0:
            log.warning("Shortcut '%s' failed: %s", name, stderr.decode()[:200])
            return "", False
        return output, True
    except asyncio.TimeoutError:
        log.warning("Shortcut '%s' timed out", name)
        return "", False
    except Exception as e:
        log.warning("Shortcut '%s' error: %s", name, e)
        return "", False


async def _list_shortcuts() -> list[str]:
    """List available Shortcuts (cached for the session)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "shortcuts", "list",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        return [l.strip() for l in stdout.decode().split("\n") if l.strip()]
    except Exception:
        return []


async def _find_shortcut(prefix: str) -> str | None:
    """Find a Shortcut matching a prefix (case-insensitive)."""
    shortcuts = await _list_shortcuts()
    prefix_lower = prefix.lower()
    # Exact match first
    for s in shortcuts:
        if s.lower() == prefix_lower:
            return s
    # Prefix match
    for s in shortcuts:
        if s.lower().startswith(prefix_lower):
            return s
    # Contains
    for s in shortcuts:
        if prefix_lower in s.lower():
            return s
    return None


# ---------------------------------------------------------------------------
# Intent handler
# ---------------------------------------------------------------------------

async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle smart home intents."""
    query = intent.get("query", "") or intent.get("user_query", "")
    query_lower = query.lower()

    if action == "homekit_lights":
        # Determine on/off/dim
        if re.search(r"\b(?:turn\s+)?off\b", query_lower):
            shortcut = await _find_shortcut("Lights Off")
            if shortcut:
                _, ok = await _run_shortcut(shortcut)
                await ctx.reply("💡 Lights off." if ok else "❌ Failed to turn off lights.")
            else:
                await ctx.reply("❌ No \"Lights Off\" shortcut found. Create one in Shortcuts.app.")
        elif re.search(r"\bdim\b", query_lower):
            # Try to extract percentage
            m = re.search(r"(\d+)", query)
            level = m.group(1) if m else "50"
            shortcut = await _find_shortcut("Dim Lights")
            if shortcut:
                _, ok = await _run_shortcut(shortcut, level)
                await ctx.reply(f"💡 Lights dimmed to {level}%." if ok else "❌ Failed to dim lights.")
            else:
                await ctx.reply("❌ No \"Dim Lights\" shortcut found.")
        else:
            shortcut = await _find_shortcut("Lights On")
            if shortcut:
                _, ok = await _run_shortcut(shortcut)
                await ctx.reply("💡 Lights on." if ok else "❌ Failed to turn on lights.")
            else:
                await ctx.reply("❌ No \"Lights On\" shortcut found. Create one in Shortcuts.app.")
        return True

    elif action == "homekit_thermostat":
        # Check if reading or setting
        if re.search(r"\bwhat|current|check\b", query_lower):
            shortcut = await _find_shortcut("Home Temperature")
            if shortcut:
                output, ok = await _run_shortcut(shortcut)
                if ok and output:
                    await ctx.reply(f"🌡️ Current temperature: {output}")
                else:
                    await ctx.reply("❌ Could not read temperature.")
            else:
                await ctx.reply("❌ No \"Home Temperature\" shortcut found.")
        else:
            m = re.search(r"(\d+)", query)
            if m:
                temp = m.group(1)
                shortcut = await _find_shortcut("Set Thermostat")
                if shortcut:
                    _, ok = await _run_shortcut(shortcut, temp)
                    await ctx.reply(f"🌡️ Thermostat set to {temp}°." if ok else "❌ Failed to set thermostat.")
                else:
                    await ctx.reply("❌ No \"Set Thermostat\" shortcut found.")
            else:
                await ctx.reply("What temperature should I set?")
        return True

    elif action == "homekit_scene":
        # Extract scene name
        text = re.sub(r"\b(?:run|activate|trigger)\s+(?:the\s+)?scene\s*(?:called|named)?\s*", "", query, flags=re.IGNORECASE)
        text = text.strip().strip('"\'')

        # Map common aliases
        scene_aliases = {
            "good morning": "Good Morning",
            "good night": "Good Night",
            "good evening": "Good Evening",
            "movie mode": "Movie Night",
            "movie night": "Movie Night",
        }
        scene_name = scene_aliases.get(text.lower(), text)

        if not scene_name:
            await ctx.reply("Which scene should I activate?")
            return True

        shortcut = await _find_shortcut(f"Scene {scene_name}")
        if not shortcut:
            shortcut = await _find_shortcut(scene_name)

        if shortcut:
            _, ok = await _run_shortcut(shortcut)
            await ctx.reply(f"🏠 Activated scene: **{scene_name}**" if ok else f"❌ Failed to activate \"{scene_name}\".")
        else:
            await ctx.reply(f"❌ No shortcut found for scene \"{scene_name}\". Create one in Shortcuts.app.")
        return True

    elif action == "homekit_status":
        shortcut = await _find_shortcut("Home Status")
        if shortcut:
            output, ok = await _run_shortcut(shortcut)
            if ok and output:
                await ctx.reply(f"🏠 **Home Status**\n\n{output}")
            else:
                await ctx.reply("❌ Could not read home status.")
        else:
            await ctx.reply(
                "❌ No \"Home Status\" shortcut found.\n"
                "Create a Shortcut that queries your HomeKit devices and returns their status."
            )
        return True

    elif action == "homekit_lock":
        if re.search(r"\bunlock\b", query_lower):
            shortcut = await _find_shortcut("Unlock Door")
            if shortcut:
                _, ok = await _run_shortcut(shortcut)
                await ctx.reply("🔓 Door unlocked." if ok else "❌ Failed to unlock door.")
            else:
                await ctx.reply("❌ No \"Unlock Door\" shortcut found.")
        elif re.search(r"\bis\b.*\blocked\b", query_lower):
            shortcut = await _find_shortcut("Door Status")
            if shortcut:
                output, ok = await _run_shortcut(shortcut)
                await ctx.reply(f"🔐 Door status: {output}" if ok else "❌ Could not check door status.")
            else:
                await ctx.reply("❌ No \"Door Status\" shortcut found.")
        else:
            shortcut = await _find_shortcut("Lock Door")
            if shortcut:
                _, ok = await _run_shortcut(shortcut)
                await ctx.reply("🔒 Door locked." if ok else "❌ Failed to lock door.")
            else:
                await ctx.reply("❌ No \"Lock Door\" shortcut found.")
        return True

    return False
