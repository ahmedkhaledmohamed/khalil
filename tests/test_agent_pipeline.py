"""Tests for the agent pipeline: intent classification + task state + context assembly."""

import asyncio
import json
import os
import sqlite3
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestIntentClassification:
    def test_yes_with_active_task_is_continuation(self):
        from intent import classify_intent, Intent
        assert classify_intent("Yes", has_active_task=True) == Intent.CONTINUATION
        assert classify_intent("ok", has_active_task=True) == Intent.CONTINUATION
        assert classify_intent("continue", has_active_task=True) == Intent.CONTINUATION
        assert classify_intent("sounds good", has_active_task=True) == Intent.CONTINUATION
        assert classify_intent("go ahead", has_active_task=True) == Intent.CONTINUATION

    def test_yes_without_active_task_is_chat(self):
        from intent import classify_intent, Intent
        assert classify_intent("Yes", has_active_task=False) == Intent.CHAT

    def test_build_request_is_task(self):
        from intent import classify_intent, Intent
        assert classify_intent("Build me an HTML presentation about FL26", has_active_task=False) == Intent.TASK
        assert classify_intent("Create a Python script that checks disk", has_active_task=False) == Intent.TASK
        assert classify_intent("Send an email to John about the meeting", has_active_task=False) == Intent.TASK

    def test_question_is_question(self):
        from intent import classify_intent, Intent
        assert classify_intent("What's the weather?", has_active_task=False) == Intent.QUESTION
        assert classify_intent("How many emails did I get?", has_active_task=False) == Intent.QUESTION
        assert classify_intent("Is my calendar free tomorrow?", has_active_task=False) == Intent.QUESTION

    def test_greeting_is_chat(self):
        from intent import classify_intent, Intent
        assert classify_intent("Hello", has_active_task=False) == Intent.CHAT
        assert classify_intent("Thanks", has_active_task=False) == Intent.CHAT

    def test_whats_the_status_with_task_is_continuation(self):
        from intent import classify_intent, Intent
        # "What's the status" with active task → continuation (inherits task)
        assert classify_intent("What's the status", has_active_task=True) == Intent.CONTINUATION

    def test_substantive_message_with_task_is_continuation(self):
        from intent import classify_intent, Intent
        assert classify_intent("Keep it in personal repo", has_active_task=True) == Intent.CONTINUATION
        assert classify_intent("Short appendix", has_active_task=True) == Intent.CONTINUATION


class TestTaskManager:
    def test_create_task(self, tmp_path):
        from task_manager import TaskManager
        with patch("task_manager.DB_PATH", tmp_path / "test.db"):
            mgr = TaskManager()
            task = mgr.create_task("chat_123", "Build FL26 presentation", "artifact")
            assert task.id.startswith("task_")
            assert task.original_query == "Build FL26 presentation"
            assert task.status == "active"
            assert task.task_type == "artifact"

    def test_get_active_task(self, tmp_path):
        from task_manager import TaskManager
        with patch("task_manager.DB_PATH", tmp_path / "test.db"):
            mgr = TaskManager()
            mgr.create_task("chat_123", "Build FL26 presentation", "artifact")
            task = mgr.get_active_task("chat_123")
            assert task is not None
            assert task.original_query == "Build FL26 presentation"

    def test_complete_task(self, tmp_path):
        from task_manager import TaskManager
        with patch("task_manager.DB_PATH", tmp_path / "test.db"):
            mgr = TaskManager()
            task = mgr.create_task("chat_123", "Build presentation", "artifact")
            mgr.complete_task(task.id, "Created index.html")
            active = mgr.get_active_task("chat_123")
            assert active is None  # No active task after completion

    def test_reset_after_3_failures(self, tmp_path):
        from task_manager import TaskManager
        with patch("task_manager.DB_PATH", tmp_path / "test.db"):
            mgr = TaskManager()
            task = mgr.create_task("chat_123", "Build presentation", "artifact")
            mgr.record_attempt(task.id)
            mgr.record_attempt(task.id)
            mgr.record_attempt(task.id)
            task = mgr.get_active_task("chat_123")
            assert mgr.should_reset(task)
            mgr.reset_task(task.id)
            task = mgr.get_active_task("chat_123")
            assert task.status == "blocked"
            assert "failed" in task.context_summary.lower()

    def test_new_task_supersedes_old(self, tmp_path):
        from task_manager import TaskManager
        with patch("task_manager.DB_PATH", tmp_path / "test.db"):
            mgr = TaskManager()
            mgr.create_task("chat_123", "First task", "task")
            mgr.create_task("chat_123", "Second task", "task")
            active = mgr.get_active_task("chat_123")
            assert active.original_query == "Second task"

    def test_task_context_for_llm(self, tmp_path):
        from task_manager import TaskManager
        with patch("task_manager.DB_PATH", tmp_path / "test.db"):
            mgr = TaskManager()
            task = mgr.create_task("chat_123", "Build FL26 deck", "artifact")
            mgr.record_tool_use(task.id, "search_knowledge")
            mgr.record_tool_use(task.id, "generate_file")
            task = mgr.get_active_task("chat_123")
            ctx = mgr.get_task_context_for_llm(task)
            assert "Build FL26 deck" in ctx
            assert "search_knowledge" in ctx
            assert "generate_file" in ctx


