"""tmux session management — list, create, send commands, read output.

Wraps the tmux CLI via subprocess. No external dependencies.
"""

from __future__ import annotations

import asyncio
import logging
import re

log = logging.getLogger("khalil.actions.tmux_control")

SKILL = {
    "name": "tmux_control",
    "description": "Manage tmux sessions — list, create, kill, send commands, read output",
    "category": "system",
    "patterns": [
        (r"\btmux\s+(?:session|list|ls)\b", "tmux_list"),
        (r"\blist\s+(?:my\s+)?(?:tmux\s+)?sessions?\b", "tmux_list"),
        (r"\bwhat(?:'s|\s+is)\s+running\s+in\s+tmux\b", "tmux_list"),
        (r"\btmux\s+(?:send|run|exec)\b", "tmux_send"),
        (r"\bsend\s+.+\s+to\s+(?:the\s+)?\w+\s+session\b", "tmux_send"),
        (r"\brun\s+.+\s+in\s+(?:the\s+)?\w+\s+(?:tmux\s+)?session\b", "tmux_send"),
        (r"\btmux\s+(?:read|output|show|capture)\b", "tmux_read"),
        (r"\bwhat(?:'s|\s+is)\s+(?:the\s+)?(?:output|screen)\s+(?:in|of|from)\s+(?:the\s+)?\w+\s+session\b", "tmux_read"),
        (r"\btmux\s+(?:new|create|start)\b", "tmux_create"),
        (r"\bcreate\s+(?:a\s+)?(?:new\s+)?tmux\s+session\b", "tmux_create"),
        (r"\btmux\s+(?:kill|close|stop|destroy)\b", "tmux_kill"),
        (r"\bkill\s+(?:the\s+)?\w+\s+(?:tmux\s+)?session\b", "tmux_kill"),
    ],
    "actions": [
        {"type": "tmux_list", "handler": "handle_intent", "keywords": "tmux session list sessions running", "description": "List tmux sessions"},
        {"type": "tmux_send", "handler": "handle_intent", "keywords": "tmux send run command session execute", "description": "Send command to a tmux session"},
        {"type": "tmux_read", "handler": "handle_intent", "keywords": "tmux read output capture screen show", "description": "Read tmux pane output"},
        {"type": "tmux_create", "handler": "handle_intent", "keywords": "tmux create new start session", "description": "Create a new tmux session"},
        {"type": "tmux_kill", "handler": "handle_intent", "keywords": "tmux kill close stop destroy session", "description": "Kill a tmux session"},
    ],
    "examples": [
        "List my tmux sessions",
        "Send 'npm start' to the dev session",
        "What's running in tmux?",
        "Create a new tmux session called build",
    ],
    "voice": {"confirm_before_execute": True, "response_style": "brief"},
}


