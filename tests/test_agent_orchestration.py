"""Tests for agent swarm orchestration — decomposition, execution, synthesis, and wiring."""

import asyncio
import json
import os
import sqlite3
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _run(coro):
    """Run async test without pytest-asyncio."""
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Decompose to swarm
# ---------------------------------------------------------------------------

class TestDecomposeToSwarm:
    def test_returns_agents_for_parallelizable(self):
        from agents.coordinator import decompose_to_swarm

        mock_llm = AsyncMock(return_value=json.dumps({
            "parallel": True,
            "agents": [
                {"name": "weather", "task": "check weather"},
                {"name": "email", "task": "find email from Sarah"},
                {"name": "calendar", "task": "list today's events"},
            ],
        }))

        with patch("agents.coordinator.SWARM_ENABLED", True):
            result = _run(decompose_to_swarm(
                "Check weather, find email from Sarah, and show my calendar",
                "context", mock_llm,
            ))

        assert result is not None
        assert len(result) == 3
        assert result[0].name == "weather"
        assert result[2].name == "calendar"

    def test_returns_none_for_simple(self):
        from agents.coordinator import decompose_to_swarm

        mock_llm = AsyncMock(return_value=json.dumps({
            "parallel": False, "agents": [],
        }))

        with patch("agents.coordinator.SWARM_ENABLED", True):
            result = _run(decompose_to_swarm("What's the weather?", "ctx", mock_llm))

        assert result is None

    def test_returns_none_when_disabled(self):
        from agents.coordinator import decompose_to_swarm

        mock_llm = AsyncMock()
        with patch("agents.coordinator.SWARM_ENABLED", False):
            result = _run(decompose_to_swarm("anything", "ctx", mock_llm))

        assert result is None
        mock_llm.assert_not_called()

    def test_handles_malformed_response(self):
        from agents.coordinator import decompose_to_swarm

        mock_llm = AsyncMock(return_value="not json")
        with patch("agents.coordinator.SWARM_ENABLED", True):
            result = _run(decompose_to_swarm("check x and y", "ctx", mock_llm))

        assert result is None

    def test_caps_at_five_agents(self):
        from agents.coordinator import decompose_to_swarm

        mock_llm = AsyncMock(return_value=json.dumps({
            "parallel": True,
            "agents": [{"name": f"a{i}", "task": f"task {i}"} for i in range(10)],
        }))

        with patch("agents.coordinator.SWARM_ENABLED", True):
            result = _run(decompose_to_swarm("do many things", "ctx", mock_llm))

        assert result is not None
        assert len(result) <= 5


# ---------------------------------------------------------------------------
# Run swarm
# ---------------------------------------------------------------------------

class TestRunSwarm:
    def test_executes_parallel(self):
        from agents.coordinator import SubAgent, run_swarm

        agents = [SubAgent(name="a1", task="t1"), SubAgent(name="a2", task="t2")]

        with patch("agents.pool.fan_out_named", new_callable=AsyncMock) as mock_fan:
            mock_fan.return_value = {"a1": "result 1", "a2": "result 2"}
            result = _run(run_swarm(agents))

        assert len(result.results) == 2
        assert result.results["a1"] == "result 1"
        assert len(result.errors) == 0
        assert result.elapsed_ms >= 0

    def test_captures_errors(self):
        from agents.coordinator import SubAgent, run_swarm

        agents = [SubAgent(name="ok", task="t1"), SubAgent(name="fail", task="t2")]

        with patch("agents.pool.fan_out_named", new_callable=AsyncMock) as mock_fan:
            mock_fan.return_value = {"ok": "success", "fail": "[sub-agent error] timeout"}
            result = _run(run_swarm(agents))

        assert len(result.results) == 1
        assert len(result.errors) == 1
        assert "ok" in result.results
        assert "fail" in result.errors


# ---------------------------------------------------------------------------
# Synthesize results
# ---------------------------------------------------------------------------

