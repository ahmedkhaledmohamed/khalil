"""Multi-step task orchestrator — decompose compound requests, execute with dependencies."""

import json
import logging
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from config import ActionType, DB_PATH
from execution import ActionResult, ExecutionContext, ExecutionSource
from execution_graph import (
    ExecutionGraphRepository,
    GraphNode,
    GraphRun,
    GraphStatus,
    NodeStatus,
)
from graph_runner import ExecutionGraphRunner, NodePreparation

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
    status: str = "pending"          # pending, running, completed, failed, blocked, skipped
    result: str | None = None        # output from execution
    error: str | None = None         # error message if failed
    # M2: Conditional execution — skip step without LLM call when condition not met
    condition: dict | None = None    # {"if": "step_1.result contains 'no events'", "then": "skip"}
    replan_count: int = 0            # number of re-plans attempted for this step

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "action": self.action,
            "description": self.description,
            "params": self.params,
            "depends_on": self.depends_on,
            "status": self.status,
            "result": self.result,
            "error": self.error,
        }
        if self.condition:
            d["condition"] = self.condition
        if self.replan_count:
            d["replan_count"] = self.replan_count
        return d

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
            condition=d.get("condition"),
            replan_count=d.get("replan_count", 0),
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
        if any(step.status == "waiting_for_approval" for step in self.steps):
            return "waiting_for_approval"
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


MAX_REPLANS = 2  # maximum re-plan attempts per step


def evaluate_step_condition(step: TaskStep, step_results: dict[str, str]) -> bool:
    """Evaluate a step's condition against prior results.

    Returns True if the step should execute, False if it should be skipped.
    Condition format: {"if": "step_1.result contains 'no events'", "then": "skip"}
    """
    if not step.condition:
        return True  # No condition = always execute

    condition_expr = step.condition.get("if", "")
    action_on_match = step.condition.get("then", "skip")

    if not condition_expr:
        return True

    # Parse "step_X.result contains 'value'" pattern
    import re as _re
    m = _re.match(r"(\w+)\.result\s+contains\s+'([^']*)'", condition_expr)
    if m:
        ref_step_id, search_text = m.groups()
        ref_result = step_results.get(ref_step_id, "")
        condition_met = search_text.lower() in ref_result.lower()

        if condition_met and action_on_match == "skip":
            return False  # Condition met + skip = don't execute
        if not condition_met and action_on_match == "execute":
            return False  # Condition not met + execute-only = don't execute
        return True

    # Parse "step_X.result is empty" pattern
    m = _re.match(r"(\w+)\.result\s+is\s+empty", condition_expr)
    if m:
        ref_step_id = m.group(1)
        ref_result = step_results.get(ref_step_id, "")
        is_empty = not ref_result.strip()
        if is_empty and action_on_match == "skip":
            return False
        return True

    log.warning("Unparseable condition: %s", condition_expr)
    return True  # Execute by default on unparseable conditions


def substitute_step_params(step: TaskStep, step_results: dict[str, str]):
    """Inject prior step results into downstream step descriptions and params.

    Template syntax: {step_1.result} in description or param values.
    """
    import re as _re
    pattern = r"\{(\w+)\.result\}"

    def _replace(match):
        ref_id = match.group(1)
        return step_results.get(ref_id, f"[{ref_id} result unavailable]")

    step.description = _re.sub(pattern, _replace, step.description)
    for k, v in step.params.items():
        if isinstance(v, str):
            step.params[k] = _re.sub(pattern, _replace, v)