async def _run_tmux(*args: str, timeout: float = 10) -> tuple[str, int]:
    """Run a tmux command and return (stdout, returncode)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return stdout.decode().strip(), proc.returncode
    except FileNotFoundError:
        return "tmux not installed — run: brew install tmux", 1
    except asyncio.TimeoutError:
        return "tmux command timed out", 1


async def list_sessions() -> str:
    """List all tmux sessions."""
    output, rc = await _run_tmux("list-sessions", "-F", "#{session_name}: #{session_windows} windows (#{session_activity})")
    if rc != 0:
        if "no server running" in output.lower() or "no current" in output.lower():
            return "No tmux sessions running."
        return f"Error: {output}"
    if not output:
        return "No tmux sessions running."
    return f"📟 tmux sessions:\n{output}"


async def send_command(session: str, command: str) -> str:
    """Send a command to a tmux session."""
    # Verify session exists
    output, rc = await _run_tmux("has-session", "-t", session)
    if rc != 0:
        return f"Session '{session}' not found. Use 'list tmux sessions' to see available sessions."
    _, rc = await _run_tmux("send-keys", "-t", session, command, "Enter")
    if rc != 0:
        return f"Failed to send command to session '{session}'."
    return f"Sent `{command}` to session **{session}**"


async def read_output(session: str, lines: int = 50) -> str:
    """Capture recent output from a tmux pane."""
    output, rc = await _run_tmux("capture-pane", "-t", session, "-p", "-S", f"-{lines}")
    if rc != 0:
        return f"Could not read session '{session}': {output}"
    # Strip trailing empty lines
    cleaned = output.rstrip()
    if not cleaned:
        return f"Session '{session}' pane is empty."
    # Truncate for Telegram
    if len(cleaned) > 3000:
        cleaned = cleaned[-3000:]
        cleaned = "...(truncated)\n" + cleaned
    return f"📟 Output from **{session}**:\n```\n{cleaned}\n```"


async def create_session(name: str) -> str:
    """Create a new detached tmux session."""
    # Check if session already exists
    _, rc = await _run_tmux("has-session", "-t", name)
    if rc == 0:
        return f"Session '{name}' already exists."
    _, rc = await _run_tmux("new-session", "-d", "-s", name)
    if rc != 0:
        return f"Failed to create session '{name}'."
    return f"Created tmux session **{name}**"


async def kill_session(name: str) -> str:
    """Kill a tmux session."""
    _, rc = await _run_tmux("has-session", "-t", name)
    if rc != 0:
        return f"Session '{name}' not found."
    _, rc = await _run_tmux("kill-session", "-t", name)
    if rc != 0:
        return f"Failed to kill session '{name}'."
    return f"Killed tmux session **{name}**"


def _extract_session_name(query: str) -> str | None:
    """Extract session name from natural language query."""
    # "send X to the <session> session"
    m = re.search(r"to\s+(?:the\s+)?(\w+)\s+session", query, re.IGNORECASE)
    if m:
        return m.group(1)
    # "in the <session> session"
    m = re.search(r"in\s+(?:the\s+)?(\w+)\s+(?:tmux\s+)?session", query, re.IGNORECASE)
    if m:
        return m.group(1)
    # "from <session>"
    m = re.search(r"(?:from|of)\s+(?:the\s+)?(\w+)\s+session", query, re.IGNORECASE)
    if m:
        return m.group(1)
    # "session <name>"
    m = re.search(r"session\s+(?:called\s+|named\s+)?(\w+)", query, re.IGNORECASE)
    if m:
        return m.group(1)
    # "tmux kill/read/send <name>"
    m = re.search(r"tmux\s+(?:kill|close|read|output|send)\s+(\w+)", query, re.IGNORECASE)
    if m:
        return m.group(1)
    # "kill the <name> session"
    m = re.search(r"kill\s+(?:the\s+)?(\w+)", query, re.IGNORECASE)
    if m and m.group(1).lower() not in ("tmux", "session", "this", "that"):
        return m.group(1)
    return None


def _extract_command(query: str) -> str | None:
    """Extract the command to send from natural language."""
    # "send 'command' to ..."
    m = re.search(r"send\s+['\"](.+?)['\"]\s+to", query, re.IGNORECASE)
    if m:
        return m.group(1)
    # "send <command> to ..."
    m = re.search(r"send\s+(.+?)\s+to\s+", query, re.IGNORECASE)
    if m:
        return m.group(1).strip("'\"")
    # "run <command> in ..."
    m = re.search(r"run\s+['\"]?(.+?)['\"]?\s+in\s+", query, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle tmux control intents."""
    # Availability guard: check if tmux binary exists
    import shutil
    if not shutil.which("tmux"):
        await ctx.reply("tmux is not installed on this system.")
        return True

    query = intent.get("query", "") or intent.get("user_query", "")

    if action == "tmux_list":
        result = await list_sessions()
        await ctx.reply(result)
        return True

    if action == "tmux_send":
        session = _extract_session_name(query)
        command = _extract_command(query)
        if not session or not command:
            await ctx.reply("Usage: send '<command>' to the <session> session")
            return True
        result = await send_command(session, command)
        await ctx.reply(result)
        return True

    if action == "tmux_read":
        session = _extract_session_name(query)
        if not session:
            # Default to listing sessions if no session specified
            result = await list_sessions()
            await ctx.reply(result + "\n\nSpecify a session name to read its output.")
            return True
        result = await read_output(session)
        await ctx.reply(result)
        return True

    if action == "tmux_create":
        # Extract session name
        m = re.search(r"(?:called|named)\s+(\w+)", query, re.IGNORECASE)
        if not m:
            m = re.search(r"(?:create|new|start)\s+(?:a\s+)?(?:new\s+)?(?:tmux\s+)?session\s+(\w+)", query, re.IGNORECASE)
        if not m:
            m = re.search(r"tmux\s+(?:new|create|start)\s+(\w+)", query, re.IGNORECASE)
        name = m.group(1) if m else None
        if not name:
            await ctx.reply("What should I name the session?")
            return True
        result = await create_session(name)
        await ctx.reply(result)
        return True

    if action == "tmux_kill":
        session = _extract_session_name(query)
        if not session:
            await ctx.reply("Which session should I kill?")
            return True
        result = await kill_session(session)
        await ctx.reply(result)
        return True

    return False