class TestSynthesizeResults:
    def test_combines_results(self):
        from agents.coordinator import SwarmResult, synthesize_results

        swarm_result = SwarmResult(
            results={"weather": "15C sunny", "email": "3 unread from Sarah"},
            errors={}, elapsed_ms=500,
        )
        mock_llm = AsyncMock(return_value="Weather is 15C. You have 3 emails from Sarah.")

        response = _run(synthesize_results("check weather and emails", swarm_result, mock_llm))

        assert "15C" in response or "Sarah" in response
        call_args = mock_llm.call_args[0][0]
        assert "15C sunny" in call_args
        assert "Sarah" in call_args

    def test_handles_all_failures(self):
        from agents.coordinator import SwarmResult, synthesize_results

        swarm_result = SwarmResult(
            results={}, errors={"a": "[error] timeout"}, elapsed_ms=100,
        )
        mock_llm = AsyncMock()

        response = _run(synthesize_results("query", swarm_result, mock_llm))
        assert "failed" in response.lower()
        mock_llm.assert_not_called()


# ---------------------------------------------------------------------------
# Background agents
# ---------------------------------------------------------------------------

class TestBackgroundAgents:
    def test_spawn_persists(self, tmp_path):
        from agents.coordinator import spawn_background_agent, get_background_agents

        db_path = tmp_path / "test.db"
        with patch("config.DB_PATH", db_path):
            agent = spawn_background_agent("analyze trends", {"scope": "weekly"})

        assert agent.id.startswith("bg_")
        assert agent.task == "analyze trends"

        with patch("config.DB_PATH", db_path):
            agents = get_background_agents()
        assert len(agents) == 1
        assert agents[0]["status"] == "running"

    def test_lifecycle(self, tmp_path):
        from agents.coordinator import (
            spawn_background_agent, update_background_agent, get_background_agents,
        )

        db_path = tmp_path / "test.db"
        with patch("config.DB_PATH", db_path):
            agent = spawn_background_agent("long task")
            update_background_agent(agent.id, progress_entry="Step 1")

            agents = get_background_agents()
            assert "Step 1" in agents[0]["progress"]

            update_background_agent(agent.id, status="completed", final_result="Done")
            agents = get_background_agents(status="completed")
            assert len(agents) == 1
            assert agents[0]["completed_at"] is not None


# ---------------------------------------------------------------------------
# Heuristic gate
# ---------------------------------------------------------------------------

class TestHeuristicGate:
    def test_short_query_rejected(self):
        from orchestrator import looks_like_multi_step
        assert not looks_like_multi_step("Hello")
        assert not looks_like_multi_step("What's the weather?")

    def test_multi_intent_accepted(self):
        from orchestrator import looks_like_multi_step
        assert looks_like_multi_step(
            "Check the weather, email Sarah about the meeting, and update my calendar"
        )
        assert looks_like_multi_step("Prep for my standup and also review the open PRs")

    def test_single_complex_rejected(self):
        from orchestrator import looks_like_multi_step
        assert not looks_like_multi_step(
            "Send a detailed email to John about the Q3 budget review"
        )

    def test_comma_multiple_verbs(self):
        from orchestrator import looks_like_multi_step
        assert looks_like_multi_step(
            "Summarize my emails, check calendar, draft a status update"
        )


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------

class TestFallback:
    def test_decompose_exception_returns_none(self):
        from agents.coordinator import decompose_to_swarm

        mock_llm = AsyncMock(side_effect=RuntimeError("API timeout"))
        with patch("agents.coordinator.SWARM_ENABLED", True):
            result = _run(decompose_to_swarm("check x and y", "ctx", mock_llm))

        assert result is None


# ---------------------------------------------------------------------------
# Typed execution outcomes
# ---------------------------------------------------------------------------

class _Registry:
    def __init__(self, handler=None, action_type=None):
        self.handler = handler
        self.action_type = action_type

    def get_handler(self, action):
        return self.handler

    def get_action_type(self, action):
        return self.action_type


class _Autonomy:
    def __init__(self, *, needs_approval=False, rate_limit=(True, "")):
        from config import AutonomyLevel
        self.level = AutonomyLevel.SUPERVISED
        self._needs_approval = needs_approval
        self._rate_limit = rate_limit

    def needs_approval(self, action, payload=None, declared_type=None):
        self.last_approval_check = (action, payload, declared_type)
        return self._needs_approval

    def check_rate_limit(self, action):
        return self._rate_limit

    def log_audit(self, **kwargs):
        pass


