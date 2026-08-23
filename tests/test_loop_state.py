"""Behavioral tests for durable foreground tool-loop state."""

import sqlite3

import pytest

from config import ActionType
from loop_controller import LoopBudget, LoopTerminationReason
from loop_state import (
    LoopCheckpointBoundary,
    PendingToolAction,
    RecoveryDisposition,
    ToolLoopCheckpoint,
    ToolLoopRepository,
    ToolLoopRun,
    ToolLoopStatus,
    classify_recovery,
)


@pytest.fixture
def repository():
    conn = sqlite3.connect(":memory:")
    repo = ToolLoopRepository(conn)
    repo.ensure_schema()
    yield repo
    conn.close()


def _run(run_id="tool_loop_test"):
    return ToolLoopRun(
        id=run_id,
        chat_id=42,
        query="prepare tomorrow's meeting brief",
        model="taskforce/default",
        budget=LoopBudget(
            max_iterations=12,
            max_actions=24,
            max_elapsed_seconds=240,
            max_no_progress_iterations=3,
        ),
    )


def test_run_creation_round_trips_budget_and_initial_checkpoint(repository):
    created = repository.create_run(_run())
    loaded = repository.load_run(created.id)

    assert loaded is not None
    assert loaded.chat_id == "42"
    assert loaded.status is ToolLoopStatus.PENDING
    assert loaded.budget.max_actions == 24
    assert loaded.latest_checkpoint.boundary is LoopCheckpointBoundary.CREATED
    assert loaded.latest_checkpoint.sequence == 1
    assert loaded.created_at is not None


def test_checkpoint_round_trips_resume_state(repository):
    repository.create_run(_run())
    checkpoint = ToolLoopCheckpoint(
        boundary=LoopCheckpointBoundary.AFTER_ACTIONS,
        messages=[{"role": "tool", "content": "calendar result"}],
        iteration_count=2,
        action_count=3,
        no_progress_count=1,
        elapsed_seconds=12.5,
        phase={"has_called_action": True},
        progress_fingerprint="calendar-result",
    )

    updated = repository.append_checkpoint(
        "tool_loop_test", checkpoint, status=ToolLoopStatus.RUNNING,
    )

    assert updated.status is ToolLoopStatus.RUNNING
    assert updated.latest_checkpoint.sequence == 2
    assert updated.latest_checkpoint.messages[0]["content"] == "calendar result"
    assert updated.latest_checkpoint.iteration_count == 2
    assert updated.latest_checkpoint.action_count == 3
    assert updated.latest_checkpoint.phase == {"has_called_action": True}
    assert updated.latest_checkpoint.progress_fingerprint == "calendar-result"


def test_pending_actions_preserve_risk_and_idempotency(repository):
    repository.create_run(_run())
    action = PendingToolAction(
        id="call_1",
        name="generate_file",
        arguments='{"target_path":"brief.md"}',
        action_type=ActionType.WRITE,
        idempotency_key="tool_loop_test:call_1",
    )

    updated = repository.append_checkpoint(
        "tool_loop_test",
        ToolLoopCheckpoint(
            boundary=LoopCheckpointBoundary.BEFORE_ACTIONS,
            pending_actions=[action],
        ),
        status=ToolLoopStatus.RUNNING,
    )

    restored = updated.latest_checkpoint.pending_actions[0]
    assert restored.action_type is ActionType.WRITE
    assert restored.idempotency_key == "tool_loop_test:call_1"


def test_interrupted_read_batch_is_safe_to_resume(repository):
    repository.create_run(_run())
    run = repository.append_checkpoint(
        "tool_loop_test",
        ToolLoopCheckpoint(
            boundary=LoopCheckpointBoundary.BEFORE_ACTIONS,
            pending_actions=[PendingToolAction(
                id="call_1",
                name="search_knowledge",
                arguments='{"query":"meeting"}',
                action_type=ActionType.READ,
            )],
        ),
        status=ToolLoopStatus.RUNNING,
    )

    recovery = classify_recovery(run)

    assert recovery.disposition is RecoveryDisposition.RESUME


