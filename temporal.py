"""M9: Temporal Reasoning Engine — handle time-dependent instructions.

Supports:
- datetime triggers: "remind me at 3pm"
- condition triggers: "notify me when PR #42 is merged"
- recurring triggers: "check daily until resolved"
- sequence triggers: "prepare for X next month" (multi-stage prep)

Persisted in SQLite, survives restarts. Agent loop checks temporal tasks each tick.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Any

from config import DB_PATH

log = logging.getLogger("khalil.temporal")


@dataclass
class TemporalTask:
    id: str
    description: str
    trigger_type: str  # "datetime", "condition", "recurring", "sequence"
    trigger_config: dict  # type-specific config
    action: str  # execution bus action to run when triggered
    params: dict  # params for the action
    status: str = "waiting"  # "waiting", "active", "completed", "expired", "failed"
    check_count: int = 0
    max_checks: int = 100  # safety limit
    created_at: str = ""
    last_checked_at: str | None = None
    completed_at: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> TemporalTask:
        return cls(
            id=d["id"],
            description=d["description"],
            trigger_type=d["trigger_type"],
            trigger_config=d.get("trigger_config", {}),
            action=d.get("action", ""),
            params=d.get("params", {}),
            status=d.get("status", "waiting"),
            check_count=d.get("check_count", 0),
            max_checks=d.get("max_checks", 100),
            created_at=d.get("created_at", ""),
            last_checked_at=d.get("last_checked_at"),
            completed_at=d.get("completed_at"),
        )


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def ensure_table():
    """Create temporal_tasks table if not exists."""
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS temporal_tasks (
            id TEXT PRIMARY KEY,
            description TEXT NOT NULL,
            trigger_type TEXT NOT NULL,
            trigger_config TEXT NOT NULL DEFAULT '{}',
            action TEXT NOT NULL DEFAULT '',
            params TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'waiting',
            check_count INTEGER NOT NULL DEFAULT 0,
            max_checks INTEGER NOT NULL DEFAULT 100,
            created_at TEXT NOT NULL,
            last_checked_at TEXT,
            completed_at TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_temporal_status ON temporal_tasks(status)")
    conn.commit()
    conn.close()


def create_temporal_task(
    description: str,
    trigger_type: str,
    trigger_config: dict,
    action: str = "",
    params: dict | None = None,
    max_checks: int = 100,
) -> TemporalTask:
    """Create and persist a new temporal task."""
    ensure_table()
    task = TemporalTask(
        id=f"temporal_{uuid.uuid4().hex[:8]}",
        description=description,
        trigger_type=trigger_type,
        trigger_config=trigger_config,
        action=action,
        params=params or {},
        created_at=datetime.now(timezone.utc).isoformat(),
        max_checks=max_checks,
    )
    _save_task(task)
    log.info("Created temporal task: %s (%s)", task.id, trigger_type)
    return task


def _save_task(task: TemporalTask):
    conn = _get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO temporal_tasks
           (id, description, trigger_type, trigger_config, action, params,
            status, check_count, max_checks, created_at, last_checked_at, completed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (task.id, task.description, task.trigger_type,
         json.dumps(task.trigger_config), task.action, json.dumps(task.params),
         task.status, task.check_count, task.max_checks,
         task.created_at, task.last_checked_at, task.completed_at),
    )
    conn.commit()
    conn.close()


def get_active_tasks() -> list[TemporalTask]:
    """Get all waiting/active temporal tasks."""
    ensure_table()
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, description, trigger_type, trigger_config, action, params, "
        "status, check_count, max_checks, created_at, last_checked_at, completed_at "
        "FROM temporal_tasks WHERE status IN ('waiting', 'active') "
        "ORDER BY created_at"
    ).fetchall()
    conn.close()
    tasks = []
    for r in rows:
        tasks.append(TemporalTask(
            id=r[0], description=r[1], trigger_type=r[2],
            trigger_config=json.loads(r[3]), action=r[4], params=json.loads(r[5]),
            status=r[6], check_count=r[7], max_checks=r[8],
            created_at=r[9], last_checked_at=r[10], completed_at=r[11],
        ))
    return tasks


def list_all_tasks(limit: int = 20) -> list[TemporalTask]:
    """List all temporal tasks (including completed/expired)."""
    ensure_table()
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, description, trigger_type, trigger_config, action, params, "
        "status, check_count, max_checks, created_at, last_checked_at, completed_at "
        "FROM temporal_tasks ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [
        TemporalTask(
            id=r[0], description=r[1], trigger_type=r[2],
            trigger_config=json.loads(r[3]), action=r[4], params=json.loads(r[5]),
            status=r[6], check_count=r[7], max_checks=r[8],
            created_at=r[9], last_checked_at=r[10], completed_at=r[11],
        )
        for r in rows
    ]


async def check_temporal_tasks(ask_llm_fn=None) -> list[tuple[TemporalTask, str]]:
    """Check all active temporal tasks and return triggered ones.

    Returns list of (task, trigger_reason) for tasks that should fire.
    Called by agent loop each tick.
    """
    tasks = get_active_tasks()
    triggered: list[tuple[TemporalTask, str]] = []
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    for task in tasks:
        # Safety: expire tasks that exceeded max checks
        if task.check_count >= task.max_checks:
            task.status = "expired"
            task.completed_at = now_iso
            _save_task(task)
            log.info("Temporal task expired (max checks): %s", task.id)
            continue

        task.check_count += 1
        task.last_checked_at = now_iso

        reason = await _evaluate_trigger(task, now, ask_llm_fn)
        if reason:
            triggered.append((task, reason))
            task.status = "active"
        _save_task(task)

    return triggered


async def _evaluate_trigger(
    task: TemporalTask, now: datetime, ask_llm_fn=None,
) -> str | None:
    """Evaluate if a task's trigger condition is met. Returns reason string or None."""
    config = task.trigger_config

    if task.trigger_type == "datetime":
        trigger_at = config.get("at", "")
        if trigger_at:
            try:
                trigger_dt = datetime.fromisoformat(trigger_at)
                if now >= trigger_dt:
                    return f"Time reached: {trigger_at}"
            except ValueError:
                pass
        return None

    if task.trigger_type == "recurring":
        interval_hours = config.get("interval_hours", 24)
        last = task.last_checked_at
        if not last:
            return "First check"
        try:
            last_dt = datetime.fromisoformat(last)
            if (now - last_dt).total_seconds() >= interval_hours * 3600:
                return f"Recurring: {interval_hours}h interval"
        except ValueError:
            pass
        return None

    if task.trigger_type == "condition":
        # LLM evaluates the condition
        condition_text = config.get("condition", "")
        if not condition_text or not ask_llm_fn:
            return None
        try:
            response = await ask_llm_fn(
                f"Evaluate this condition and respond with ONLY 'true' or 'false':\n{condition_text}",
                "", "Respond with exactly one word: true or false.",
            )
            if response.strip().lower().startswith("true"):
                return f"Condition met: {condition_text[:80]}"
        except Exception as e:
            log.debug("Condition evaluation failed for %s: %s", task.id, e)
        return None

    if task.trigger_type == "sequence":
        # Sequence: check if current stage is due
        stages = config.get("stages", [])
        current_stage = config.get("current_stage", 0)
        if current_stage >= len(stages):
            return None
        stage = stages[current_stage]
        stage_date = stage.get("date", "")
        if stage_date:
            try:
                stage_dt = datetime.fromisoformat(stage_date)
                if now >= stage_dt:
                    return f"Stage {current_stage + 1}/{len(stages)}: {stage.get('description', '')}"
            except ValueError:
                pass
        return None

    return None


