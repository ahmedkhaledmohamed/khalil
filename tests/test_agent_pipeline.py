"""Tests for the agent pipeline: intent classification + task state management."""

import json
import os
import sqlite3
import sys
from unittest.mock import patch

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
