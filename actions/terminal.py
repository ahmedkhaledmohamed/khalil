"""Terminal and Cursor IDE awareness and control.

Provides read-only status queries and write commands for:
- Cursor IDE: windows, projects, extensions, file opening
- iTerm2: sessions, running processes, command injection
"""

import asyncio
import logging
import re

log = logging.getLogger("khalil.actions.terminal")


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


async def get_cursor_status() -> dict:
    """Run `cursor --status` and return parsed dict."""
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
    """Run `cursor --list-extensions` and return list of extension IDs."""
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
