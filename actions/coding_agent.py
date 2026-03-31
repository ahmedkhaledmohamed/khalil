"""Coding agent — delegate coding tasks to Claude Code from Telegram.

User-facing skill that wraps the existing claude_code.py utility module.
Adds project selection, async task tracking, and result reporting.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("khalil.actions.coding_agent")

SKILL = {
    "name": "coding_agent",
    "description": "Delegate coding tasks to Claude Code — fix bugs, refactor, add features",
    "category": "development",
    "patterns": [
        (r"\b(?:fix|debug)\s+(?:the\s+)?(?:bug|error|issue|test|failing)\b", "code_task"),
        (r"\brefactor\s+(?:the\s+)?\w+", "code_task"),
        (r"\badd\s+(?:error\s+)?handling\s+to\b", "code_task"),
        (r"\bcode\s+(?:task|this|that)\b", "code_task"),
        (r"\bimplement\s+(?:the\s+)?\w+\s+(?:feature|function|method)\b", "code_task"),
        (r"\bcoding?\s+(?:task\s+)?status\b", "code_status"),
        (r"\bhow(?:'s|\s+is)\s+(?:the\s+)?coding?\s+task\b", "code_status"),
        (r"\bshow\s+(?:the\s+)?(?:last\s+)?(?:code\s+)?diff\b", "code_diff"),
        (r"\bwhat\s+(?:did\s+(?:the\s+)?code\s+agent|code)\s+change\b", "code_diff"),
    ],
    "actions": [
        {"type": "code_task", "handler": "handle_intent", "keywords": "fix refactor add implement code bug error feature debug", "description": "Run a coding task"},
        {"type": "code_status", "handler": "handle_intent", "keywords": "coding task status progress how", "description": "Check coding task status"},
        {"type": "code_diff", "handler": "handle_intent", "keywords": "diff changes code last show what changed", "description": "Show last coding task diff"},
    ],
    "examples": [
        "Fix the failing test in khalil",
        "Refactor the blogwatcher module",
        "Add error handling to summarize.py",
        "Coding task status",
    ],
    "voice": {"confirm_before_execute": True, "response_style": "brief"},
}

# Known project repos
_PROJECTS = {
    "khalil": Path(__file__).parent.parent,
    "personal": Path.home() / "Developer" / "Personal",
}


@dataclass
class CodingTask:
    """Tracks an active or completed coding task."""
    prompt: str
    project: str
    status: str = "pending"  # pending, running, completed, failed
    started_at: float = 0
    finished_at: float = 0
    output: str = ""
    branch: str = ""


# In-memory task tracking (most recent task per chat)
_active_tasks: dict[int, CodingTask] = {}
_last_task: dict[int, CodingTask] = {}


def _detect_project(query: str) -> tuple[str, Path]:
    """Detect which project the user is referring to."""
    query_lower = query.lower()
    for name, path in _PROJECTS.items():
        if name in query_lower:
            return name, path
    # Default to khalil
    return "khalil", _PROJECTS["khalil"]


async def run_coding_task(prompt: str, project_path: Path, chat_id: int, ctx) -> None:
    """Run a coding task in the background and notify when done."""
    from actions.claude_code import run_claude_code, create_worktree, cleanup_worktree

    task = _active_tasks.get(chat_id)
    if not task:
        return

    task.status = "running"
    task.started_at = time.time()

    # Create a descriptive branch name
    slug = re.sub(r"[^a-z0-9]+", "-", prompt.lower())[:40].strip("-")
    branch_name = f"agent/{slug}"
    task.branch = branch_name

    try:
        worktree_path = await asyncio.to_thread(create_worktree, branch_name)
        success, output = await run_claude_code(prompt, worktree_path, timeout=300)

        task.output = output
        task.finished_at = time.time()
        task.status = "completed" if success else "failed"

        elapsed = int(task.finished_at - task.started_at)
        status_emoji = "✅" if success else "❌"

        # Notify user
        summary = output[:500] if output else "No output"
        await ctx.reply(
            f"{status_emoji} Coding task **{task.status}** ({elapsed}s)\n"
            f"Branch: `{branch_name}`\n\n"
            f"```\n{summary}\n```"
        )

        if not success:
            await asyncio.to_thread(cleanup_worktree, branch_name)

    except Exception as e:
        task.status = "failed"
        task.output = str(e)
        task.finished_at = time.time()
        log.error("Coding task failed: %s", e)
        await ctx.reply(f"❌ Coding task failed: {e}")

    # Move to last_task
    _last_task[chat_id] = task
    _active_tasks.pop(chat_id, None)


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle coding agent intents."""
    query = intent.get("query", "") or intent.get("user_query", "")
    chat_id = getattr(ctx, "chat_id", 0)

    if action == "code_task":
        # Check if there's already an active task
        if chat_id in _active_tasks and _active_tasks[chat_id].status == "running":
            await ctx.reply("A coding task is already running. Check status with 'coding task status'.")
            return True

        project_name, project_path = _detect_project(query)

        # Clean up the prompt — remove command words
        prompt = re.sub(
            r"^\s*(?:code\s+task|please|can\s+you|khalil)\s*[,:]?\s*",
            "", query, flags=re.IGNORECASE,
        ).strip()
        if not prompt:
            await ctx.reply("What coding task should I run?")
            return True

        task = CodingTask(prompt=prompt, project=project_name)
        _active_tasks[chat_id] = task

        await ctx.reply(
            f"🔧 Starting coding task on **{project_name}**...\n"
            f"Task: {prompt[:200]}\n\n"
            f"I'll notify you when it's done (usually 1-5 min)."
        )

        # Run in background
        asyncio.create_task(run_coding_task(prompt, project_path, chat_id, ctx))
        return True

    if action == "code_status":
        active = _active_tasks.get(chat_id)
        if active and active.status == "running":
            elapsed = int(time.time() - active.started_at)
            await ctx.reply(
                f"⏳ Coding task running ({elapsed}s elapsed)\n"
                f"Project: {active.project}\n"
                f"Task: {active.prompt[:200]}"
            )
            return True

        last = _last_task.get(chat_id)
        if last:
            elapsed = int(last.finished_at - last.started_at)
            status_emoji = "✅" if last.status == "completed" else "❌"
            await ctx.reply(
                f"{status_emoji} Last task: **{last.status}** ({elapsed}s)\n"
                f"Project: {last.project}\n"
                f"Branch: `{last.branch}`\n"
                f"Task: {last.prompt[:200]}"
            )
            return True

        await ctx.reply("No coding tasks in progress or recently completed.")
        return True

    if action == "code_diff":
        last = _last_task.get(chat_id)
        if not last or not last.branch:
            await ctx.reply("No recent coding task to show diff for.")
            return True

        # Show the output which contains the changes
        output = last.output
        if not output:
            await ctx.reply("No output from the last coding task.")
            return True

        # Truncate for Telegram
        if len(output) > 3000:
            output = output[:3000] + "\n...(truncated)"
        await ctx.reply(f"📝 Changes from `{last.branch}`:\n```\n{output}\n```")
        return True

    return False
