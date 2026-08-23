"""Behavioral tests for durable graph execution and recovery."""

import asyncio
import sqlite3
from unittest.mock import patch

import pytest

from config import ActionType
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
    NodeStatus,
)
from graph_runner import ExecutionGraphRunner, action_result_to_payload
from orchestrator import TaskStep, execute_plan, load_plan


_TEST_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_TEST_LOOP)


class FakeBus:
    def __init__(self, results=None, action_types=None):
        self.results = results or {}
        self.action_types = action_types or {}
        self.calls = []

    async def execute_request(self, request):
        self.calls.append(request)
        result = self.results.get(request.action, ActionResult.succeeded(request.action))
        if isinstance(result, list):
            return result.pop(0)
        return result

    def get_declared_action_type(self, action):
        return self.action_types.get(action, ActionType.READ)


class SimulatedProcessCrash(BaseException):
    pass


class FakeChannel:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))


def _repository(path=None):
    conn = sqlite3.connect(str(path) if path else ":memory:")
    repo = ExecutionGraphRepository(conn)
    repo.ensure_schema()
    return conn, repo


def _context():
    return ExecutionContext(source=ExecutionSource.ORCHESTRATOR, chat_id=42)


def _run(coro):
    return _TEST_LOOP.run_until_complete(coro)


def test_runner_executes_dependencies_and_checkpoints_results():
    conn, repo = _repository()
    repo.create_graph(GraphRun(
        id="graph_dependencies",
        source="orchestrator",
        nodes=[
            GraphNode(id="first", action="lookup"),
            GraphNode(id="second", action="summarize", dependencies=["first"]),
        ],
    ))
    bus = FakeBus({
        "lookup": ActionResult.succeeded("source material"),
        "summarize": ActionResult.succeeded("summary"),
    })

    graph = _run(ExecutionGraphRunner(repo, bus).run("graph_dependencies", _context()))

    assert graph.status is GraphStatus.SUCCEEDED
    assert [request.action for request in bus.calls] == ["lookup", "summarize"]
    assert bus.calls[1].context.prior_results == {"first": "source material"}
    assert graph.nodes[0].outputs["output"] == "source material"
    assert graph.nodes[1].outputs["output"] == "summary"
    assert all(node.completed_at for node in graph.nodes)
    conn.close()


def test_only_one_repository_can_atomically_claim_a_ready_node(tmp_path):
    database = tmp_path / "graph.db"
    conn_one, repo_one = _repository(database)
    repo_one.create_graph(GraphRun(
        id="graph_claim",
        source="test",
        nodes=[GraphNode(id="node", action="read")],
    ))
    repo_one.transition_node("graph_claim", "node", NodeStatus.READY)
    conn_two, repo_two = _repository(database)

    first_claim = repo_one.claim_node("graph_claim", "node")
    second_claim = repo_two.claim_node("graph_claim", "node")

    assert first_claim is not None
    assert first_claim.status is NodeStatus.RUNNING
    assert second_claim is None
    assert repo_two.load_graph("graph_claim").nodes[0].attempt_count == 1
    conn_one.close()
    conn_two.close()


def test_ordinary_runner_does_not_reclaim_live_running_node():
    conn, repo = _repository()
    repo.create_graph(GraphRun(
        id="graph_live",
        source="test",
        nodes=[GraphNode(id="node", action="read")],
    ))
    repo.transition_graph("graph_live", GraphStatus.RUNNING)
    repo.transition_node("graph_live", "node", NodeStatus.READY)
    repo.claim_node("graph_live", "node")
    bus = FakeBus()

    observed = _run(ExecutionGraphRunner(repo, bus).run("graph_live", _context()))

    assert observed.status is GraphStatus.RUNNING
    assert observed.nodes[0].status is NodeStatus.RUNNING
    assert bus.calls == []
    conn.close()


def test_restart_resumes_without_reexecuting_completed_nodes(tmp_path):
    database = tmp_path / "resume.db"
    first_conn, first_repo = _repository(database)
    first_repo.create_graph(GraphRun(
        id="graph_resume",
        source="orchestrator",
        nodes=[
            GraphNode(id="first", action="first"),
            GraphNode(id="second", action="second", dependencies=["first"]),
        ],
    ))
    first_calls = []

    async def crash_on_second(node, request):
        first_calls.append(node.id)
        if node.id == "second":
            raise SimulatedProcessCrash()
        return ActionResult.succeeded("checkpoint one")

    with pytest.raises(SimulatedProcessCrash):
        _run(ExecutionGraphRunner(first_repo, FakeBus()).run(
            "graph_resume", _context(), execute_node=crash_on_second,
        ))
    interrupted = first_repo.load_graph("graph_resume")
    assert [node.status for node in interrupted.nodes] == [
        NodeStatus.SUCCEEDED, NodeStatus.RUNNING,
    ]
    first_conn.close()

    second_conn, second_repo = _repository(database)
    resumed_calls = []

    async def finish_second(node, request):
        resumed_calls.append(node.id)
        return ActionResult.succeeded("checkpoint two")

    resumed = _run(ExecutionGraphRunner(second_repo, FakeBus()).run(
        "graph_resume", _context(), execute_node=finish_second, recover_interrupted=True,
    ))

    assert first_calls == ["first", "second"]
    assert resumed_calls == ["second"]
    assert resumed.status is GraphStatus.SUCCEEDED
    assert resumed.nodes[0].outputs["output"] == "checkpoint one"
    assert resumed.nodes[1].attempt_count == 1
    second_conn.close()


