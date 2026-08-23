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

from config import ActionType, AutonomyLevel

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


class ActionStatus(str, Enum):
    """Observable state of one action execution."""

    SUCCEEDED = "succeeded"
    EMPTY = "empty"
    FAILED = "failed"
    REJECTED = "rejected"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    NOT_HANDLED = "not_handled"


class ActionErrorKind(str, Enum):
    """Stable failure categories for routing, recovery, and metrics."""

    VALIDATION = "validation"
    AUTHENTICATION = "authentication"
    NETWORK = "network"
    APPROVAL_REQUIRED = "approval_required"
    RATE_LIMITED = "rate_limited"
    NOT_FOUND = "not_found"
    TIMEOUT = "timeout"
    OPERATIONAL = "operational"
    INTERNAL = "internal"


class ApprovalDecision(str, Enum):
    """Approval state attached to an action outcome."""

    NOT_REQUIRED = "not_required"
    APPROVED = "approved"
    REJECTED = "rejected"
    REQUIRED = "required"


class VerificationStatus(str, Enum):
    """Whether an action's requested outcome has been verified."""

    NOT_RUN = "not_run"
    PASSED = "passed"
    FAILED = "failed"


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
class ActionRequest:
    """Typed request accepted by the execution boundary."""

    action: str
    params: dict[str, Any]
    context: ExecutionContext
    response_context: Any = None


@dataclass
class ActionError:
    """Structured operational failure."""

    kind: ActionErrorKind
    message: str
    retryable: bool = False


@dataclass
class VerificationResult:
    """Evidence that the requested outcome occurred."""

    status: VerificationStatus = VerificationStatus.NOT_RUN
    evidence: list[str] = field(default_factory=list)
    message: str = ""


@dataclass
class ActionResult:
    """Typed result of any action through the execution boundary."""

    status: ActionStatus
    output: str = ""
    data: Any = None
    side_effects: list[str] = field(default_factory=list)
    failure: ActionError | None = None
    approval: ApprovalDecision = ApprovalDecision.NOT_REQUIRED
    verification: VerificationResult = field(default_factory=VerificationResult)
    latency_ms: float = 0.0
    action: str = ""
    source: str = ""

    @property
    def success(self) -> bool:
        """Compatibility view for callers that still consume a boolean."""
        return self.status in (ActionStatus.SUCCEEDED, ActionStatus.EMPTY)

    @property
    def error(self) -> str | None:
        """Compatibility view for callers that still consume an error string."""
        return self.failure.message if self.failure else None

    @classmethod
    def succeeded(cls, output: str = "", **kwargs) -> ActionResult:
        has_result = (
            bool(output)
            or kwargs.get("data") is not None
            or bool(kwargs.get("side_effects"))
        )
        status = ActionStatus.SUCCEEDED if has_result else ActionStatus.EMPTY
        return cls(status=status, output=output, **kwargs)

    @classmethod
    def failed(
        cls,
        message: str,
        *,
        kind: ActionErrorKind = ActionErrorKind.OPERATIONAL,
        retryable: bool = False,
        **kwargs,
    ) -> ActionResult:
        return cls(
            status=ActionStatus.FAILED,
            failure=ActionError(kind=kind, message=message, retryable=retryable),
            **kwargs,
        )

    @classmethod
    def rejected(
        cls,
        message: str,
        *,
        kind: ActionErrorKind,
        retryable: bool = False,
        **kwargs,
    ) -> ActionResult:
        return cls(
            status=ActionStatus.REJECTED,
            failure=ActionError(kind=kind, message=message, retryable=retryable),
            **kwargs,
        )

    @classmethod
    def waiting_for_approval(cls, message: str, **kwargs) -> ActionResult:
        return cls(
            status=ActionStatus.WAITING_FOR_APPROVAL,
            failure=ActionError(
                kind=ActionErrorKind.APPROVAL_REQUIRED,
                message=message,
                retryable=False,
            ),
            approval=ApprovalDecision.REQUIRED,
            **kwargs,
        )

    @classmethod
    def not_handled(cls, **kwargs) -> ActionResult:
        return cls(status=ActionStatus.NOT_HANDLED, **kwargs)