@pytest.mark.parametrize("action_type", [ActionType.WRITE, ActionType.DANGEROUS])
def test_interrupted_side_effect_requires_review(repository, action_type):
    repository.create_run(_run())
    run = repository.append_checkpoint(
        "tool_loop_test",
        ToolLoopCheckpoint(
            boundary=LoopCheckpointBoundary.BEFORE_ACTIONS,
            pending_actions=[PendingToolAction(
                id="call_1",
                name="side_effect",
                arguments="{}",
                action_type=action_type,
            )],
        ),
        status=ToolLoopStatus.RUNNING,
    )

    recovery = classify_recovery(run)

    assert recovery.disposition is RecoveryDisposition.REVIEW_REQUIRED
    assert "side_effect" in recovery.reason


def test_approval_boundary_and_status_are_atomic(repository):
    repository.create_run(_run())
    repository.append_checkpoint(
        "tool_loop_test",
        ToolLoopCheckpoint(boundary=LoopCheckpointBoundary.BEFORE_MODEL),
        status=ToolLoopStatus.RUNNING,
    )

    waiting = repository.append_checkpoint(
        "tool_loop_test",
        ToolLoopCheckpoint(boundary=LoopCheckpointBoundary.WAITING_FOR_APPROVAL),
        status=ToolLoopStatus.WAITING_FOR_APPROVAL,
    )

    assert classify_recovery(waiting).disposition is RecoveryDisposition.WAIT_FOR_APPROVAL
    with pytest.raises(ValueError, match="persisted together"):
        repository.append_checkpoint(
            "tool_loop_test",
            ToolLoopCheckpoint(boundary=LoopCheckpointBoundary.BEFORE_MODEL),
        )


def test_terminal_checkpoint_excludes_run_from_recovery(repository):
    repository.create_run(_run())
    repository.append_checkpoint(
        "tool_loop_test",
        ToolLoopCheckpoint(boundary=LoopCheckpointBoundary.BEFORE_MODEL),
        status=ToolLoopStatus.RUNNING,
    )

    completed = repository.append_checkpoint(
        "tool_loop_test",
        ToolLoopCheckpoint(boundary=LoopCheckpointBoundary.TERMINAL),
        status=ToolLoopStatus.SUCCEEDED,
        termination_reason=LoopTerminationReason.COMPLETED,
    )

    assert completed.completed_at is not None
    assert completed.termination_reason is LoopTerminationReason.COMPLETED
    assert classify_recovery(completed).disposition is RecoveryDisposition.TERMINAL
    assert repository.list_recoverable_runs() == []
    with pytest.raises(ValueError, match="terminal"):
        repository.append_checkpoint(
            "tool_loop_test",
            ToolLoopCheckpoint(boundary=LoopCheckpointBoundary.BEFORE_MODEL),
        )


def test_terminal_state_requires_terminal_boundary(repository):
    repository.create_run(_run())

    with pytest.raises(ValueError, match="Terminal state"):
        repository.append_checkpoint(
            "tool_loop_test",
            ToolLoopCheckpoint(boundary=LoopCheckpointBoundary.BEFORE_MODEL),
            status=ToolLoopStatus.CANCELLED,
            termination_reason=LoopTerminationReason.CANCELLED,
        )


def test_recoverable_runs_are_returned_oldest_first(repository):
    repository.create_run(_run("run_one"))
    repository.create_run(_run("run_two"))
    repository.append_checkpoint(
        "run_two",
        ToolLoopCheckpoint(boundary=LoopCheckpointBoundary.BEFORE_MODEL),
        status=ToolLoopStatus.RUNNING,
    )

    recoverable = repository.list_recoverable_runs()

    assert [run.id for run in recoverable] == ["run_one", "run_two"]