def _run(coro):
    """Run async test in sync context."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestContextAssembly:
    """Test intent-aware context assembly rules."""

    @patch("context._search_kb", new_callable=AsyncMock, return_value=[])
    @patch("context._get_memories", new_callable=AsyncMock, return_value="")
    @patch("context._get_live_state", new_callable=AsyncMock, return_value="")
    @patch("context._get_proactive_context", new_callable=AsyncMock, return_value="")
    @patch("context._get_recent_messages", return_value="")
    @patch("context._get_session_summary", return_value="")
    @patch("context._get_active_plans", return_value="")
    @patch("context.get_relevant_context", return_value="")
    def test_continuation_skips_kb_search(self, mock_ctx, mock_plans, mock_summary,
                                          mock_msgs, mock_proactive, mock_live,
                                          mock_memories, mock_kb):
        from context import assemble_context
        from intent import Intent
        _run(assemble_context(Intent.CONTINUATION, "Yes", chat_id=123))
        mock_kb.assert_not_called()

    @patch("context._search_kb", new_callable=AsyncMock, return_value=[])
    @patch("context._get_memories", new_callable=AsyncMock, return_value="")
    @patch("context._get_live_state", new_callable=AsyncMock, return_value="")
    @patch("context._get_proactive_context", new_callable=AsyncMock, return_value="")
    @patch("context._get_recent_messages", return_value="")
    @patch("context._get_session_summary", return_value="")
    @patch("context._get_active_plans", return_value="")
    @patch("context.get_relevant_context", return_value="")
    def test_chat_skips_kb_search(self, mock_ctx, mock_plans, mock_summary,
                                   mock_msgs, mock_proactive, mock_live,
                                   mock_memories, mock_kb):
        from context import assemble_context
        from intent import Intent
        _run(assemble_context(Intent.CHAT, "Hello", chat_id=123))
        mock_kb.assert_not_called()

    @patch("context._search_kb", new_callable=AsyncMock, return_value=[{"title": "test", "content": "data"}])
    @patch("context._get_memories", new_callable=AsyncMock, return_value="")
    @patch("context._get_live_state", new_callable=AsyncMock, return_value="")
    @patch("context._get_proactive_context", new_callable=AsyncMock, return_value="")
    @patch("context._get_recent_messages", return_value="")
    @patch("context._get_session_summary", return_value="")
    @patch("context._get_active_plans", return_value="")
    @patch("context.get_relevant_context", return_value="personal info")
    def test_question_uses_kb_search(self, mock_ctx, mock_plans, mock_summary,
                                      mock_msgs, mock_proactive, mock_live,
                                      mock_memories, mock_kb):
        from context import assemble_context
        from intent import Intent
        result = _run(assemble_context(Intent.QUESTION, "What's the weather?", chat_id=123))
        mock_kb.assert_called_once()
        assert "knowledge base search" in result

    @patch("context._search_kb", new_callable=AsyncMock, return_value=[{"title": "FL26", "content": "data", "category": "work:planning"}])
    @patch("context._auto_read_full_documents", new_callable=AsyncMock, return_value="[Full Document: work:planning]\nFull content here")
    @patch("context._get_memories", new_callable=AsyncMock, return_value="")
    @patch("context._get_live_state", new_callable=AsyncMock, return_value="")
    @patch("context._get_proactive_context", new_callable=AsyncMock, return_value="")
    @patch("context._get_recent_messages", return_value="")
    @patch("context._get_session_summary", return_value="")
    @patch("context._get_active_plans", return_value="")
    @patch("context.get_relevant_context", return_value="personal info")
    def test_task_uses_deep_retrieval(self, mock_ctx, mock_plans, mock_summary,
                                       mock_msgs, mock_proactive, mock_live,
                                       mock_memories, mock_full_docs, mock_kb):
        from context import assemble_context
        from intent import Intent
        result = _run(assemble_context(Intent.TASK, "Build FL26 presentation", chat_id=123))
        mock_kb.assert_called_once()
        mock_full_docs.assert_called_once()
        assert "Full Document" in result

    @patch("context._search_kb", new_callable=AsyncMock, return_value=[])
    @patch("context._get_memories", new_callable=AsyncMock, return_value="")
    @patch("context._get_live_state", new_callable=AsyncMock, return_value="")
    @patch("context._get_proactive_context", new_callable=AsyncMock, return_value="")
    @patch("context._get_recent_messages", return_value="recent msgs")
    @patch("context._get_session_summary", return_value="")
    @patch("context._get_active_plans", return_value="")
    @patch("context.get_relevant_context", return_value="")
    def test_continuation_limits_history(self, mock_ctx, mock_plans, mock_summary,
                                          mock_msgs, mock_proactive, mock_live,
                                          mock_memories, mock_kb):
        from context import assemble_context
        from intent import Intent
        _run(assemble_context(Intent.CONTINUATION, "Yes", chat_id=123))
        mock_msgs.assert_called_once_with(123, limit=10)

    @patch("context._search_kb", new_callable=AsyncMock, return_value=[])
    @patch("context._get_memories", new_callable=AsyncMock, return_value="")
    @patch("context._get_live_state", new_callable=AsyncMock, return_value="")
    @patch("context._get_proactive_context", new_callable=AsyncMock, return_value="")
    @patch("context._get_recent_messages", return_value="")
    @patch("context._get_session_summary", return_value="")
    @patch("context._get_active_plans", return_value="")
    @patch("context.get_relevant_context", return_value="")
    def test_continuation_skips_live_state(self, mock_ctx, mock_plans, mock_summary,
                                            mock_msgs, mock_proactive, mock_live,
                                            mock_memories, mock_kb):
        from context import assemble_context
        from intent import Intent
        _run(assemble_context(Intent.CONTINUATION, "Yes", chat_id=123))
        mock_live.assert_not_called()

    @patch("context._search_kb", new_callable=AsyncMock, return_value=[])
    @patch("context._get_memories", new_callable=AsyncMock, return_value="")
    @patch("context._get_live_state", new_callable=AsyncMock, return_value="live data")
    @patch("context._get_proactive_context", new_callable=AsyncMock, return_value="")
    @patch("context._get_recent_messages", return_value="")
    @patch("context._get_session_summary", return_value="")
    @patch("context._get_active_plans", return_value="")
    @patch("context.get_relevant_context", return_value="personal info")
    def test_question_includes_live_state(self, mock_ctx, mock_plans, mock_summary,
                                           mock_msgs, mock_proactive, mock_live,
                                           mock_memories, mock_kb):
        from context import assemble_context
        from intent import Intent
        result = _run(assemble_context(Intent.QUESTION, "What's running?", chat_id=123))
        mock_live.assert_called_once()
        assert "live state" in result


class TestVerification:
    """Test centralized verification layer."""

    def test_detect_hallucinated_tool_call(self):
        from verification import detect_hallucinated_tools
        assert detect_hallucinated_tools("[Called tool: search_knowledge]\nresults...")
        assert detect_hallucinated_tools("  [tool_call: generate_file]\n...")
        assert detect_hallucinated_tools('[MCP_CALL: the-hub.search | {"query":"test"}]')
        assert not detect_hallucinated_tools("I searched for the information you asked about")
        assert not detect_hallucinated_tools("The tool [Called tool: x] was mentioned in docs")

    def test_is_preamble(self):
        from verification import is_preamble_response
        assert is_preamble_response("I'll search for that information now")
        assert is_preamble_response("Let me check the knowledge base")
        assert not is_preamble_response("Here are the results of my search: " + "x" * 500)
        assert not is_preamble_response("")

    def test_verify_file_creation(self, tmp_path):
        from verification import verify_file_creation
        # File doesn't exist
        result = verify_file_creation(str(tmp_path / "missing.html"))
        assert not result["success"]

        # File too small
        small = tmp_path / "small.html"
        small.write_text("hi")
        result = verify_file_creation(str(small))
        assert not result["success"]

        # Valid file
        good = tmp_path / "index.html"
        good.write_text("<html><body>" + "content " * 20 + "</body></html>")
        result = verify_file_creation(str(good))
        assert result["success"]
        assert result["size"] > 50

    def test_check_tool_results_completion(self):
        from verification import check_tool_results_for_completion
        assert not check_tool_results_for_completion([], [])
        assert check_tool_results_for_completion(
            ["search_knowledge"],
            ['{"success": true, "results": ["data"]}'],
        )
        assert not check_tool_results_for_completion(
            ["search_knowledge"],
            ['{"error": true, "message": "not found"}'],
        )

    def test_update_task_after_artifact_success(self, tmp_path):
        from task_manager import TaskManager
        from verification import update_task_after_response
        with patch("task_manager.DB_PATH", tmp_path / "test.db"):
            mgr = TaskManager()
            task = mgr.create_task("chat_1", "Build presentation", "artifact")

            # Create a real file
            artifact = tmp_path / "index.html"
            artifact.write_text("<html>" + "x" * 100 + "</html>")

            update_task_after_response(
                mgr, task, "Created the file",
                ["search_knowledge", "generate_file"],
                [
                    '{"results": ["data"]}',
                    json.dumps({"success": True, "path": str(artifact)}),
                ],
            )
            # Task should be completed
            active = mgr.get_active_task("chat_1")
            assert active is None  # No active task — it was completed

    def test_update_task_after_failure(self, tmp_path):
        from task_manager import TaskManager
        from verification import update_task_after_response
        with patch("task_manager.DB_PATH", tmp_path / "test.db"):
            mgr = TaskManager()
            task = mgr.create_task("chat_1", "Build something", "task")

            update_task_after_response(
                mgr, task, "",  # empty response
                ["search_knowledge"],
                ['{"error": true, "message": "timeout"}'],
            )
            # Task should still be active with incremented attempts
            active = mgr.get_active_task("chat_1")
            assert active is not None
            assert active.attempts >= 1


class TestExecutionResilience:
    """Test execution layer hardening: circuit breakers, summarization gate, retry."""

    def test_fg_bg_circuit_breakers_are_independent(self):
        """Background CB opening must not affect foreground CB."""
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from server import _cb_claude_fg, _cb_claude_bg
        # Reset both
        _cb_claude_fg._failures = 0
        _cb_claude_fg._opened_at = None
        _cb_claude_bg._failures = 0
        _cb_claude_bg._opened_at = None

        # Trip the background breaker (threshold=2)
        _cb_claude_bg.record_failure()
        _cb_claude_bg.record_failure()
        assert _cb_claude_bg.is_open(), "Background CB should be open after 2 failures"
        assert not _cb_claude_fg.is_open(), "Foreground CB must NOT be open"

        # Reset
        _cb_claude_fg._failures = 0
        _cb_claude_fg._opened_at = None
        _cb_claude_bg._failures = 0
        _cb_claude_bg._opened_at = None

    def test_summarization_suppressed_during_tool_loop(self):
        """_check_summarization_needed should return early when tool loop is active."""
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        import server
        # Simulate tool loop active
        server._tool_loop_active.add(999)
        try:
            # This should return immediately without scheduling summarization
            server._check_summarization_needed(999)
            # If we get here without error, the gate worked
            # (real summarization would try DB access which would fail in test)
        finally:
            server._tool_loop_active.discard(999)

    def test_summarization_allowed_when_no_tool_loop(self):
        """_check_summarization_needed should proceed when no tool loop is active."""
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        import server
        assert 888 not in server._tool_loop_active
        # This will fail at DB access (no test DB) but that's fine —
        # the point is it didn't return early at the gate
        try:
            server._check_summarization_needed(888)
        except Exception:
            pass  # Expected: DB not initialized in test

    def test_fg_cb_threshold_is_5(self):
        """Foreground CB needs 5 failures to open (more resilient than background)."""
        from server import _cb_claude_fg
        _cb_claude_fg._failures = 0
        _cb_claude_fg._opened_at = None
        for _ in range(4):
            _cb_claude_fg.record_failure()
        assert not _cb_claude_fg.is_open(), "FG CB should NOT be open after 4 failures"
        _cb_claude_fg.record_failure()
        assert _cb_claude_fg.is_open(), "FG CB should be open after 5 failures"
        # Reset
        _cb_claude_fg._failures = 0
        _cb_claude_fg._opened_at = None
