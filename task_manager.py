"""Task state manager — persistent task tracking across messages.

Replaces the fragile _save_pending_task() hack with proper lifecycle:
created → active → blocked → completed → failed

Tasks persist in the DB. Follow-up messages inherit from active task.
Tasks reset after 3 failures to break poisoned context.
"""

import json
import logging
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from config import DB_PATH

log = logging.getLogger("khalil.task_manager")


@dataclass
class Task:
    id: str
    chat_id: str | int
    original_query: str
    task_type: str  # "artifact", "question", "multi_step", "background"
    status: str = "active"  # "active", "blocked", "completed", "failed"
    context_summary: str = ""
    tools_used: list[str] = field(default_factory=list)
    results: list[str] = field(default_factory=list)
    attempts: int = 0
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self):
        now = datetime.now(timezone.utc).isoformat()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_table():
    """Create the tasks table if it doesn't exist."""
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_tasks (
            id TEXT PRIMARY KEY,
            chat_id TEXT NOT NULL,
            original_query TEXT NOT NULL,
            task_type TEXT NOT NULL DEFAULT 'task',
            status TEXT NOT NULL DEFAULT 'active',
            context_summary TEXT DEFAULT '',
            tools_used TEXT DEFAULT '[]',
            results TEXT DEFAULT '[]',
            attempts INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


class TaskManager:
    """Manages task lifecycle across messages."""

    def __init__(self):
        ensure_table()

    def get_active_task(self, chat_id: str | int) -> Task | None:
        """Get the active or blocked task for a chat, if any."""
        conn = _get_conn()
        row = conn.execute(
            "SELECT * FROM agent_tasks WHERE chat_id = ? AND status IN ('active', 'blocked') "
            "ORDER BY updated_at DESC LIMIT 1",
            (str(chat_id),),
        ).fetchone()
        conn.close()
        if not row:
            return None
        return Task(
            id=row["id"],
            chat_id=row["chat_id"],
            original_query=row["original_query"],
            task_type=row["task_type"],
            status=row["status"],
            context_summary=row["context_summary"] or "",
            tools_used=json.loads(row["tools_used"]) if row["tools_used"] else [],
            results=json.loads(row["results"]) if row["results"] else [],
            attempts=row["attempts"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def create_task(self, chat_id: str | int, query: str, task_type: str = "task") -> Task:
        """Create a new active task. Completes any existing active task first."""
        # Complete any existing active task
        existing = self.get_active_task(chat_id)
        if existing:
            self.complete_task(existing.id, "Superseded by new task")

        task = Task(
            id=f"task_{uuid.uuid4().hex[:8]}",
            chat_id=str(chat_id),
            original_query=query[:500],
            task_type=task_type,
        )
        conn = _get_conn()
        conn.execute(
            "INSERT INTO agent_tasks (id, chat_id, original_query, task_type, status, "
            "context_summary, tools_used, results, attempts, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task.id, task.chat_id, task.original_query, task.task_type,
             task.status, task.context_summary, json.dumps(task.tools_used),
             json.dumps(task.results), task.attempts, task.created_at, task.updated_at),
        )
        conn.commit()
        conn.close()
        log.info("Task created: %s — %s", task.id, task.original_query[:60])
        return task

    def update_task(self, task_id: str, **kwargs):
        """Update task fields."""
        conn = _get_conn()
        kwargs["updated_at"] = datetime.now(timezone.utc).isoformat()

        # Serialize list fields
        for key in ("tools_used", "results"):
            if key in kwargs and isinstance(kwargs[key], list):
                kwargs[key] = json.dumps(kwargs[key])

        set_clause = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [task_id]
        conn.execute(f"UPDATE agent_tasks SET {set_clause} WHERE id = ?", values)
        conn.commit()
        conn.close()

    def record_tool_use(self, task_id: str, tool_name: str):
        """Record that a tool was used in this task."""
        conn = _get_conn()
        row = conn.execute("SELECT tools_used FROM agent_tasks WHERE id = ?", (task_id,)).fetchone()
        if row:
            tools = json.loads(row["tools_used"]) if row["tools_used"] else []
            if tool_name not in tools:
                tools.append(tool_name)
            conn.execute(
                "UPDATE agent_tasks SET tools_used = ?, updated_at = ? WHERE id = ?",
                (json.dumps(tools), datetime.now(timezone.utc).isoformat(), task_id),
            )
            conn.commit()
        conn.close()

    def record_attempt(self, task_id: str):
        """Increment attempt counter."""
        conn = _get_conn()
        conn.execute(
            "UPDATE agent_tasks SET attempts = attempts + 1, updated_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), task_id),
        )
        conn.commit()
        conn.close()

    def complete_task(self, task_id: str, result: str = ""):
        """Mark task as completed."""
        self.update_task(task_id, status="completed",
                         results=json.dumps([result[:500]] if result else []))
        log.info("Task completed: %s", task_id)

    def fail_task(self, task_id: str, reason: str = ""):
        """Mark task as failed."""
        self.update_task(task_id, status="failed",
                         results=json.dumps([f"Failed: {reason[:200]}"] if reason else []))
        log.info("Task failed: %s — %s", task_id, reason[:60])

    def should_reset(self, task: Task) -> bool:
        """Check if task should be reset (too many failures)."""
        return task.attempts >= 3 and task.status != "completed"

    def reset_task(self, task_id: str):
        """Reset task context after repeated failures."""
        self.update_task(
            task_id,
            status="blocked",
            context_summary="Previous approaches failed. Try a different strategy.",
            attempts=0,
        )
        log.info("Task reset: %s", task_id)

    def get_task_context_for_llm(self, task: Task) -> str:
        """Format task state for injection into LLM context."""
        parts = [f"[Active Task] {task.original_query}"]
        if task.context_summary:
            parts.append(f"[Task Context] {task.context_summary}")
        if task.tools_used:
            parts.append(f"[Tools Already Used] {', '.join(task.tools_used[-5:])}")
        if task.status == "blocked":
            parts.append("[Status] Previous approaches failed — try a different strategy.")
        if task.attempts > 0:
            parts.append(f"[Attempts] {task.attempts}")
        return "\n".join(parts)
