from __future__ import annotations

"""Dev tools status — check coding-agent sessions and terminal activity.

Answers questions like "is Codex working?" by inspecting running processes.
"""

import asyncio
import logging
import re

log = logging.getLogger("khalil.actions.dev_tools")

SKILL = {
    "name": "dev_tools",
    "description": "Check developer tool status — Codex, Claude Code, git, terminals",
    "category": "development",
    "patterns": [
        (r"\bcodex\s+(?:status|session|instance|process)", "claude_code_status"),
        (r"\b(?:is|any)\s+codex\s+(?:waiting|running|active|idle|working)\b", "claude_code_status"),
        (r"\bclaude\s*code\b", "claude_code_status"),
        (r"\bclaude\s+(?:session|instance|process)", "claude_code_status"),
        (r"\b(?:is|any)\s+claude\s+(?:waiting|running|active|idle)\b", "claude_code_status"),
        (r"\bcoding?\s+(?:session|agent)s?\s+(?:status|running|waiting|active)\b", "claude_code_status"),
    ],
    "actions": [
        {
            "type": "claude_code_status",
            "handler": "handle_intent",
            "keywords": "codex claude code coding agent session waiting running active idle terminal",
            "description": "Check coding-agent session status",
        },
    ],
    "examples": [
        "Is Codex working?",
        "Any active coding agent sessions?",
        "Is Claude Code waiting on me?",
    ],
}


async def _get_coding_agent_processes() -> list[dict]:
    """Get running Codex and Claude Code CLI processes with their state."""
    proc = await asyncio.create_subprocess_exec(
        "ps", "aux",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()

    processes = []
    for line in stdout.decode().splitlines():
        line_lower = line.lower()
        if "claude" not in line_lower and "codex" not in line_lower:
            continue
        # Skip desktop apps, helpers, grep, and this ps call.
        if any(skip in line for skip in [
            "Claude.app", "Claude Helper", "Codex.app", "crashpad", "ShipIt", "grep", "ps aux",
        ]):
            continue

        parts = line.split(None, 10)
        if len(parts) < 11:
            continue

        cpu = float(parts[2])
        stat = parts[7]  # e.g. S+, R+, S
        tty = parts[6]   # e.g. s057, s131, ??
        started = parts[8]
        command = parts[10]
        agent = "Codex" if "codex" in command.lower() else "Claude Code"

        # Determine status
        if "S+" in stat and cpu < 1.0:
            status = "waiting for input"
        elif cpu > 5.0:
            status = "actively working"
        elif "S+" in stat:
            status = "idle (foreground)"
        else:
            status = "background"

        processes.append({
            "pid": int(parts[1]),
            "tty": tty,
            "cpu": cpu,
            "stat": stat,
            "started": started,
            "status": status,
            "agent": agent,
            "command": command[:60],
        })

    # Resolve CWD for each process via lsof
    for p in processes:
        p["cwd"] = await _resolve_cwd(p["pid"])

    return processes


async def _get_claude_processes() -> list[dict]:
    """Compatibility helper for features that specifically target Claude TTYs."""
    return [
        process for process in await _get_coding_agent_processes()
        if process["agent"] == "Claude Code"
    ]


async def _resolve_cwd(pid: int) -> str | None:
    """Resolve the current working directory of a process via lsof."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "lsof", "-d", "cwd", "-p", str(pid), "-Fn",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode != 0:
            return None
        for line in stdout.decode().splitlines():
            if line.startswith("n/"):
                return line[1:]  # Strip the 'n' prefix
        return None
    except Exception:
        return None


def _format_processes(processes: list[dict]) -> str:
    """Format process list for Telegram display."""
    if not processes:
        return "No coding-agent sessions running."

    waiting = [p for p in processes if "waiting" in p["status"]]
    working = [p for p in processes if "working" in p["status"]]

    lines = [f"**Coding Agent Sessions** ({len(processes)} total)\n"]

    if waiting:
        lines.append(f"⏳ **{len(waiting)} waiting for your input:**")
        for p in waiting:
            cwd = f"\n    📂 {p['cwd']}" if p.get("cwd") else ""
            lines.append(f"  • {p['agent']} on {p['tty']} (started {p['started']}){cwd}")

    if working:
        lines.append(f"🔄 **{len(working)} actively working:**")
        for p in working:
            cwd = f"\n    📂 {p['cwd']}" if p.get("cwd") else ""
            lines.append(f"  • {p['agent']} on {p['tty']} — CPU {p['cpu']:.0f}%{cwd}")

    idle = [p for p in processes if p not in waiting and p not in working]
    if idle:
        lines.append(f"💤 **{len(idle)} idle:**")
        for p in idle:
            cwd = f"\n    📂 {p['cwd']}" if p.get("cwd") else ""
            lines.append(f"  • {p['agent']} on {p['tty']} (started {p['started']}){cwd}")

    return "\n".join(lines)


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle dev tools queries."""
    if action == "claude_code_status":
        processes = await _get_coding_agent_processes()
        response = _format_processes(processes)
        await ctx.reply(response)
        return True

    return False