async def execute_plan(
    steps: list[TaskStep],
    query: str,
    channel,
    chat_id: int,
    execute_step_fn=None,
    ask_llm_fn=None,
    *,
    execution_bus=None,
    execution_context: ExecutionContext | None = None,
    plan_id: str | None = None,
    recover_interrupted: bool = False,
) -> PlanResult:
    """Execute or resume an orchestrator plan through the durable graph runner."""
    if execution_bus is None and execute_step_fn is None:
        raise ValueError("execute_plan requires an ExecutionBus or execute_step_fn")

    plan_id = plan_id or f"plan_{uuid.uuid4().hex[:8]}"
    total = len(steps)
    step_map = {s.id: s for s in steps}
    conn = _get_conn()
    repository = ExecutionGraphRepository(conn)
    repository.ensure_schema()
    graph = repository.load_graph(plan_id)
    if graph is None:
        graph_nodes = []
        for step in steps:
            idempotency_key = step.params.get("idempotency_key")
            if idempotency_key is None and execution_bus is not None:
                get_action_type = getattr(execution_bus, "get_declared_action_type", None)
                declared_type = get_action_type(step.action) if get_action_type else None
                if declared_type in {ActionType.WRITE, ActionType.DANGEROUS}:
                    idempotency_key = f"{plan_id}:{step.id}"
            graph_nodes.append(GraphNode(
                id=step.id,
                action=step.action,
                dependencies=list(step.depends_on),
                inputs=dict(step.params),
                idempotency_key=idempotency_key,
                max_attempts=MAX_REPLANS + 1,
                metadata={
                    "description": step.description,
                    "condition": step.condition,
                },
            ))
        graph = repository.create_graph(GraphRun(
            id=plan_id,
            source=ExecutionSource.ORCHESTRATOR.value,
            nodes=graph_nodes,
            inputs={"query": query},
            metadata={"chat_id": chat_id, "kind": "orchestrator"},
        ))

    async def _prepare(node: GraphNode, prior_results: dict[str, str]) -> NodePreparation:
        step = step_map[node.id]
        if not evaluate_step_condition(step, prior_results):
            return NodePreparation(
                params=dict(step.params),
                skip_output=f"Skipped: condition not met ({step.condition})",
            )
        substitute_step_params(step, prior_results)
        return NodePreparation(params=dict(step.params))

    async def _progress(event: str, node: GraphNode, action_result: ActionResult | None):
        step = step_map[node.id]
        step_num = steps.index(step) + 1
        if event == "started":
            step.status = "running"
            await channel.send_message(
                chat_id, f"⏳ Step {step_num}/{total}: {step.description}..."
            )
        elif event == "succeeded":
            step.status = "completed"
            step.result = action_result.output if action_result else ""
            await channel.send_message(
                chat_id, f"✅ Step {step_num}/{total}: {step.description}"
            )
        elif event == "skipped":
            step.status = "skipped"
            step.result = action_result.output if action_result else "Skipped"
            await channel.send_message(
                chat_id, f"⏭ Step {step_num}/{total}: {step.description} (skipped)"
            )
        elif event == "retrying":
            step.status = "pending"
            step.replan_count = node.attempt_count
            await channel.send_message(chat_id, f"🔄 Retrying step {step_num}...")
        elif event == "waiting_for_approval":
            step.status = "waiting_for_approval"
            step.error = action_result.error if action_result else "Approval required"
            await channel.send_message(
                chat_id,
                f"⏸ Step {step_num}/{total}: {step.description}\n{step.error}",
            )
        elif event == "failed":
            step.status = "failed"
            step.error = action_result.error if action_result else "Execution failed"
            await channel.send_message(
                chat_id,
                f"❌ Step {step_num}/{total}: {step.description}\nError: {step.error}",
            )

    async def _legacy_execute(node: GraphNode, request) -> ActionResult:
        step = step_map[node.id]
        try:
            output = await execute_step_fn(step, request.context.prior_results)
            if isinstance(output, ActionResult):
                return output
            return ActionResult.succeeded(str(output or ""))
        except Exception as error:
            return ActionResult.failed(str(error)[:500])

    context = execution_context or ExecutionContext(
        source=ExecutionSource.ORCHESTRATOR,
        chat_id=chat_id,
        parent_plan_id=plan_id,
    )
    runner = ExecutionGraphRunner(repository, execution_bus)
    try:
        graph = await runner.run(
            plan_id,
            context,
            prepare_node=_prepare,
            progress=_progress,
            execute_node=_legacy_execute if execution_bus is None else None,
            recover_interrupted=recover_interrupted,
        )
        result = _plan_from_graph(graph)
    finally:
        conn.close()

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


