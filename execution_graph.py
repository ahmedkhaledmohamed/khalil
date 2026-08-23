"""Durable state model for Khalil execution graphs."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class GraphStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    COMPENSATED = "compensated"
    CANCELLED = "cancelled"


class NodeStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    COMPENSATED = "compensated"
    CANCELLED = "cancelled"


class TerminationReason(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    BUDGET_EXHAUSTED = "budget_exhausted"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    CANCELLED = "cancelled"
    NO_PROGRESS = "no_progress"
    COMPENSATED = "compensated"


class IdempotencyClaimStatus(str, Enum):
    ACQUIRED = "acquired"
    COMPLETED = "completed"
    AMBIGUOUS = "ambiguous"


_GRAPH_TRANSITIONS = {
    GraphStatus.PENDING: {GraphStatus.RUNNING, GraphStatus.CANCELLED},
    GraphStatus.RUNNING: {
        GraphStatus.WAITING_FOR_APPROVAL,
        GraphStatus.SUCCEEDED,
        GraphStatus.FAILED,
        GraphStatus.COMPENSATED,
        GraphStatus.CANCELLED,
    },
    GraphStatus.WAITING_FOR_APPROVAL: {GraphStatus.RUNNING, GraphStatus.CANCELLED},
    GraphStatus.FAILED: set(),
    GraphStatus.SUCCEEDED: set(),
    GraphStatus.COMPENSATED: set(),
    GraphStatus.CANCELLED: set(),
}

_NODE_TRANSITIONS = {
    NodeStatus.PENDING: {NodeStatus.READY, NodeStatus.CANCELLED},
    NodeStatus.READY: {NodeStatus.RUNNING, NodeStatus.FAILED, NodeStatus.CANCELLED},
    NodeStatus.RUNNING: {
        NodeStatus.WAITING_FOR_APPROVAL,
        NodeStatus.SUCCEEDED,
        NodeStatus.FAILED,
        NodeStatus.CANCELLED,
    },
    NodeStatus.WAITING_FOR_APPROVAL: {NodeStatus.READY, NodeStatus.CANCELLED},
    NodeStatus.SUCCEEDED: {NodeStatus.COMPENSATED},
    NodeStatus.FAILED: {
        NodeStatus.READY,
        NodeStatus.COMPENSATED,
        NodeStatus.CANCELLED,
    },
    NodeStatus.COMPENSATED: set(),
    NodeStatus.CANCELLED: set(),
}

_TERMINAL_GRAPH_STATUSES = {
    GraphStatus.SUCCEEDED,
    GraphStatus.FAILED,
    GraphStatus.COMPENSATED,
    GraphStatus.CANCELLED,
}
_TERMINAL_NODE_STATUSES = {
    NodeStatus.SUCCEEDED,
    NodeStatus.FAILED,
    NodeStatus.COMPENSATED,
    NodeStatus.CANCELLED,
}
_DEFAULT_TERMINATION_REASON = {
    GraphStatus.SUCCEEDED: TerminationReason.SUCCEEDED,
    GraphStatus.FAILED: TerminationReason.FAILED,
    GraphStatus.COMPENSATED: TerminationReason.COMPENSATED,
    GraphStatus.CANCELLED: TerminationReason.CANCELLED,
}
_UNSET = object()


class InvalidTransition(ValueError):
    """Raised when persisted execution state would move illegally."""


@dataclass(frozen=True)
class IdempotencyClaim:
    status: IdempotencyClaimStatus
    result: dict[str, Any] | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _validate_transition(current: Enum, target: Enum, allowed: dict) -> None:
    if target not in allowed[current]:
        raise InvalidTransition(f"Cannot transition from {current.value} to {target.value}")


@dataclass
class GraphNode:
    id: str
    action: str
    dependencies: list[str] = field(default_factory=list)
    inputs: dict[str, Any] = field(default_factory=dict)
    status: NodeStatus = NodeStatus.PENDING
    outputs: dict[str, Any] | None = None
    evidence: list[str] = field(default_factory=list)
    error: dict[str, Any] | None = None
    idempotency_key: str | None = None
    attempt_count: int = 0
    max_attempts: int = 1
    timeout_seconds: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None

    def __post_init__(self) -> None:
        self.status = NodeStatus(self.status)
        if not self.id:
            raise ValueError("Graph node id cannot be empty")
        if not self.action:
            raise ValueError(f"Graph node {self.id!r} action cannot be empty")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.attempt_count < 0:
            raise ValueError("attempt_count cannot be negative")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")


@dataclass
class GraphRun:
    source: str
    nodes: list[GraphNode]
    id: str = field(default_factory=lambda: f"graph_{uuid.uuid4().hex}")
    status: GraphStatus = GraphStatus.PENDING
    inputs: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    termination_reason: TerminationReason | None = None
    created_at: str | None = None
    updated_at: str | None = None
    completed_at: str | None = None

    def __post_init__(self) -> None:
        self.status = GraphStatus(self.status)
        if self.termination_reason is not None:
            self.termination_reason = TerminationReason(self.termination_reason)
        if not self.id:
            raise ValueError("Graph id cannot be empty")
        if not self.source:
            raise ValueError("Graph source cannot be empty")
        validate_graph(self.nodes)


def validate_graph(nodes: list[GraphNode]) -> None:
    """Reject duplicate node ids, missing dependencies, and dependency cycles."""
    node_ids = [node.id for node in nodes]
    if len(node_ids) != len(set(node_ids)):
        raise ValueError("Graph node ids must be unique")
    known = set(node_ids)
    for node in nodes:
        missing = set(node.dependencies) - known
        if missing:
            raise ValueError(
                f"Graph node {node.id!r} has missing dependencies: {sorted(missing)}"
            )
        if node.id in node.dependencies:
            raise ValueError(f"Graph node {node.id!r} cannot depend on itself")

    visiting: set[str] = set()
    visited: set[str] = set()
    dependencies = {node.id: node.dependencies for node in nodes}

    def visit(node_id: str) -> None:
        if node_id in visiting:
            raise ValueError("Graph dependencies contain a cycle")
        if node_id in visited:
            return
        visiting.add(node_id)
        for dependency in dependencies[node_id]:
            visit(dependency)
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in node_ids:
        visit(node_id)


class ExecutionGraphRepository:
    """SQLite persistence for execution graphs and per-node checkpoints."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.execute("PRAGMA foreign_keys=ON")

    def ensure_schema(self) -> None:
        with self.conn:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS execution_graphs (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    inputs_json TEXT NOT NULL DEFAULT '{}',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    termination_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS execution_nodes (
                    graph_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    dependencies_json TEXT NOT NULL DEFAULT '[]',
                    inputs_json TEXT NOT NULL DEFAULT '{}',
                    outputs_json TEXT,
                    evidence_json TEXT NOT NULL DEFAULT '[]',
                    error_json TEXT,
                    idempotency_key TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 1,
                    timeout_seconds REAL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    PRIMARY KEY (graph_id, node_id),
                    FOREIGN KEY (graph_id) REFERENCES execution_graphs(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_execution_graphs_status
                    ON execution_graphs(status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_execution_nodes_status
                    ON execution_nodes(graph_id, status, position);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_nodes_idempotency
                    ON execution_nodes(graph_id, idempotency_key)
                    WHERE idempotency_key IS NOT NULL;

                CREATE TABLE IF NOT EXISTS execution_idempotency (
                    idempotency_key TEXT PRIMARY KEY,
                    graph_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    claimed_at TEXT NOT NULL,
                    completed_at TEXT,
                    FOREIGN KEY (graph_id, node_id)
                        REFERENCES execution_nodes(graph_id, node_id) ON DELETE CASCADE
                );
            """)

    def create_graph(self, graph: GraphRun) -> GraphRun:
        validate_graph(graph.nodes)
        if graph.status is not GraphStatus.PENDING:
            raise ValueError("New execution graphs must start pending")
        if graph.termination_reason is not None or graph.completed_at is not None:
            raise ValueError("New execution graphs cannot already be terminated")
        now = _utc_now()
        graph.created_at = graph.created_at or now
        graph.updated_at = graph.updated_at or graph.created_at
        with self.conn:
            self.conn.execute(
                """INSERT INTO execution_graphs
                   (id, source, status, inputs_json, metadata_json, termination_reason,
                    created_at, updated_at, completed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    graph.id, graph.source, graph.status.value, _json(graph.inputs),
                    _json(graph.metadata),
                    graph.termination_reason.value if graph.termination_reason else None,
                    graph.created_at, graph.updated_at, graph.completed_at,
                ),
            )
            for position, node in enumerate(graph.nodes):
                node.created_at = node.created_at or now
                node.updated_at = node.updated_at or node.created_at
                self.conn.execute(
                    """INSERT INTO execution_nodes
                       (graph_id, node_id, position, action, status, dependencies_json,
                        inputs_json, outputs_json, evidence_json, error_json,
                        idempotency_key, attempt_count, max_attempts, timeout_seconds,
                        metadata_json, created_at, updated_at, started_at, completed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        graph.id, node.id, position, node.action, node.status.value,
                        _json(node.dependencies), _json(node.inputs),
                        _json(node.outputs) if node.outputs is not None else None,
                        _json(node.evidence),
                        _json(node.error) if node.error is not None else None,
                        node.idempotency_key, node.attempt_count, node.max_attempts,
                        node.timeout_seconds, _json(node.metadata), node.created_at,
                        node.updated_at, node.started_at, node.completed_at,
                    ),
                )
        return graph

    def load_graph(self, graph_id: str) -> GraphRun | None:
        graph_row = self.conn.execute(
            """SELECT id, source, status, inputs_json, metadata_json,
                      termination_reason, created_at, updated_at, completed_at
               FROM execution_graphs WHERE id = ?""",
            (graph_id,),
        ).fetchone()
        if graph_row is None:
            return None
        node_rows = self.conn.execute(
            """SELECT node_id, action, status, dependencies_json, inputs_json,
                      outputs_json, evidence_json, error_json, idempotency_key,
                      attempt_count, max_attempts, timeout_seconds, metadata_json,
                      created_at, updated_at, started_at, completed_at
               FROM execution_nodes WHERE graph_id = ? ORDER BY position""",
            (graph_id,),
        ).fetchall()
        nodes = [
            GraphNode(
                id=row[0], action=row[1], status=NodeStatus(row[2]),
                dependencies=json.loads(row[3]), inputs=json.loads(row[4]),
                outputs=json.loads(row[5]) if row[5] is not None else None,
                evidence=json.loads(row[6]),
                error=json.loads(row[7]) if row[7] is not None else None,
                idempotency_key=row[8], attempt_count=row[9], max_attempts=row[10],
                timeout_seconds=row[11], metadata=json.loads(row[12]),
                created_at=row[13], updated_at=row[14], started_at=row[15],
                completed_at=row[16],
            )
            for row in node_rows
        ]
        return GraphRun(
            id=graph_row[0], source=graph_row[1], status=GraphStatus(graph_row[2]),
            inputs=json.loads(graph_row[3]), metadata=json.loads(graph_row[4]),
            termination_reason=(
                TerminationReason(graph_row[5]) if graph_row[5] is not None else None
            ),
            created_at=graph_row[6], updated_at=graph_row[7],
            completed_at=graph_row[8], nodes=nodes,
        )

    def transition_graph(
        self,
        graph_id: str,
        target: GraphStatus,
        *,
        termination_reason: TerminationReason | None = None,
    ) -> GraphRun:
        target = GraphStatus(target)
        row = self.conn.execute(
            "SELECT status FROM execution_graphs WHERE id = ?", (graph_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown execution graph {graph_id!r}")
        current = GraphStatus(row[0])
        _validate_transition(current, target, _GRAPH_TRANSITIONS)
        reason = TerminationReason(termination_reason) if termination_reason else None
        if target in _TERMINAL_GRAPH_STATUSES and reason is None:
            reason = _DEFAULT_TERMINATION_REASON[target]
        now = _utc_now()
        completed_at = now if target in _TERMINAL_GRAPH_STATUSES else None
        with self.conn:
            cursor = self.conn.execute(
                """UPDATE execution_graphs
                   SET status = ?, termination_reason = ?, updated_at = ?, completed_at = ?
                   WHERE id = ? AND status = ?""",
                (
                    target.value, reason.value if reason else None, now,
                    completed_at, graph_id, current.value,
                ),
            )
        if cursor.rowcount != 1:
            raise InvalidTransition("Graph state changed concurrently")
        return self.load_graph(graph_id)  # type: ignore[return-value]

    def transition_node(
        self, graph_id: str, node_id: str, target: NodeStatus,
    ) -> GraphNode:
        target = NodeStatus(target)
        row = self.conn.execute(
            """SELECT status, attempt_count, started_at
               FROM execution_nodes WHERE graph_id = ? AND node_id = ?""",
            (graph_id, node_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown execution node {graph_id!r}/{node_id!r}")
        current = NodeStatus(row[0])
        _validate_transition(current, target, _NODE_TRANSITIONS)
        now = _utc_now()
        if target == NodeStatus.RUNNING:
            attempt_count = row[1] + 1
        elif target == NodeStatus.WAITING_FOR_APPROVAL and current == NodeStatus.RUNNING:
            # The Execution Bus returns before invoking a protected handler, so
            # an approval pause does not consume an execution attempt.
            attempt_count = max(0, row[1] - 1)
        else:
            attempt_count = row[1]
        started_at = row[2] or (now if target == NodeStatus.RUNNING else None)
        completed_at = now if target in _TERMINAL_NODE_STATUSES else None
        with self.conn:
            cursor = self.conn.execute(
                """UPDATE execution_nodes
                   SET status = ?, attempt_count = ?, started_at = ?,
                       completed_at = ?, updated_at = ?
                   WHERE graph_id = ? AND node_id = ? AND status = ?""",
                (
                    target.value, attempt_count, started_at, completed_at, now,
                    graph_id, node_id, current.value,
                ),
            )
            if cursor.rowcount == 1:
                self.conn.execute(
                    "UPDATE execution_graphs SET updated_at = ? WHERE id = ?",
                    (now, graph_id),
                )
        if cursor.rowcount != 1:
            raise InvalidTransition("Node state changed concurrently")
        graph = self.load_graph(graph_id)
        if graph is None:
            raise KeyError(f"Unknown execution graph {graph_id!r}")
        return next(node for node in graph.nodes if node.id == node_id)

    def claim_node(self, graph_id: str, node_id: str) -> GraphNode | None:
        """Atomically move one ready node to running.

        Returning ``None`` means another runner claimed it or its retry budget
        was already exhausted.
        """
        now = _utc_now()
        with self.conn:
            cursor = self.conn.execute(
                """UPDATE execution_nodes
                   SET status = ?, attempt_count = attempt_count + 1,
                       started_at = COALESCE(started_at, ?), completed_at = NULL,
                       updated_at = ?
                   WHERE graph_id = ? AND node_id = ? AND status = ?
                     AND attempt_count < max_attempts""",
                (
                    NodeStatus.RUNNING.value, now, now, graph_id, node_id,
                    NodeStatus.READY.value,
                ),
            )
            if cursor.rowcount == 1:
                self.conn.execute(
                    "UPDATE execution_graphs SET updated_at = ? WHERE id = ?",
                    (now, graph_id),
                )
        if cursor.rowcount != 1:
            return None
        graph = self.load_graph(graph_id)
        if graph is None:
            raise KeyError(f"Unknown execution graph {graph_id!r}")
        return next(node for node in graph.nodes if node.id == node_id)

    def reset_interrupted_node(self, graph_id: str, node_id: str) -> bool:
        """Return an interrupted read-only node to ready without a new attempt."""
        now = _utc_now()
        with self.conn:
            cursor = self.conn.execute(
                """UPDATE execution_nodes
                   SET status = ?,
                       attempt_count = CASE
                           WHEN attempt_count > 0 THEN attempt_count - 1
                           ELSE 0
                       END,
                       completed_at = NULL, updated_at = ?
                   WHERE graph_id = ? AND node_id = ? AND status = ?""",
                (
                    NodeStatus.READY.value, now, graph_id, node_id,
                    NodeStatus.RUNNING.value,
                ),
            )
            if cursor.rowcount == 1:
                self.conn.execute(
                    "UPDATE execution_graphs SET updated_at = ? WHERE id = ?",
                    (now, graph_id),
                )
        return cursor.rowcount == 1

    def claim_idempotency(
        self, idempotency_key: str, graph_id: str, node_id: str,
    ) -> IdempotencyClaim:
        """Claim a protected effect or return its durable prior outcome."""
        now = _utc_now()
        with self.conn:
            cursor = self.conn.execute(
                """INSERT OR IGNORE INTO execution_idempotency
                   (idempotency_key, graph_id, node_id, status, claimed_at)
                   VALUES (?, ?, ?, 'claimed', ?)""",
                (idempotency_key, graph_id, node_id, now),
            )
            if cursor.rowcount == 1:
                return IdempotencyClaim(IdempotencyClaimStatus.ACQUIRED)
            row = self.conn.execute(
                """SELECT status, result_json FROM execution_idempotency
                   WHERE idempotency_key = ?""",
                (idempotency_key,),
            ).fetchone()
        if row and row[0] == "completed":
            return IdempotencyClaim(
                IdempotencyClaimStatus.COMPLETED,
                json.loads(row[1]) if row[1] is not None else None,
            )
        return IdempotencyClaim(IdempotencyClaimStatus.AMBIGUOUS)

    def complete_idempotency(
        self,
        idempotency_key: str,
        graph_id: str,
        node_id: str,
        result: dict[str, Any],
    ) -> None:
        now = _utc_now()
        with self.conn:
            cursor = self.conn.execute(
                """UPDATE execution_idempotency
                   SET status = 'completed', result_json = ?, completed_at = ?
                   WHERE idempotency_key = ? AND graph_id = ? AND node_id = ?
                     AND status = 'claimed'""",
                (_json(result), now, idempotency_key, graph_id, node_id),
            )
        if cursor.rowcount != 1:
            raise InvalidTransition("Idempotency claim is not owned by this node")

    def release_idempotency(
        self, idempotency_key: str, graph_id: str, node_id: str,
    ) -> bool:
        """Release a claim only when execution stopped before its handler ran."""
        with self.conn:
            cursor = self.conn.execute(
                """DELETE FROM execution_idempotency
                   WHERE idempotency_key = ? AND graph_id = ? AND node_id = ?
                     AND status = 'claimed'""",
                (idempotency_key, graph_id, node_id),
            )
        return cursor.rowcount == 1

    def get_idempotency_claim(self, idempotency_key: str) -> IdempotencyClaim | None:
        row = self.conn.execute(
            """SELECT status, result_json FROM execution_idempotency
               WHERE idempotency_key = ?""",
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        if row[0] == "completed":
            return IdempotencyClaim(
                IdempotencyClaimStatus.COMPLETED,
                json.loads(row[1]) if row[1] is not None else None,
            )
        return IdempotencyClaim(IdempotencyClaimStatus.AMBIGUOUS)

    def checkpoint_node(
        self,
        graph_id: str,
        node_id: str,
        *,
        outputs: dict[str, Any] | None | object = _UNSET,
        evidence: list[str] | object = _UNSET,
        error: dict[str, Any] | None | object = _UNSET,
    ) -> GraphNode:
        assignments = ["updated_at = ?"]
        now = _utc_now()
        values: list[Any] = [now]
        for column, value in (
            ("outputs_json", outputs),
            ("evidence_json", evidence),
            ("error_json", error),
        ):
            if value is not _UNSET:
                assignments.append(f"{column} = ?")
                values.append(_json(value) if value is not None else None)
        values.extend((graph_id, node_id))
        with self.conn:
            cursor = self.conn.execute(
                f"UPDATE execution_nodes SET {', '.join(assignments)} "
                "WHERE graph_id = ? AND node_id = ?",
                values,
            )
            if cursor.rowcount == 1:
                self.conn.execute(
                    "UPDATE execution_graphs SET updated_at = ? WHERE id = ?",
                    (now, graph_id),
                )
        if cursor.rowcount != 1:
            raise KeyError(f"Unknown execution node {graph_id!r}/{node_id!r}")
        graph = self.load_graph(graph_id)
        if graph is None:
            raise KeyError(f"Unknown execution graph {graph_id!r}")
        return next(node for node in graph.nodes if node.id == node_id)

    def list_resumable_runs(self, limit: int = 100) -> list[GraphRun]:
        if limit < 1:
            return []
        rows = self.conn.execute(
            """SELECT id FROM execution_graphs
               WHERE status IN (?, ?, ?)
               ORDER BY updated_at ASC LIMIT ?""",
            (
                GraphStatus.PENDING.value,
                GraphStatus.RUNNING.value,
                GraphStatus.WAITING_FOR_APPROVAL.value,
                limit,
            ),
        ).fetchall()
        graphs = [self.load_graph(row[0]) for row in rows]
        return [graph for graph in graphs if graph is not None]