class TestTypedExecutionOutcomes:
    def _bus(self, handler=None, autonomy=None):
        from execution import ExecutionBus
        registry = _Registry(handler)
        return ExecutionBus(lambda: registry, autonomy or _Autonomy())

    def _context(self, source=None):
        from execution import ExecutionContext, ExecutionSource
        return ExecutionContext(source=source or ExecutionSource.USER)

    def test_success_and_valid_empty_are_distinct(self):
        from execution import ActionStatus

        async def with_output(action, intent, ctx):
            await ctx.reply("done")

        async def without_output(action, intent, ctx):
            return True

        output_result = asyncio.run(
            self._bus(with_output).execute("demo", {}, self._context())
        )
        empty_result = asyncio.run(
            self._bus(without_output).execute("demo", {}, self._context())
        )

        assert output_result.status == ActionStatus.SUCCEEDED
        assert empty_result.status == ActionStatus.EMPTY
        assert output_result.success is True
        assert empty_result.success is True

    def test_explicit_decline_is_not_handled(self):
        from execution import ActionStatus

        async def decline(action, intent, ctx):
            return False

        result = asyncio.run(
            self._bus(decline).execute("demo", {}, self._context())
        )

        assert result.status == ActionStatus.NOT_HANDLED
        assert result.success is False

    def test_response_context_forwards_reply_and_preserves_output(self):
        from execution import ActionStatus

        class ResponseContext:
            def __init__(self):
                self.chat_id = "channel-123"
                self.replies = []

            async def reply(self, text, **kwargs):
                self.replies.append((text, kwargs))

        async def handler(action, intent, ctx):
            assert ctx.chat_id == "channel-123"
            await ctx.reply("done", parse_mode="Markdown")
            return True

        response_context = ResponseContext()
        result = asyncio.run(
            self._bus(handler).execute(
                "demo", {}, self._context(), response_context=response_context,
            )
        )

        assert result.status == ActionStatus.SUCCEEDED
        assert result.output == "done"
        assert response_context.replies == [("done", {"parse_mode": "Markdown"})]

    def test_reply_from_falsy_handler_still_counts_as_handled(self):
        from execution import ActionStatus

        async def handler(action, intent, ctx):
            await ctx.reply("already answered")
            return False

        result = asyncio.run(
            self._bus(handler).execute("demo", {}, self._context())
        )

        assert result.status == ActionStatus.SUCCEEDED
        assert result.output == "already answered"

    def test_media_reply_is_forwarded_and_recorded_as_side_effect(self):
        from execution import ActionStatus

        class ResponseContext:
            def __init__(self):
                self.photos = []

            async def reply_photo(self, photo_path, caption=""):
                self.photos.append((photo_path, caption))

        async def handler(action, intent, ctx):
            await ctx.reply_photo("chart.png", caption="Status")
            return True

        response_context = ResponseContext()
        result = asyncio.run(
            self._bus(handler).execute(
                "demo", {}, self._context(), response_context=response_context,
            )
        )

        assert result.status == ActionStatus.SUCCEEDED
        assert result.side_effects == ["reply_photo"]
        assert response_context.photos == [("chart.png", "Status")]

    def test_operational_failure_is_structured(self):
        from execution import ActionErrorKind, ActionStatus

        async def broken(action, intent, ctx):
            raise ConnectionError("service unavailable")

        result = asyncio.run(
            self._bus(broken).execute("demo", {}, self._context())
        )

        assert result.status == ActionStatus.FAILED
        assert result.failure.kind == ActionErrorKind.OPERATIONAL
        assert result.error == "service unavailable"
        assert result.success is False

    def test_authentication_and_network_failures_are_distinct(self):
        from execution import ActionErrorKind, ActionResult

        authentication = ActionResult.failed(
            "token expired", kind=ActionErrorKind.AUTHENTICATION,
        )
        network = ActionResult.failed(
            "connection reset", kind=ActionErrorKind.NETWORK, retryable=True,
        )

        assert authentication.failure.kind == ActionErrorKind.AUTHENTICATION
        assert authentication.failure.retryable is False
        assert network.failure.kind == ActionErrorKind.NETWORK
        assert network.failure.retryable is True

    def test_approval_wait_is_not_reported_as_failure(self):
        from execution import ActionStatus, ApprovalDecision, ExecutionSource

        async def handler(action, intent, ctx):
            raise AssertionError("approval should stop execution")

        result = asyncio.run(
            self._bus(handler, _Autonomy(needs_approval=True)).execute(
                "send", {}, self._context(ExecutionSource.WORKFLOW),
            )
        )

        assert result.status == ActionStatus.WAITING_FOR_APPROVAL
        assert result.approval == ApprovalDecision.REQUIRED
        assert result.success is False

    def test_registry_classification_reaches_approval_policy(self):
        from config import ActionType

        async def handler(action, intent, ctx):
            return True

        autonomy = _Autonomy()
        registry = _Registry(handler, action_type=ActionType.READ)
        from execution import ExecutionBus
        bus = ExecutionBus(lambda: registry, autonomy)

        asyncio.run(bus.execute("demo", {"value": 1}, self._context()))

        assert autonomy.last_approval_check == (
            "demo", {"value": 1}, ActionType.READ,
        )

    def test_rate_limit_rejection_and_missing_handler_have_distinct_kinds(self):
        from execution import ActionErrorKind, ActionStatus

        rate_limited = asyncio.run(
            self._bus(
                handler=lambda *args: None,
                autonomy=_Autonomy(rate_limit=(False, "slow down")),
            ).execute("demo", {}, self._context())
        )
        missing = asyncio.run(
            self._bus().execute("unknown", {}, self._context())
        )

        assert rate_limited.status == ActionStatus.REJECTED
        assert rate_limited.failure.kind == ActionErrorKind.RATE_LIMITED
        assert missing.status == ActionStatus.FAILED
        assert missing.failure.kind == ActionErrorKind.NOT_FOUND

    def test_typed_request_adapter_preserves_existing_execution(self):
        from execution import ActionRequest, ActionStatus

        async def handler(action, intent, ctx):
            await ctx.reply(intent["value"])

        bus = self._bus(handler)
        class ResponseContext:
            def __init__(self):
                self.replies = []

            async def reply(self, text, **kwargs):
                self.replies.append(text)

        response_context = ResponseContext()
        request = ActionRequest(
            action="demo",
            params={"value": "typed"},
            context=self._context(),
            response_context=response_context,
        )

        result = asyncio.run(bus.execute_request(request))

        assert result.status == ActionStatus.SUCCEEDED
        assert result.output == "typed"
        assert response_context.replies == ["typed"]


