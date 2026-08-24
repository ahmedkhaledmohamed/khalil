"""Behavioral tests for durable foreground tool-loop coordination."""

import asyncio
import sqlite3
from types import SimpleNamespace

from config import ActionType
from loop_controller import LoopBudget, LoopTerminationReason
from loop_state import (
    LoopCheckpointBoundary,
    PendingToolAction,
    ToolLoopRepository,
    ToolLoopRun,
    ToolLoopStatus,
)
from tool_loop_runner import DurableToolLoopRunner


def _runner(budget=None):
    conn = sqlite3.connect(":memory:")
    repository = ToolLoopRepository(conn)
    repository.ensure_schema()
    run = repository.create_run(ToolLoopRun(
        id="tool_loop_runner_test",
        chat_id=42,
        query="prepare a brief",
        model="taskforce/default",
        budget=budget or LoopBudget(
            max_iterations=4,
            max_actions=4,
            max_elapsed_seconds=60,
            max_no_progress_iterations=2,
        ),
    ))
    return conn, DurableToolLoopRunner(repository, run)


def _action(action_type=ActionType.READ):
    return PendingToolAction(
        id="call_1",
        name="search_knowledge" if action_type is ActionType.READ else "generate_file",
        arguments="{}",
        action_type=action_type,
    )


def test_iteration_and_model_boundaries_are_checkpointed():
    conn, runner = _runner()
    messages = [{"role": "user", "content": "prepare a brief"}]

    assert runner.begin_iteration(messages, phase={"total_research": 0}) is True
    assert runner.run.status is ToolLoopStatus.RUNNING
    assert runner.run.latest_checkpoint.boundary is LoopCheckpointBoundary.BEFORE_MODEL
    assert runner.run.latest_checkpoint.iteration_count == 1

    runner.after_model(
        messages + [{"role": "assistant", "content": ""}],
        pending_actions=[_action()],
        phase={"total_research": 1},
    )

    assert runner.run.latest_checkpoint.boundary is LoopCheckpointBoundary.AFTER_MODEL
    assert runner.run.latest_checkpoint.pending_actions[0].name == "search_knowledge"
    assert runner.run.latest_checkpoint.phase == {"total_research": 1}
    conn.close()


def test_action_batch_is_checkpointed_before_and_after_execution():
    conn, runner = _runner()
    messages = [{"role": "user", "content": "prepare a brief"}]
    runner.begin_iteration(messages)

    assert runner.reserve_actions([_action()], messages) is True
    assert runner.run.latest_checkpoint.boundary is LoopCheckpointBoundary.BEFORE_ACTIONS
    assert runner.run.latest_checkpoint.action_count == 1

    messages.append({"role": "tool", "content": "result"})
    assert runner.after_actions(messages, "result-one") is True
    assert runner.run.latest_checkpoint.boundary is LoopCheckpointBoundary.AFTER_ACTIONS
    assert runner.run.latest_checkpoint.progress_fingerprint == "result-one"
    assert runner.run.latest_checkpoint.messages[-1]["content"] == "result"
    conn.close()


def test_repeated_model_state_stops_the_bounded_loop():
    conn, runner = _runner()
    messages = [{"role": "assistant", "content": "Let me research that"}]

    assert runner.observe_model_progress(messages, "same-preamble") is True
    assert runner.observe_model_progress(messages, "same-preamble") is True
    assert runner.observe_model_progress(messages, "same-preamble") is False

    assert runner.termination_reason is LoopTerminationReason.NO_PROGRESS
    assert runner.run.latest_checkpoint.no_progress_count == 2
    conn.close()


def test_successful_synthesis_preserves_budget_termination_reason():
    conn, runner = _runner(LoopBudget(max_iterations=2, max_actions=1))
    messages = [{"role": "user", "content": "prepare a brief"}]
    runner.begin_iteration(messages)

    assert runner.reserve_actions([_action(), _action()], messages) is False
    assert runner.termination_reason is LoopTerminationReason.ACTION_BUDGET_EXHAUSTED

    runner.before_synthesis(messages)
    runner.terminate(
        messages + [{"role": "assistant", "content": "partial answer"}],
        succeeded=True,
    )

    assert runner.run.status is ToolLoopStatus.SUCCEEDED
    assert runner.run.termination_reason is LoopTerminationReason.ACTION_BUDGET_EXHAUSTED
    assert runner.run.latest_checkpoint.boundary is LoopCheckpointBoundary.TERMINAL
    conn.close()


