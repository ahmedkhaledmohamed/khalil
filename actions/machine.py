"""Machine control meta-tool — unified access to terminal sessions, Claude Code, system state, GUI.

Composes existing modules (terminal, tmux_control, dev_tools, macos, gui_automation)
into one LLM-facing tool with ~12 actions. The LLM sees:

    machine(action, target?, command?, lines?)

READ actions are auto-approved. WRITE actions go through autonomy approval.
"""

from __future__ import annotations

import logging

log = logging.getLogger("khalil.actions.machine")

SKILL = {
    "name": "machine",
    "description": "Machine control — terminal sessions, Claude Code processes, system info, GUI automation",
    "category": "system",
    "patterns": [
        (r"\b(?:list|show|what)\b.*\b(?:terminal|session)s?\b", "list_sessions"),
        (r"\bread\s+(?:the\s+)?(?:terminal|output|screen)\b", "read_terminal"),
        (r"\bclaude\s*code\s+(?:status|session|instance|process)", "claude_code_status"),
        (r"\bsystem\s+(?:info|status)\b", "system_info"),
        (r"\b(?:frontmost|active|focused)\s+(?:app|window)\b", "frontmost_app"),
        (r"\bscreenshot\b", "screenshot"),
        (r"\bsend\s+(?:to|command)\s+(?:the\s+)?terminal\b", "send_to_terminal"),
        (r"\bsend\s+(?:to|command)\s+(?:the\s+)?claude\b", "send_to_claude"),
        (r"\b(?:new|create|open)\s+(?:a\s+)?terminal\b", "create_terminal"),
        (r"\btype\s+['\"].+?['\"]", "type_text"),
        (r"\bclick\s+(?:at|on)\s+\d+", "click"),
    ],
    "actions": [
        {
            "type": "list_sessions",
            "handler": "handle_intent",
            "keywords": "terminal sessions tabs list iterm tmux running",
            "description": "List all terminal sessions (iTerm2 + tmux) with running processes",
        },
        {
            "type": "read_terminal",
            "handler": "handle_intent",
            "keywords": "read terminal output screen content tty session",
            "description": "Read recent output from a terminal session by TTY or tmux session name",
            "parameters": {
                "target": {"type": "string", "description": "TTY path (e.g. /dev/ttys057) or tmux session name"},
                "lines": {"type": "integer", "description": "Number of lines to read (default 50)"},
            },
        },
        {
            "type": "claude_code_status",
            "handler": "handle_intent",
            "keywords": "claude code session process running waiting active idle cwd",
            "description": "Show Claude Code processes with CWD, TTY, and state",
        },
        {
            "type": "system_info",
            "handler": "handle_intent",
            "keywords": "system info battery storage cpu memory apps running",
            "description": "System info: battery, storage, running apps",
        },
        {
            "type": "frontmost_app",
            "handler": "handle_intent",
            "keywords": "frontmost active focused app window title",
            "description": "Currently focused app and window title",
        },
        {
            "type": "screenshot",
            "handler": "handle_intent",
            "keywords": "screenshot capture screen",
            "description": "Capture a screenshot of the screen",
        },
        {
            "type": "send_to_terminal",
            "handler": "handle_intent",
            "keywords": "send command terminal iterm tmux session tty",
            "description": "Send a command to a terminal session (goes through shell safety checks)",
            "parameters": {
                "target": {"type": "string", "description": "TTY path (e.g. /dev/ttys057), tmux session name, or 'current'"},
                "command": {"type": "string", "description": "Command to send"},
            },
        },
        {
            "type": "send_to_claude",
            "handler": "handle_intent",
            "keywords": "send claude code session terminal prompt message",
            "description": "Send text to a running Claude Code session (validates target is Claude)",
            "parameters": {
                "target": {"type": "string", "description": "TTY path of the Claude Code session (e.g. /dev/ttys057)"},
                "command": {"type": "string", "description": "Text/prompt to send to Claude Code"},
            },
        },
        {
            "type": "create_terminal",
            "handler": "handle_intent",
            "keywords": "new create open terminal tab iterm tmux session",
            "description": "Open a new terminal tab (iTerm2) or tmux session",
            "parameters": {
                "command": {"type": "string", "description": "Optional command to run in the new terminal"},
            },
        },
        {
            "type": "type_text",
            "handler": "handle_intent",
            "keywords": "type text keyboard input gui",
            "description": "Type text via GUI keyboard input",
            "parameters": {
                "command": {"type": "string", "description": "Text to type"},
            },
        },
        {
            "type": "click",
            "handler": "handle_intent",
            "keywords": "click mouse tap coordinates gui",
            "description": "Click at screen coordinates",
            "parameters": {
                "command": {"type": "string", "description": "Coordinates as 'x,y' (e.g. '500,300')"},
            },
        },
    ],
    "examples": [
        "What Claude Code sessions are running?",
        "Read the output in ttys057",
        "Send 'git status' to the terminal",
        "What's running on my machine?",
        "Take a screenshot",
    ],
}


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle machine control intents by delegating to existing modules."""

    # Infer action if the LLM only sent command/target without picking an action
    if action == "machine" or action not in {
        "list_sessions", "read_terminal", "claude_code_status", "system_info",
        "frontmost_app", "screenshot", "send_to_terminal", "send_to_claude",
        "create_terminal", "type_text", "click",
    }:
        if intent.get("command") and intent.get("target"):
            # Has target + command — check if target is a Claude session
            from actions.dev_tools import _get_claude_processes
            processes = await _get_claude_processes()
            tty_short = intent["target"].replace("/dev/", "")
            if tty_short in {p["tty"] for p in processes}:
                action = "send_to_claude"
            else:
                action = "send_to_terminal"
            log.info("Inferred action=%s from args (target=%s)", action, intent["target"])
        elif intent.get("command") and not intent.get("target"):
            action = "send_to_terminal"
            log.info("Inferred action=send_to_terminal (command only, no target)")

    if action == "list_sessions":
        return await _handle_list_sessions(ctx)

    if action == "read_terminal":
        target = intent.get("target", "")
        lines = int(intent.get("lines", 50))
        return await _handle_read_terminal(target, lines, ctx)

    if action == "claude_code_status":
        return await _handle_claude_code_status(ctx)

    if action == "system_info":
        return await _handle_system_info(ctx)

    if action == "frontmost_app":
        return await _handle_frontmost_app(ctx)

    if action == "screenshot":
        return await _handle_screenshot(ctx)

    if action in ("send_to_terminal", "send_to_claude"):
        target = intent.get("target", "current")
        command = intent.get("command", "")
        # Auto-upgrade to send_to_claude if target is a Claude session
        if action == "send_to_terminal" and target.startswith("/dev/"):
            from actions.dev_tools import _get_claude_processes
            processes = await _get_claude_processes()
            tty_short = target.replace("/dev/", "")
            if tty_short in {p["tty"] for p in processes}:
                log.info("Upgraded send_to_terminal -> send_to_claude (target %s is Claude)", target)
                action = "send_to_claude"
        if action == "send_to_claude":
            return await _handle_send_to_claude(target, command, intent, ctx)
        return await _handle_send_to_terminal(target, command, intent, ctx)

    if action == "create_terminal":
        command = intent.get("command")
        return await _handle_create_terminal(command, intent, ctx)

    if action == "type_text":
        text = intent.get("command", "")
        return await _handle_type_text(text, intent, ctx)

    if action == "click":
        coords = intent.get("command", "")
        return await _handle_click(coords, intent, ctx)

    return False


# --- READ actions ---

async def _handle_list_sessions(ctx) -> bool:
    """List all terminal sessions from iTerm2 and tmux."""
    from actions.terminal import get_iterm_sessions, get_active_processes
    from actions.tmux_control import list_sessions as tmux_list

    sessions = await get_iterm_sessions()
    processes = await get_active_processes(sessions)

    # Build tty -> process map
    tty_procs = {}
    for p in processes:
        tty_procs[p["tty"]] = p

    lines = []
    if sessions:
        lines.append(f"**iTerm2 Sessions** ({len(sessions)})")
        for s in sessions:
            proc = tty_procs.get(s["tty"])
            process_info = proc["command"] if proc else "idle"
            current = " <-" if s.get("is_current") else ""
            lines.append(f"  {s['tty']}  {process_info}{current}")
            lines.append(f"    Window: {s['window']}")

    tmux_result = await tmux_list()
    if tmux_result and "No tmux" not in tmux_result:
        lines.append("")
        lines.append(tmux_result)

    if not lines:
        lines.append("No terminal sessions found.")

    await ctx.reply("\n".join(lines))
    return True


async def _handle_read_terminal(target: str, lines: int, ctx) -> bool:
    """Read output from a terminal — iTerm2 (by TTY) or tmux (by session name)."""
    if not target:
        await ctx.reply("Specify a target: TTY path (e.g. /dev/ttys057) or tmux session name.")
        return True

    # iTerm2 session (by TTY path)
    if target.startswith("/dev/"):
        from actions.terminal import read_iterm_session
        result = await read_iterm_session(target, lines=lines)
        if result["success"]:
            await ctx.reply(f"**Terminal {target}:**\n```\n{result['content']}\n```")
        else:
            await ctx.reply(f"Could not read {target}: {result['error']}")
        return True

    # tmux session (by name)
    from actions.tmux_control import read_output
    result = await read_output(target, lines=lines)
    await ctx.reply(result)
    return True


async def _handle_claude_code_status(ctx) -> bool:
    """Show Claude Code processes with CWD."""
    from actions.dev_tools import _get_claude_processes, _format_processes
    processes = await _get_claude_processes()
    await ctx.reply(_format_processes(processes))
    return True


async def _handle_system_info(ctx) -> bool:
    """System info + running apps."""
    from actions.macos import get_system_info, get_running_apps

    info = await get_system_info()
    apps = await get_running_apps()

    lines = ["**System Info**"]
    if "battery_percent" in info:
        charge = "charging" if info.get("battery_charging") else "discharging"
        lines.append(f"  Battery: {info['battery_percent']}% ({charge})")
    if "storage_available" in info:
        lines.append(f"  Storage: {info['storage_used']} used / {info['storage_total']} ({info['storage_available']} free)")
    if "memory_total_gb" in info:
        lines.append(f"  Memory: {info['memory_total_gb']} GB")
    if "cpu_brand" in info:
        lines.append(f"  CPU: {info['cpu_brand']}")
    if apps:
        lines.append(f"\n**Running Apps** ({len(apps)}): {', '.join(sorted(apps)[:15])}")
        if len(apps) > 15:
            lines.append(f"  ...and {len(apps) - 15} more")

    await ctx.reply("\n".join(lines))
    return True


async def _handle_frontmost_app(ctx) -> bool:
    """Frontmost app + window title."""
    from actions.macos import get_frontmost_app, get_active_window_title
    app = await get_frontmost_app()
    title = await get_active_window_title()
    if app:
        text = f"Frontmost: **{app}**"
        if title:
            text += f"\nWindow: {title}"
    else:
        text = "Could not determine frontmost app."
    await ctx.reply(text)
    return True


async def _handle_screenshot(ctx) -> bool:
    """Capture screenshot."""
    from actions.macos import capture_screenshot
    path = await capture_screenshot()
    if path:
        try:
            await ctx.reply_photo(str(path), caption="Screenshot captured")
        except Exception:
            await ctx.reply(f"Screenshot saved: {path}")
    else:
        await ctx.reply("Screenshot failed.")
    return True


# --- WRITE actions ---

async def _handle_send_to_terminal(target: str, command: str, intent: dict, ctx) -> bool:
    """Send command to terminal. Goes through shell safety classification."""
    if not command:
        await ctx.reply("No command specified.")
        return True

    # tmux session
    if not target.startswith("/dev/") and target != "current":
        from actions.tmux_control import send_command
        result = await send_command(target, command)
        await ctx.reply(result)
        return True

    # Try iTerm2 first, fall back to direct TTY write
    from actions.terminal import send_to_iterm
    result = await send_to_iterm(command, session_tty=target)
    if result["success"]:
        await ctx.reply(f"Sent to {target}: `{command}`")
        return True

    # iTerm2 unavailable — write directly to TTY
    import asyncio as _aio
    tty_path = target if target.startswith("/dev/") else f"/dev/{target}"
    try:
        proc = await _aio.create_subprocess_exec(
            "bash", "-c", f'printf "%s\\n" "$1" > "$2"', "_", command, tty_path,
            stdout=_aio.subprocess.PIPE, stderr=_aio.subprocess.PIPE,
        )
        stdout, stderr = await _aio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode == 0:
            await ctx.reply(f"Sent to {target}: `{command}`")
        else:
            await ctx.reply(f"Failed to write to {tty_path}: {stderr.decode()[:200]}")
    except _aio.TimeoutError:
        await ctx.reply(f"Timed out writing to {tty_path}")
    return True


async def _handle_send_to_claude(target: str, command: str, intent: dict, ctx) -> bool:
    """Send text to a Claude Code session. Validates target has a Claude process."""
    if not target:
        await ctx.reply("Specify the TTY of the Claude Code session (e.g. /dev/ttys057).")
        return True
    if not command:
        await ctx.reply("No text specified to send.")
        return True

    # Cap length
    if len(command) > 2000:
        await ctx.reply("Text too long (max 2000 characters).")
        return True

    # Validate target is actually a Claude Code process
    from actions.dev_tools import _get_claude_processes
    processes = await _get_claude_processes()
    tty_short = target.replace("/dev/", "")
    claude_ttys = {p["tty"] for p in processes}
    if tty_short not in claude_ttys:
        available = ", ".join(f"/dev/{t}" for t in sorted(claude_ttys)) if claude_ttys else "none found"
        await ctx.reply(f"No Claude Code session on {target}.\nActive Claude sessions: {available}")
        return True

    # Write directly to the TTY device — works regardless of terminal app
    import asyncio
    tty_path = target if target.startswith("/dev/") else f"/dev/{target}"
    try:
        # Use 'write to tty' approach: newline-terminated so Claude Code sees it as input
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", f'printf "%s\\n" "$1" > "$2"', "_", command, tty_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0:
            err = stderr.decode()[:200]
            await ctx.reply(f"Failed to send to Claude session on {tty_path}: {err}")
            return True

        log.info("Sent to Claude Code session %s: %s", tty_path, command[:80])

        cwd = None
        for p in processes:
            if p["tty"] == tty_short:
                cwd = p.get("cwd")
                break
        cwd_info = f" ({cwd})" if cwd else ""
        await ctx.reply(f"Sent to Claude Code on {tty_path}{cwd_info}:\n`{command[:200]}`")
        return True
    except asyncio.TimeoutError:
        await ctx.reply("Timed out sending to Claude session.")
        return True
    except Exception as e:
        await ctx.reply(f"Error: {e}")
        return True


async def _handle_create_terminal(command: str | None, intent: dict, ctx) -> bool:
    """Create a new terminal tab."""
    from actions.terminal import create_iterm_tab
    result = await create_iterm_tab(command)
    if result["success"]:
        msg = "New terminal tab opened"
        if command:
            msg += f"\nRunning: `{command}`"
        await ctx.reply(msg)
    else:
        await ctx.reply(f"Failed: {result['error']}")
    return True


async def _handle_type_text(text: str, intent: dict, ctx) -> bool:
    """Type text via GUI keyboard."""
    if not text:
        await ctx.reply("No text specified.")
        return True
    from actions.gui_automation import type_text
    result = await type_text(text)
    await ctx.reply(result)
    return True


async def _handle_click(coords: str, intent: dict, ctx) -> bool:
    """Click at screen coordinates."""
    import re
    m = re.search(r"(\d+)\s*[,\s]+\s*(\d+)", coords)
    if not m:
        await ctx.reply("Specify coordinates: x,y (e.g. 500,300)")
        return True
    x, y = int(m.group(1)), int(m.group(2))
    from actions.gui_automation import click_at
    result = await click_at(x, y)
    await ctx.reply(result)
    return True
