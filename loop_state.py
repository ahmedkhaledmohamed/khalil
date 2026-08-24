"""Durable state and recovery classification for foreground tool loops."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from config import ActionType
from loop_controller import LoopBudget, LoopTerminationReason


class ToolLoopStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class LoopCheckpointBoundary(str, Enum):
    CREATED = "created"
    BEFORE_MODEL = "before_model"
    AFTER_MODEL = "after_model"
    BEFORE_ACTIONS = "before_actions"
    AFTER_ACTIONS = "after_actions"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    BEFORE_SYNTHESIS = "before_synthesis"
    TERMINAL = "terminal"


class RecoveryDisposition(str, Enum):
    RESUME = "resume"
    WAIT_FOR_APPROVAL = "wait_for_approval"
    REVIEW_REQUIRED = "review_required"
    TERMINAL = "terminal"


_TERMINAL_STATUSES = {
    ToolLoopStatus.SUCCEEDED,
    ToolLoopStatus.FAILED,
    ToolLoopStatus.CANCELLED,
}

_STATUS_TRANSITIONS = {
    ToolLoopStatus.PENDING: {
        ToolLoopStatus.RUNNING,
        ToolLoopStatus.CANCELLED,
    },
    ToolLoopStatus.RUNNING: {
        ToolLoopStatus.WAITING_FOR_APPROVAL,
        ToolLoopStatus.SUCCEEDED,
        ToolLoopStatus.FAILED,
        ToolLoopStatus.CANCELLED,
    },
    ToolLoopStatus.WAITING_FOR_APPROVAL: {
        ToolLoopStatus.RUNNING,
        ToolLoopStatus.CANCELLED,
    },
    ToolLoopStatus.SUCCEEDED: set(),
    ToolLoopStatus.FAILED: set(),
    ToolLoopStatus.CANCELLED: set(),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class PendingToolAction:
    id: str
    name: str
    arguments: str
    action_type: ActionType = ActionType.READ
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "action_type", ActionType(self.action_type))
        if not self.id:
            raise ValueError("Pending tool action id cannot be empty")
        if not self.name:
            raise ValueError("Pending tool action name cannot be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "arguments": self.arguments,
            "action_type": self.action_type.value,
            "idempotency_key": self.idempotency_key,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PendingToolAction":
        return cls(
            id=str(value["id"]),
            name=str(value["name"]),
            arguments=str(value.get("arguments") or ""),
            action_type=ActionType(value.get("action_type", ActionType.READ.value)),
            idempotency_key=(
                str(value["idempotency_key"])
                if value.get("idempotency_key") is not None else None
            ),
        )


@dataclass(frozen=True)
class ToolLoopCheckpoint:
    boundary: LoopCheckpointBoundary
    messages: list[dict[str, Any]] = field(default_factory=list)
    pending_actions: list[PendingToolAction] = field(default_factory=list)
    iteration_count: int = 0
    action_count: int = 0
    no_progress_count: int = 0
    elapsed_seconds: float = 0.0
    phase: dict[str, Any] = field(default_factory=dict)
    progress_fingerprint: str | None = None
    sequence: int | None = None
    created_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "boundary", LoopCheckpointBoundary(self.boundary))
        object.__setattr__(
            self,
            "pending_actions",
            [
                action if isinstance(action, PendingToolAction)
                else PendingToolAction.from_dict(action)
                for action in self.pending_actions
            ],
        )
        if min(self.iteration_count, self.action_count, self.no_progress_count) < 0:
            raise ValueError("Loop checkpoint counters cannot be negative")
        if self.elapsed_seconds < 0:
            raise ValueError("Loop checkpoint elapsed time cannot be negative")
        if self.boundary is LoopCheckpointBoundary.BEFORE_ACTIONS and not self.pending_actions:
            raise ValueError("Before-actions checkpoints require pending actions")


@dataclass
class ToolLoopRun:
    chat_id: int | str
    query: str
    model: str
    budget: LoopBudget
    id: str = field(default_factory=lambda: f"tool_loop_{uuid.uuid4().hex}")
    status: ToolLoopStatus = ToolLoopStatus.PENDING
    termination_reason: LoopTerminationReason | None = None
    latest_checkpoint: ToolLoopCheckpoint | None = None
    created_at: str | None = None
    updated_at: str | None = None
    completed_at: str | None = None

    def __post_init__(self) -> None:
        self.status = ToolLoopStatus(self.status)
        if self.termination_reason is not None:
            self.termination_reason = LoopTerminationReason(self.termination_reason)
        if not self.id:
            raise ValueError("Tool loop run id cannot be empty")
        if not str(self.chat_id):
            raise ValueError("Tool loop chat id cannot be empty")
        if not self.query:
            raise ValueError("Tool loop query cannot be empty")
        if not self.model:
            raise ValueError("Tool loop model cannot be empty")


@dataclass(frozen=True)
class RecoveryClassification:
    disposition: RecoveryDisposition
    reason: str


def classify_recovery(run: ToolLoopRun) -> RecoveryClassification:
    """Classify the safest next step from the latest durable boundary."""
    if run.status in _TERMINAL_STATUSES:
        return RecoveryClassification(
            RecoveryDisposition.TERMINAL,
            f"Run already ended with status {run.status.value}",
        )

    checkpoint = run.latest_checkpoint
    if run.status is ToolLoopStatus.WAITING_FOR_APPROVAL or (
        checkpoint
        and checkpoint.boundary is LoopCheckpointBoundary.WAITING_FOR_APPROVAL
    ):
        return RecoveryClassification(
            RecoveryDisposition.WAIT_FOR_APPROVAL,
            "Run is paused at an approval boundary",
        )

    if checkpoint and checkpoint.boundary in {
        LoopCheckpointBoundary.AFTER_MODEL,
        LoopCheckpointBoundary.BEFORE_ACTIONS,
    }:
        unsafe = [
            action.name
            for action in checkpoint.pending_actions
            if action.action_type is not ActionType.READ
        ]
        if unsafe:
            if checkpoint.boundary is LoopCheckpointBoundary.BEFORE_ACTIONS:
                reason = "Interrupted action outcome may be ambiguous: "
            else:
                reason = "Interrupted write requires confirmation before recovery: "
            return RecoveryClassification(
                RecoveryDisposition.REVIEW_REQUIRED,
                reason + ", ".join(unsafe),
            )

    return RecoveryClassification(
        RecoveryDisposition.RESUME,
        "Latest checkpoint is safe to resume",
    )


class ToolLoopRepository:
    """Persist loop runs and append-only checkpoints in SQLite."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.execute("PRAGMA foreign_keys=ON")

    def ensure_schema(self) -> None:
        with self.conn:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS tool_loop_runs (
                    id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    query TEXT NOT NULL,
                    model TEXT NOT NULL,
                    status TEXT NOT NULL,
                    budget_json TEXT NOT NULL,
                    termination_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS tool_loop_checkpoints (
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    boundary TEXT NOT NULL,
                    messages_json TEXT NOT NULL DEFAULT '[]',
                    pending_actions_json TEXT NOT NULL DEFAULT '[]',
                    iteration_count INTEGER NOT NULL DEFAULT 0,
                    action_count INTEGER NOT NULL DEFAULT 0,
                    no_progress_count INTEGER NOT NULL DEFAULT 0,
                    elapsed_seconds REAL NOT NULL DEFAULT 0,
                    phase_json TEXT NOT NULL DEFAULT '{}',
                    progress_fingerprint TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, sequence),
                    FOREIGN KEY (run_id) REFERENCES tool_loop_runs(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_tool_loop_runs_status
                    ON tool_loop_runs(status, updated_at);
            """)

    def create_run(self, run: ToolLoopRun) -> ToolLoopRun:
        if run.status is not ToolLoopStatus.PENDING:
            raise ValueError("New tool loop runs must start pending")
        if run.termination_reason is not None or run.completed_at is not None:
            raise ValueError("New tool loop runs cannot already be terminated")
        now = _utc_now()
        run.created_at = run.created_at or now
        run.updated_at = run.updated_at or run.created_at
        checkpoint = ToolLoopCheckpoint(
            boundary=LoopCheckpointBoundary.CREATED,
            created_at=now,
            sequence=1,
        )
        with self.conn:
            self.conn.execute(
                """INSERT INTO tool_loop_runs
                   (id, chat_id, query, model, status, budget_json,
                    termination_reason, created_at, updated_at, completed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run.id,
                    str(run.chat_id),
                    run.query,
                    run.model,
                    run.status.value,
                    _json(self._budget_to_dict(run.budget)),
                    None,
                    run.created_at,
                    run.updated_at,
                    None,
                ),
            )
            self._insert_checkpoint(run.id, checkpoint)
        created = self.load_run(run.id)
        assert created is not None
        return created

    def append_checkpoint(
        self,
        run_id: str,
        checkpoint: ToolLoopCheckpoint,
        *,
        status: ToolLoopStatus | None = None,
        termination_reason: LoopTerminationReason | None = None,
    ) -> ToolLoopRun:
        reason = (
            LoopTerminationReason(termination_reason)
            if termination_reason is not None else None
        )
        now = _utc_now()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            run = self.load_run(run_id)
            if run is None:
                raise KeyError(f"Unknown tool loop run {run_id!r}")
            if run.status in _TERMINAL_STATUSES:
                raise ValueError("Cannot checkpoint a terminal tool loop run")

            target = ToolLoopStatus(status) if status is not None else run.status
            if target is not run.status and target not in _STATUS_TRANSITIONS[run.status]:
                raise ValueError(
                    f"Cannot transition tool loop from {run.status.value} to {target.value}"
                )
            self._validate_checkpoint_transition(checkpoint, target, reason)
            completed_at = now if target in _TERMINAL_STATUSES else None

            row = self.conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM tool_loop_checkpoints WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            sequence = int(row[0]) + 1
            persisted = ToolLoopCheckpoint(
                boundary=checkpoint.boundary,
                messages=checkpoint.messages,
                pending_actions=checkpoint.pending_actions,
                iteration_count=checkpoint.iteration_count,
                action_count=checkpoint.action_count,
                no_progress_count=checkpoint.no_progress_count,
                elapsed_seconds=checkpoint.elapsed_seconds,
                phase=checkpoint.phase,
                progress_fingerprint=checkpoint.progress_fingerprint,
                sequence=sequence,
                created_at=checkpoint.created_at or now,
            )
            self._insert_checkpoint(run_id, persisted)
            self.conn.execute(
                """UPDATE tool_loop_runs
                   SET status = ?, termination_reason = ?, updated_at = ?, completed_at = ?
                   WHERE id = ?""",
                (
                    target.value,
                    reason.value if reason else None,
                    now,
                    completed_at,
                    run_id,
                ),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        updated = self.load_run(run_id)
        assert updated is not None
        return updated

    def load_run(self, run_id: str) -> ToolLoopRun | None:
        row = self.conn.execute(
            """SELECT id, chat_id, query, model, status, budget_json,
                      termination_reason, created_at, updated_at, completed_at
               FROM tool_loop_runs WHERE id = ?""",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        checkpoint = self._load_latest_checkpoint(run_id)
        return ToolLoopRun(
            id=row[0],
            chat_id=row[1],
            query=row[2],
            model=row[3],
            status=ToolLoopStatus(row[4]),
            budget=LoopBudget(**json.loads(row[5])),
            termination_reason=(
                LoopTerminationReason(row[6]) if row[6] is not None else None
            ),
            created_at=row[7],
            updated_at=row[8],
            completed_at=row[9],
            latest_checkpoint=checkpoint,
        )

    def list_recoverable_runs(self, limit: int = 100) -> list[ToolLoopRun]:
        rows = self.conn.execute(
            """SELECT id FROM tool_loop_runs
               WHERE status IN (?, ?, ?)
               ORDER BY updated_at ASC LIMIT ?""",
            (
                ToolLoopStatus.PENDING.value,
                ToolLoopStatus.RUNNING.value,
                ToolLoopStatus.WAITING_FOR_APPROVAL.value,
                limit,
            ),
        ).fetchall()
        return [run for row in rows if (run := self.load_run(row[0])) is not None]

    def _insert_checkpoint(self, run_id: str, checkpoint: ToolLoopCheckpoint) -> None:
        self.conn.execute(
            """INSERT INTO tool_loop_checkpoints
               (run_id, sequence, boundary, messages_json, pending_actions_json,
                iteration_count, action_count, no_progress_count, elapsed_seconds,
                phase_json, progress_fingerprint, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                checkpoint.sequence,
                checkpoint.boundary.value,
                _json(checkpoint.messages),
                _json([action.to_dict() for action in checkpoint.pending_actions]),
                checkpoint.iteration_count,
                checkpoint.action_count,
                checkpoint.no_progress_count,
                checkpoint.elapsed_seconds,
                _json(checkpoint.phase),
                checkpoint.progress_fingerprint,
                checkpoint.created_at,
            ),
        )

    def _load_latest_checkpoint(self, run_id: str) -> ToolLoopCheckpoint | None:
        row = self.conn.execute(
            """SELECT sequence, boundary, messages_json, pending_actions_json,
                      iteration_count, action_count, no_progress_count,
                      elapsed_seconds, phase_json, progress_fingerprint, created_at
               FROM tool_loop_checkpoints WHERE run_id = ?
               ORDER BY sequence DESC LIMIT 1""",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return ToolLoopCheckpoint(
            sequence=row[0],
            boundary=LoopCheckpointBoundary(row[1]),
            messages=json.loads(row[2]),
            pending_actions=[
                PendingToolAction.from_dict(value) for value in json.loads(row[3])
            ],
            iteration_count=row[4],
            action_count=row[5],
            no_progress_count=row[6],
            elapsed_seconds=row[7],
            phase=json.loads(row[8]),
            progress_fingerprint=row[9],
            created_at=row[10],
        )

    @staticmethod
    def _budget_to_dict(budget: LoopBudget) -> dict[str, Any]:
        return {
            "max_iterations": budget.max_iterations,
            "max_actions": budget.max_actions,
            "max_elapsed_seconds": budget.max_elapsed_seconds,
            "max_no_progress_iterations": budget.max_no_progress_iterations,
        }

    @staticmethod
    def _validate_checkpoint_transition(
        checkpoint: ToolLoopCheckpoint,
        status: ToolLoopStatus,
        termination_reason: LoopTerminationReason | None,
    ) -> None:
        if checkpoint.boundary is LoopCheckpointBoundary.TERMINAL:
            if status not in _TERMINAL_STATUSES or termination_reason is None:
                raise ValueError(
                    "Terminal checkpoints require terminal status and termination reason"
                )
            return
        if status in _TERMINAL_STATUSES or termination_reason is not None:
            raise ValueError("Terminal state requires a terminal checkpoint")
        if (
            checkpoint.boundary is LoopCheckpointBoundary.WAITING_FOR_APPROVAL
        ) != (status is ToolLoopStatus.WAITING_FOR_APPROVAL):
            raise ValueError(
                "Approval status and checkpoint boundary must be persisted together"
            )
