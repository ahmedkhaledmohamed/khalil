"""Terminal and Cursor IDE awareness and control.

Provides read-only status queries and write commands for:
- Cursor IDE: windows, projects, extensions, file opening
- iTerm2: sessions, running processes, command injection
- Proactive polling: detect state changes and notify
"""

import asyncio
import json
import logging
import re
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from config import DB_PATH, TIMEZONE

log = logging.getLogger("pharoclaw.actions.terminal")

SKILL = {
    "name": "terminal",
    "description": "Terminal and Cursor IDE awareness and control",
    "category": "development",
    "patterns": [
        (r"\bcursor\s+(?:status|windows?|projects?|info)\b", "cursor_status"),
        (r"\b(?:what.s|which)\s+(?:(?:files?|projects?)\s+)?(?:are\s+)?open\s+in\s+cursor\b", "cursor_status"),
        (r"\b(?:what|which)\s+(?:am\s+i\s+)?(?:working\s+on|editing)\s+in\s+cursor\b", "cursor_status"),
        (r"\bcursor\s+extensions?\b", "cursor_extensions"),
        (r"\bcursor\s+terminal\s+(?:status|list|sessions?)\b", "cursor_terminal_status"),
        (r"\b(?:what.s|what\s+is)\s+(?:running\s+)?in\s+(?:the\s+)?cursor\s+terminal\b", "cursor_terminal_status"),
        (r"\b(?:list|show)\s+(?:the\s+)?terminals?\s+in\s+cursor\b", "cursor_terminal_status"),
        (r"\brun\s+.+\s+in\s+cursor\s+terminal\b", "cursor_terminal_exec"),
        (r"\bsend\s+.+\s+to\s+cursor\s+terminal\b", "cursor_terminal_exec"),
        (r"\bnew\s+cursor\s+terminal\b", "cursor_terminal_new"),
        (r"\bcreate\s+(?:a\s+)?cursor\s+terminal\b", "cursor_terminal_new"),
        (r"\b(?:what.s|what\s+is)\s+running\s+in\s+(?:my\s+)?(?:terminal|iterm)\b", "terminal_status"),
        (r"\bterminal\s+(?:status|sessions?)\b", "terminal_status"),
        (r"\biterm\s+(?:status|sessions?)\b", "terminal_status"),
        (r"\bactive\s+(?:terminal\s+)?(?:processes|commands)\b", "terminal_status"),
        (r"\brun\s+.+\s+in\s+(?:the\s+)?(?:terminal|iterm|tab|session)\b", "terminal_exec"),
        (r"\bsend\s+.+\s+to\s+(?:the\s+)?(?:terminal|iterm)\b", "terminal_exec"),
        (r"\bnew\s+(?:terminal\s+)?tab\b", "terminal_new_tab"),
        (r"\bopen\s+(?:a\s+)?(?:new\s+)?terminal(?:\s+tab)?\b", "terminal_new_tab"),
        (r"\bopen\s+.+\s+in\s+cursor\b", "cursor_open"),
        (r"\bcursor\s+open\s+", "cursor_open"),
        (r"\bjump\s+to\s+(?:line\s+)?\d+", "cursor_goto"),
        (r"\bcursor\s+diff\b", "cursor_diff"),
    ],
    "actions": [
        {"type": "cursor_status", "handler": "handle_intent", "keywords": "cursor ide status windows projects info", "description": "Show Cursor IDE status and open windows"},
        {"type": "cursor_extensions", "handler": "handle_intent", "keywords": "cursor extensions list", "description": "List installed Cursor extensions"},
        {"type": "cursor_terminal_status", "handler": "handle_intent", "keywords": "cursor terminal sessions status list terminals", "description": "Show Cursor terminal sessions"},
        {"type": "cursor_terminal_exec", "handler": None, "keywords": "run send command cursor terminal", "description": "Run a command in Cursor terminal"},
        {"type": "cursor_terminal_new", "handler": None, "keywords": "create new cursor terminal", "description": "Create a new Cursor terminal"},
        {"type": "terminal_status", "handler": "handle_intent", "keywords": "terminal iterm sessions running status", "description": "Show iTerm2 terminal sessions and processes"},
        {"type": "terminal_exec", "handler": None, "keywords": "run send command terminal iterm", "description": "Run a command in iTerm2"},
        {"type": "terminal_new_tab", "handler": None, "keywords": "new terminal tab", "description": "Open a new iTerm2 tab"},
        {"type": "cursor_open", "handler": None, "keywords": "open file cursor jump goto line", "description": "Open a file in Cursor"},
        {"type": "cursor_diff", "handler": "handle_intent", "keywords": "cursor diff files compare", "description": "Open a diff view in Cursor"},
    ],
    "examples": ["Cursor status", "What's running in terminal?", "Open file in Cursor"],
}