# Compatibility name for existing extension imports during the migration.
ExecutionResult = ActionResult


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

    def get_declared_action_type(self, action: str) -> ActionType | None:
        """Return the action's intrinsic risk type from the skill manifest."""
        registry = self._get_registry()
        get_action_type = getattr(registry, "get_action_type", None)
        return get_action_type(action) if get_action_type else None

    async def execute(
        self,
        action: str,
        params: dict,
        context: ExecutionContext,
        *,
        response_context: Any = None,
    ) -> ActionResult:
        """Execute an action through the bus.

        Routes through: depth check → autonomy check → handler lookup → execute → audit.
        """
        t0 = time.monotonic()

        # Depth guard
        if context.depth > MAX_EXECUTION_DEPTH:
            return ActionResult.rejected(
                f"Max execution depth ({MAX_EXECUTION_DEPTH}) exceeded",
                kind=ActionErrorKind.VALIDATION,
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
                result = ActionResult.failed(
                    str(e)[:500], kind=ActionErrorKind.INTERNAL,
                    latency_ms=elapsed, action=action, source=context.source.value,
                )
                self._audit(action, params, context, result)
                self._record_signal(action, context, result)
                return result

        registry = self._get_registry()
        declared_type = self.get_declared_action_type(action)
        handler = registry.get_handler(action)
        if handler is None:
            return ActionResult.failed(
                f"No handler found for '{action}'",
                kind=ActionErrorKind.NOT_FOUND,
                action=action, source=context.source.value,
                latency_ms=(time.monotonic() - t0) * 1000,
            )

        # Autonomy check
        effective_autonomy = context.autonomy_override or (
            self._autonomy.level if self._autonomy else AutonomyLevel.SUPERVISED
        )
        if self._autonomy and self._autonomy.needs_approval(
            action, params, declared_type=declared_type,
        ):
            # For non-user sources, check if autonomy allows auto-execution
            if context.source != ExecutionSource.USER:
                if effective_autonomy == AutonomyLevel.SUPERVISED:
                    return ActionResult.waiting_for_approval(
                        f"Action '{action}' requires approval (supervised mode)",
                        action=action,
                        source=context.source.value,
                        latency_ms=(time.monotonic() - t0) * 1000,
                    )

        # Rate limit check
        if self._autonomy:
            allowed, reason = self._autonomy.check_rate_limit(action)
            if not allowed:
                return ActionResult.rejected(
                    reason, kind=ActionErrorKind.RATE_LIMITED,
                    retryable=True,
                    action=action, source=context.source.value,
                    latency_ms=(time.monotonic() - t0) * 1000,
                )

        # Build intent dict matching existing handler signature
        intent = {"action": action, **params}

        # Create a capture context for the handler
        capture_ctx = _BusCaptureContext(response_context)

        try:
            handled = await asyncio.wait_for(
                handler(action, intent, capture_ctx),
                timeout=60,
            )
            elapsed = (time.monotonic() - t0) * 1000
            output = capture_ctx.get_result()
            result_kwargs = {
                "output": output,
                "side_effects": capture_ctx.side_effects,
                "latency_ms": elapsed,
                "action": action,
                "source": context.source.value,
            }
            if not handled and not capture_ctx.replied and not capture_ctx.side_effects:
                result = ActionResult.not_handled(**result_kwargs)
            else:
                result = ActionResult.succeeded(**result_kwargs)
        except asyncio.TimeoutError:
            elapsed = (time.monotonic() - t0) * 1000
            result = ActionResult.failed(
                f"{action} timed out after 60s",
                kind=ActionErrorKind.TIMEOUT,
                retryable=True,
                latency_ms=elapsed, action=action, source=context.source.value,
            )
        except Exception as e:
            elapsed = (time.monotonic() - t0) * 1000
            result = ActionResult.failed(
                str(e)[:500], kind=ActionErrorKind.OPERATIONAL,
                latency_ms=elapsed, action=action, source=context.source.value,
            )

        self._audit(action, params, context, result)
        self._record_signal(action, context, result)
        return result

    async def execute_request(self, request: ActionRequest) -> ActionResult:
        """Typed adapter for callers migrating to request objects."""
        return await self.execute(
            request.action,
            request.params,
            request.context,
            response_context=request.response_context,
        )

    def _audit(self, action: str, params: dict, context: ExecutionContext, result: ActionResult):
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
                result=(
                    "ok" if result.success
                    else "not_handled" if result.status == ActionStatus.NOT_HANDLED
                    else f"error: {result.error}"
                ),
            )
        except Exception as e:
            log.warning("Execution bus audit failed: %s", e)

    def _record_signal(self, action: str, context: ExecutionContext, result: ActionResult):
        """Record execution signal for learning system."""
        try:
            from learning import record_signal
            record_signal("execution_bus", {
                "action": action,
                "source": context.source.value,
                "success": result.success,
                "status": result.status.value,
                "error_kind": result.failure.kind.value if result.failure else None,
                "latency_ms": round(result.latency_ms, 1),
                "depth": context.depth,
                "error": result.error[:100] if result.error else None,
            })
        except Exception:
            pass


class _BusCaptureContext:
    """Capture handler output and optionally forward it to a real channel context."""

    def __init__(self, target: Any = None):
        self._target = target
        self._replies: list[str] = []
        self.side_effects: list[str] = []
        self.replied = False

    async def reply(self, text: str, **kwargs):
        if text:
            self._replies.append(text)
        self.replied = True
        if self._target is not None:
            return await self._target.reply(text, **kwargs)
        return None

    async def send_message(self, chat_id: int, text: str, **kwargs):
        if text:
            self._replies.append(text)
        self.replied = True
        if self._target is not None:
            if hasattr(self._target, "send_message"):
                return await self._target.send_message(chat_id, text, **kwargs)
            return await self._target.reply(text, **kwargs)
        return None

    async def reply_photo(self, *args, **kwargs):
        self.side_effects.append("reply_photo")
        if self._target is not None and hasattr(self._target, "reply_photo"):
            return await self._target.reply_photo(*args, **kwargs)
        return None

    async def reply_voice(self, *args, **kwargs):
        self.side_effects.append("reply_voice")
        if self._target is not None and hasattr(self._target, "reply_voice"):
            return await self._target.reply_voice(*args, **kwargs)
        return None

    async def reply_video(self, *args, **kwargs):
        self.side_effects.append("reply_video")
        if self._target is not None and hasattr(self._target, "reply_video"):
            return await self._target.reply_video(*args, **kwargs)
        return None

    async def reply_document(self, *args, **kwargs):
        self.side_effects.append("reply_document")
        if self._target is not None and hasattr(self._target, "reply_document"):
            return await self._target.reply_document(*args, **kwargs)
        return None

    async def typing(self):
        if self._target is not None and hasattr(self._target, "typing"):
            return await self._target.typing()
        return None

    def __getattr__(self, name: str):
        if self._target is not None:
            return getattr(self._target, name)
        if name == "_raw_update":
            return None
        raise AttributeError(name)

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