class TestDirectSkillExecutionBoundary:
    class _Context:
        chat_id = "channel-123"

        def __init__(self):
            self.replies = []

        async def reply(self, text, **kwargs):
            self.replies.append(text)

    def test_registered_action_routes_through_execution_bus(self):
        from execution import ActionResult
        from server import handle_action_intent

        bus = type("Bus", (), {})()
        bus.execute = AsyncMock(return_value=ActionResult.succeeded(output="done"))
        registry = _Registry(handler=lambda *args: True)
        ctx = self._Context()

        with patch("skills.get_registry", return_value=registry), \
             patch("execution.get_execution_bus", return_value=bus):
            handled = asyncio.run(handle_action_intent({"action": "demo"}, ctx))

        assert handled is True
        call = bus.execute.await_args
        assert call.args[0] == "demo"
        assert call.args[2].chat_id == "channel-123"
        assert call.kwargs["response_context"] is ctx

    def test_not_handled_result_preserves_legacy_fallback(self):
        from execution import ActionResult
        from server import handle_action_intent

        bus = type("Bus", (), {})()
        bus.execute = AsyncMock(return_value=ActionResult.not_handled())
        registry = _Registry(handler=lambda *args: False)
        ctx = self._Context()

        with patch("skills.get_registry", return_value=registry), \
             patch("execution.get_execution_bus", return_value=bus):
            handled = asyncio.run(handle_action_intent({"action": "demo"}, ctx))

        assert handled is False
        assert ctx.replies == []
