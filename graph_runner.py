"""Durable executor for graph nodes routed through the Execution Bus."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable

from execution import (
    ActionError,
    ActionErrorKind,
    ActionRequest,
    ActionResult,
    ActionStatus,
    ApprovalDecision,
    ExecutionContext,
    VerificationResult,
    VerificationStatus,
)
from execution_graph import (
    ExecutionGraphRepository,
    GraphNode,
    GraphRun,
    GraphStatus,
    IdempotencyClaimStatus,
    InvalidTransition,
    NodeStatus,
    TerminationReason,
)


log = logging.getLogger("khalil.graph_runner")


@dataclass
class NodePreparation:
    """Inputs resolved immediately before a node is claimed."""

    params: dict[str, Any]
    skip_output: str | None = None


PrepareNode = Callable[
    [GraphNode, dict[str, str]], NodePreparation | Awaitable[NodePreparation]
]
ProgressCallback = Callable[
    [str, GraphNode, ActionResult | None], None | Awaitable[None]
]
NodeExecutor = Callable[[GraphNode, ActionRequest], Awaitable[ActionResult]]


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def action_result_to_payload(result: ActionResult) -> dict[str, Any]:
    return {
        "status": result.status.value,
        "output": result.output,
        "data": _json_safe(result.data),
        "side_effects": list(result.side_effects),
        "failure": (
            {
                "kind": result.failure.kind.value,
                "message": result.failure.message,
                "retryable": result.failure.retryable,
            }
            if result.failure else None
        ),
        "approval": result.approval.value,
        "verification": {
            "status": result.verification.status.value,
            "evidence": list(result.verification.evidence),
            "message": result.verification.message,
        },
        "latency_ms": result.latency_ms,
        "action": result.action,
        "source": result.source,
    }


def action_result_from_payload(payload: dict[str, Any]) -> ActionResult:
    failure_payload = payload.get("failure")
    verification_payload = payload.get("verification") or {}
    return ActionResult(
        status=ActionStatus(payload["status"]),
        output=str(payload.get("output") or ""),
        data=payload.get("data"),
        side_effects=list(payload.get("side_effects") or []),
        failure=(
            ActionError(
                kind=ActionErrorKind(failure_payload["kind"]),
                message=str(failure_payload.get("message") or ""),
                retryable=bool(failure_payload.get("retryable")),
            )
            if failure_payload else None
        ),
        approval=ApprovalDecision(payload.get("approval", "not_required")),
        verification=VerificationResult(
            status=VerificationStatus(verification_payload.get("status", "not_run")),
            evidence=list(verification_payload.get("evidence") or []),
            message=str(verification_payload.get("message") or ""),
        ),
        latency_ms=float(payload.get("latency_ms") or 0),
        action=str(payload.get("action") or ""),
        source=str(payload.get("source") or ""),
    )


class ExecutionGraphRunner:
    """Claim, execute, checkpoint, and recover one durable graph."""

    def __init__(self, repository: ExecutionGraphRepository, execution_bus: Any):
        self.repository = repository
        self.execution_bus = execution_bus

    async def run(
        self,
        graph_id: str,
        context: ExecutionContext,
        *,
        response_context: Any = None,
        prepare_node: PrepareNode | None = None,
        progress: ProgressCallback | None = None,
        execute_node: NodeExecutor | None = None,
        recover_interrupted: bool = False,
    ) -> GraphRun:
        graph = self._require_graph(graph_id)
        if graph.status in {
            GraphStatus.SUCCEEDED,
            GraphStatus.FAILED,
            GraphStatus.COMPENSATED,
            GraphStatus.CANCELLED,
        }:
            return graph

        if graph.status == GraphStatus.WAITING_FOR_APPROVAL:
            if any(node.status == NodeStatus.WAITING_FOR_APPROVAL for node in graph.nodes):
                return graph
            graph = self._transition_graph(graph_id, GraphStatus.RUNNING)
        elif graph.status == GraphStatus.PENDING:
            graph = self._transition_graph(graph_id, GraphStatus.RUNNING)

        if recover_interrupted:
            await self._recover_interrupted_nodes(graph_id, progress)

        while True:
            graph = self._require_graph(graph_id)
            self._cancel_blocked_nodes(graph)
            graph = self._require_graph(graph_id)

            ready = self._mark_ready_nodes(graph)
            if ready:
                await asyncio.gather(*(
                    self._execute_node(
                        graph_id,
                        node.id,
                        context,
                        response_context=response_context,
                        prepare_node=prepare_node,
                        progress=progress,
                        execute_node=execute_node,
                    )
                    for node in ready
                ))
                continue

            graph = self._require_graph(graph_id)
            if any(node.status == NodeStatus.RUNNING for node in graph.nodes):
                return graph
            if any(node.status == NodeStatus.WAITING_FOR_APPROVAL for node in graph.nodes):
                return self._transition_graph(
                    graph_id,
                    GraphStatus.WAITING_FOR_APPROVAL,
                    termination_reason=TerminationReason.WAITING_FOR_APPROVAL,
                )
            if all(node.status == NodeStatus.SUCCEEDED for node in graph.nodes):
                return self._transition_graph(graph_id, GraphStatus.SUCCEEDED)
            if any(node.status == NodeStatus.PENDING for node in graph.nodes):
                return self._transition_graph(
                    graph_id,
                    GraphStatus.FAILED,
                    termination_reason=TerminationReason.NO_PROGRESS,
                )
            return self._transition_graph(graph_id, GraphStatus.FAILED)

    async def _recover_interrupted_nodes(
        self, graph_id: str, progress: ProgressCallback | None,
    ) -> None:
        graph = self._require_graph(graph_id)
        for node in graph.nodes:
            if node.status != NodeStatus.RUNNING:
                continue
            if not node.idempotency_key:
                if self.repository.reset_interrupted_node(graph_id, node.id):
                    await self._notify(progress, "recovered", node, None)
                continue

            claim = self.repository.get_idempotency_claim(node.idempotency_key)
            if claim is None:
                if self.repository.reset_interrupted_node(graph_id, node.id):
                    await self._notify(progress, "recovered", node, None)
                continue
            if claim.status == IdempotencyClaimStatus.COMPLETED and claim.result:
                result = action_result_from_payload(claim.result)
                await self._finish_node(graph_id, node, result, progress, replayed=True)
                continue

            message = (
                "Execution stopped after this protected side effect was claimed; "
                "the outcome is ambiguous and was not retried."
            )
            self.repository.checkpoint_node(
                graph_id,
                node.id,
                error={
                    "kind": "idempotency_ambiguous",
                    "message": message,
                    "retryable": False,
                },
            )
            failed = self.repository.transition_node(graph_id, node.id, NodeStatus.FAILED)
            await self._notify(
                progress,
                "failed",
                failed,
                ActionResult.failed(message, kind=ActionErrorKind.OPERATIONAL),
            )

    def _mark_ready_nodes(self, graph: GraphRun) -> list[GraphNode]:
        statuses = {node.id: node.status for node in graph.nodes}
        for node in graph.nodes:
            if node.status != NodeStatus.PENDING:
                continue
            if all(statuses[dependency] == NodeStatus.SUCCEEDED for dependency in node.dependencies):
                try:
                    self.repository.transition_node(graph.id, node.id, NodeStatus.READY)
                except InvalidTransition:
                    pass
        refreshed = self._require_graph(graph.id)
        return [node for node in refreshed.nodes if node.status == NodeStatus.READY]

    def _cancel_blocked_nodes(self, graph: GraphRun) -> None:
        statuses = {node.id: node.status for node in graph.nodes}
        blocking = {NodeStatus.FAILED, NodeStatus.CANCELLED, NodeStatus.COMPENSATED}
        for node in graph.nodes:
            if node.status not in {NodeStatus.PENDING, NodeStatus.READY}:
                continue
            failed_dependencies = [
                dependency for dependency in node.dependencies
                if statuses[dependency] in blocking
            ]
            if not failed_dependencies:
                continue
            self.repository.checkpoint_node(
                graph.id,
                node.id,
                error={
                    "kind": "blocked_dependency",
                    "message": f"Blocked by dependencies: {', '.join(failed_dependencies)}",
                    "retryable": False,
                },
            )
            try:
                self.repository.transition_node(graph.id, node.id, NodeStatus.CANCELLED)
            except InvalidTransition:
                pass

    async def _execute_node(
        self,
        graph_id: str,
        node_id: str,
        context: ExecutionContext,
        *,
        response_context: Any,
        prepare_node: PrepareNode | None,
        progress: ProgressCallback | None,
        execute_node: NodeExecutor | None,
    ) -> None:
        graph = self._require_graph(graph_id)
        node = next(node for node in graph.nodes if node.id == node_id)
        prior_results = self._prior_results(graph, node)
        preparation = NodePreparation(params=dict(node.inputs))
        if prepare_node:
            prepared = prepare_node(node, prior_results)
            preparation = await prepared if inspect.isawaitable(prepared) else prepared

        claimed = self.repository.claim_node(graph_id, node_id)
        if claimed is None:
            refreshed = self._require_graph(graph_id)
            current = next(item for item in refreshed.nodes if item.id == node_id)
            if current.status == NodeStatus.READY and current.attempt_count >= current.max_attempts:
                self.repository.checkpoint_node(
                    graph_id,
                    node_id,
                    error={
                        "kind": "retry_exhausted",
                        "message": "Node retry budget exhausted",
                        "retryable": False,
                    },
                )
                self.repository.transition_node(graph_id, node_id, NodeStatus.FAILED)
            return
        await self._notify(progress, "started", claimed, None)

        if preparation.skip_output is not None:
            result = ActionResult.succeeded(
                preparation.skip_output,
                action=claimed.action,
                source=context.source.value,
            )
            await self._finish_node(graph_id, claimed, result, progress, skipped=True)
            return

        idempotency_claimed = False
        if claimed.idempotency_key:
            receipt = self.repository.claim_idempotency(
                claimed.idempotency_key, graph_id, node_id,
            )
            if receipt.status == IdempotencyClaimStatus.COMPLETED and receipt.result:
                await self._finish_node(
                    graph_id,
                    claimed,
                    action_result_from_payload(receipt.result),
                    progress,
                    replayed=True,
                )
                return
            if receipt.status == IdempotencyClaimStatus.AMBIGUOUS:
                message = "Protected side effect already has an unresolved execution claim"
                await self._finish_node(
                    graph_id,
                    claimed,
                    ActionResult.failed(message, kind=ActionErrorKind.OPERATIONAL),
                    progress,
                    ambiguous=True,
                )
                return
            idempotency_claimed = True

        node_context = replace(
            context,
            parent_plan_id=graph_id,
            prior_results=prior_results,
            trigger_id=node_id,
        )
        params = dict(preparation.params)
        if claimed.metadata.get("description"):
            params.setdefault("description", claimed.metadata["description"])
        if claimed.idempotency_key:
            params.setdefault("_execution_idempotency_key", claimed.idempotency_key)
        request = ActionRequest(
            action=claimed.action,
            params=params,
            context=node_context,
            response_context=response_context,
        )
        try:
            operation = (
                execute_node(claimed, request)
                if execute_node else self.execution_bus.execute_request(request)
            )
            if claimed.timeout_seconds:
                result = await asyncio.wait_for(operation, timeout=claimed.timeout_seconds)
            else:
                result = await operation
        except asyncio.TimeoutError:
            result = ActionResult.failed(
                f"{claimed.action} timed out after {claimed.timeout_seconds}s",
                kind=ActionErrorKind.TIMEOUT,
                retryable=True,
                action=claimed.action,
                source=context.source.value,
            )
        except Exception as error:
            result = ActionResult.failed(
                str(error)[:500],
                kind=ActionErrorKind.OPERATIONAL,
                action=claimed.action,
                source=context.source.value,
            )

        payload = action_result_to_payload(result)
        if idempotency_claimed:
            if result.status in {
                ActionStatus.WAITING_FOR_APPROVAL,
                ActionStatus.REJECTED,
                ActionStatus.NOT_HANDLED,
            }:
                self.repository.release_idempotency(
                    claimed.idempotency_key, graph_id, node_id,
                )
            else:
                self.repository.complete_idempotency(
                    claimed.idempotency_key, graph_id, node_id, payload,
                )
        await self._finish_node(graph_id, claimed, result, progress)

    async def _finish_node(
        self,
        graph_id: str,
        node: GraphNode,
        result: ActionResult,
        progress: ProgressCallback | None,
        *,
        replayed: bool = False,
        skipped: bool = False,
        ambiguous: bool = False,
    ) -> None:
        payload = action_result_to_payload(result)
        evidence = list(result.verification.evidence) + list(result.side_effects)
        error = payload.get("failure")
        if ambiguous:
            error = {
                "kind": "idempotency_ambiguous",
                "message": result.error,
                "retryable": False,
            }
        self.repository.checkpoint_node(
            graph_id,
            node.id,
            outputs={
                "output": result.output,
                "data": payload.get("data"),
                "status": result.status.value,
                "replayed": replayed,
                "skipped": skipped,
            },
            evidence=evidence,
            error=error,
        )

        if result.status in {ActionStatus.SUCCEEDED, ActionStatus.EMPTY}:
            finished = self.repository.transition_node(graph_id, node.id, NodeStatus.SUCCEEDED)
            await self._notify(progress, "skipped" if skipped else "succeeded", finished, result)
            return
        if result.status == ActionStatus.WAITING_FOR_APPROVAL:
            waiting = self.repository.transition_node(
                graph_id, node.id, NodeStatus.WAITING_FOR_APPROVAL,
            )
            await self._notify(progress, "waiting_for_approval", waiting, result)
            return

        failed = self.repository.transition_node(graph_id, node.id, NodeStatus.FAILED)
        if (
            result.failure
            and result.failure.retryable
            and not node.idempotency_key
            and failed.attempt_count < failed.max_attempts
        ):
            ready = self.repository.transition_node(graph_id, node.id, NodeStatus.READY)
            await self._notify(progress, "retrying", ready, result)
            return
        await self._notify(progress, "failed", failed, result)

    def _prior_results(self, graph: GraphRun, node: GraphNode) -> dict[str, str]:
        by_id = {item.id: item for item in graph.nodes}
        return {
            dependency: str((by_id[dependency].outputs or {}).get("output") or "")
            for dependency in node.dependencies
        }

    async def _notify(
        self,
        progress: ProgressCallback | None,
        event: str,
        node: GraphNode,
        result: ActionResult | None,
    ) -> None:
        if not progress:
            return
        try:
            callback_result = progress(event, node, result)
            if inspect.isawaitable(callback_result):
                await callback_result
        except Exception as error:
            log.warning("Graph progress callback failed: %s", error)

    def _require_graph(self, graph_id: str) -> GraphRun:
        graph = self.repository.load_graph(graph_id)
        if graph is None:
            raise KeyError(f"Unknown execution graph {graph_id!r}")
        return graph

    def _transition_graph(
        self,
        graph_id: str,
        target: GraphStatus,
        *,
        termination_reason: TerminationReason | None = None,
    ) -> GraphRun:
        try:
            return self.repository.transition_graph(
                graph_id, target, termination_reason=termination_reason,
            )
        except InvalidTransition:
            graph = self._require_graph(graph_id)
            if graph.status == target:
                return graph
            raise
