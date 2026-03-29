"""Reactive workflow engine — trigger → condition → action chains with autonomy."""

import asyncio
import glob as _glob
import json
import logging
import os
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from config import DB_PATH, WORKFLOW_ENGINE_ENABLED, WORKFLOW_MAX_RUNS_PER_HOUR

log = logging.getLogger("pharoclaw.workflows")

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


# --- Workflow Evolver — pattern detection and workflow proposal ---


class WorkflowEvolver:
    """Detects patterns in interaction signals and proposes new workflows."""

    def __init__(self, conn: sqlite3.Connection, engine: WorkflowEngine,
                 ask_llm_fn, channel, chat_id: int | None):
        self._conn = conn
        self._engine = engine
        self._ask_llm = ask_llm_fn
        self._channel = channel
        self._chat_id = chat_id

    def detect_temporal_patterns(self, days: int = 14) -> list[dict]:
        """Find signals that repeat at similar times on multiple days.

        Groups by (signal_type, hour, day_of_week) and returns patterns where
        the same signal occurs 3+ times within a 30-minute window over 5+ distinct days.
        """
        cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        rows = self._conn.execute(
            """SELECT signal_type,
                      strftime('%%H', created_at) AS hour,
                      strftime('%%M', created_at) AS minute,
                      strftime('%%w', created_at) AS dow,
                      date(created_at) AS day,
                      created_at
               FROM interaction_signals
               WHERE created_at > ?
               ORDER BY created_at""",
            (cutoff,),
        ).fetchall()

        # Group by (signal_type, hour) and cluster within 30-min windows
        from collections import defaultdict
        buckets: dict[tuple[str, int], list[dict]] = defaultdict(list)
        for r in rows:
            sig = r["signal_type"]
            hour = int(r["hour"])
            minute = int(r["minute"])
            buckets[(sig, hour)].append({
                "minute": minute, "dow": r["dow"], "day": r["day"],
            })

        patterns = []
        for (sig, hour), entries in buckets.items():
            # Cluster entries within 30-minute windows
            # Use two windows: :00-:29 and :30-:59
            for window_start in (0, 30):
                window_entries = [e for e in entries if window_start <= e["minute"] < window_start + 30]
                if len(window_entries) < 3:
                    continue
                distinct_days = set(e["day"] for e in window_entries)
                if len(distinct_days) < 5:
                    continue
                dow_counts: dict[str, int] = defaultdict(int)
                for e in window_entries:
                    dow_counts[e["dow"]] += 1
                day_names = {
                    "0": "Sun", "1": "Mon", "2": "Tue", "3": "Wed",
                    "4": "Thu", "5": "Fri", "6": "Sat",
                }
                top_days = sorted(dow_counts.items(), key=lambda x: -x[1])[:3]
                day_pattern = ", ".join(f"{day_names.get(d, d)}({c})" for d, c in top_days)

                patterns.append({
                    "signal_type": sig,
                    "hour": hour,
                    "day_pattern": day_pattern,
                    "count": len(window_entries),
                    "evidence": (
                        f"Signal '{sig}' occurs {len(window_entries)} times around "
                        f"{hour}:{window_start:02d}-{hour}:{window_start+29:02d} "
                        f"across {len(distinct_days)} days. Top days: {day_pattern}"
                    ),
                })

        return patterns

    def detect_correlation_patterns(self, days: int = 14) -> list[dict]:
        """Find signal A followed by signal B within 10 minutes, occurring 3+ times."""
        cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        rows = self._conn.execute(
            """SELECT signal_type, created_at
               FROM interaction_signals
               WHERE created_at > ?
               ORDER BY created_at""",
            (cutoff,),
        ).fetchall()

        if len(rows) < 2:
            return []

        # Find sequential pairs within 10-minute window
        from collections import defaultdict
        pair_gaps: dict[tuple[str, str], list[float]] = defaultdict(list)

        for i in range(len(rows) - 1):
            a_type = rows[i]["signal_type"]
            a_time = datetime.strptime(rows[i]["created_at"], "%Y-%m-%d %H:%M:%S")
            for j in range(i + 1, min(i + 20, len(rows))):  # Look ahead up to 20 rows
                b_type = rows[j]["signal_type"]
                b_time = datetime.strptime(rows[j]["created_at"], "%Y-%m-%d %H:%M:%S")
                gap = (b_time - a_time).total_seconds() / 60.0
                if gap > 10:
                    break
                if a_type != b_type:
                    pair_gaps[(a_type, b_type)].append(gap)

        patterns = []
        for (a, b), gaps in pair_gaps.items():
            if len(gaps) >= 3:
                avg_gap = sum(gaps) / len(gaps)
                patterns.append({
                    "signal_a": a,
                    "signal_b": b,
                    "count": len(gaps),
                    "avg_gap_minutes": round(avg_gap, 1),
                })

        return patterns

    def detect_failure_escalation_patterns(self, days: int = 14) -> list[dict]:
        """Find proactive alerts followed by user actions within 30 minutes.

        Suggests automating the user's response to reduce manual intervention.
        """
        cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

        # Proactive alerts are signals with "alert" or "proactive" in the type
        alerts = self._conn.execute(
            """SELECT signal_type, created_at
               FROM interaction_signals
               WHERE created_at > ?
                 AND (signal_type LIKE '%%alert%%' OR signal_type LIKE '%%proactive%%'
                      OR signal_type LIKE '%%monitor%%' OR signal_type LIKE '%%threshold%%')
               ORDER BY created_at""",
            (cutoff,),
        ).fetchall()

        if not alerts:
            return []

        # User actions are signals with "user_" prefix or action-like types
        user_actions = self._conn.execute(
            """SELECT signal_type, created_at
               FROM interaction_signals
               WHERE created_at > ?
                 AND (signal_type LIKE 'user_%%' OR signal_type LIKE '%%_action'
                      OR signal_type LIKE '%%_executed' OR signal_type LIKE '%%_command')
               ORDER BY created_at""",
            (cutoff,),
        ).fetchall()

        if not user_actions:
            return []

        from collections import defaultdict
        escalations: dict[tuple[str, str], int] = defaultdict(int)

        for alert in alerts:
            alert_time = datetime.strptime(alert["created_at"], "%Y-%m-%d %H:%M:%S")
            for ua in user_actions:
                ua_time = datetime.strptime(ua["created_at"], "%Y-%m-%d %H:%M:%S")
                gap = (ua_time - alert_time).total_seconds() / 60.0
                if gap < 0:
                    continue
                if gap > 30:
                    break
                escalations[(alert["signal_type"], ua["signal_type"])] += 1

        patterns = []
        for (alert_type, response_action), count in escalations.items():
            if count >= 3:
                patterns.append({
                    "alert_type": alert_type,
                    "response_action": response_action,
                    "count": count,
                })

        return patterns

    async def propose_workflow(self, pattern: dict) -> Workflow | None:
        """Use the LLM to generate a workflow definition from a detected pattern."""
        # Discover available action modules
        actions_dir = os.path.join(os.path.dirname(__file__), "actions")
        action_files = [
            os.path.basename(f).replace(".py", "")
            for f in _glob.glob(os.path.join(actions_dir, "*.py"))
            if not os.path.basename(f).startswith("_")
        ]

        prompt = f"""Analyze this detected pattern and propose a reactive workflow.

Pattern evidence:
{json.dumps(pattern, indent=2)}

Available action modules (use as "module.function_name"):
{', '.join(sorted(action_files))}

Special actions: "notify" (send Telegram message), "shell" (run command), "llm_summarize"

Respond with a single JSON object following this schema:
{{
  "name": "Human-readable workflow name",
  "trigger_type": "signal" | "cron" | "threshold",
  "trigger_config": {{"signal_type": "..."}} or {{"hour": "*/N"}} or {{"metric": "..."}},
  "condition": {{"field": "...", "op": "==|>|<|contains|present|absent", "value": "..."}} or null,
  "actions": [
    {{"action": "module.function or notify or shell", "params": {{}}, "description": "What this step does"}}
  ]
}}

Rules:
- Be conservative. Default to SUPERVISED autonomy.
- Prefer READ-only actions (get_, list_, check_) over write actions.
- Include a "notify" step at the end so the user sees results.
- Keep workflows to 2-3 steps maximum.
- Respond with ONLY the JSON object, no markdown."""

        try:
            response = await self._ask_llm(
                prompt, "",
                system_extra="You are designing a workflow automation. Respond with ONLY valid JSON.",
            )
            response = response.strip()
            # Handle markdown code blocks
            if "```" in response:
                response = response.split("```")[1]
                if response.startswith("json"):
                    response = response[4:]
                response = response.strip()

            wf_data = json.loads(response)
        except (json.JSONDecodeError, Exception) as e:
            log.error("Failed to parse workflow proposal from LLM: %s", e)
            return None

        wf_id = f"wf_evolved_{uuid.uuid4().hex[:8]}"
        actions = [
            WorkflowStep(
                action=a.get("action", "notify"),
                params=a.get("params", {}),
                description=a.get("description", ""),
            )
            for a in wf_data.get("actions", [])
        ]

        if not actions:
            return None

        condition = wf_data.get("condition")
        return Workflow(
            id=wf_id,
            name=wf_data.get("name", "Evolved workflow"),
            trigger_type=wf_data.get("trigger_type", "signal"),
            trigger_config=wf_data.get("trigger_config", {}),
            actions=actions,
            condition=condition,
            autonomy_override="SUPERVISED",
            enabled=False,  # Not enabled until approved
            created_by="evolved",
            confidence=0.5,
        )

    async def present_proposal(self, workflow: Workflow, evidence: str) -> None:
        """Send a workflow proposal to Telegram with approve/dismiss buttons."""
        if not self._channel or not self._chat_id:
            log.warning("Cannot present proposal — no channel configured")
            return

        step_descriptions = "\n".join(
            f"  {i+1}. {s.description or s.action}" for i, s in enumerate(workflow.actions)
        )
        msg = (
            f"\U0001f504 Workflow Proposal: {workflow.name}\n"
            f"{evidence}\n"
            f"Actions:\n{step_descriptions}\n"
            f"Autonomy: SUPERVISED"
        )

        from channels import ActionButton
        buttons = [[
            ActionButton("\u2705 Approve", f"wf_approve:{workflow.id}"),
            ActionButton("\u274c Dismiss", f"wf_dismiss:{workflow.id}"),
        ]]

        await self._channel.send_message(self._chat_id, msg, buttons=buttons)

    async def run_evolution_cycle(self, ask_llm_fn) -> list[dict]:
        """Main entry point. Detect patterns, filter, propose, present.

        Returns list of proposed workflow summaries.
        """
        # Run all three detectors
        temporal = self.detect_temporal_patterns()
        correlation = self.detect_correlation_patterns()
        escalation = self.detect_failure_escalation_patterns()

        all_patterns = []
        for p in temporal:
            p["detector"] = "temporal"
            all_patterns.append(p)
        for p in correlation:
            p["detector"] = "correlation"
            all_patterns.append(p)
        for p in escalation:
            p["detector"] = "escalation"
            all_patterns.append(p)

        if not all_patterns:
            log.info("Workflow evolution: no patterns detected")
            return []

        # Filter out already-dismissed patterns
        dismissed = self._conn.execute(
            """SELECT context FROM interaction_signals
               WHERE signal_type = 'workflow_proposal_dismissed'"""
        ).fetchall()
        dismissed_keys = set()
        for row in dismissed:
            ctx = json.loads(row["context"]) if row["context"] else {}
            # Build a key from the pattern that was dismissed
            dismissed_keys.add(ctx.get("pattern_key", ""))

        proposals = []
        for pattern in all_patterns:
            # Build a stable key for dedup
            if pattern["detector"] == "temporal":
                key = f"temporal:{pattern['signal_type']}:{pattern['hour']}"
            elif pattern["detector"] == "correlation":
                key = f"correlation:{pattern['signal_a']}:{pattern['signal_b']}"
            else:
                key = f"escalation:{pattern['alert_type']}:{pattern['response_action']}"

            if key in dismissed_keys:
                log.debug("Skipping dismissed pattern: %s", key)
                continue

            # Generate proposal
            wf = await self.propose_workflow(pattern)
            if not wf:
                continue

            # Store the workflow (disabled) so it can be approved later
            self._engine.register(wf)

            # Build evidence string
            evidence = pattern.get("evidence", json.dumps(pattern, indent=2))

            # Present to user
            await self.present_proposal(wf, evidence)

            proposals.append({
                "workflow_id": wf.id,
                "name": wf.name,
                "pattern_key": key,
                "detector": pattern["detector"],
            })

        log.info("Workflow evolution: %d patterns found, %d proposals generated",
                 len(all_patterns), len(proposals))
        return proposals


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
