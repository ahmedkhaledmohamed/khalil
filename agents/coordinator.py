"""Agent swarm coordinator — decompose complex queries into parallel sub-agents."""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field

from config import CLAUDE_MODEL, SWARM_ENABLED, MAX_CONCURRENT_AGENTS

log = logging.getLogger("pharoclaw.agents.coordinator")


@dataclass
class SubAgent:
    """A focused sub-agent with a specific task and context slice."""
    name: str
    task: str
    context_slice: str = ""
    model: str = ""  # defaults to CLAUDE_MODEL

    def __post_init__(self):
        if not self.model:
            self.model = CLAUDE_MODEL


@dataclass
class SwarmResult:
    """Aggregated results from a swarm execution."""
    results: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    elapsed_ms: int = 0


async def decompose_to_swarm(query: str, context: str, ask_llm_fn) -> list[SubAgent] | None:
    """Use LLM to decide if a query benefits from parallel sub-agents.

    Returns list of SubAgents if parallelizable, None if sequential is better.
    """
    if not SWARM_ENABLED:
        return None

    prompt = (
        "Analyze this user request and decide if it benefits from parallel execution "
        "by multiple focused sub-agents. If yes, decompose into 2-5 independent sub-tasks.\n\n"
        f"Request: {query}\n\n"
        "Respond in this exact JSON format:\n"
        '{"parallel": true/false, "agents": [{"name": "...", "task": "..."}]}\n\n'
        "Rules:\n"
        "- Only use parallel if tasks are genuinely independent (no data dependencies)\n"
        "- Each agent gets a focused task, not the full query\n"
        "- If the query is simple or sequential, set parallel=false and agents=[]\n"
    )

    try:
        response = await ask_llm_fn(prompt, context)
        # Extract JSON from response (may have markdown fencing)
        json_str = response
        if "```" in response:
            match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', response, re.DOTALL)
            if match:
                json_str = match.group(1)

        data = json.loads(json_str)
        if not data.get("parallel") or not data.get("agents"):
            return None

        return [
            SubAgent(name=a["name"], task=a["task"], context_slice=context[:2000])
            for a in data["agents"][:5]  # cap at 5 agents
        ]
    except Exception as e:
        log.warning("Swarm decomposition failed: %s", e)
        return None


async def run_swarm(agents: list[SubAgent]) -> SwarmResult:
    """Execute sub-agents in parallel with bounded concurrency.

    Uses the existing fan_out_named() from agents/pool.py.
    """
    from agents.pool import fan_out_named

    start = time.monotonic()

    tasks = {agent.name: agent.task for agent in agents}
    context = {}
    # Use the first agent's context slice as shared context (they share the query context)
    for agent in agents:
        if agent.context_slice:
            context = {"context": agent.context_slice}
            break

    raw_results = await fan_out_named(tasks, context=context)

    result = SwarmResult(elapsed_ms=int((time.monotonic() - start) * 1000))

    for name, response in raw_results.items():
        if response.startswith("[sub-agent error]"):
            result.errors[name] = response
        else:
            result.results[name] = response

    log.info(
        "Swarm completed: %d/%d succeeded in %dms",
        len(result.results), len(agents), result.elapsed_ms,
    )

    # Record signal for learning
    try:
        from learning import record_signal
        record_signal("swarm_execution", {
            "agent_count": len(agents),
            "success_count": len(result.results),
            "error_count": len(result.errors),
            "elapsed_ms": result.elapsed_ms,
        })
    except Exception:
        pass

    return result


async def synthesize_results(query: str, swarm_result: SwarmResult, ask_llm_fn) -> str:
    """Use LLM to synthesize parallel sub-agent results into a coherent response."""
    if not swarm_result.results:
        errors = "\n".join(f"- {k}: {v}" for k, v in swarm_result.errors.items())
        return f"All sub-agents failed:\n{errors}"

    parts = []
    for name, result in swarm_result.results.items():
        parts.append(f"[{name}]:\n{result}")

    if swarm_result.errors:
        parts.append("\n[Failed agents]:")
        for name, err in swarm_result.errors.items():
            parts.append(f"- {name}: {err}")

    combined = "\n\n".join(parts)

    synthesis_prompt = (
        f"The user asked: {query}\n\n"
        f"Multiple sub-agents worked on this in parallel. Here are their results:\n\n"
        f"{combined}\n\n"
        "Synthesize these into a single coherent response for the user. "
        "Combine insights, remove redundancy, and present a unified answer."
    )

    return await ask_llm_fn(synthesis_prompt, "")
