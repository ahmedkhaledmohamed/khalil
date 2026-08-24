#!/usr/bin/env python3
"""Demonstrate Khalil's durable graph and foreground-loop recovery guarantees."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import ActionType
from execution import ActionResult, ExecutionContext, ExecutionSource
from execution_graph import ExecutionGraphRepository, GraphNode, GraphRun
from graph_runner import ExecutionGraphRunner
from loop_controller import LoopBudget
from loop_state import PendingToolAction, classify_recovery
from tool_loop_runner import DurableToolLoopRunner


class SimulatedProcessCrash(BaseException):
    """Stop execution without letting normal exception handling checkpoint failure."""


class DemoBus:
    async def execute_request(self, request):
        return ActionResult.succeeded(request.action)


def _graph_repository(database: Path):
    connection = sqlite3.connect(str(database))
    repository = ExecutionGraphRepository(connection)
    repository.ensure_schema()
    return connection, repository


def _context() -> ExecutionContext:
    return ExecutionContext(source=ExecutionSource.ORCHESTRATOR, chat_id=42)


async def demonstrate_safe_graph_recovery(database: Path) -> dict:
    first_connection, first_repository = _graph_repository(database)
    first_repository.create_graph(GraphRun(
        id="demo_safe_graph",
        source=ExecutionSource.ORCHESTRATOR.value,
        nodes=[
            GraphNode(id="collect", action="collect_context"),
            GraphNode(
                id="summarize",
                action="summarize_context",
                dependencies=["collect"],
            ),
        ],
    ))
    before_restart_calls = []

    async def crash_during_second_node(node, _request):
        before_restart_calls.append(node.id)
        if node.id == "summarize":
            raise SimulatedProcessCrash()
        return ActionResult.succeeded("context checkpointed")

    try:
        await ExecutionGraphRunner(first_repository, DemoBus()).run(
            "demo_safe_graph",
            _context(),
            execute_node=crash_during_second_node,
        )
    except SimulatedProcessCrash:
        pass
    interrupted = first_repository.load_graph("demo_safe_graph")
    before_restart = [node.status.value for node in interrupted.nodes]
    first_connection.close()

    second_connection, second_repository = _graph_repository(database)
    after_restart_calls = []

    async def finish_recovered_node(node, _request):
        after_restart_calls.append(node.id)
        return ActionResult.succeeded("summary checkpointed")

    recovered = await ExecutionGraphRunner(second_repository, DemoBus()).run(
        "demo_safe_graph",
        _context(),
        execute_node=finish_recovered_node,
        recover_interrupted=True,
    )
    result = {
        "before_restart": before_restart,
        "after_restart": [node.status.value for node in recovered.nodes],
        "executed_before_restart": before_restart_calls,
        "executed_after_restart": after_restart_calls,
        "completed_node_replayed": "collect" in after_restart_calls,
        "result": recovered.status.value,
    }
    second_connection.close()
    return result


async def demonstrate_protected_write_recovery(database: Path) -> dict:
    first_connection, first_repository = _graph_repository(database)
    first_repository.create_graph(GraphRun(
        id="demo_protected_write",
        source=ExecutionSource.ORCHESTRATOR.value,
        nodes=[GraphNode(
            id="send",
            action="send_message",
            idempotency_key="demo:message:1",
        )],
    ))
    external_effects = []

    async def crash_after_effect(_node, _request):
        external_effects.append("message sent")
        raise SimulatedProcessCrash()

    try:
        await ExecutionGraphRunner(first_repository, DemoBus()).run(
            "demo_protected_write",
            _context(),
            execute_node=crash_after_effect,
        )
    except SimulatedProcessCrash:
        pass
    first_connection.close()

    second_connection, second_repository = _graph_repository(database)

    async def would_duplicate(_node, _request):
        external_effects.append("duplicate message")
        return ActionResult.succeeded("sent twice")

    recovered = await ExecutionGraphRunner(second_repository, DemoBus()).run(
        "demo_protected_write",
        _context(),
        execute_node=would_duplicate,
        recover_interrupted=True,
    )
    node = recovered.nodes[0]
    result = {
        "external_effect_count": len(external_effects),
        "external_effects": external_effects,
        "duplicate_blocked": "duplicate message" not in external_effects,
        "error_kind": (node.error or {}).get("kind"),
        "result": recovered.status.value,
    }
    second_connection.close()
    return result


def demonstrate_foreground_approval_recovery(database: Path) -> dict:
    messages = [{"role": "user", "content": "Create the requested file"}]
    action = PendingToolAction(
        id="call_generate",
        name="generate_file",
        arguments='{"filename":"brief.md","content":"demo"}',
        action_type=ActionType.WRITE,
    )
    runner = DurableToolLoopRunner.create(
        database,
        chat_id=42,
        query="Create the requested file",
        model="taskforce/default",
        budget=LoopBudget(
            max_iterations=4,
            max_actions=4,
            max_elapsed_seconds=60,
            max_no_progress_iterations=2,
        ),
    )
    run_id = runner.id
    runner.begin_iteration(messages)
    runner.after_model(messages, pending_actions=[action])
    runner.reserve_actions([action], messages)
    runner.wait_for_approval()
    before_restart = runner.run.status.value
    runner.close()

    recovered = DurableToolLoopRunner.resume(database, run_id)
    recovery = classify_recovery(recovered.run)
    recovered.resume_after_approval()
    approved_boundary = recovered.run.latest_checkpoint.boundary.value
    recovered.after_actions(
        messages + [{"role": "tool", "content": "brief.md created"}],
        "brief-created",
    )
    recovered.before_synthesis(messages)
    recovered.terminate(
        messages + [{"role": "assistant", "content": "Created brief.md"}],
        succeeded=True,
    )
    result = {
        "before_restart": before_restart,
        "restart_disposition": recovery.disposition.value,
        "approval_boundary": approved_boundary,
        "action_count": recovered.snapshot().actions,
        "result": recovered.run.status.value,
    }
    recovered.close()
    return result


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="khalil-recovery-demo-") as temporary:
        directory = Path(temporary)
        evidence = {
            "safe_graph_recovery": await demonstrate_safe_graph_recovery(
                directory / "safe-graph.db",
            ),
            "protected_write_recovery": await demonstrate_protected_write_recovery(
                directory / "protected-write.db",
            ),
            "foreground_approval_recovery": demonstrate_foreground_approval_recovery(
                directory / "foreground-loop.db",
            ),
        }
    print(json.dumps(evidence, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
