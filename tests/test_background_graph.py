import asyncio
import sqlite3
from unittest.mock import AsyncMock

import background_graph
import execution
import workflows
from agent_loop import AgentLoop, Opportunity, Urgency
from background_graph import (
    create_background_graph,
    execute_background_trigger,
    install_background_dispatch,
    register_background_handler,
    resume_background_graphs,
    unregister_background_handler,
)
from config import AutonomyLevel
from execution import ActionResult, ExecutionBus, ExecutionSource
from execution_graph import (
    ExecutionGraphRepository,
    GraphStatus,
    NodeStatus,
)
from workflows import Workflow, WorkflowEngine, WorkflowStep


_TEST_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_TEST_LOOP)


def _run(coro):
    return _TEST_LOOP.run_until_complete(coro)


class _Registry:
    def get_handler(self, action):
        return None

    def get_action_type(self, action):
        return None


class _Autonomy:
    level = AutonomyLevel.AUTONOMOUS

    def needs_approval(self, *args, **kwargs):
        return False

    def check_rate_limit(self, action):
        return True, ""

    def log_audit(self, **kwargs):
        return None


def _bus():
    bus = ExecutionBus(lambda: _Registry(), _Autonomy())
    install_background_dispatch(bus)
    return bus


def _patch_db(monkeypatch, tmp_path):
    db_path = tmp_path / "khalil.db"
    monkeypatch.setattr(background_graph, "DB_PATH", db_path)
    return db_path


def test_background_trigger_persists_and_executes(monkeypatch, tmp_path):
    db_path = _patch_db(monkeypatch, tmp_path)
    calls = []

    async def handler(payload, context):
        calls.append((payload, context.source))
        return f"processed {payload['value']}"

    register_background_handler("test.schedule", handler)
    try:
        graph = _run(execute_background_trigger(
            execution_bus=_bus(),
            source=ExecutionSource.SCHEDULER,
            trigger_id="daily-test",
            handler="test.schedule",
            payload={"value": 7},
        ))
    finally:
        unregister_background_handler("test.schedule")

    assert graph.status == GraphStatus.SUCCEEDED
    assert calls == [({"value": 7}, ExecutionSource.SCHEDULER)]
    conn = sqlite3.connect(db_path)
    persisted = ExecutionGraphRepository(conn).load_graph(graph.id)
    conn.close()
    assert persisted is not None
    assert persisted.source == ExecutionSource.SCHEDULER.value
    assert persisted.nodes[0].outputs["output"] == "processed 7"


def test_recovery_does_not_repeat_claimed_background_effect(monkeypatch, tmp_path):
    db_path = _patch_db(monkeypatch, tmp_path)
    calls = []

    async def handler(payload, context):
        calls.append(payload)
        return "should not run"

    register_background_handler("test.protected", handler)
    graph = create_background_graph(
        source=ExecutionSource.SCHEDULER,
        trigger_id="protected-test",
        handler="test.protected",
        graph_id="protected_graph",
    )
    conn = sqlite3.connect(db_path)
    repo = ExecutionGraphRepository(conn)
    repo.transition_graph(graph.id, GraphStatus.RUNNING)
    repo.transition_node(graph.id, "execute", NodeStatus.READY)
    claimed = repo.claim_node(graph.id, "execute")
    assert claimed is not None
    repo.claim_idempotency(claimed.idempotency_key, graph.id, claimed.id)
    conn.close()

    try:
        resumed = _run(resume_background_graphs(
            _bus(), sources={ExecutionSource.SCHEDULER},
        ))
    finally:
        unregister_background_handler("test.protected")

    assert calls == []
    assert resumed[0].status == GraphStatus.FAILED
    assert resumed[0].nodes[0].error["kind"] == "idempotency_ambiguous"