def complete_task(task_id: str):
    """Mark a temporal task as completed."""
    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE temporal_tasks SET status = 'completed', completed_at = ? WHERE id = ?",
        (now, task_id),
    )
    conn.commit()
    conn.close()


def advance_sequence(task_id: str):
    """Advance a sequence task to the next stage."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT trigger_config FROM temporal_tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row:
        config = json.loads(row[0])
        current = config.get("current_stage", 0)
        config["current_stage"] = current + 1
        conn.execute(
            "UPDATE temporal_tasks SET trigger_config = ? WHERE id = ?",
            (json.dumps(config), task_id),
        )
        conn.commit()
    conn.close()


def format_task_summary(task: TemporalTask) -> str:
    """Format a temporal task for display."""
    status_icons = {
        "waiting": "⏳", "active": "🔄", "completed": "✅",
        "expired": "💤", "failed": "❌",
    }
    icon = status_icons.get(task.status, "❓")
    line = f"{icon} [{task.trigger_type}] {task.description}"
    if task.trigger_type == "recurring":
        line += f" (every {task.trigger_config.get('interval_hours', '?')}h, checked {task.check_count}x)"
    elif task.trigger_type == "condition":
        line += f" (condition: {task.trigger_config.get('condition', '?')[:60]})"
    return line
