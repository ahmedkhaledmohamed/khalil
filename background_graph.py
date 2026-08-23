"""Durable graph adapter for scheduled, workflow, and proactive execution."""

from __future__ import annotations

import inspect
import logging
import sqlite3
import uuid
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from config import DB_PATH
from execution import (
    ActionErrorKind,
    ActionResult,
    ExecutionContext,
    ExecutionSource,
)
from execution_graph import (
    ExecutionGraphRepository,
    GraphNode,
    GraphRun,
    GraphStatus,
)
from graph_runner import ExecutionGraphRunner


log = logging.getLogger("khalil.background_graph")

DISPATCH_ACTION = "background.dispatch"
BackgroundHandler = Callable[
    [dict[str, Any], ExecutionContext],
    ActionResult | Any | Awaitable[ActionResult | Any],
]

_handlers: dict[str, BackgroundHandler] = {}


def register_background_handler(name: str, handler: BackgroundHandler) -> None:
    """Register a stable handler name that can be resolved after restart."""
    if not name:
        raise ValueError("Background handler name cannot be empty")
    _handlers[name] = handler


def unregister_background_handler(name: str) -> None:
    """Remove a handler, primarily for lifecycle cleanup and tests."""
    _handlers.pop(name, None)


def install_background_dispatch(execution_bus: Any) -> None:
    """Install the single Execution Bus action used by background graph nodes."""

    async def _dispatch(params: dict, context: ExecutionContext) -> ActionResult:
        handler_name = str(params.get("handler") or "")
        handler = _handlers.get(handler_name)
        if handler is None:
            return ActionResult.failed(
                f"Background handler '{handler_name}' is not registered",
                kind=ActionErrorKind.NOT_FOUND,
            )
        payload = params.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        result = handler(payload, context)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, ActionResult):
            return result
        if result is None:
            return ActionResult.succeeded(data={"completed": True})
        return ActionResult.succeeded(str(result))

    execution_bus.register_composite_action(DISPATCH_ACTION, _dispatch)


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def create_background_graph(
    *,
    source: ExecutionSource,
    trigger_id: str,
    handler: str,
    payload: dict[str, Any] | None = None,
    chat_id: int | None = None,
    idempotent: bool = True,
    graph_id: str | None = None,
) -> GraphRun:
    """Persist a one-node graph for a named background trigger."""
    graph_id = graph_id or f"{source.value}_{uuid.uuid4().hex}"
    node_id = "execute"
    graph = GraphRun(
        id=graph_id,
        source=source.value,
        nodes=[GraphNode(
            id=node_id,
            action=DISPATCH_ACTION,
            inputs={"handler": handler, "payload": payload or {}},
            idempotency_key=f"{graph_id}:{node_id}" if idempotent else None,
            metadata={"trigger_id": trigger_id, "handler": handler},
        )],
        inputs={"trigger_id": trigger_id, "payload": payload or {}},
        metadata={
            "kind": "background_trigger",
            "trigger_id": trigger_id,
            "handler": handler,
            "chat_id": chat_id,
        },
    )
    conn = _get_conn()
    try:
        repository = ExecutionGraphRepository(conn)
        repository.ensure_schema()
        return repository.create_graph(graph)
    finally:
        conn.close()


async def run_background_graph(
    graph_id: str,
    execution_bus: Any,
    *,
    recover_interrupted: bool = False,
) -> GraphRun:
    """Execute or recover a persisted background graph."""
    conn = _get_conn()
    try:
        repository = ExecutionGraphRepository(conn)
        repository.ensure_schema()
        graph = repository.load_graph(graph_id)
        if graph is None:
            raise KeyError(f"Unknown background graph {graph_id!r}")
        try:
            source = ExecutionSource(graph.source)
        except ValueError:
            source = ExecutionSource.BACKGROUND_AGENT
        chat_id = graph.metadata.get("chat_id")
        return await ExecutionGraphRunner(repository, execution_bus).run(
            graph_id,
            ExecutionContext(
                source=source,
                chat_id=int(chat_id) if chat_id else None,
                parent_plan_id=graph_id,
                trigger_id=str(graph.metadata.get("trigger_id") or ""),
            ),
            recover_interrupted=recover_interrupted,
        )
    finally:
        conn.close()


async def execute_background_trigger(
    *,
    execution_bus: Any,
    source: ExecutionSource,
    trigger_id: str,
    handler: str,
    payload: dict[str, Any] | None = None,
    chat_id: int | None = None,
    idempotent: bool = True,
) -> GraphRun:
    """Create and execute a durable one-node background trigger."""
    graph = create_background_graph(
        source=source,
        trigger_id=trigger_id,
        handler=handler,
        payload=payload,
        chat_id=chat_id,
        idempotent=idempotent,
    )
    return await run_background_graph(graph.id, execution_bus)


async def resume_background_graphs(
    execution_bus: Any,
    *,
    sources: Iterable[ExecutionSource],
) -> list[GraphRun]:
    """Recover active background graphs whose named handlers are registered."""
    source_values = {source.value for source in sources}
    conn = _get_conn()
    try:
        repository = ExecutionGraphRepository(conn)
        repository.ensure_schema()
        graphs = [
            graph for graph in repository.list_resumable_runs(limit=100)
            if graph.source in source_values
            and graph.status in {GraphStatus.PENDING, GraphStatus.RUNNING}
        ]
    finally:
        conn.close()

    resumed = []
    for graph in graphs:
        try:
            resumed.append(await run_background_graph(
                graph.id,
                execution_bus,
                recover_interrupted=True,
            ))
        except Exception as error:
            log.exception("Failed to resume background graph %s: %s", graph.id, error)
    return resumed


def graph_output(graph: GraphRun) -> str:
    """Return the final observable output of a one-node background graph."""
    if not graph.nodes:
        return ""
    return str((graph.nodes[-1].outputs or {}).get("output") or "")
