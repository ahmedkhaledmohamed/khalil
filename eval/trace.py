"""Structured routing trace capture for eval pipeline.

Uses learning.py signal hooks to capture which routing stage handled each query
during eval runs, eliminating brittle latency-based path inference.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass
class Trace:
    """Routing trace captured during a single case execution."""
    matched_path: str | None = None    # "direct_shell" | "skill_pattern" | "llm_intent" | "conversational"
    matched_action: str | None = None  # the action_type that was dispatched
    handler_name: str | None = None    # handler function that was called
    events: list[dict] = field(default_factory=list)  # all trace events


# Thread-local storage for the active trace (one per concurrent case)
_local = threading.local()


def _get_active_trace() -> Trace | None:
    """Get the active trace for the current thread, if any."""
    return getattr(_local, "trace", None)


def emit_trace(stage: str, action: str | None = None, handler: str | None = None, **extra):
    """Emit a trace event. Called from server.py routing decision points.

    No-op if no trace is active (i.e., not running under eval).
    """
    trace = _get_active_trace()
    if trace is None:
        return

    event = {"stage": stage, "action": action, "handler": handler, **extra}
    trace.events.append(event)

    # First match wins for matched_path/matched_action
    if trace.matched_path is None and stage in ("direct_shell", "skill_pattern", "llm_intent"):
        trace.matched_path = stage
        trace.matched_action = action
    if handler and trace.handler_name is None:
        trace.handler_name = handler


@contextmanager
def capture_trace():
    """Context manager that captures routing trace during a case execution.

    Usage:
        with capture_trace() as trace:
            await server_mod.handle_message_generic(ctx)
        print(trace.matched_path)  # "direct_shell" | "skill_pattern" | etc.
    """
    trace = Trace()
    _local.trace = trace
    try:
        yield trace
    finally:
        # If no routing stage matched, it fell through to conversational
        if trace.matched_path is None and trace.events == []:
            trace.matched_path = "conversational"
        _local.trace = None
