"""Reusable budgets and termination state for bounded execution loops."""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable


class LoopTerminationReason(str, Enum):
    COMPLETED = "completed"
    ITERATION_BUDGET_EXHAUSTED = "iteration_budget_exhausted"
    ACTION_BUDGET_EXHAUSTED = "action_budget_exhausted"
    TIME_BUDGET_EXHAUSTED = "time_budget_exhausted"
    NO_PROGRESS = "no_progress"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True)
class LoopBudget:
    max_iterations: int
    max_actions: int | None = None
    max_elapsed_seconds: float | None = None
    max_no_progress_iterations: int | None = None

    def __post_init__(self) -> None:
        if self.max_iterations < 1:
            raise ValueError("max_iterations must be at least 1")
        if self.max_actions is not None and self.max_actions < 1:
            raise ValueError("max_actions must be at least 1")
        if self.max_elapsed_seconds is not None and self.max_elapsed_seconds <= 0:
            raise ValueError("max_elapsed_seconds must be positive")
        if (
            self.max_no_progress_iterations is not None
            and self.max_no_progress_iterations < 1
        ):
            raise ValueError("max_no_progress_iterations must be at least 1")


@dataclass(frozen=True)
class LoopSnapshot:
    iterations: int
    actions: int
    no_progress_iterations: int
    elapsed_seconds: float
    termination_reason: LoopTerminationReason | None


class BoundedLoopController:
    """Track loop work and stop it when a declared budget is exhausted."""

    def __init__(
        self,
        budget: LoopBudget,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.budget = budget
        self._monotonic = monotonic
        self._started_at = monotonic()
        self._iterations = 0
        self._actions = 0
        self._no_progress_iterations = 0
        self._last_progress_fingerprint: str | None = None
        self._termination_reason: LoopTerminationReason | None = None

    @classmethod
    def restore(
        cls,
        budget: LoopBudget,
        snapshot: LoopSnapshot,
        *,
        last_progress_fingerprint: str | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> "BoundedLoopController":
        """Rebuild a live controller from a durable checkpoint snapshot."""
        controller = cls(budget, monotonic=monotonic)
        controller._started_at = monotonic() - snapshot.elapsed_seconds
        controller._iterations = snapshot.iterations
        controller._actions = snapshot.actions
        controller._no_progress_iterations = snapshot.no_progress_iterations
        controller._last_progress_fingerprint = last_progress_fingerprint
        controller._termination_reason = snapshot.termination_reason
        return controller

    @property
    def termination_reason(self) -> LoopTerminationReason | None:
        return self._termination_reason

    @property
    def is_running(self) -> bool:
        return self._termination_reason is None

    def begin_iteration(self) -> bool:
        """Reserve one iteration after checking elapsed and iteration budgets."""
        if not self.is_running:
            return False
        if self._time_exhausted():
            self.terminate(LoopTerminationReason.TIME_BUDGET_EXHAUSTED)
            return False
        if self._iterations >= self.budget.max_iterations:
            self.terminate(LoopTerminationReason.ITERATION_BUDGET_EXHAUSTED)
            return False
        self._iterations += 1
        return True

    def reserve_actions(self, count: int) -> bool:
        """Atomically reserve a batch of actions without partially exceeding budget."""
        if count < 0:
            raise ValueError("action count cannot be negative")
        if not self.is_running:
            return False
        if self._time_exhausted():
            self.terminate(LoopTerminationReason.TIME_BUDGET_EXHAUSTED)
            return False
        if (
            self.budget.max_actions is not None
            and self._actions + count > self.budget.max_actions
        ):
            self.terminate(LoopTerminationReason.ACTION_BUDGET_EXHAUSTED)
            return False
        self._actions += count
        return True

    def observe_progress(self, fingerprint: str | None) -> bool:
        """Stop after the same meaningful state repeats without progress."""
        if not self.is_running:
            return False
        if self._time_exhausted():
            self.terminate(LoopTerminationReason.TIME_BUDGET_EXHAUSTED)
            return False

        normalized = (fingerprint or "").strip()
        if normalized and normalized != self._last_progress_fingerprint:
            self._last_progress_fingerprint = normalized
            self._no_progress_iterations = 0
            return True

        self._no_progress_iterations += 1
        if (
            self.budget.max_no_progress_iterations is not None
            and self._no_progress_iterations >= self.budget.max_no_progress_iterations
        ):
            self.terminate(LoopTerminationReason.NO_PROGRESS)
            return False
        return True

    def complete(self) -> None:
        self.terminate(LoopTerminationReason.COMPLETED)

    def terminate(self, reason: LoopTerminationReason) -> None:
        """Record the first terminal reason; later cleanup cannot overwrite it."""
        if self._termination_reason is None:
            self._termination_reason = LoopTerminationReason(reason)

    def ensure_iteration_termination(self) -> LoopTerminationReason:
        """Classify natural loop exhaustion when a for-loop reaches its end."""
        if self._termination_reason is None:
            self.terminate(LoopTerminationReason.ITERATION_BUDGET_EXHAUSTED)
        assert self._termination_reason is not None
        return self._termination_reason

    def snapshot(self) -> LoopSnapshot:
        return LoopSnapshot(
            iterations=self._iterations,
            actions=self._actions,
            no_progress_iterations=self._no_progress_iterations,
            elapsed_seconds=max(0.0, self._monotonic() - self._started_at),
            termination_reason=self._termination_reason,
        )

    def _time_exhausted(self) -> bool:
        return (
            self.budget.max_elapsed_seconds is not None
            and self._monotonic() - self._started_at
            >= self.budget.max_elapsed_seconds
        )
