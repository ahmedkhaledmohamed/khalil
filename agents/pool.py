"""Sub-agent delegation pool for parallel task execution."""

import asyncio
import json
import logging
import time

import anthropic

from config import CLAUDE_MODEL, KEYRING_SERVICE, MAX_CONCURRENT_AGENTS

log = logging.getLogger("pharoclaw.agents")

_SUB_AGENT_SYSTEM = (
    "You are a sub-agent for PharoClaw, a personal AI assistant. "
    "Complete the following task concisely. Return only the result, no preamble."
)


def _get_api_key() -> str | None:
    """Get Anthropic API key from keyring or env."""
    import keyring
    import os
    val = keyring.get_password(KEYRING_SERVICE, "anthropic-api-key")
    if val:
        return val
    return os.environ.get("ANTHROPIC_API_KEY")


async def delegate(task: str, context: dict | None = None, model: str = None) -> str:
    """Run an isolated Claude API call for a sub-task.

    Args:
        task: The task description for the sub-agent.
        context: Optional context dict to include in the system prompt.
        model: Override model (defaults to CLAUDE_MODEL).

    Returns:
        The sub-agent's response text, or an error string on failure.
    """
    model = model or CLAUDE_MODEL
    start = time.monotonic()
    success = False
    result = ""

    try:
        api_key = _get_api_key()
        if not api_key:
            result = "[sub-agent error] No Anthropic API key configured."
            return result

        system = _SUB_AGENT_SYSTEM
        if context:
            system += f"\n\nContext:\n{json.dumps(context, default=str)}"

        client = anthropic.AsyncAnthropic(api_key=api_key)
        response = await client.messages.create(
            model=model,
            max_tokens=1500,
            system=system,
            messages=[{"role": "user", "content": task}],
            timeout=30.0,
        )
        result = response.content[0].text
        success = True
        return result

    except Exception as e:
        log.error("Sub-agent failed: %s", e)
        result = f"[sub-agent error] {type(e).__name__}: {e}"
        return result

    finally:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        try:
            from learning import record_signal
            record_signal("agent_delegation", {
                "task": task[:200],
                "model": model,
                "latency_ms": elapsed_ms,
                "success": success,
                "result_length": len(result),
            })
        except Exception:
            pass  # Learning DB may not be initialized


async def fan_out(tasks: list[str], context: dict | None = None, model: str = None) -> list[str]:
    """Run multiple delegate() calls in parallel with bounded concurrency.

    Args:
        tasks: List of task strings.
        context: Optional shared context for all sub-agents.
        model: Override model.

    Returns:
        Results in the same order as tasks.
    """
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_AGENTS)

    async def _bounded(task: str) -> str:
        async with semaphore:
            return await delegate(task, context=context, model=model)

    return await asyncio.gather(*[_bounded(t) for t in tasks])


async def fan_out_named(
    tasks: dict[str, str], context: dict | None = None, model: str = None,
) -> dict[str, str]:
    """Run named delegate() calls in parallel and return {name: result}.

    Args:
        tasks: {name: task_string} mapping.
        context: Optional shared context for all sub-agents.
        model: Override model.

    Returns:
        {name: result} dict in same key order.
    """
    names = list(tasks.keys())
    task_list = [tasks[n] for n in names]
    results = await fan_out(task_list, context=context, model=model)
    return dict(zip(names, results))
