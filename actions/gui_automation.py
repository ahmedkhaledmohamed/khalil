"""macOS GUI automation — click, type, window management, region screenshots.

Uses pyautogui for mouse/keyboard and AppleScript for window management.
All destructive actions (click, type) require confirmation via voice config.
"""

from __future__ import annotations

import asyncio
import logging
import re
import tempfile

log = logging.getLogger("khalil.actions.gui_automation")

SKILL = {
    "name": "gui_automation",
    "description": "macOS GUI automation — click, type, window tiling, region screenshots",
    "category": "system",
    "patterns": [
        (r"\bclick\s+(?:at|on)\s+\d+", "gui_click"),
        (r"\bclick\s+(?:the\s+)?\w+\s+button\b", "gui_click"),
        (r"\btype\s+['\"].+?['\"]", "gui_type"),
        (r"\btype\s+(?:in|into)\b", "gui_type"),
        (r"\btile\s+.+\s+(?:side\s+by\s+side|left|right)\b", "gui_window"),
        (r"\bmove\s+\w+\s+to\s+(?:the\s+)?(?:left|right)\s+(?:half|side)\b", "gui_window"),
        (r"\bresize\s+\w+\s+(?:window|to)\b", "gui_window"),
        (r"\bfull\s*screen\s+\w+\b", "gui_window"),
        (r"\bscreenshot\s+(?:the\s+)?(?:top|bottom|left|right|center|region)\b", "gui_screenshot_region"),
        (r"\bcapture\s+(?:a\s+)?(?:region|area|portion)\b", "gui_screenshot_region"),
    ],
    "actions": [
        {"type": "gui_click", "handler": "handle_intent", "keywords": "click mouse tap press button coordinates", "description": "Click at coordinates or on element"},
        {"type": "gui_type", "handler": "handle_intent", "keywords": "type text keyboard input write field", "description": "Type text via keyboard"},
        {"type": "gui_window", "handler": "handle_intent", "keywords": "tile window move resize left right side fullscreen arrange", "description": "Window management and tiling"},
        {"type": "gui_screenshot_region", "handler": "handle_intent", "keywords": "screenshot capture region area portion screen", "description": "Screenshot a specific screen region"},
    ],
    "examples": [
        "Click at 500, 300",
        "Type 'hello world'",
        "Tile Slack and Cursor side by side",
        "Move Safari to the left half",
        "Screenshot the top-right corner",
    ],
    "voice": {"confirm_before_execute": True, "response_style": "brief"},
}

# Safety: block clicks in dangerous screen regions
_BLOCKED_Y_RANGE = (0, 25)  # macOS menu bar


def _check_pyautogui():
    """Check if pyautogui is available."""
    try:
        import pyautogui
        return pyautogui
    except ImportError:
        return None


async def _run_applescript(script: str) -> str:
    """Run an AppleScript and return output."""
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
    if proc.returncode != 0:
        return f"Error: {stderr.decode().strip()}"
    return stdout.decode().strip()


async def click_at(x: int, y: int) -> str:
    """Click at screen coordinates."""
    # Safety check
    if _BLOCKED_Y_RANGE[0] <= y <= _BLOCKED_Y_RANGE[1]:
        return f"Blocked: clicking in the menu bar area (y={y}) is not allowed."

    pag = _check_pyautogui()
    if not pag:
        return "pyautogui not installed — run: pip install pyautogui"

    screen_w, screen_h = pag.size()
    if x < 0 or x > screen_w or y < 0 or y > screen_h:
        return f"Coordinates ({x}, {y}) are outside screen bounds ({screen_w}x{screen_h})."

    await asyncio.to_thread(pag.click, x, y)
    return f"Clicked at ({x}, {y})"


async def type_text(text: str) -> str:
    """Type text via keyboard."""
    pag = _check_pyautogui()
    if not pag:
        return "pyautogui not installed — run: pip install pyautogui"

    # Safety: cap text length
    if len(text) > 500:
        return "Text too long (max 500 characters)."

    await asyncio.to_thread(pag.typewrite, text, interval=0.02)
    return f"Typed {len(text)} characters"


async def manage_window(app: str, position: str) -> str:
    """Move/resize a window using AppleScript."""
    app_name = app.strip()

    if position in ("left", "left half"):
        script = f'''
        tell application "System Events"
            tell process "{app_name}"
                set frontmost to true
            end tell
        end tell
        tell application "{app_name}"
            set bounds of front window to {{0, 25, 960, 900}}
        end tell
        '''
    elif position in ("right", "right half"):
        script = f'''
        tell application "System Events"
            tell process "{app_name}"
                set frontmost to true
            end tell
        end tell
        tell application "{app_name}"
            set bounds of front window to {{960, 25, 1920, 900}}
        end tell
        '''
    elif position in ("fullscreen", "full screen", "maximize"):
        script = f'''
        tell application "System Events"
            tell process "{app_name}"
                set frontmost to true
                click menu item "Enter Full Screen" of menu "View" of menu bar 1
            end tell
        end tell
        '''
    else:
        return f"Unknown position: {position}. Use: left, right, fullscreen."

    result = await _run_applescript(script)
    if result.startswith("Error"):
        return f"Could not move {app_name}: {result}"
    return f"Moved **{app_name}** to {position}"


