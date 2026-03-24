"""macOS system awareness — apps, windows, system info, notifications, screenshots, search.

Provides async functions for querying macOS state via osascript, mdfind,
screencapture, and other system utilities.
"""

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("khalil.actions.macos")


async def _run(cmd: list[str], timeout: float = 10) -> tuple[str, str, int]:
    """Run a subprocess and return (stdout, stderr, returncode)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return stdout.decode().strip(), stderr.decode().strip(), proc.returncode


# ---------------------------------------------------------------------------
# Running apps
# ---------------------------------------------------------------------------

async def get_running_apps() -> list[str]:
    """Return names of all visible (non-background) applications."""
    try:
        stdout, stderr, rc = await _run([
            "osascript", "-e",
            'tell application "System Events" to get name of every process whose background only is false',
        ])
        if rc != 0:
            log.warning("get_running_apps failed: %s", stderr[:200])
            return []
        # AppleScript returns comma-separated list
        return [app.strip() for app in stdout.split(",") if app.strip()]
    except asyncio.TimeoutError:
        log.warning("get_running_apps timed out")
        return []
    except Exception as e:
        log.warning("get_running_apps error: %s", e)
        return []


# ---------------------------------------------------------------------------
# Frontmost app
# ---------------------------------------------------------------------------

async def get_frontmost_app() -> str:
    """Return the name of the currently focused application."""
    try:
        stdout, stderr, rc = await _run([
            "osascript", "-e",
            'tell application "System Events" to get name of first application process whose frontmost is true',
        ])
        if rc != 0:
            log.warning("get_frontmost_app failed: %s", stderr[:200])
            return ""
        return stdout or ""
    except asyncio.TimeoutError:
        log.warning("get_frontmost_app timed out")
        return ""
    except Exception as e:
        log.warning("get_frontmost_app error: %s", e)
        return ""


# ---------------------------------------------------------------------------
# Active window title
# ---------------------------------------------------------------------------

_WINDOW_TITLE_SCRIPT = '''
tell application "System Events"
    set frontApp to first application process whose frontmost is true
    try
        set winTitle to name of front window of frontApp
    on error
        set winTitle to ""
    end try
    return winTitle
end tell
'''


async def get_active_window_title() -> str:
    """Return the window title of the frontmost application."""
    try:
        stdout, stderr, rc = await _run([
            "osascript", "-e", _WINDOW_TITLE_SCRIPT,
        ])
        if rc != 0:
            log.warning("get_active_window_title failed: %s", stderr[:200])
            return ""
        return stdout or ""
    except asyncio.TimeoutError:
        log.warning("get_active_window_title timed out")
        return ""
    except Exception as e:
        log.warning("get_active_window_title error: %s", e)
        return ""


# ---------------------------------------------------------------------------
# System info
# ---------------------------------------------------------------------------

async def get_system_info() -> dict:
    """Return battery %, storage, memory, and CPU info in one dict.

    Keys: battery_percent, battery_charging, storage_total, storage_used,
          storage_available, memory_total_gb, cpu_brand.
    """
    info: dict = {}

    async def _battery():
        try:
            stdout, _, rc = await _run(["pmset", "-g", "batt"], timeout=5)
            if rc == 0:
                # e.g. "... 87%; charging; ..."
                m = re.search(r"(\d+)%", stdout)
                if m:
                    info["battery_percent"] = int(m.group(1))
                info["battery_charging"] = "charging" in stdout.lower()
        except Exception as e:
            log.warning("battery collection failed: %s", e)

    async def _storage():
        try:
            stdout, _, rc = await _run(["df", "-h", "/"], timeout=5)
            if rc == 0:
                # Second line has the data
                lines = stdout.splitlines()
                if len(lines) >= 2:
                    parts = lines[1].split()
                    if len(parts) >= 4:
                        info["storage_total"] = parts[1]
                        info["storage_used"] = parts[2]
                        info["storage_available"] = parts[3]
        except Exception as e:
            log.warning("storage collection failed: %s", e)

    async def _memory():
        try:
            stdout, _, rc = await _run(["sysctl", "hw.memsize"], timeout=5)
            if rc == 0:
                m = re.search(r"(\d+)", stdout)
                if m:
                    bytes_total = int(m.group(1))
                    info["memory_total_gb"] = round(bytes_total / (1024 ** 3), 1)
        except Exception as e:
            log.warning("memory collection failed: %s", e)

    async def _cpu():
        try:
            stdout, _, rc = await _run(["sysctl", "-n", "machdep.cpu.brand_string"], timeout=5)
            if rc == 0 and stdout:
                info["cpu_brand"] = stdout
        except Exception as e:
            log.warning("cpu collection failed: %s", e)

    await asyncio.gather(_battery(), _storage(), _memory(), _cpu())
    return info


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------

async def post_notification(title: str, body: str) -> None:
    """Post a macOS notification via osascript."""
    # Escape double quotes for AppleScript
    safe_title = title.replace('"', '\\"')
    safe_body = body.replace('"', '\\"')
    script = f'display notification "{safe_body}" with title "{safe_title}"'
    try:
        _, stderr, rc = await _run(["osascript", "-e", script], timeout=5)
        if rc != 0:
            log.warning("post_notification failed: %s", stderr[:200])
    except asyncio.TimeoutError:
        log.warning("post_notification timed out")
    except Exception as e:
        log.warning("post_notification error: %s", e)


# ---------------------------------------------------------------------------
# Screenshot
# ---------------------------------------------------------------------------

async def capture_screenshot(region: str | None = None) -> Path | None:
    """Capture a screenshot. Returns path to the PNG or None on failure.

    Args:
        region: Optional region string for -R flag, e.g. "x,y,w,h".
                If None, captures the full screen.
    """
    output_path = Path("/tmp/khalil_screenshot.png")
    cmd = ["screencapture", "-x"]
    if region:
        cmd.extend(["-R", region])
    cmd.append(str(output_path))

    try:
        _, stderr, rc = await _run(cmd, timeout=15)
        if rc != 0:
            log.warning("capture_screenshot failed: %s", stderr[:200])
            return None
        if output_path.exists():
            return output_path
        return None
    except asyncio.TimeoutError:
        log.warning("capture_screenshot timed out")
        return None
    except Exception as e:
        log.warning("capture_screenshot error: %s", e)
        return None


# ---------------------------------------------------------------------------
# Spotlight search
# ---------------------------------------------------------------------------

_KIND_MAP = {
    "document": "kMDItemContentTypeTree == 'public.text'",
    "image": "kMDItemContentTypeTree == 'public.image'",
    "pdf": "kMDItemContentType == 'com.adobe.pdf'",
    "presentation": "kMDItemContentTypeTree == 'public.presentation'",
}


async def spotlight_search(query: str, kind: str | None = None, limit: int = 10) -> list[dict]:
    """Search files via mdfind (Spotlight). Returns [{path, name, size, modified}].

    Args:
        query: Search query string.
        kind: Optional filter — "document", "image", "pdf", "presentation".
        limit: Max results to return (default 10).
    """
    cmd = ["mdfind"]
    if kind and kind in _KIND_MAP:
        cmd.extend(["-onlyin", os.path.expanduser("~"), f"({_KIND_MAP[kind]}) && ({query})"])
    else:
        cmd.append(query)

    try:
        stdout, stderr, rc = await _run(cmd, timeout=15)
        if rc != 0:
            log.warning("spotlight_search failed: %s", stderr[:200])
            return []

        paths = [p for p in stdout.splitlines() if p.strip()][:limit]
        results = []
        for p in paths:
            path_obj = Path(p)
            entry = {"path": p, "name": path_obj.name}
            try:
                stat = path_obj.stat()
                entry["size"] = stat.st_size
                entry["modified"] = datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc
                ).isoformat()
            except OSError:
                entry["size"] = None
                entry["modified"] = None
            results.append(entry)

        return results
    except asyncio.TimeoutError:
        log.warning("spotlight_search timed out")
        return []
    except Exception as e:
        log.warning("spotlight_search error: %s", e)
        return []


# ---------------------------------------------------------------------------
# Browser tabs
# ---------------------------------------------------------------------------

_SAFARI_TABS_SCRIPT = '''
tell application "Safari"
    set output to ""
    repeat with w in windows
        repeat with t in tabs of w
            set output to output & name of t & "|||" & URL of t & linefeed
        end repeat
    end repeat
    return output
end tell
'''

_CHROME_TABS_SCRIPT = '''
tell application "Google Chrome"
    set output to ""
    repeat with w in windows
        repeat with t in tabs of w
            set output to output & title of t & "|||" & URL of t & linefeed
        end repeat
    end repeat
    return output
end tell
'''


async def get_browser_tabs(browser: str = "Safari") -> list[dict]:
    """Query open browser tabs via AppleScript. Returns [{title, url}].

    Args:
        browser: "Safari" or "Google Chrome".
    """
    if browser == "Google Chrome":
        script = _CHROME_TABS_SCRIPT
    else:
        script = _SAFARI_TABS_SCRIPT

    try:
        stdout, stderr, rc = await _run(["osascript", "-e", script], timeout=10)
        if rc != 0:
            log.warning("get_browser_tabs(%s) failed: %s", browser, stderr[:200])
            return []

        tabs = []
        for line in stdout.splitlines():
            parts = line.split("|||")
            if len(parts) >= 2:
                tabs.append({
                    "title": parts[0].strip(),
                    "url": parts[1].strip(),
                })
        return tabs
    except asyncio.TimeoutError:
        log.warning("get_browser_tabs(%s) timed out", browser)
        return []
    except Exception as e:
        log.warning("get_browser_tabs(%s) error: %s", browser, e)
        return []