def _plan_from_graph(graph: GraphRun) -> PlanResult:
    """Project durable graph state onto the existing plan response contract."""
    status_map = {
        NodeStatus.PENDING: "pending",
        NodeStatus.READY: "pending",
        NodeStatus.RUNNING: "running",
        NodeStatus.WAITING_FOR_APPROVAL: "waiting_for_approval",
        NodeStatus.SUCCEEDED: "completed",
        NodeStatus.FAILED: "failed",
        NodeStatus.COMPENSATED: "blocked",
        NodeStatus.CANCELLED: "blocked",
    }
    steps = []
    for node in graph.nodes:
        outputs = node.outputs or {}
        status = status_map[node.status]
        if node.status == NodeStatus.SUCCEEDED and outputs.get("skipped"):
            status = "skipped"
        error = node.error.get("message") if node.error else None
        steps.append(TaskStep(
            id=node.id,
            action=node.action,
            description=str(node.metadata.get("description") or node.action),
            params=dict(node.inputs),
            depends_on=list(node.dependencies),
            status=status,
            result=str(outputs.get("output") or "") or None,
            error=error,
            condition=node.metadata.get("condition"),
            replan_count=max(0, node.attempt_count - 1),
        ))
    return PlanResult(
        plan_id=graph.id,
        query=str(graph.inputs.get("query") or ""),
        steps=steps,
        completed_count=sum(step.status in {"completed", "skipped"} for step in steps),
        failed_count=sum(step.status == "failed" for step in steps),
        blocked_count=sum(step.status == "blocked" for step in steps),
    )


async def resume_active_plans(execution_bus, channel) -> list[PlanResult]:
    """Resume interrupted orchestrator graphs after a process restart."""
    conn = _get_conn()
    repository = ExecutionGraphRepository(conn)
    repository.ensure_schema()
    resumable = [
        graph for graph in repository.list_resumable_runs(limit=100)
        if graph.source == ExecutionSource.ORCHESTRATOR.value
        and graph.status in {GraphStatus.PENDING, GraphStatus.RUNNING}
    ]
    conn.close()

    results = []
    for graph in resumable:
        plan = _plan_from_graph(graph)
        chat_id = int(graph.metadata.get("chat_id") or 0)
        if not chat_id:
            log.warning("Cannot resume graph %s without a chat id", graph.id)
            continue
        await channel.send_message(chat_id, f"🔄 Resuming interrupted plan {graph.id}...")
        try:
            results.append(await execute_plan(
                plan.steps,
                plan.query,
                channel,
                chat_id,
                execution_bus=execution_bus,
                execution_context=ExecutionContext(
                    source=ExecutionSource.ORCHESTRATOR,
                    chat_id=chat_id,
                    parent_plan_id=graph.id,
                ),
                plan_id=graph.id,
                recover_interrupted=True,
            ))
        except Exception as error:
            log.exception("Failed to resume execution graph %s: %s", graph.id, error)
    return results


# --- Persistence ---

def _get_conn() -> sqlite3.Connection:
    """Get a DB connection for orchestrator persistence."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def ensure_table():
    """Ensure durable graph state and the legacy plan-history table exist."""
    conn = _get_conn()
    ExecutionGraphRepository(conn).ensure_schema()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS active_plans (
            plan_id TEXT PRIMARY KEY,
            chat_id INTEGER,
            query TEXT NOT NULL,
            steps_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'in_progress',
            created_at TEXT NOT NULL,
            completed_at TEXT
        )
    """)
    # Migration: add chat_id if table already exists without it
    try:
        conn.execute("ALTER TABLE active_plans ADD COLUMN chat_id INTEGER")
    except sqlite3.OperationalError:
        pass  # Column already exists
    conn.execute("CREATE INDEX IF NOT EXISTS idx_plans_status ON active_plans(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_plans_chat ON active_plans(chat_id, status)")
    conn.commit()
    conn.close()