async def tile_side_by_side(app1: str, app2: str) -> str:
    """Tile two apps side by side."""
    r1 = await manage_window(app1, "left")
    r2 = await manage_window(app2, "right")
    if "Error" in r1 or "Error" in r2:
        return f"Tiling failed:\n  {r1}\n  {r2}"
    return f"Tiled **{app1}** (left) and **{app2}** (right)"


async def screenshot_region(region: str) -> str | None:
    """Capture a screen region. Returns path to screenshot file."""
    # Map region names to screencapture coordinates
    regions = {
        "top-left": "-R0,0,960,450",
        "top-right": "-R960,0,960,450",
        "bottom-left": "-R0,450,960,450",
        "bottom-right": "-R960,450,960,450",
        "top": "-R0,0,1920,450",
        "bottom": "-R0,450,1920,450",
        "left": "-R0,0,960,900",
        "right": "-R960,0,960,900",
        "center": "-R480,225,960,450",
    }

    region_key = region.lower().replace(" ", "-")
    rect = regions.get(region_key)
    if not rect:
        return f"Unknown region: {region}. Available: {', '.join(regions.keys())}"

    output_path = tempfile.mktemp(suffix=".png")
    proc = await asyncio.create_subprocess_exec(
        "screencapture", rect, output_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(proc.communicate(), timeout=10)
    if proc.returncode != 0:
        return None
    return output_path


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle GUI automation intents."""
    query = intent.get("query", "") or intent.get("user_query", "")

    if action == "gui_click":
        # Extract coordinates
        m = re.search(r"(\d+)\s*[,\s]+\s*(\d+)", query)
        if not m:
            await ctx.reply("Please specify coordinates: click at X, Y")
            return True
        x, y = int(m.group(1)), int(m.group(2))
        result = await click_at(x, y)
        await ctx.reply(result)
        return True

    if action == "gui_type":
        # Extract quoted text
        m = re.search(r"['\"](.+?)['\"]", query)
        if m:
            text = m.group(1)
        else:
            text = re.sub(
                r"\b(?:type|write|input|text)\b", "", query, flags=re.IGNORECASE,
            ).strip()
        if not text:
            await ctx.reply("What should I type?")
            return True
        result = await type_text(text)
        await ctx.reply(result)
        return True

    if action == "gui_window":
        # "tile X and Y side by side"
        m = re.search(r"tile\s+(\w+)\s+and\s+(\w+)", query, re.IGNORECASE)
        if m:
            result = await tile_side_by_side(m.group(1), m.group(2))
            await ctx.reply(result)
            return True

        # "move X to left/right"
        m = re.search(r"move\s+(\w+)\s+to\s+(?:the\s+)?(left|right|fullscreen|full\s+screen)", query, re.IGNORECASE)
        if m:
            result = await manage_window(m.group(1), m.group(2).lower())
            await ctx.reply(result)
            return True

        # "fullscreen X"
        m = re.search(r"full\s*screen\s+(\w+)", query, re.IGNORECASE)
        if m:
            result = await manage_window(m.group(1), "fullscreen")
            await ctx.reply(result)
            return True

        # "resize X to left/right"
        m = re.search(r"resize\s+(\w+)\s+(?:to\s+)?(?:the\s+)?(left|right)", query, re.IGNORECASE)
        if m:
            result = await manage_window(m.group(1), m.group(2).lower())
            await ctx.reply(result)
            return True

        await ctx.reply("Usage: 'tile X and Y side by side', 'move X to left', 'fullscreen X'")
        return True

    if action == "gui_screenshot_region":
        # Extract region
        m = re.search(r"(top-?left|top-?right|bottom-?left|bottom-?right|top|bottom|left|right|center)", query, re.IGNORECASE)
        if not m:
            await ctx.reply("Which region? Options: top, bottom, left, right, top-left, top-right, bottom-left, bottom-right, center")
            return True
        region = m.group(1)
        result = await screenshot_region(region)
        if result and not result.startswith("Unknown"):
            # Send as photo if channel supports it
            try:
                await ctx.reply_voice(result)  # reuse voice for file sending
            except Exception:
                await ctx.reply(f"Screenshot saved: {result}")
        else:
            await ctx.reply(result or "Screenshot failed.")
        return True

    return False
