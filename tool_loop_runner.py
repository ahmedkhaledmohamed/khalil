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
        self._checkpoint(
            LoopCheckpointBoundary.BEFORE_SYNTHESIS,
            messages,
            phase=phase,
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
        self._checkpoint(
            LoopCheckpointBoundary.TERMINAL,
            messages,
            phase=phase,
            status=(ToolLoopStatus.SUCCEEDED if succeeded else ToolLoopStatus.FAILED),
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
