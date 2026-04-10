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
