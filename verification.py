"""Verification layer — centralized post-action checks.

Replaces scattered hallucination detectors and ad-hoc file checks with
a single module that verifies:
1. Hallucinated tool calls (LLM pretends to invoke tools in text)
2. Side effects (file exists after generate_file)
3. Task completion (mark tasks done when work succeeds)
4. Task failure (track attempts and block after repeated failures)
"""

import json
import logging
import re
from pathlib import Path

log = logging.getLogger("khalil.verification")

# Hallucinated tool call pattern — line starts with fake tool invocation
_FAKE_TOOL_RE = re.compile(r'^\s*\[(?:Called tool|tool_call)[:\s]', re.MULTILINE)

# Preamble patterns — LLM announces intent instead of delivering results
_PREAMBLE_PATTERNS = [
    r"^(?:I'll|Let me|I will|I'm going to)\s",
    r"^(?:Here's what|Here is what|I can help)",
    r"^(?:To (?:do|answer|help|build|create|complete) (?:this|that))",
]
_PREAMBLE_RE = re.compile("|".join(_PREAMBLE_PATTERNS), re.MULTILINE | re.IGNORECASE)


def detect_hallucinated_tools(response: str) -> bool:
    """Check if the response contains fake tool invocations.

    Returns True if hallucinated tool calls are detected.
    """
    return bool(_FAKE_TOOL_RE.search(response))


def is_preamble_response(text: str) -> bool:
    """Check if the response is just announcing intent without delivering results."""
    if not text or len(text) > 500:
        return False
    return bool(_PREAMBLE_RE.match(text.strip()))


def verify_file_creation(path: str) -> dict:
    """Verify that a generated file exists and has content.

    Returns: {"success": bool, "error": str | None, "size": int}
    """
    p = Path(path).expanduser()
    if not p.exists():
        return {"success": False, "error": "File does not exist", "size": 0}
    size = p.stat().st_size
    if size < 50:
        return {"success": False, "error": f"File too small ({size} bytes)", "size": size}
    return {"success": True, "error": None, "size": size}


def check_tool_results_for_completion(tool_names: list[str], tool_results: list[str]) -> bool:
    """Heuristic: did the tool-use loop produce meaningful results?

    True if we have at least one successful tool result.
    """
    if not tool_results:
        return False
    for result in tool_results:
        if "success" in result.lower()[:200] and "error" not in result.lower()[:200]:
            return True
        if len(result) > 500:  # Substantive result
            return True
    return False


def update_task_after_response(
    task_mgr,
    task,
    response: str,
    tool_names_used: list[str],
    tool_results: list[str],
):
    """Update task state based on what happened in the tool-use loop.

    - Record tools used
    - Mark complete if successful
    - Track attempts if not
    """
    if not task:
        return

    # Record which tools were used
    for tool_name in tool_names_used:
        task_mgr.record_tool_use(task.id, tool_name)

    # Check if the task looks complete
    has_meaningful_response = response and len(response) > 100 and not is_preamble_response(response)
    has_successful_tools = check_tool_results_for_completion(tool_names_used, tool_results)

    # File creation tasks: check if generate_file was called and succeeded
    if task.task_type == "artifact" and "generate_file" in tool_names_used:
        for result in tool_results:
            try:
                data = json.loads(result)
                if data.get("success") and data.get("path"):
                    check = verify_file_creation(data["path"])
                    if check["success"]:
                        task_mgr.complete_task(task.id, f"Created {data['path']} ({check['size']} bytes)")
                        return
            except (json.JSONDecodeError, KeyError):
                continue

    # General task completion: meaningful response + successful tool use
    if has_meaningful_response and has_successful_tools:
        task_mgr.complete_task(task.id, response[:200])
    elif has_meaningful_response and not tool_names_used:
        # Answered without tools (e.g., from context)
        task_mgr.complete_task(task.id, response[:200])
    else:
        # Task not completed — record attempt
        task_mgr.record_attempt(task.id)
        if task_mgr.should_reset(task_mgr.get_active_task(task.chat_id)):
            task_mgr.reset_task(task.id)
            log.warning("Task %s reset after repeated failures", task.id)
