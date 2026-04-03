"""Multi-step task orchestrator — decompose compound requests, execute with dependencies."""

import asyncio
import json
import logging
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from config import DB_PATH, SWARM_ENABLED

log = logging.getLogger("khalil.orchestrator")

# Heuristic: skip LLM decomposition if none of these signals are present
_MULTI_STEP_SIGNALS = re.compile(
    r"\band\b|\bthen\b|\balso\b|\bafter that\b|,\s*(?:also|then|and)\b|,\s*\w+\s+(?:a|an|the|my)\b",
    re.IGNORECASE,
)


@dataclass
class TaskStep:
    id: str                          # "step_1", "step_2", etc.
    action: str                      # action type ("email_draft", "remind", "calendar_create", "shell")
    description: str                 # human-readable ("Draft email to Sarah about sprint")
    params: dict                     # action-specific parameters
    depends_on: list[str] = field(default_factory=list)  # step IDs this depends on
    status: str = "pending"          # pending, running, completed, failed, blocked
    result: str | None = None        # output from execution
    error: str | None = None         # error message if failed

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "action": self.action,
            "description": self.description,
            "params": self.params,
            "depends_on": self.depends_on,
            "status": self.status,
            "result": self.result,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TaskStep":
        return cls(
            id=d["id"],
            action=d["action"],
            description=d["description"],
            params=d.get("params", {}),
            depends_on=d.get("depends_on", []),
            status=d.get("status", "pending"),
            result=d.get("result"),
            error=d.get("error"),
        )


@dataclass
class PlanResult:
    plan_id: str
    query: str
    steps: list[TaskStep]
    completed_count: int = 0
    failed_count: int = 0
    blocked_count: int = 0

    @property
    def status(self) -> str:
        if self.failed_count > 0:
            return "partial_failure"
        if self.blocked_count > 0:
            return "blocked"
        if self.completed_count == len(self.steps):
            return "completed"
        return "in_progress"


def looks_like_multi_step(query: str) -> bool:
    """Quick heuristic: does the query look like it contains multiple actions?

    Returns True if decomposition should be attempted.
    False means the existing single-intent flow should handle it.
    """
    # Fast path: explicit conjunctions
    if _MULTI_STEP_SIGNALS.search(query):
        return True
    # Catch implicit sequences: comma-separated clauses with multiple verbs
    if len(query) > 30 and "," in query:
        # Comma + action verbs on both sides suggests multi-step
        _ACTION_VERBS = re.compile(
            r"\b(?:check|send|email|remind|set|create|add|draft|schedule|book|"
            r"cancel|find|search|get|look|open|start|stop|summarize|plan)\b",
            re.IGNORECASE,
        )
        verbs = _ACTION_VERBS.findall(query)
        if len(verbs) >= 2:
            return True
    return False


async def decompose_request(query: str, context: str, ask_llm_fn) -> list[TaskStep]:
    """Decompose a compound request into individual TaskSteps.

    Uses LLM to analyze the request and extract structured steps.
    Returns an empty list if the query is a single action (fast path).
    Returns a list of TaskStep objects if multiple actions are detected.
    """
    if not looks_like_multi_step(query):
        return []

    prompt = (
        "Analyze this user request and determine if it contains MULTIPLE distinct actions.\n\n"
        f'Request: "{query}"\n\n'
        "Rules:\n"
        "- Only decompose if there are genuinely separate actions (2+)\n"
        "- A single complex action is NOT multi-step (e.g., 'send an email about the meeting' is one action)\n"
        "- Each step must be independently executable\n"
        "- Identify dependencies between steps (e.g., 'draft email then send it' means step 2 depends on step 1)\n\n"
        "If this is a SINGLE action, respond with exactly: SINGLE\n\n"
        "If there are MULTIPLE actions, respond with ONLY a JSON array (no markdown):\n"
        "[\n"
        '  {"id": "step_1", "action": "<type>", "description": "<human-readable>", '
        '"params": {<action-specific>}, "depends_on": []},\n'
        '  {"id": "step_2", "action": "<type>", "description": "<human-readable>", '
        '"params": {<action-specific>}, "depends_on": ["step_1"]}\n'
        "]\n\n"
        "Valid action types: reminder, email, calendar, shell, search, summarize\n\n"
        "Examples:\n"
        '- "Remind me to call Sarah and draft an email to John about the project"\n'
        '  [{"id":"step_1","action":"reminder","description":"Remind to call Sarah",'
        '"params":{"text":"Call Sarah","time":""},"depends_on":[]},\n'
        '   {"id":"step_2","action":"email","description":"Draft email to John about project",'
        '"params":{"to":"John","subject":"Project update","context_query":"project"},"depends_on":[]}]\n\n'
        '- "Check my calendar then send a summary email to the team"\n'
        '  [{"id":"step_1","action":"calendar","description":"Check today\'s calendar",'
        '"params":{},"depends_on":[]},\n'
        '   {"id":"step_2","action":"email","description":"Send calendar summary to team",'
        '"params":{"to":"team","subject":"Calendar summary","context_query":"calendar"},"depends_on":["step_1"]}]'
    )

    response = await ask_llm_fn(
        prompt, context,
        system_extra="Respond with SINGLE or a JSON array. No explanation, no markdown fences.",
    )
    response = response.strip()

    if response.upper() == "SINGLE" or response.startswith("⚠️"):
        return []

    from llm import TaskStepModel, parse_llm_json_list

    steps = parse_llm_json_list(response, TaskStepModel)
    if len(steps) < 2:
        return []
    return [TaskStep.from_dict(s.model_dump()) for s in steps]