def test_failed_loop_is_terminal_and_cannot_be_overwritten():
    conn, runner = _runner()
    messages = [{"role": "user", "content": "prepare a brief"}]
    runner.begin_iteration(messages)

    runner.terminate(messages, succeeded=False, reason=LoopTerminationReason.FAILED)
    runner.terminate(messages, succeeded=True)

    assert runner.run.status is ToolLoopStatus.FAILED
    assert runner.run.termination_reason is LoopTerminationReason.FAILED
    conn.close()


def test_live_server_loop_persists_completed_run(monkeypatch, tmp_path):
    import config
    import evolution
    import intent
    import learning
    import model_router
    import server
    import skills
    import task_manager
    import tool_catalog

    database = tmp_path / "khalil.db"

    class FakeRegistry:
        def get_action_type(self, _name):
            return ActionType.READ

    class FakeCompletions:
        async def create(self, **_kwargs):
            message = SimpleNamespace(content="Completed response", tool_calls=None)
            choice = SimpleNamespace(message=message, finish_reason="stop")
            return SimpleNamespace(choices=[choice], usage=None)

    class FakeCircuitBreaker:
        def is_open(self):
            return False

        def record_success(self):
            pass

        def record_failure(self):
            pass

    class FakeTaskManager:
        def get_active_task(self, _chat_id):
            return None

    class FakeProgress:
        async def edit(self, _text):
            pass

    async def no_reflection(*_args, **_kwargs):
        return None

    monkeypatch.setattr(config, "DB_PATH", database)
    monkeypatch.setattr(skills, "get_registry", lambda: FakeRegistry())
    monkeypatch.setattr(tool_catalog, "generate_tool_schemas", lambda _registry: [{
        "type": "function",
        "function": {"name": "search_knowledge", "parameters": {}},
    }])
    monkeypatch.setattr(tool_catalog, "filter_tools_for_query", lambda *_args: _args[2])
    monkeypatch.setattr(model_router, "route_query", lambda _query: (None, "taskforce/default"))
    monkeypatch.setattr(intent, "is_artifact_request", lambda _query: False)
    monkeypatch.setattr(learning, "get_active_response_preferences", lambda: "")
    monkeypatch.setattr(learning, "record_signal", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(evolution, "post_interaction_check", no_reflection)
    monkeypatch.setattr(task_manager, "TaskManager", FakeTaskManager)
    monkeypatch.setattr(server, "_build_system_prompt", lambda *_args: "system")
    monkeypatch.setattr(server, "get_conversation_history", lambda _chat_id: "")
    monkeypatch.setattr(server, "_check_summarization_needed", lambda _chat_id: None)
    monkeypatch.setattr(server, "_cb_claude_fg", FakeCircuitBreaker())
    monkeypatch.setattr(
        server,
        "_gateway_client",
        SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions())),
    )

    result = asyncio.run(server.call_llm_with_tools(
        "prepare a brief",
        "context",
        42,
        FakeProgress(),
        SimpleNamespace(),
    ))

    assert result == "Completed response"
    conn = sqlite3.connect(database)
    repository = ToolLoopRepository(conn)
    run_id = conn.execute("SELECT id FROM tool_loop_runs").fetchone()[0]
    persisted = repository.load_run(run_id)
    assert persisted.status is ToolLoopStatus.SUCCEEDED
    assert persisted.termination_reason is LoopTerminationReason.COMPLETED
    assert persisted.latest_checkpoint.boundary is LoopCheckpointBoundary.TERMINAL
    assert persisted.latest_checkpoint.messages[-1]["content"] == "Completed response"
    conn.close()