def save_plan(plan: PlanResult, chat_id: int = None):
    """Save or update a plan in the database."""
    conn = _get_conn()
    steps_json = json.dumps([s.to_dict() for s in plan.steps])
    now = datetime.now(timezone.utc).isoformat()
    completed_at = now if plan.status in ("completed", "partial_failure") else None

    conn.execute(
        """INSERT OR REPLACE INTO active_plans
           (plan_id, chat_id, query, steps_json, status, created_at, completed_at)
           VALUES (?, ?, ?, ?, ?, COALESCE(
               (SELECT created_at FROM active_plans WHERE plan_id = ?), ?
           ), ?)""",
        (plan.plan_id, chat_id, plan.query, steps_json, plan.status,
         plan.plan_id, now, completed_at),
    )
    conn.commit()
    conn.close()


def load_plan(plan_id: str) -> PlanResult | None:
    """Load a plan from the database."""
    conn = _get_conn()
    repository = ExecutionGraphRepository(conn)
    repository.ensure_schema()
    graph = repository.load_graph(plan_id)
    if graph is not None and graph.source == ExecutionSource.ORCHESTRATOR.value:
        conn.close()
        return _plan_from_graph(graph)
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
    repository = ExecutionGraphRepository(conn)
    repository.ensure_schema()
    graph_rows = conn.execute(
        """SELECT id FROM execution_graphs WHERE source = ?
           ORDER BY created_at DESC LIMIT 20""",
        (ExecutionSource.ORCHESTRATOR.value,),
    ).fetchall()
    graph_plans = []
    graph_ids = set()
    for (graph_id,) in graph_rows:
        graph = repository.load_graph(graph_id)
        if graph is None:
            continue
        graph_ids.add(graph.id)
        graph_plans.append({
            "plan_id": graph.id,
            "query": str(graph.inputs.get("query") or ""),
            "step_count": len(graph.nodes),
            "status": _plan_from_graph(graph).status,
            "created_at": graph.created_at,
            "completed_at": graph.completed_at,
        })
    rows = conn.execute(
        "SELECT plan_id, query, steps_json, status, created_at, completed_at "
        "FROM active_plans ORDER BY created_at DESC LIMIT 20"
    ).fetchall()
    conn.close()
    plans = list(graph_plans)
    for r in rows:
        if r[0] in graph_ids:
            continue
        steps = json.loads(r[2])
        plans.append({
            "plan_id": r[0],
            "query": r[1],
            "step_count": len(steps),
            "status": r[3],
            "created_at": r[4],
            "completed_at": r[5],
        })
    return sorted(
        plans,
        key=lambda plan: plan.get("created_at") or "",
        reverse=True,
    )[:20]


def get_active_plans_for_chat(chat_id: int) -> list[PlanResult]:
    """Get in-progress plans for a specific chat."""
    conn = _get_conn()
    repository = ExecutionGraphRepository(conn)
    repository.ensure_schema()
    graph_plans = [
        _plan_from_graph(graph)
        for graph in repository.list_resumable_runs(limit=100)
        if graph.source == ExecutionSource.ORCHESTRATOR.value
        and graph.metadata.get("chat_id") == chat_id
    ][:3]
    rows = conn.execute(
        "SELECT plan_id, query, steps_json, status FROM active_plans "
        "WHERE chat_id = ? AND status = 'in_progress' "
        "ORDER BY created_at DESC LIMIT 3",
        (chat_id,),
    ).fetchall()
    conn.close()
    graph_ids = {plan.plan_id for plan in graph_plans}
    plans = list(graph_plans)
    for r in rows:
        if r[0] in graph_ids:
            continue
        steps = [TaskStep.from_dict(s) for s in json.loads(r[2])]
        plans.append(PlanResult(
            plan_id=r[0], query=r[1], steps=steps,
            completed_count=sum(1 for s in steps if s.status == "completed"),
            failed_count=sum(1 for s in steps if s.status == "failed"),
            blocked_count=sum(1 for s in steps if s.status == "blocked"),
        ))
    return plans[:3]


def format_plan_summary(plan: PlanResult) -> str:
    """Format a plan for display to the user."""
    status_icons = {
        "pending": "⏳",
        "running": "🔄",
        "waiting_for_approval": "⏸",
        "completed": "✅",
        "skipped": "⏭",
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
