"""Durable lifecycle coordinator for Khalil's foreground tool-use loop."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from loop_controller import (
    BoundedLoopController,
    LoopBudget,
    LoopSnapshot,
    LoopTerminationReason,
)
from loop_state import (
    LoopCheckpointBoundary,
    PendingToolAction,
    ToolLoopCheckpoint,
    ToolLoopRepository,
    ToolLoopRun,
    ToolLoopStatus,
)


class DurableToolLoopRunner:
    """Own bounded-loop state and persist every recoverable boundary."""

    def __init__(
        self,
        repository: ToolLoopRepository,
        run: ToolLoopRun,
        *,
        controller: BoundedLoopController | None = None,
        owned_connection: sqlite3.Connection | None = None,
    ) -> None:
        self.repository = repository
        self.run = run
        self.controller = controller or BoundedLoopController(run.budget)
        self._owned_connection = owned_connection

    @classmethod
    def create(
        cls,
        database: str | Path,
        *,
        chat_id: int | str,
        query: str,
        model: str,
        budget: LoopBudget,
    ) -> "DurableToolLoopRunner":
        conn = sqlite3.connect(str(database))
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        repository = ToolLoopRepository(conn)
        repository.ensure_schema()
        run = repository.create_run(ToolLoopRun(
            chat_id=chat_id,
            query=query,
            model=model,
            budget=budget,
        ))
        return cls(repository, run, owned_connection=conn)

    @classmethod
    def resume(
        cls,
        database: str | Path,
        run_id: str,
    ) -> "DurableToolLoopRunner":
        """Open a recoverable run and restore its bounded-loop counters."""
        conn = sqlite3.connect(str(database))
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        repository = ToolLoopRepository(conn)
        repository.ensure_schema()
        run = repository.load_run(run_id)
        if run is None:
            conn.close()
            raise KeyError(f"Unknown tool loop run {run_id!r}")
        if run.status in {
            ToolLoopStatus.SUCCEEDED,
            ToolLoopStatus.FAILED,
            ToolLoopStatus.CANCELLED,
        }:
            conn.close()
            raise ValueError(f"Cannot resume terminal tool loop {run_id!r}")

        checkpoint = run.latest_checkpoint
        recovered_reason = None
        if checkpoint and checkpoint.phase.get("_loop_termination_reason"):
            recovered_reason = LoopTerminationReason(
                checkpoint.phase["_loop_termination_reason"]
            )
        snapshot = LoopSnapshot(
            iterations=checkpoint.iteration_count if checkpoint else 0,
            actions=checkpoint.action_count if checkpoint else 0,
            no_progress_iterations=(checkpoint.no_progress_count if checkpoint else 0),
            elapsed_seconds=checkpoint.elapsed_seconds if checkpoint else 0.0,
            termination_reason=recovered_reason,
        )
        controller = BoundedLoopController.restore(
            run.budget,
            snapshot,
            last_progress_fingerprint=(
                checkpoint.progress_fingerprint if checkpoint else None
            ),
        )
        return cls(
            repository,
            run,
            controller=controller,
            owned_connection=conn,
        )

    @property
    def id(self) -> str:
        return self.run.id

    @property
    def termination_reason(self) -> LoopTerminationReason | None:
        return self.controller.termination_reason

    def snapshot(self) -> LoopSnapshot:
        return self.controller.snapshot()

    def stop(self, reason: LoopTerminationReason) -> None:
        """Stop further loop work while allowing final synthesis to checkpoint."""
        self.controller.terminate(reason)

    def ensure_iteration_termination(self) -> LoopTerminationReason:
        return self.controller.ensure_iteration_termination()

    def begin_iteration(
        self,
        messages: list[dict[str, Any]],
        *,
        phase: dict[str, Any] | None = None,
    ) -> bool:
        if not self.controller.begin_iteration():
            return False
        self._checkpoint(
            LoopCheckpointBoundary.BEFORE_MODEL,
            messages,
            phase=phase,
            status=ToolLoopStatus.RUNNING,
        )
        return True

    def after_model(
        self,
        messages: list[dict[str, Any]],
        *,
        pending_actions: list[PendingToolAction] | None = None,
        phase: dict[str, Any] | None = None,
    ) -> None:
        self._checkpoint(
            LoopCheckpointBoundary.AFTER_MODEL,
            messages,
            pending_actions=pending_actions,
            phase=phase,
        )

    def reserve_actions(
        self,
        actions: list[PendingToolAction],
        messages: list[dict[str, Any]],
        *,
        phase: dict[str, Any] | None = None,
    ) -> bool:
        if not self.controller.reserve_actions(len(actions)):
            return False
        self._checkpoint(
            LoopCheckpointBoundary.BEFORE_ACTIONS,
            messages,
            pending_actions=actions,
            phase=phase,
        )
        return True

    def after_actions(
        self,
        messages: list[dict[str, Any]],
        progress_fingerprint: str,
        *,
        phase: dict[str, Any] | None = None,
    ) -> bool:
        made_progress = self.controller.observe_progress(progress_fingerprint)
        self._checkpoint(
            LoopCheckpointBoundary.AFTER_ACTIONS,
            messages,
            phase=phase,
            progress_fingerprint=progress_fingerprint,
        )
        return made_progress

    def observe_model_progress(
        self,
        messages: list[dict[str, Any]],
        progress_fingerprint: str,
        *,
        phase: dict[str, Any] | None = None,
    ) -> bool:
        made_progress = self.controller.observe_progress(progress_fingerprint)
        self._checkpoint(
            LoopCheckpointBoundary.AFTER_MODEL,
            messages,
            phase=phase,
            progress_fingerprint=progress_fingerprint,
        )
        return made_progress

    def before_synthesis(
        self,
        messages: list[dict[str, Any]],
        *,
        phase: dict[str, Any] | None = None,
    ) -> None:
        durable_phase = dict(phase or {})
        if self.controller.termination_reason is not None:
            durable_phase["_loop_termination_reason"] = (
                self.controller.termination_reason.value
            )
        self._checkpoint(
            LoopCheckpointBoundary.BEFORE_SYNTHESIS,
            messages,
            phase=durable_phase,
        )

    def wait_for_approval(self) -> None:
        """Pause an ambiguous action batch at a durable approval boundary."""
        checkpoint = self.run.latest_checkpoint
        if checkpoint is None or not checkpoint.pending_actions:
            raise ValueError("Approval waits require pending actions")
        if checkpoint.boundary is LoopCheckpointBoundary.AFTER_MODEL:
            if not self.controller.reserve_actions(len(checkpoint.pending_actions)):
                raise ValueError("Pending actions exceed the remaining loop budget")
        self._checkpoint(
            LoopCheckpointBoundary.WAITING_FOR_APPROVAL,
            checkpoint.messages,
            pending_actions=checkpoint.pending_actions,
            phase=checkpoint.phase,
            progress_fingerprint=checkpoint.progress_fingerprint,
            status=ToolLoopStatus.WAITING_FOR_APPROVAL,
        )

    def resume_after_approval(self) -> None:
        """Return an explicitly approved action batch to its execution boundary."""
        checkpoint = self.run.latest_checkpoint
        if (
            self.run.status is not ToolLoopStatus.WAITING_FOR_APPROVAL
            or checkpoint is None
        ):
            raise ValueError("Tool loop is not waiting for approval")
        self._checkpoint(
            LoopCheckpointBoundary.BEFORE_ACTIONS,
            checkpoint.messages,
            pending_actions=checkpoint.pending_actions,
            phase=checkpoint.phase,
            progress_fingerprint=checkpoint.progress_fingerprint,
            status=ToolLoopStatus.RUNNING,
        )

    def cancel(self) -> None:
        """Persist an explicit cancellation as a terminal checkpoint."""
        checkpoint = self.run.latest_checkpoint
        self.terminate(
            checkpoint.messages if checkpoint else [],
            succeeded=False,
            reason=LoopTerminationReason.CANCELLED,
            phase=checkpoint.phase if checkpoint else None,
        )

    def terminate(
        self,
        messages: list[dict[str, Any]],
        *,
        succeeded: bool,
        reason: LoopTerminationReason | None = None,
        phase: dict[str, Any] | None = None,
    ) -> None:
        if self.run.status in {
            ToolLoopStatus.SUCCEEDED,
            ToolLoopStatus.FAILED,
            ToolLoopStatus.CANCELLED,
        }:
            return
        if reason is not None:
            self.controller.terminate(reason)
        elif succeeded:
            self.controller.complete()
        else:
            self.controller.terminate(LoopTerminationReason.FAILED)
        terminal_reason = self.controller.termination_reason
        assert terminal_reason is not None
        if terminal_reason is LoopTerminationReason.CANCELLED:
            status = ToolLoopStatus.CANCELLED
        else:
            status = ToolLoopStatus.SUCCEEDED if succeeded else ToolLoopStatus.FAILED
        self._checkpoint(
            LoopCheckpointBoundary.TERMINAL,
            messages,
            phase=phase,
            status=status,
            termination_reason=terminal_reason,
        )

    def close(self) -> None:
        if self._owned_connection is not None:
            self._owned_connection.close()
            self._owned_connection = None

    def _checkpoint(
        self,
        boundary: LoopCheckpointBoundary,
        messages: list[dict[str, Any]],
        *,
        pending_actions: list[PendingToolAction] | None = None,
        phase: dict[str, Any] | None = None,
        progress_fingerprint: str | None = None,
        status: ToolLoopStatus | None = None,
        termination_reason: LoopTerminationReason | None = None,
    ) -> None:
        state = self.controller.snapshot()
        checkpoint = ToolLoopCheckpoint(
            boundary=boundary,
            messages=messages,
            pending_actions=pending_actions or [],
            iteration_count=state.iterations,
            action_count=state.actions,
            no_progress_count=state.no_progress_iterations,
            elapsed_seconds=state.elapsed_seconds,
            phase=phase or {},
            progress_fingerprint=progress_fingerprint,
        )
        self.run = self.repository.append_checkpoint(
            self.run.id,
            checkpoint,
            status=status,
            termination_reason=termination_reason,
        )
