"""Agent swarm coordinator — decompose complex queries into parallel sub-agents."""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field

from config import CLAUDE_MODEL, SWARM_ENABLED, MAX_CONCURRENT_AGENTS

log = logging.getLogger("khalil.agents.coordinator")


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


# ---------------------------------------------------------------------------
# M11: Background Agent Delegation
# ---------------------------------------------------------------------------

@dataclass
class BackgroundAgent:
    """A long-running background agent that reports back asynchronously."""
    id: str
    task: str
    status: str = "running"  # "running", "completed", "failed", "expired"
    check_interval_s: int = 300  # 5 min
    max_duration_s: int = 3600  # 1 hour
    progress: list[str] = field(default_factory=list)
    final_result: str | None = None
    created_at: float = 0.0  # monotonic time
    context: dict = field(default_factory=dict)

    def elapsed_s(self) -> float:
        return time.monotonic() - self.created_at if self.created_at else 0

    def is_expired(self) -> bool:
        return self.elapsed_s() > self.max_duration_s


def _ensure_bg_table():
    """Create background_agents table if not exists."""
    import sqlite3
    from config import DB_PATH
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS background_agents (
            id TEXT PRIMARY KEY,
            task TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            progress TEXT NOT NULL DEFAULT '[]',
            final_result TEXT,
            context TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            completed_at TEXT,
            max_duration_s INTEGER NOT NULL DEFAULT 3600
        )
    """)
    conn.commit()
    conn.close()


def spawn_background_agent(
    task: str,
    context: dict | None = None,
    max_duration_s: int = 3600,
) -> BackgroundAgent:
    """Spawn a background agent for a complex, long-running task.

    The agent is persisted in DB and checked by the agent loop each tick.
    """
    import uuid
    import sqlite3
    from config import DB_PATH
    from datetime import datetime, timezone

    _ensure_bg_table()
    agent = BackgroundAgent(
        id=f"bg_{uuid.uuid4().hex[:8]}",
        task=task,
        context=context or {},
        max_duration_s=max_duration_s,
        created_at=time.monotonic(),
    )
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        "INSERT INTO background_agents (id, task, status, progress, context, created_at, max_duration_s) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (agent.id, task, "running", "[]", json.dumps(context or {}),
         datetime.now(timezone.utc).isoformat(), max_duration_s),
    )
    conn.commit()
    conn.close()
    log.info("Spawned background agent: %s — %s", agent.id, task[:60])
    return agent


def get_background_agents(status: str | None = None) -> list[dict]:
    """Get background agents, optionally filtered by status."""
    import sqlite3
    from config import DB_PATH
    _ensure_bg_table()
    conn = sqlite3.connect(str(DB_PATH))
    if status:
        rows = conn.execute(
            "SELECT id, task, status, progress, final_result, created_at, completed_at "
            "FROM background_agents WHERE status = ? ORDER BY created_at DESC LIMIT 20",
            (status,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, task, status, progress, final_result, created_at, completed_at "
            "FROM background_agents ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
    conn.close()
    return [
        {"id": r[0], "task": r[1], "status": r[2],
         "progress": json.loads(r[3]), "final_result": r[4],
         "created_at": r[5], "completed_at": r[6]}
        for r in rows
    ]


def update_background_agent(agent_id: str, status: str = None,
                             progress_entry: str = None, final_result: str = None):
    """Update a background agent's state."""
    import sqlite3
    from config import DB_PATH
    from datetime import datetime, timezone

    conn = sqlite3.connect(str(DB_PATH))
    if progress_entry:
        row = conn.execute("SELECT progress FROM background_agents WHERE id = ?", (agent_id,)).fetchone()
        if row:
            progress = json.loads(row[0])
            progress.append(progress_entry)
            conn.execute("UPDATE background_agents SET progress = ? WHERE id = ?",
                         (json.dumps(progress), agent_id))
    if status:
        conn.execute("UPDATE background_agents SET status = ? WHERE id = ?", (status, agent_id))
        if status in ("completed", "failed", "expired"):
            conn.execute("UPDATE background_agents SET completed_at = ? WHERE id = ?",
                         (datetime.now(timezone.utc).isoformat(), agent_id))
    if final_result is not None:
        conn.execute("UPDATE background_agents SET final_result = ? WHERE id = ?",
                     (final_result, agent_id))
    conn.commit()
    conn.close()


async def run_background_agent(agent_id: str, ask_llm_fn) -> str:
    """Execute a background agent's task using the tool-use loop.

    Called by agent loop when checking on running background agents.
    """
    agents = get_background_agents(status="running")
    agent_data = next((a for a in agents if a["id"] == agent_id), None)
    if not agent_data:
        return "Agent not found"

    task = agent_data["task"]
    context = json.loads(agent_data.get("context", "{}")) if isinstance(agent_data.get("context"), str) else agent_data.get("context", {})

    try:
        update_background_agent(agent_id, progress_entry="Starting execution...")

        # Use execution bus if available, otherwise direct LLM
        try:
            from execution import get_execution_bus, ExecutionContext, ExecutionSource
            bus = get_execution_bus()
            if bus:
                ctx = ExecutionContext(source=ExecutionSource.BACKGROUND_AGENT)
                result = await bus.execute("tool_reason", {"query": task}, ctx)
                if result.success:
                    update_background_agent(agent_id, status="completed",
                                            final_result=result.output[:2000])
                    return result.output
        except ImportError:
            pass

        # Fallback: direct LLM call
        result = await ask_llm_fn(task, json.dumps(context)[:2000] if context else "", "")
        update_background_agent(agent_id, status="completed", final_result=result[:2000])
        return result
    except Exception as e:
        update_background_agent(agent_id, status="failed", final_result=str(e)[:500])
        return f"Background agent failed: {e}"


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
