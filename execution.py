"""Unified Execution Bus — central dispatcher for all subsystem actions.

All subsystems (agent loop, orchestrator, workflows, tool-use loop) route actions
through this bus. This enables composability: a workflow step can trigger an
orchestrated plan, an orchestrated step can use the tool-use loop, etc.

Every execution is audited with correct source attribution.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Awaitable

from config import AutonomyLevel

log = logging.getLogger("khalil.execution")

# Maximum recursion depth to prevent infinite loops
MAX_EXECUTION_DEPTH = 3


class ExecutionSource(str, Enum):
    USER = "user"
    AGENT_LOOP = "agent_loop"
    WORKFLOW = "workflow"
    ORCHESTRATOR = "orchestrator"
    TOOL_USE = "tool_use"
    TEMPORAL = "temporal"
    BACKGROUND_AGENT = "background_agent"


@dataclass
class ExecutionContext:
    """Context passed through every execution, enabling traceability and recursion control."""
    source: ExecutionSource
    autonomy_override: AutonomyLevel | None = None
    parent_plan_id: str | None = None
    depth: int = 0
    prior_results: dict[str, str] = field(default_factory=dict)
    chat_id: int | None = None
    # Metadata for audit trail
    trigger_id: str | None = None  # workflow_id, plan_id, etc.

    def child(self, source: ExecutionSource, **overrides) -> ExecutionContext:
        """Create a child context with incremented depth."""
        return ExecutionContext(
            source=source,
            autonomy_override=overrides.get("autonomy_override", self.autonomy_override),
            parent_plan_id=overrides.get("parent_plan_id", self.parent_plan_id),
            depth=self.depth + 1,
            prior_results=overrides.get("prior_results", dict(self.prior_results)),
            chat_id=overrides.get("chat_id", self.chat_id),
            trigger_id=overrides.get("trigger_id", self.trigger_id),
        )


@dataclass
class ExecutionResult:
    """Result of any execution through the bus."""
    success: bool
    output: str
    side_effects: list[str] = field(default_factory=list)
    error: str | None = None
    latency_ms: float = 0.0
    action: str = ""
    source: str = ""


class ExecutionBus:
    """Central dispatcher that all subsystems route actions through.

    Provides:
    - Unified dispatch via SkillRegistry handlers
    - Autonomy checks via AutonomyController
    - Audit logging with source attribution
    - Recursion depth guards
    - Signal recording for learning
    """

    def __init__(
        self,
        get_registry_fn: Callable,
        autonomy_controller: Any,
        ask_llm_fn: Callable[..., Awaitable[str]] | None = None,
    ):
        self._get_registry = get_registry_fn
        self._autonomy = autonomy_controller
        self._ask_llm = ask_llm_fn
        # Pluggable action handlers for composite actions (M8: layer composition)
        self._composite_handlers: dict[str, Callable] = {}

    def register_composite_action(self, action_type: str, handler: Callable):
        """Register a handler for composite action types (orchestrate, tool_reason, workflow)."""
        self._composite_handlers[action_type] = handler

    async def execute(
        self,
        action: str,
        params: dict,
        context: ExecutionContext,
    ) -> ExecutionResult:
        """Execute an action through the bus.

        Routes through: depth check → autonomy check → handler lookup → execute → audit.
        """
        t0 = time.monotonic()

        # Depth guard
        if context.depth > MAX_EXECUTION_DEPTH:
            return ExecutionResult(
                success=False,
                output="",
                error=f"Max execution depth ({MAX_EXECUTION_DEPTH}) exceeded",
                action=action,
                source=context.source.value,
            )

        # Check composite handlers first (M8: orchestrate, tool_reason, workflow)
        if action in self._composite_handlers:
            try:
                result = await self._composite_handlers[action](params, context)
                elapsed = (time.monotonic() - t0) * 1000
                result.latency_ms = elapsed
                result.action = action
                result.source = context.source.value
                self._audit(action, params, context, result)
                self._record_signal(action, context, result)
                return result
            except Exception as e:
                elapsed = (time.monotonic() - t0) * 1000
                result = ExecutionResult(
                    success=False, output="", error=str(e)[:500],
                    latency_ms=elapsed, action=action, source=context.source.value,
                )
                self._audit(action, params, context, result)
                self._record_signal(action, context, result)
                return result

        # Autonomy check
        effective_autonomy = context.autonomy_override or (
            self._autonomy.level if self._autonomy else AutonomyLevel.SUPERVISED
        )
        if self._autonomy and self._autonomy.needs_approval(action):
            # For non-user sources, check if autonomy allows auto-execution
            if context.source != ExecutionSource.USER:
                if effective_autonomy == AutonomyLevel.SUPERVISED:
                    return ExecutionResult(
                        success=False,
                        output="",
                        error=f"Action '{action}' requires approval (supervised mode)",
                        action=action,
                        source=context.source.value,
                        latency_ms=(time.monotonic() - t0) * 1000,
                    )

        # Rate limit check
        if self._autonomy:
            allowed, reason = self._autonomy.check_rate_limit(action)
            if not allowed:
                return ExecutionResult(
                    success=False, output="", error=reason,
                    action=action, source=context.source.value,
                    latency_ms=(time.monotonic() - t0) * 1000,
                )

        # Look up handler from skill registry
        registry = self._get_registry()
        handler = registry.get_handler(action)
        if handler is None:
            return ExecutionResult(
                success=False, output="",
                error=f"No handler found for '{action}'",
                action=action, source=context.source.value,
                latency_ms=(time.monotonic() - t0) * 1000,
            )

        # Build intent dict matching existing handler signature
        intent = {"action": action, **params}

        # Create a capture context for the handler
        capture_ctx = _BusCaptureContext()

        try:
            await asyncio.wait_for(
                handler(action, intent, capture_ctx),
                timeout=60,
            )
            elapsed = (time.monotonic() - t0) * 1000
            output = capture_ctx.get_result()
            result = ExecutionResult(
                success=True, output=output,
                side_effects=capture_ctx.side_effects,
                latency_ms=elapsed, action=action, source=context.source.value,
            )
        except asyncio.TimeoutError:
            elapsed = (time.monotonic() - t0) * 1000
            result = ExecutionResult(
                success=False, output="", error=f"{action} timed out after 60s",
                latency_ms=elapsed, action=action, source=context.source.value,
            )
        except Exception as e:
            elapsed = (time.monotonic() - t0) * 1000
            result = ExecutionResult(
                success=False, output="", error=str(e)[:500],
                latency_ms=elapsed, action=action, source=context.source.value,
            )

        self._audit(action, params, context, result)
        self._record_signal(action, context, result)
        return result

    def _audit(self, action: str, params: dict, context: ExecutionContext, result: ExecutionResult):
        """Write execution to audit log with source attribution."""
        if not self._autonomy:
            return
        try:
            self._autonomy.log_audit(
                action_type=action,
                description=f"[{context.source.value}] {action} (depth={context.depth})",
                payload={
                    "params": {k: str(v)[:200] for k, v in params.items()},
                    "source": context.source.value,
                    "depth": context.depth,
                    "parent_plan_id": context.parent_plan_id,
                    "trigger_id": context.trigger_id,
                },
                result="ok" if result.success else f"error: {result.error}",
            )
        except Exception as e:
            log.warning("Execution bus audit failed: %s", e)

    def _record_signal(self, action: str, context: ExecutionContext, result: ExecutionResult):
        """Record execution signal for learning system."""
        try:
            from learning import record_signal
            record_signal("execution_bus", {
                "action": action,
                "source": context.source.value,
                "success": result.success,
                "latency_ms": round(result.latency_ms, 1),
                "depth": context.depth,
                "error": result.error[:100] if result.error else None,
            })
        except Exception:
            pass


class _BusCaptureContext:
    """Minimal context that captures handler output (mirrors _ToolCaptureContext in server.py)."""

    def __init__(self):
        self._replies: list[str] = []
        self.side_effects: list[str] = []
        # Stub attributes that handlers may access
        self._raw_update = None

    async def reply(self, text: str, **kwargs):
        self._replies.append(text)

    async def send_message(self, chat_id: int, text: str, **kwargs):
        self._replies.append(text)

    def get_result(self) -> str:
        return "\n".join(self._replies) if self._replies else ""


# ---------------------------------------------------------------------------
# Singleton management
# ---------------------------------------------------------------------------

_bus_instance: ExecutionBus | None = None


def get_execution_bus() -> ExecutionBus | None:
    """Get the global execution bus instance."""
    return _bus_instance


def init_execution_bus(
    get_registry_fn: Callable,
    autonomy_controller: Any,
    ask_llm_fn: Callable[..., Awaitable[str]] | None = None,
) -> ExecutionBus:
    """Initialize and return the global execution bus."""
    global _bus_instance
    _bus_instance = ExecutionBus(get_registry_fn, autonomy_controller, ask_llm_fn)
    log.info("Execution bus initialized")
    return _bus_instance