async def execute_plan(
    steps: list[TaskStep],
    query: str,
    channel,
    chat_id: int,
    execute_step_fn,
) -> PlanResult:
    """Execute a plan of TaskSteps respecting dependencies.

    Args:
        steps: list of TaskSteps to execute
        query: original user query
        channel: Channel instance for progress updates
        chat_id: chat to send updates to
        execute_step_fn: async callable(step: TaskStep, prior_results: dict[str, str]) -> str
                         that executes a single step with results from completed dependencies,
                         and returns a result string. Raises on failure.
    """
    plan_id = f"plan_{uuid.uuid4().hex[:8]}"
    total = len(steps)

    # Build dependency graph: step_id -> set of step_ids it depends on
    pending_deps = {s.id: set(s.depends_on) for s in steps}
    step_map = {s.id: s for s in steps}
    # Accumulate results from completed steps for downstream consumption
    step_results: dict[str, str] = {}

    result = PlanResult(plan_id=plan_id, query=query, steps=steps)

    # Save initial plan state
    save_plan(result)

    while True:
        # Find steps ready to execute (pending, no unresolved dependencies)
        ready = [
            step_map[sid]
            for sid, deps in pending_deps.items()
            if step_map[sid].status == "pending" and not deps
        ]

        if not ready:
            break  # No more steps can execute

        # Swarm path: when 3+ independent steps are ready, use swarm coordinator
        if len(ready) >= 3 and SWARM_ENABLED:
            from agents.coordinator import SubAgent, run_swarm
            sub_agents = [
                SubAgent(name=s.id, task=s.description)
                for s in ready
            ]
            await channel.send_message(
                chat_id,
                f"🐝 Running {len(ready)} steps as swarm...",
            )
            swarm_result = await run_swarm(sub_agents)
            for step in ready:
                step_num = steps.index(step) + 1
                if step.id in swarm_result.results:
                    step.result = swarm_result.results[step.id]
                    step.status = "completed"
                    result.completed_count += 1
                    await channel.send_message(
                        chat_id, f"✅ Step {step_num}/{total}: {step.description}"
                    )
                elif step.id in swarm_result.errors:
                    step.error = swarm_result.errors[step.id]
                    step.status = "failed"
                    result.failed_count += 1
                    await channel.send_message(
                        chat_id,
                        f"❌ Step {step_num}/{total}: {step.description}\nError: {step.error}",
                    )
                    _block_downstream(step.id, step_map, pending_deps, result)
        else:
            # Standard path: execute ready steps in parallel
            async def _run_step(step: TaskStep):
                step_num = steps.index(step) + 1
                step.status = "running"
                # Gather results from this step's dependencies
                prior = {dep_id: step_results.get(dep_id, "") for dep_id in step.depends_on}
                try:
                    await channel.send_message(
                        chat_id, f"⏳ Step {step_num}/{total}: {step.description}..."
                    )
                    step_result = await execute_step_fn(step, prior)
                    step.status = "completed"
                    step.result = step_result
                    step_results[step.id] = step_result or ""
                    result.completed_count += 1
                    await channel.send_message(
                        chat_id, f"✅ Step {step_num}/{total}: {step.description}"
                    )
                except Exception as e:
                    step.status = "failed"
                    step.error = str(e)[:500]
                    result.failed_count += 1
                    await channel.send_message(
                        chat_id, f"❌ Step {step_num}/{total}: {step.description}\nError: {step.error}"
                    )
                    # Block all downstream steps
                    _block_downstream(step.id, step_map, pending_deps, result)

            tasks = [_run_step(s) for s in ready]
            await asyncio.gather(*tasks)

        # Remove completed/failed steps from dependency lists of remaining steps
        done_ids = {s.id for s in steps if s.status in ("completed", "failed", "blocked")}
        for sid in pending_deps:
            pending_deps[sid] -= done_ids

        # Safety: if nothing changed, break to avoid infinite loop
        still_pending = [s for s in steps if s.status == "pending"]
        if not still_pending:
            break
        # Check if all pending steps are stuck (circular deps or blocked)
        ready_next = [
            s for s in still_pending
            if not pending_deps[s.id]
        ]
        if not ready_next:
            # All remaining steps are blocked
            for s in still_pending:
                s.status = "blocked"
                s.error = "Unresolvable dependency"
                result.blocked_count += 1
            break

    # Save final state
    save_plan(result)

    # Record signal
    try:
        from learning import record_signal
        record_signal("task_orchestrated", {
            "plan_id": plan_id,
            "step_count": total,
            "completed": result.completed_count,
            "failed": result.failed_count,
            "blocked": result.blocked_count,
        })
    except Exception:
        pass

    return result