# --- Cursor IDE ---

def parse_cursor_status(raw: str) -> dict:
    """Parse `cursor --status` output into structured data.

    Returns: {version, memory_system, memory_free, cpus, windows: [{id, name, project, pid, cpu_pct, mem_mb}]}
    """
    result = {"version": None, "memory_system": None, "memory_free": None, "cpus": None, "windows": []}

    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("Version:"):
            result["version"] = line.split(":", 1)[1].strip().split()[0]  # e.g. "Cursor 2.6.19"
            # Extract just version number
            parts = line.split(":", 1)[1].strip().split()
            result["version"] = parts[0] if parts else None
        elif line.startswith("Memory (System):"):
            m = re.search(r"([\d.]+GB)\s+\(([\d.]+GB)\s+free\)", line)
            if m:
                result["memory_system"] = m.group(1)
                result["memory_free"] = m.group(2)
        elif line.startswith("CPUs:"):
            result["cpus"] = line.split(":", 1)[1].strip()

        # Window lines: "    0	   524	  1618	window [1] (.env — compass-AI)"
        m = re.match(r"\s*(\d+)\s+(\d+)\s+(\d+)\s+window\s+\[(\d+)]\s+\((.+)\)", line)
        if m:
            cpu_pct, mem_mb, pid, win_id, title = m.groups()
            # Title format: "filename — project" or just "filename"
            name, project = title, None
            if " — " in title:
                name, project = title.rsplit(" — ", 1)
            elif " — " in title:  # em dash
                name, project = title.rsplit(" — ", 1)
            result["windows"].append({
                "id": int(win_id),
                "name": name.strip(),
                "project": project.strip() if project else None,
                "pid": int(pid),
                "cpu_pct": int(cpu_pct),
                "mem_mb": int(mem_mb),
            })

    return result


def _is_cursor_running() -> bool:
    """Check if Cursor is already running via pgrep (doesn't launch it)."""
    import subprocess
    try:
        return subprocess.run(
            ["pgrep", "-xq", "Cursor"],
            capture_output=True, timeout=3,
        ).returncode == 0
    except Exception:
        return False


