"""Behavioral tests for bounded execution-loop termination."""

from loop_controller import (
    BoundedLoopController,
    LoopBudget,
    LoopTerminationReason,
)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_iteration_budget_stops_natural_loop_exhaustion():
    loop = BoundedLoopController(LoopBudget(max_iterations=2))

    assert loop.begin_iteration() is True
    assert loop.begin_iteration() is True
    assert loop.begin_iteration() is False
    assert loop.termination_reason is LoopTerminationReason.ITERATION_BUDGET_EXHAUSTED
    assert loop.snapshot().iterations == 2


def test_action_budget_reserves_batches_atomically():
    loop = BoundedLoopController(LoopBudget(max_iterations=3, max_actions=3))

    assert loop.reserve_actions(2) is True
    assert loop.reserve_actions(2) is False
    assert loop.termination_reason is LoopTerminationReason.ACTION_BUDGET_EXHAUSTED
    assert loop.snapshot().actions == 2


def test_elapsed_time_budget_stops_before_more_work():
    clock = FakeClock()
    loop = BoundedLoopController(
        LoopBudget(max_iterations=3, max_elapsed_seconds=5),
        monotonic=clock,
    )

    assert loop.begin_iteration() is True
    clock.now = 5.0

    assert loop.reserve_actions(1) is False
    assert loop.termination_reason is LoopTerminationReason.TIME_BUDGET_EXHAUSTED
    assert loop.snapshot().actions == 0


def test_repeated_state_stops_after_no_progress_budget():
    loop = BoundedLoopController(
        LoopBudget(max_iterations=5, max_no_progress_iterations=2)
    )

    assert loop.observe_progress("state-one") is True
    assert loop.observe_progress("state-one") is True
    assert loop.observe_progress("state-one") is False
    assert loop.termination_reason is LoopTerminationReason.NO_PROGRESS
    assert loop.snapshot().no_progress_iterations == 2


def test_new_state_resets_no_progress_count():
    loop = BoundedLoopController(
        LoopBudget(max_iterations=5, max_no_progress_iterations=2)
    )

    loop.observe_progress("state-one")
    loop.observe_progress("state-one")
    assert loop.snapshot().no_progress_iterations == 1

    assert loop.observe_progress("state-two") is True
    assert loop.snapshot().no_progress_iterations == 0


def test_first_terminal_reason_is_preserved():
    loop = BoundedLoopController(LoopBudget(max_iterations=1))

    loop.terminate(LoopTerminationReason.CANCELLED)
    loop.complete()

    assert loop.termination_reason is LoopTerminationReason.CANCELLED