def _block_downstream(failed_id: str, step_map: dict, pending_deps: dict, result: PlanResult):
    """Mark all steps that transitively depend on a failed step as blocked."""
    to_block = set()
    queue = [failed_id]
    while queue:
        current = queue.pop(0)
        for sid, deps in pending_deps.items():
            if current in deps and sid not in to_block:
                to_block.add(sid)
                queue.append(sid)
    for sid in to_block:
        step = step_map[sid]
        if step.status == "pending":
            step.status = "blocked"
            step.error = f"Blocked: dependency '{failed_id}' failed"
            result.blocked_count += 1


# --- Persistence ---

def _get_conn() -> sqlite3.Connection:
    """Get a DB connection for orchestrator persistence."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def ensure_table():
    """Create the active_plans table if it doesn't exist."""
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS active_plans (
            plan_id TEXT PRIMARY KEY,
            query TEXT NOT NULL,
            steps_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'in_progress',
            created_at TEXT NOT NULL,
            completed_at TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_plans_status ON active_plans(status)")
    conn.commit()
    conn.close()


def save_plan(plan: PlanResult):
    """Save or update a plan in the database."""
    conn = _get_conn()
    steps_json = json.dumps([s.to_dict() for s in plan.steps])
    now = datetime.now(timezone.utc).isoformat()
    completed_at = now if plan.status in ("completed", "partial_failure") else None

    conn.execute(
        """INSERT OR REPLACE INTO active_plans
           (plan_id, query, steps_json, status, created_at, completed_at)
           VALUES (?, ?, ?, ?, COALESCE(
               (SELECT created_at FROM active_plans WHERE plan_id = ?), ?
           ), ?)""",
        (plan.plan_id, plan.query, steps_json, plan.status,
         plan.plan_id, now, completed_at),
    )
    conn.commit()
    conn.close()


def load_plan(plan_id: str) -> PlanResult | None:
    """Load a plan from the database."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT plan_id, query, steps_json, status FROM active_plans WHERE plan_id = ?",
        (plan_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None

    steps = [TaskStep.from_dict(s) for s in json.loads(row[2])]
    result = PlanResult(
        plan_id=row[0],
        query=row[1],
        steps=steps,
        completed_count=sum(1 for s in steps if s.status == "completed"),
        failed_count=sum(1 for s in steps if s.status == "failed"),
        blocked_count=sum(1 for s in steps if s.status == "blocked"),
    )
    return result


def list_active_plans() -> list[dict]:
    """List all active and recently completed plans."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT plan_id, query, steps_json, status, created_at, completed_at "
        "FROM active_plans ORDER BY created_at DESC LIMIT 20"
    ).fetchall()
    conn.close()
    plans = []
    for r in rows:
        steps = json.loads(r[2])
        plans.append({
            "plan_id": r[0],
            "query": r[1],
            "step_count": len(steps),
            "status": r[3],
            "created_at": r[4],
            "completed_at": r[5],
        })
    return plans


def format_plan_summary(plan: PlanResult) -> str:
    """Format a plan for display to the user."""
    status_icons = {
        "pending": "⏳",
        "running": "🔄",
        "completed": "✅",
        "failed": "❌",
        "blocked": "🚫",
    }
    lines = [f"📋 Plan: {plan.query[:80]}"]
    lines.append(f"ID: {plan.plan_id} | Status: {plan.status}")
    lines.append("")
    for i, step in enumerate(plan.steps, 1):
        icon = status_icons.get(step.status, "❓")
        line = f"{icon} Step {i}: {step.description}"
        if step.result:
            line += f"\n   → {step.result[:100]}"
        if step.error:
            line += f"\n   ⚠️ {step.error[:100]}"
        lines.append(line)
    lines.append("")
    lines.append(
        f"✅ {plan.completed_count} completed | "
        f"❌ {plan.failed_count} failed | "
        f"🚫 {plan.blocked_count} blocked"
    )
    return "\n".join(lines)