async def get_cursor_status() -> dict:
    """Run `cursor --status` and return parsed dict. Skips if Cursor isn't running."""
    if not _is_cursor_running():
        return {"error": None, "windows": []}
    try:
        proc = await asyncio.create_subprocess_exec(
            "cursor", "--status",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        return parse_cursor_status(stdout.decode())
    except FileNotFoundError:
        return {"error": "Cursor CLI not found", "windows": []}
    except asyncio.TimeoutError:
        return {"error": "Cursor --status timed out", "windows": []}
    except Exception as e:
        return {"error": str(e), "windows": []}


async def get_cursor_windows() -> list[dict]:
    """Get just the Cursor window list (lightweight)."""
    status = await get_cursor_status()
    return status.get("windows", [])


async def get_cursor_extensions() -> list[str]:
    """Run `cursor --list-extensions` and return list of extension IDs. Skips if Cursor isn't running."""
    if not _is_cursor_running():
        return []
    try:
        proc = await asyncio.create_subprocess_exec(
            "cursor", "--list-extensions",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        return [line.strip() for line in stdout.decode().splitlines() if line.strip()]
    except Exception as e:
        log.warning("Failed to list Cursor extensions: %s", e)
        return []


def format_cursor_status(status: dict) -> str:
    """Format cursor status for Telegram."""
    if status.get("error"):
        return f"⚠️ Cursor: {status['error']}"

    lines = ["🖥 Cursor Status"]
    if status.get("version"):
        lines[0] += f" (v{status['version']})"

    if not status["windows"]:
        lines.append("  No windows open")
    else:
        for w in status["windows"]:
            cpu_note = f" ⚠️ {w['cpu_pct']}% CPU" if w["cpu_pct"] > 50 else ""
            project = f" — {w['project']}" if w["project"] else ""
            lines.append(f"  [{w['id']}] {w['name']}{project} ({w['mem_mb']} MB{cpu_note})")

    if status.get("memory_free"):
        lines.append(f"\n💾 System: {status['memory_system']} ({status['memory_free']} free)")

    return "\n".join(lines)


# --- iTerm2 ---

_ITERM_SESSIONS_SCRIPT = '''
tell application "iTerm2"
    set output to ""
    repeat with w in windows
        set wName to name of w
        repeat with t in tabs of w
            repeat with s in sessions of t
                try
                    set sName to name of s
                    set sTty to tty of s
                    set sCurrent to is current of s
                    set output to output & wName & "|||" & sName & "|||" & sTty & "|||" & sCurrent & linefeed
                end try
            end repeat
        end repeat
    end repeat
    return output
end tell
'''


def parse_iterm_sessions(raw: str) -> list[dict]:
    """Parse iTerm2 AppleScript output into session list."""
    sessions = []
    for line in raw.strip().splitlines():
        parts = line.split("|||")
        if len(parts) >= 4:
            sessions.append({
                "window": parts[0].strip(),
                "name": parts[1].strip(),
                "tty": parts[2].strip(),
                "is_current": parts[3].strip().lower() == "true",
            })
    return sessions


async def get_iterm_sessions() -> list[dict]:
    """Get all iTerm2 sessions via AppleScript."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", _ITERM_SESSIONS_SCRIPT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0:
            log.warning("iTerm2 AppleScript failed: %s", stderr.decode()[:200])
            return []
        return parse_iterm_sessions(stdout.decode())
    except FileNotFoundError:
        return []
    except asyncio.TimeoutError:
        log.warning("iTerm2 session query timed out")
        return []
    except Exception as e:
        log.warning("Failed to get iTerm2 sessions: %s", e)
        return []


async def get_active_processes(sessions: list[dict] = None) -> list[dict]:
    """Get foreground processes for each tty from iTerm2 sessions."""
    if sessions is None:
        sessions = await get_iterm_sessions()

    ttys = [s["tty"] for s in sessions if s.get("tty")]
    if not ttys:
        return []

    try:
        # Get all processes with their tty
        proc = await asyncio.create_subprocess_exec(
            "ps", "-o", "tty=,pid=,etime=,command=", "-t", ",".join(t.replace("/dev/", "") for t in ttys),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)

        processes = []
        for line in stdout.decode().splitlines():
            line = line.strip()
            if not line:
                continue
            # Format: "ttys003  12345   01:23  python server.py"
            parts = line.split(None, 3)
            if len(parts) >= 4:
                tty_short = parts[0]
                tty_full = f"/dev/{tty_short}"
                command = parts[3]
                # Skip the shell itself (zsh, bash) — we want foreground processes
                if command.startswith("-zsh") or command.startswith("-bash") or command == "zsh" or command == "bash":
                    # Only include shell if it's the only process on this tty
                    if any(p["tty"] == tty_full for p in processes):
                        continue
                processes.append({
                    "tty": tty_full,
                    "pid": int(parts[1]),
                    "elapsed": parts[2],
                    "command": command,
                })
        return processes
    except Exception as e:
        log.warning("Failed to get active processes: %s", e)
        return []


async def get_terminal_status() -> dict:
    """Combined terminal status: sessions + active processes."""
    sessions = await get_iterm_sessions()
    processes = await get_active_processes(sessions)

    # Match processes to sessions by tty
    tty_processes = {}
    for p in processes:
        tty = p["tty"]
        if tty not in tty_processes:
            tty_processes[tty] = p

    enriched = []
    for s in sessions:
        proc = tty_processes.get(s["tty"])
        enriched.append({
            **s,
            "process": proc["command"] if proc else "idle (zsh)",
            "pid": proc["pid"] if proc else None,
            "elapsed": proc["elapsed"] if proc else None,
        })

    return {"sessions": enriched, "count": len(sessions)}


def format_terminal_status(status: dict) -> str:
    """Format terminal status for Telegram."""
    if not status["sessions"]:
        return "📟 No iTerm2 sessions found"

    lines = [f"📟 Terminal Sessions ({status['count']})"]
    for i, s in enumerate(status["sessions"], 1):
        current = " ◀" if s.get("is_current") else ""
        elapsed = f", {s['elapsed']}" if s.get("elapsed") else ""
        pid = f" PID {s['pid']}" if s.get("pid") else ""
        lines.append(f"  Tab {i}: {s['process']}{pid}{elapsed}{current}")

    return "\n".join(lines)


# --- Frontmost App ---

async def get_frontmost_app() -> str | None:
    """Get the name of the frontmost application."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e",
            'tell application "System Events" to get name of first application process whose frontmost is true',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        return stdout.decode().strip() or None
    except Exception:
        return None


# --- Proactive State Polling ---

async def snapshot_dev_state() -> dict:
    """Capture current dev environment state for change detection."""
    cursor_windows = await get_cursor_windows()
    sessions = await get_iterm_sessions()
    frontmost = await get_frontmost_app()

    return {
        "cursor_projects": sorted(set(
            w["project"] for w in cursor_windows if w.get("project")
        )),
        "cursor_window_count": len(cursor_windows),
        "cursor_high_cpu": [
            {"project": w.get("project", w["name"]), "cpu": w["cpu_pct"]}
            for w in cursor_windows if w["cpu_pct"] > 70
        ],
        "iterm_session_count": len(sessions),
        "iterm_ttys": sorted(s["tty"] for s in sessions if s.get("tty")),
        "frontmost_app": frontmost,
        "timestamp": datetime.now(ZoneInfo(TIMEZONE)).isoformat(),
    }


def diff_dev_state(old: dict, new: dict) -> list[str]:
    """Compare two state snapshots. Returns list of human-readable change descriptions."""
    if not old:
        return []  # First snapshot, nothing to compare

    changes = []

    # Cursor project changes
    old_projects = set(old.get("cursor_projects", []))
    new_projects = set(new.get("cursor_projects", []))
    for p in new_projects - old_projects:
        changes.append(f"🖥 Cursor: opened project {p}")
    for p in old_projects - new_projects:
        changes.append(f"🖥 Cursor: closed project {p}")

    # Cursor window count changes
    old_wc = old.get("cursor_window_count", 0)
    new_wc = new.get("cursor_window_count", 0)
    if new_wc == 0 and old_wc > 0:
        changes.append("🖥 Cursor: all windows closed")
    elif old_wc == 0 and new_wc > 0:
        changes.append(f"🖥 Cursor: opened ({new_wc} windows)")

    # High CPU alerts (only alert once per project per occurrence)
    for item in new.get("cursor_high_cpu", []):
        old_high = {h["project"] for h in old.get("cursor_high_cpu", [])}
        if item["project"] not in old_high:
            changes.append(f"⚠️ Cursor: {item['project']} at {item['cpu']}% CPU")

    # iTerm session count changes
    old_sc = old.get("iterm_session_count", 0)
    new_sc = new.get("iterm_session_count", 0)
    if new_sc > old_sc:
        changes.append(f"📟 Terminal: {new_sc - old_sc} new session(s) opened")
    elif new_sc < old_sc:
        changes.append(f"📟 Terminal: {old_sc - new_sc} session(s) closed")

    # Frontmost app change
    old_app = old.get("frontmost_app")
    new_app = new.get("frontmost_app")
    if old_app and new_app and old_app != new_app:
        changes.append(f"🔍 Switched to {new_app}")

    return changes


def format_state_changes(changes: list[str]) -> str:
    """Format state changes for Telegram notification."""
    if not changes:
        return ""
    return "🔔 Dev Environment Update\n\n" + "\n".join(f"  {c}" for c in changes)


def _load_saved_state() -> dict:
    """Load last dev state from settings table."""
    import sqlite3
    try:
        conn = sqlite3.connect(str(DB_PATH))
        row = conn.execute("SELECT value FROM settings WHERE key = 'dev_state'").fetchone()
        conn.close()
        if row:
            return json.loads(row[0])
    except Exception as e:
        log.debug("Failed to load dev state: %s", e)
    return {}


def _save_state(state: dict):
    """Save dev state to settings table."""
    import sqlite3
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('dev_state', ?)",
            (json.dumps(state),),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.debug("Failed to save dev state: %s", e)


async def poll_and_diff() -> list[str]:
    """Take a new snapshot, compare with saved state, save new state. Returns changes."""
    old_state = _load_saved_state()
    new_state = await snapshot_dev_state()
    changes = diff_dev_state(old_state, new_state)
    _save_state(new_state)
    return changes


# --- Terminal Control (M4) ---

_ITERM_WRITE_SCRIPT = '''
tell application "iTerm2"
    tell current session of current tab of current window
        write text "{command}"
    end tell
end tell
'''

_ITERM_WRITE_TO_SESSION_SCRIPT = '''
tell application "iTerm2"
    repeat with w in windows
        repeat with t in tabs of w
            repeat with s in sessions of t
                if tty of s is "{tty}" then
                    tell s to write text "{command}"
                    return "ok"
                end if
            end repeat
        end repeat
    end repeat
    return "session_not_found"
end tell
'''

_ITERM_NEW_TAB_SCRIPT = '''
tell application "iTerm2"
    tell current window
        create tab with default profile
        {write_cmd}
    end tell
end tell
'''


def _escape_applescript(text: str) -> str:
    """Escape a string for embedding in AppleScript."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


async def send_to_iterm(command: str, session_tty: str = "current") -> dict:
    """Send a command to a specific iTerm2 session.

    Args:
        command: The command text to send.
        session_tty: TTY path like "/dev/ttys003", or "current" for current session.

    Returns: {success: bool, error: str | None}
    """
    # Sanitize command using shell.py's sanitizer
    from actions.shell import sanitize_command
    sanitized, reason = sanitize_command(command)
    if sanitized is None:
        return {"success": False, "error": f"Command rejected: {reason}"}

    escaped_cmd = _escape_applescript(sanitized)

    if session_tty == "current":
        script = _ITERM_WRITE_SCRIPT.format(command=escaped_cmd)
    else:
        escaped_tty = _escape_applescript(session_tty)
        script = _ITERM_WRITE_TO_SESSION_SCRIPT.format(command=escaped_cmd, tty=escaped_tty)

    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0:
            return {"success": False, "error": stderr.decode()[:200]}
        output = stdout.decode().strip()
        if output == "session_not_found":
            return {"success": False, "error": f"Session with tty {session_tty} not found"}
        return {"success": True, "error": None}
    except asyncio.TimeoutError:
        return {"success": False, "error": "Timed out sending command to iTerm"}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def create_iterm_tab(command: str = None) -> dict:
    """Create a new iTerm2 tab, optionally run a command.

    Returns: {success: bool, error: str | None}
    """
    write_cmd = ""
    if command:
        from actions.shell import sanitize_command
        sanitized, reason = sanitize_command(command)
        if sanitized is None:
            return {"success": False, "error": f"Command rejected: {reason}"}
        escaped = _escape_applescript(sanitized)
        write_cmd = f'tell current session of current tab to write text "{escaped}"'

    script = _ITERM_NEW_TAB_SCRIPT.format(write_cmd=write_cmd)

    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0:
            return {"success": False, "error": stderr.decode()[:200]}
        return {"success": True, "error": None}
    except Exception as e:
        return {"success": False, "error": str(e)}


# --- Cursor Control (M5) ---

async def cursor_open(path: str, line: int = None) -> dict:
    """Open a file or folder in Cursor, optionally at a specific line.

    Returns: {success: bool, path: str, error: str | None}
    """
    cmd = ["cursor"]
    if line:
        cmd.extend(["-g", f"{path}:{line}"])
    else:
        cmd.append(path)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            return {"success": False, "path": path, "error": stderr.decode()[:200]}
        return {"success": True, "path": path, "error": None}
    except FileNotFoundError:
        return {"success": False, "path": path, "error": "Cursor CLI not found"}
    except Exception as e:
        return {"success": False, "path": path, "error": str(e)}


async def cursor_diff(file1: str, file2: str) -> dict:
    """Open a diff view in Cursor comparing two files.

    Returns: {success: bool, error: str | None}
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "cursor", "--diff", file1, file2,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            return {"success": False, "error": stderr.decode()[:200]}
        return {"success": True, "error": None}
    except FileNotFoundError:
        return {"success": False, "error": "Cursor CLI not found"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# --- Cursor Terminal Bridge Client ---

BRIDGE_URL = "http://127.0.0.1:8034"


async def _bridge_request(method: str, path: str, body: dict = None, timeout: int = 10) -> dict:
    """Make a request to the PharoClaw Terminal Bridge extension."""
    url = f"{BRIDGE_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method, url, json=body)
            data = resp.json()
            if resp.status_code >= 400:
                return {"error": data.get("error", f"HTTP {resp.status_code}"), "status": resp.status_code}
            return data
    except httpx.ConnectError:
        return {"error": "Cursor Terminal Bridge not running. Install the pharoclaw-terminal-bridge extension."}
    except httpx.TimeoutException:
        return {"error": "Bridge request timed out"}
    except Exception as e:
        return {"error": str(e)}


async def bridge_status() -> dict:
    """Check if the Cursor Terminal Bridge is running."""
    return await _bridge_request("GET", "/status")


async def bridge_list_terminals() -> list[dict]:
    """List all terminals in Cursor via the bridge."""
    result = await _bridge_request("GET", "/terminals")
    return result.get("terminals", [])


async def bridge_create_terminal(name: str = "PharoClaw", cwd: str = None, command: str = None) -> dict:
    """Create a new terminal in Cursor."""
    body = {"name": name}
    if cwd:
        body["cwd"] = cwd
    if command:
        body["command"] = command
    return await _bridge_request("POST", "/terminals", body)


async def bridge_send_command(target: str | int, command: str, show: bool = True) -> dict:
    """Send a command to a Cursor terminal.

    Args:
        target: Terminal name or index.
        command: The command text to send.
        show: Whether to focus the terminal.
    """
    encoded = quote(str(target), safe="")
    return await _bridge_request("POST", f"/terminals/{encoded}/send", {
        "command": command,
        "show": show,
    })


async def bridge_close_terminal(target: str | int) -> dict:
    """Close a Cursor terminal."""
    encoded = quote(str(target), safe="")
    return await _bridge_request("DELETE", f"/terminals/{encoded}")


async def bridge_get_output(target: str, lines: int = 50) -> dict:
    """Get buffered output from a Cursor terminal."""
    encoded = quote(str(target), safe="")
    return await _bridge_request("GET", f"/output/{encoded}?lines={lines}")


async def bridge_workspace() -> dict:
    """Get current workspace info from Cursor."""
    return await _bridge_request("GET", "/workspace")


async def get_cursor_terminal_status() -> dict:
    """Get combined Cursor terminal status via the bridge.

    Returns a formatted dict with terminal list and workspace info.
    Falls back gracefully if bridge is not running.
    """
    status = await bridge_status()
    if status.get("error"):
        return {"error": status["error"], "terminals": [], "workspace": None}

    terminals = await bridge_list_terminals()
    workspace = await bridge_workspace()

    return {
        "terminals": terminals,
        "workspace": workspace,
        "count": len(terminals),
    }


def format_cursor_terminal_status(status: dict) -> str:
    """Format Cursor terminal status for Telegram."""
    if status.get("error"):
        return f"⚠️ Cursor Terminal Bridge: {status['error']}"

    lines = [f"🖥 Cursor Terminals ({status.get('count', 0)})"]

    ws = status.get("workspace")
    if ws and ws.get("folders"):
        lines.append(f"  Workspace: {ws['folders'][0]['name']}")
    if ws and ws.get("activeFile"):
        f = ws["activeFile"]
        lines.append(f"  Editing: {f['path']} (line {f['line']})")

    if not status.get("terminals"):
        lines.append("  No terminals open")
    else:
        for t in status["terminals"]:
            active = " ◀" if t.get("isActive") else ""
            pid = f" PID {t['pid']}" if t.get("pid") else ""
            lines.append(f"  [{t.get('id', '?')}] {t['name']}{pid}{active}")

    return "\n".join(lines)


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle a natural language intent. Returns True if handled."""
    if action == "cursor_status":
        status = await get_cursor_status()
        await ctx.reply(format_cursor_status(status))
        return True
    elif action == "cursor_extensions":
        extensions = await get_cursor_extensions()
        if extensions:
            text = f"🧩 Cursor Extensions ({len(extensions)}):\n" + "\n".join(f"  • {e}" for e in extensions)
        else:
            text = "🧩 No Cursor extensions found (or Cursor not running)"
        await ctx.reply(text)
        return True
    elif action == "cursor_terminal_status":
        status = await get_cursor_terminal_status()
        await ctx.reply(format_cursor_terminal_status(status))
        return True
    elif action == "terminal_status":
        status = await get_terminal_status()
        await ctx.reply(format_terminal_status(status))
        return True
    elif action == "cursor_diff":
        f1 = intent.get("file1", "")
        f2 = intent.get("file2", "")
        if not f1 or not f2:
            return False
        result = await cursor_diff(f1, f2)
        if result["success"]:
            await ctx.reply(f"🖥 Diff opened in Cursor: {f1} vs {f2}")
        else:
            await ctx.reply(f"⚠️ Failed: {result['error']}")
        return True
    return False