def test_interrupted_protected_effect_is_not_executed_twice(tmp_path):
    database = tmp_path / "idempotency.db"
    first_conn, first_repo = _repository(database)
    first_repo.create_graph(GraphRun(
        id="graph_write",
        source="orchestrator",
        nodes=[GraphNode(
            id="send",
            action="email_send",
            idempotency_key="email:message-123",
        )],
    ))
    external_effects = []

    async def crash_after_effect(node, request):
        external_effects.append("sent")
        raise SimulatedProcessCrash()

    with pytest.raises(SimulatedProcessCrash):
        _run(ExecutionGraphRunner(first_repo, FakeBus()).run(
            "graph_write", _context(), execute_node=crash_after_effect,
        ))
    first_conn.close()

    second_conn, second_repo = _repository(database)

    async def would_duplicate(node, request):
        external_effects.append("duplicate")
        return ActionResult.succeeded("sent again")

    recovered = _run(ExecutionGraphRunner(second_repo, FakeBus()).run(
        "graph_write", _context(), execute_node=would_duplicate, recover_interrupted=True,
    ))

    assert external_effects == ["sent"]
    assert recovered.status is GraphStatus.FAILED
    assert recovered.nodes[0].error["kind"] == "idempotency_ambiguous"
    assert recovered.nodes[0].attempt_count == 1
    second_conn.close()


def test_completed_idempotency_receipt_recovers_missing_node_checkpoint():
    conn, repo = _repository()
    repo.create_graph(GraphRun(
        id="graph_receipt",
        source="test",
        nodes=[GraphNode(
            id="send",
            action="email_send",
            idempotency_key="email:message-456",
        )],
    ))
    repo.transition_graph("graph_receipt", GraphStatus.RUNNING)
    repo.transition_node("graph_receipt", "send", NodeStatus.READY)
    repo.claim_node("graph_receipt", "send")
    repo.claim_idempotency("email:message-456", "graph_receipt", "send")
    repo.complete_idempotency(
        "email:message-456",
        "graph_receipt",
        "send",
        action_result_to_payload(ActionResult.succeeded("sent once")),
    )
    bus = FakeBus()

    recovered = _run(ExecutionGraphRunner(repo, bus).run(
        "graph_receipt", _context(), recover_interrupted=True,
    ))

    assert recovered.status is GraphStatus.SUCCEEDED
    assert recovered.nodes[0].outputs["output"] == "sent once"
    assert recovered.nodes[0].outputs["replayed"] is True
    assert bus.calls == []
    conn.close()


def test_waiting_node_can_resume_after_explicit_approval_transition():
    conn, repo = _repository()
    repo.create_graph(GraphRun(
        id="graph_approval",
        source="test",
        nodes=[GraphNode(
            id="send",
            action="email_send",
            idempotency_key="email:message-789",
        )],
    ))
    bus = FakeBus({
        "email_send": [
            ActionResult.waiting_for_approval("Approve sending"),
            ActionResult.succeeded("sent"),
        ],
    })
    runner = ExecutionGraphRunner(repo, bus)

    waiting = _run(runner.run("graph_approval", _context()))
    assert waiting.status is GraphStatus.WAITING_FOR_APPROVAL
    assert waiting.nodes[0].status is NodeStatus.WAITING_FOR_APPROVAL
    assert repo.get_idempotency_claim("email:message-789") is None

    repo.transition_node("graph_approval", "send", NodeStatus.READY)
    completed = _run(runner.run("graph_approval", _context()))

    assert completed.status is GraphStatus.SUCCEEDED
    assert completed.nodes[0].outputs["output"] == "sent"
    assert len(bus.calls) == 2
    conn.close()


def test_retryable_read_retries_within_attempt_budget():
    conn, repo = _repository()
    repo.create_graph(GraphRun(
        id="graph_retry",
        source="test",
        nodes=[GraphNode(id="read", action="lookup", max_attempts=2)],
    ))
    bus = FakeBus({
        "lookup": [
            ActionResult.failed(
                "temporary outage", kind=ActionErrorKind.NETWORK, retryable=True,
            ),
            ActionResult.succeeded("recovered"),
        ],
    })

    graph = _run(ExecutionGraphRunner(repo, bus).run("graph_retry", _context()))

    assert graph.status is GraphStatus.SUCCEEDED
    assert len(bus.calls) == 2
    assert graph.nodes[0].attempt_count == 2
    assert graph.nodes[0].outputs["output"] == "recovered"
    conn.close()


def test_orchestrator_runs_through_graph_and_preserves_plan_contract(tmp_path):
    database = tmp_path / "orchestrator.db"
    channel = FakeChannel()
    bus = FakeBus({
        "lookup": ActionResult.succeeded("source material"),
        "summarize": ActionResult.succeeded("summary"),
    })
    steps = [
        TaskStep(id="first", action="lookup", description="Find material", params={}),
        TaskStep(
            id="second",
            action="summarize",
            description="Summarize material",
            params={},
            depends_on=["first"],
        ),
    ]

    with patch("orchestrator.DB_PATH", database):
        result = _run(execute_plan(
            steps,
            "prepare a summary",
            channel,
            42,
            execution_bus=bus,
            execution_context=_context(),
        ))
        loaded = load_plan(result.plan_id)

    assert result.status == "completed"
    assert result.completed_count == 2
    assert loaded is not None
    assert loaded.query == "prepare a summary"
    assert [step.result for step in loaded.steps] == ["source material", "summary"]
    assert [request.action for request in bus.calls] == ["lookup", "summarize"]
    assert any("Step 1/2" in text for _, text in channel.messages)
