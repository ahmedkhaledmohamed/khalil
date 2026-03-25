"""Reactive workflow engine — trigger → condition → action chains with autonomy."""

import asyncio
import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from config import DB_PATH, WORKFLOW_ENGINE_ENABLED, WORKFLOW_MAX_RUNS_PER_HOUR

log = logging.getLogger("khalil.workflows")

# Re-entrancy guard: workflow IDs currently executing
_executing: set[str] = set()

# Run count tracking for rate limiting: {workflow_id: [timestamps]}
_run_timestamps: dict[str, list[float]] = {}


@dataclass
class WorkflowStep:
    """A single action within a workflow."""
    action: str          # action module function or special: "notify", "llm_summarize", "shell"
    params: dict = field(default_factory=dict)
    description: str = ""


@dataclass
class Workflow:
    """A reactive workflow definition."""
    id: str
    name: str
    trigger_type: str                     # "cron", "signal", "webhook", "threshold"
    trigger_config: dict                  # cron: {expr}, signal: {signal_type}, webhook: {source, event}
    actions: list[WorkflowStep]
    condition: dict | None = None         # {field, op, value} or {all: [...]} or {any: [...]}
    autonomy_override: str | None = None  # None=inherit, "SUPERVISED", "GUIDED", "AUTONOMOUS"
    enabled: bool = True
    created_by: str = "user"              # "user", "system", "evolved"
    confidence: float = 1.0
    run_count: int = 0
    last_run_at: str | None = None
    last_result: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    def to_row(self) -> tuple:
        now = datetime.now(timezone.utc).isoformat()
        return (
            self.id, self.name, self.trigger_type,
            json.dumps(self.trigger_config),
            json.dumps(self.condition) if self.condition else None,
            json.dumps([{"action": s.action, "params": s.params, "description": s.description} for s in self.actions]),
            self.autonomy_override, int(self.enabled), self.created_by,
            self.confidence, self.run_count,
            self.last_run_at, self.last_result,
            self.created_at or now, self.updated_at or now,
        )

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Workflow":
        actions_raw = json.loads(row["actions"])
        return cls(
            id=row["id"], name=row["name"],
            trigger_type=row["trigger_type"],
            trigger_config=json.loads(row["trigger_config"]),
            actions=[WorkflowStep(**a) for a in actions_raw],
            condition=json.loads(row["condition"]) if row["condition"] else None,
            autonomy_override=row["autonomy_override"],
            enabled=bool(row["enabled"]),
            created_by=row["created_by"],
            confidence=row["confidence"],
            run_count=row["run_count"],
            last_run_at=row["last_run_at"],
            last_result=row["last_result"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


# --- Condition Evaluation ---

def check_condition(condition: dict | None, data: dict) -> bool:
    """Evaluate a condition dict against event data. No eval() — explicit operators only."""
    if condition is None:
        return True

    # Composite conditions
    if "all" in condition:
        return all(check_condition(c, data) for c in condition["all"])
    if "any" in condition:
        return any(check_condition(c, data) for c in condition["any"])

    field_path = condition.get("field", "")
    op = condition.get("op", "==")
    expected = condition.get("value")

    # Navigate dotted paths: "context.confidence" -> data["context"]["confidence"]
    actual = data
    for key in field_path.split("."):
        if isinstance(actual, dict):
            actual = actual.get(key)
        else:
            actual = None
            break

    if op == "==" or op == "eq":
        return actual == expected
    if op == "!=" or op == "ne":
        return actual != expected
    if op == ">" or op == "gt":
        return actual is not None and actual > expected
    if op == ">=" or op == "gte":
        return actual is not None and actual >= expected
    if op == "<" or op == "lt":
        return actual is not None and actual < expected
    if op == "<=" or op == "lte":
        return actual is not None and actual <= expected
    if op == "contains":
        return expected in (actual or "")
    if op == "not_contains":
        return expected not in (actual or "")
    if op == "absent":
        return actual is None
    if op == "present":
        return actual is not None
    if op == "in":
        return actual in (expected or [])

    log.warning("Unknown condition operator: %s", op)
    return False


# --- Threshold State (in-memory, persisted via last_result) ---

_threshold_state: dict[str, list[dict]] = {}  # workflow_id -> recent check results


def _check_consecutive_threshold(workflow_id: str, current_value: float, threshold: float,
                                  op: str, required_count: int) -> bool:
    """Track consecutive threshold breaches. Returns True if threshold met N consecutive times."""
    history = _threshold_state.setdefault(workflow_id, [])
    breached = check_condition({"field": "v", "op": op, "value": threshold}, {"v": current_value})
    history.append({"value": current_value, "breached": breached, "ts": datetime.now(timezone.utc).isoformat()})

    # Keep only last 10 entries
    if len(history) > 10:
        _threshold_state[workflow_id] = history[-10:]

    # Check consecutive breaches
    recent = history[-required_count:] if len(history) >= required_count else []
    return len(recent) == required_count and all(h["breached"] for h in recent)


# --- Workflow Engine ---

class WorkflowEngine:
    """Core workflow engine — manages registration, trigger evaluation, and execution."""

    def __init__(self, conn: sqlite3.Connection, channel=None, chat_id: int | None = None,
                 ask_llm_fn=None, execute_action_fn=None):
        self._conn = conn
        self._channel = channel
        self._chat_id = chat_id
        self._ask_llm = ask_llm_fn
        self._execute_action = execute_action_fn

    def ensure_tables(self):
        """Create workflow tables and seed defaults."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS workflows (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                trigger_type TEXT NOT NULL,
                trigger_config TEXT NOT NULL,
                condition TEXT,
                actions TEXT NOT NULL,
                autonomy_override TEXT,
                enabled INTEGER DEFAULT 1,
                created_by TEXT DEFAULT 'system',
                confidence REAL DEFAULT 1.0,
                run_count INTEGER DEFAULT 0,
                last_run_at TEXT,
                last_result TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS workflow_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_id TEXT NOT NULL,
                trigger_event TEXT,
                steps_json TEXT,
                status TEXT,
                started_at TEXT,
                completed_at TEXT
            );
        """)
        self._conn.commit()

        # Seed workflows if table is empty
        count = self._conn.execute("SELECT COUNT(*) FROM workflows").fetchone()[0]
        if count == 0:
            self._seed_workflows()

    def _seed_workflows(self):
        """Register built-in seed workflows."""
        seeds = [
            Workflow(
                id="wf_zia_health", name="Zia health monitor",
                trigger_type="cron", trigger_config={"hour": "*/6"},
                condition={"field": "downloads_delta_pct", "op": "<", "value": -30},
                actions=[
                    WorkflowStep("appstore.get_crash_reports", {"app_id": ""}, "Check crash reports"),
                    WorkflowStep("notify", {}, "Send crash diagnosis to Telegram"),
                ],
                created_by="system",
            ),
            Workflow(
                id="wf_do_cpu", name="Server CPU monitor",
                trigger_type="threshold",
                trigger_config={"metric": "digitalocean.cpu", "interval_minutes": 5},
                condition={"field": "cpu", "op": ">", "value": 80},
                actions=[
                    WorkflowStep("digitalocean.get_droplet_health", {}, "Get droplet health metrics"),
                    WorkflowStep("notify", {}, "Alert: server CPU high"),
                ],
                created_by="system",
            ),
            Workflow(
                id="wf_ext_reload", name="Extension hot-reload on merge",
                trigger_type="signal", trigger_config={"signal_type": "pr_merged"},
                condition={"any": [
                    {"field": "context.branch", "op": "contains", "value": "ext/"},
                    {"field": "context.branch", "op": "contains", "value": "heal/"},
                ]},
                actions=[
                    WorkflowStep("shell", {"command": "git pull origin main"}, "Pull latest code"),
                    WorkflowStep("extend.reload_all_extensions", {}, "Reload extensions"),
                    WorkflowStep("notify", {}, "Extensions reloaded after PR merge"),
                ],
                autonomy_override="GUIDED", created_by="system",
            ),
            Workflow(
                id="wf_heal_auto", name="Auto-merge safe healing PRs",
                trigger_type="signal", trigger_config={"signal_type": "self_heal_pr_created"},
                condition={"all": [
                    {"field": "context.confidence", "op": ">=", "value": 0.8},
                    {"field": "context.lines_changed", "op": "<", "value": 10},
                    {"field": "context.guardian_blocked", "op": "==", "value": False},
                ]},
                actions=[
                    WorkflowStep("shell", {"command": "gh pr merge {pr_url} --squash --auto"}, "Auto-merge healing PR"),
                    WorkflowStep("notify", {}, "Auto-merged healing PR"),
                ],
                autonomy_override="SUPERVISED", created_by="system",
            ),
            Workflow(
                id="wf_ext_gap", name="Auto-extend on repeated capability gaps",
                trigger_type="signal", trigger_config={"signal_type": "capability_gap_detected"},
                condition={"field": "context.same_topic_count", "op": ">=", "value": 3},
                actions=[
                    WorkflowStep("extend.generate_and_pr", {}, "Generate extension for capability gap"),
                ],
                autonomy_override="SUPERVISED", created_by="system",
            ),
        ]
        for wf in seeds:
            self.register(wf)
        log.info("Seeded %d default workflows", len(seeds))

    def register(self, workflow: Workflow):
        """Insert or update a workflow."""
        self._conn.execute(
            """INSERT OR REPLACE INTO workflows
               (id, name, trigger_type, trigger_config, condition, actions,
                autonomy_override, enabled, created_by, confidence, run_count,
                last_run_at, last_result, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            workflow.to_row(),
        )
        self._conn.commit()
        log.info("Registered workflow: %s (%s)", workflow.name, workflow.id)

    def unregister(self, workflow_id: str):
        """Disable a workflow."""
        self._conn.execute(
            "UPDATE workflows SET enabled = 0, updated_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), workflow_id),
        )
        self._conn.commit()
        log.info("Disabled workflow: %s", workflow_id)

    def list_workflows(self, enabled_only: bool = False) -> list[Workflow]:
        """List all workflows."""
        query = "SELECT * FROM workflows"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY created_at"
        rows = self._conn.execute(query).fetchall()
        return [Workflow.from_row(r) for r in rows]

    def get_workflow(self, workflow_id: str) -> Workflow | None:
        row = self._conn.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
        return Workflow.from_row(row) if row else None

    async def evaluate_trigger(self, event_type: str, event_data: dict):
        """Find and execute matching workflows for an event."""
        if not WORKFLOW_ENGINE_ENABLED:
            return

        rows = self._conn.execute(
            "SELECT * FROM workflows WHERE enabled = 1 AND trigger_type = ?",
            (event_type,),
        ).fetchall()

        for row in rows:
            wf = Workflow.from_row(row)
            try:
                await self._try_execute(wf, event_type, event_data)
            except Exception as e:
                log.error("Workflow %s failed: %s", wf.id, e)

    async def evaluate_signal(self, signal_type: str, context: dict | None):
        """Called by the signal hook — matches signal-triggered workflows."""
        if not WORKFLOW_ENGINE_ENABLED:
            return

        rows = self._conn.execute(
            "SELECT * FROM workflows WHERE enabled = 1 AND trigger_type = 'signal'",
        ).fetchall()

        event_data = {"signal_type": signal_type, "context": context or {}}

        for row in rows:
            wf = Workflow.from_row(row)
            # Check if this signal matches the workflow's trigger config
            expected_signal = wf.trigger_config.get("signal_type", "")
            if expected_signal != signal_type:
                continue
            try:
                await self._try_execute(wf, "signal", event_data)
            except Exception as e:
                log.error("Workflow %s failed on signal %s: %s", wf.id, signal_type, e)

    async def _try_execute(self, wf: Workflow, event_type: str, event_data: dict):
        """Evaluate condition and execute workflow if met."""
        # Re-entrancy guard
        if wf.id in _executing:
            log.debug("Workflow %s already executing, skipping", wf.id)
            return

        # Rate limit
        if not self._check_rate_limit(wf.id):
            log.warning("Workflow %s rate-limited (%d/hr max)", wf.id, WORKFLOW_MAX_RUNS_PER_HOUR)
            return

        # Condition check
        if not check_condition(wf.condition, event_data):
            self._record_run(wf.id, event_data, "skipped_condition")
            return

        # Execute
        _executing.add(wf.id)
        started = datetime.now(timezone.utc).isoformat()
        try:
            results = await self._execute_steps(wf, event_data)
            status = "completed" if all(r.get("ok") for r in results) else "failed"
            self._record_run(wf.id, event_data, status, results, started)
            self._update_workflow_state(wf.id, status, results)
        finally:
            _executing.discard(wf.id)

    async def _execute_steps(self, wf: Workflow, event_data: dict) -> list[dict]:
        """Execute workflow action steps sequentially."""
        results = []
        step_context = dict(event_data)  # Accumulate results for downstream steps

        for step in wf.actions:
            try:
                result = await self._execute_single_step(wf, step, step_context)
                results.append({"action": step.action, "ok": True, "result": str(result)[:500]})
                step_context[f"step_{len(results)}_result"] = result
            except Exception as e:
                log.error("Workflow %s step %s failed: %s", wf.id, step.action, e)
                results.append({"action": step.action, "ok": False, "error": str(e)})
                break  # Stop on first failure

        return results

    async def _execute_single_step(self, wf: Workflow, step: WorkflowStep, context: dict) -> Any:
        """Execute a single workflow step."""
        action = step.action
        params = dict(step.params)

        # Template substitution: replace {key} with context values
        for k, v in params.items():
            if isinstance(v, str) and "{" in v:
                for ctx_key, ctx_val in context.get("context", {}).items():
                    v = v.replace(f"{{{ctx_key}}}", str(ctx_val))
                params[k] = v

        # Special actions
        if action == "notify":
            msg = step.description or "Workflow notification"
            # Append last step result if available
            last_result = context.get(f"step_{len([k for k in context if k.startswith('step_')])}_result")
            if last_result:
                msg += f"\n\n{str(last_result)[:1000]}"
            if self._channel and self._chat_id:
                await self._channel.send_message(self._chat_id, f"⚡ [{wf.name}]\n{msg}")
            return "notified"

        if action == "llm_summarize":
            if self._ask_llm:
                text = params.get("text", str(context))
                return await self._ask_llm(f"Summarize this concisely:\n{text[:3000]}", "", "")
            return "no LLM configured"

        if action == "shell":
            import subprocess
            cmd = params.get("command", "")
            if not cmd:
                return "no command"
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
            return result.stdout.strip() or result.stderr.strip()

        # Module action: "module.function" pattern
        if "." in action:
            module_name, func_name = action.rsplit(".", 1)
            try:
                import importlib
                mod = importlib.import_module(f"actions.{module_name}")
                func = getattr(mod, func_name)
                if asyncio.iscoroutinefunction(func):
                    return await func(**params)
                return func(**params)
            except (ImportError, AttributeError) as e:
                raise RuntimeError(f"Cannot resolve {action}: {e}")

        # Fallback: try as action intent via execute_action_fn
        if self._execute_action:
            return await self._execute_action({"action": action, **params})

        raise RuntimeError(f"Unknown action: {action}")

    def _check_rate_limit(self, workflow_id: str) -> bool:
        """Check if workflow is within rate limit."""
        now = datetime.now(timezone.utc).timestamp()
        timestamps = _run_timestamps.setdefault(workflow_id, [])
        # Clean old entries
        cutoff = now - 3600
        _run_timestamps[workflow_id] = [t for t in timestamps if t > cutoff]
        return len(_run_timestamps[workflow_id]) < WORKFLOW_MAX_RUNS_PER_HOUR

    def _record_run(self, workflow_id: str, event_data: dict, status: str,
                    results: list[dict] | None = None, started_at: str | None = None):
        """Log workflow execution to DB."""
        now = datetime.now(timezone.utc)
        self._conn.execute(
            """INSERT INTO workflow_runs (workflow_id, trigger_event, steps_json, status, started_at, completed_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (workflow_id, json.dumps(event_data)[:2000], json.dumps(results) if results else None,
             status, started_at or now.isoformat(), now.isoformat()),
        )
        self._conn.commit()

        # Track for rate limiting
        _run_timestamps.setdefault(workflow_id, []).append(now.timestamp())

    def _update_workflow_state(self, workflow_id: str, status: str, results: list[dict]):
        """Update workflow run count and last result."""
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """UPDATE workflows SET run_count = run_count + 1, last_run_at = ?,
               last_result = ?, updated_at = ? WHERE id = ?""",
            (now, json.dumps(results)[:2000], now, workflow_id),
        )
        self._conn.commit()

        # Autonomy graduation: wf_heal_auto graduates from SUPERVISED to GUIDED after 3 successes
        if workflow_id == "wf_heal_auto" and status == "completed":
            wf = self.get_workflow(workflow_id)
            if wf and wf.run_count >= 3 and wf.autonomy_override == "SUPERVISED":
                self._conn.execute(
                    "UPDATE workflows SET autonomy_override = 'GUIDED', updated_at = ? WHERE id = ?",
                    (now, workflow_id),
                )
                self._conn.commit()
                log.info("Workflow %s graduated to GUIDED after %d successful runs", workflow_id, wf.run_count)

    def format_workflows_list(self) -> str:
        """Format workflows for display."""
        workflows = self.list_workflows()
        if not workflows:
            return "No workflows registered."

        lines = [f"⚡ Workflows ({len(workflows)}):\n"]
        for wf in workflows:
            status = "✅" if wf.enabled else "⏸"
            last = f" (last: {wf.last_run_at[:16]})" if wf.last_run_at else ""
            lines.append(f"  {status} {wf.name} [{wf.id}]")
            lines.append(f"     Trigger: {wf.trigger_type} | Runs: {wf.run_count}{last}")
        return "\n".join(lines)


# --- Module-level engine instance ---

_engine: WorkflowEngine | None = None


def get_engine() -> WorkflowEngine | None:
    return _engine


def init_engine(conn: sqlite3.Connection, channel=None, chat_id: int | None = None,
                ask_llm_fn=None, execute_action_fn=None) -> WorkflowEngine:
    """Initialize the global workflow engine."""
    global _engine
    _engine = WorkflowEngine(conn, channel, chat_id, ask_llm_fn, execute_action_fn)
    _engine.ensure_tables()
    return _engine