def test_workflow_steps_execute_as_sequential_graph(monkeypatch, tmp_path):
    db_path = _patch_db(monkeypatch, tmp_path)
    bus = _bus()
    monkeypatch.setattr(workflows, "get_execution_bus", lambda: bus)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    engine = WorkflowEngine(conn)
    engine.ensure_tables()
    workflow = Workflow(
        id="wf_test_graph",
        name="Graph workflow",
        trigger_type="signal",
        trigger_config={"signal_type": "test"},
        actions=[
            WorkflowStep("first", description="First"),
            WorkflowStep("second", description="Second"),
            WorkflowStep("third", description="Third"),
        ],
    )
    engine.register(workflow)

    async def execute_step(wf, step, context, **kwargs):
        if step.action == "first":
            return "first output"
        if step.action == "second":
            return f"second saw {context['step_1_result']}"
        return f"third saw {context['step_1_result']} and {context['step_2_result']}"

    monkeypatch.setattr(engine, "_execute_single_step", execute_step)
    results = _run(engine._execute_steps(workflow, {"signal_type": "test"}))

    assert [result["ok"] for result in results] == [True, True, True]
    assert results[1]["result"] == "second saw first output"
    assert results[2]["result"] == "third saw first output and second saw first output"
    graph_row = conn.execute(
        "SELECT id, status FROM execution_graphs WHERE source = ?",
        (ExecutionSource.WORKFLOW.value,),
    ).fetchone()
    assert graph_row["status"] == GraphStatus.SUCCEEDED.value
    graph = ExecutionGraphRepository(conn).load_graph(graph_row["id"])
    assert graph.nodes[2].dependencies == ["step_1", "step_2"]
    conn.close()


def test_agent_loop_action_uses_durable_graph(monkeypatch, tmp_path):
    db_path = _patch_db(monkeypatch, tmp_path)
    bus = _bus()
    monkeypatch.setattr(execution, "_bus_instance", bus)
    channel = AsyncMock()
    loop = AgentLoop(channel, 123, _Autonomy())
    legacy = AsyncMock(return_value="proactive action complete")
    monkeypatch.setattr(loop, "_execute_action_legacy", legacy)

    result = _run(loop._execute_action(Opportunity(
        id="opportunity-test",
        source="test",
        summary="Test opportunity",
        urgency=Urgency.MEDIUM,
        action_type="test_action",
        payload={"item": 1},
    )))

    assert result == "proactive action complete"
    assert legacy.await_count == 1
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT source, status FROM execution_graphs ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert row == (ExecutionSource.AGENT_LOOP.value, GraphStatus.SUCCEEDED.value)


def test_workflow_graph_preserves_approval_wait_state(monkeypatch, tmp_path):
    db_path = _patch_db(monkeypatch, tmp_path)
    bus = _bus()

    async def wait_for_approval(params, context):
        return ActionResult.waiting_for_approval("Approve protected workflow step")

    bus.register_composite_action("needs_approval", wait_for_approval)
    monkeypatch.setattr(workflows, "get_execution_bus", lambda: bus)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    engine = WorkflowEngine(conn)
    engine.ensure_tables()
    workflow = Workflow(
        id="wf_approval",
        name="Approval workflow",
        trigger_type="signal",
        trigger_config={"signal_type": "test"},
        actions=[WorkflowStep("needs_approval")],
    )
    engine.register(workflow)

    results = _run(engine._execute_steps(workflow, {}))
    graph = ExecutionGraphRepository(conn).list_resumable_runs()[0]
    conn.close()

    assert results[0]["status"] == NodeStatus.WAITING_FOR_APPROVAL.value
    assert graph.status == GraphStatus.WAITING_FOR_APPROVAL
    assert graph.nodes[0].status == NodeStatus.WAITING_FOR_APPROVAL


def test_scheduler_registers_jobs_through_durable_wrapper(monkeypatch):
    import server

    class FakeScheduler:
        def __init__(self):
            self.jobs = []

        def add_job(self, func, trigger, *args, **kwargs):
            self.jobs.append((func, trigger, kwargs))
            return kwargs["id"]

    fake_scheduler = FakeScheduler()
    monkeypatch.setattr(server, "scheduler", fake_scheduler)
    monkeypatch.setattr(server, "db_conn", None)
    server._setup_scheduler()

    jobs = {kwargs["id"]: func for func, _, kwargs in fake_scheduler.jobs}
    assert {"morning_brief", "reminder_check", "dev_state_poll", "proactive_alerts"} <= jobs.keys()
    assert all(func.__name__ == "_durable_job" for func in jobs.values())
