"""Shared fixtures for Khalil tests.

Provides:
- tmp_db: Temporary SQLite with full Khalil schema
- mock_update / mock_context: Fake Telegram Update/Context objects
- mock_ask_llm: Configurable mock LLM callable
- autonomy_controller: AutonomyController with tmp_db
"""

import json
import os
import sqlite3
import sys
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure khalil package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# --- Database Fixtures ---

@pytest.fixture
def tmp_db(tmp_path):
    """Create a temporary SQLite database with full Khalil schema."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS pending_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action_type TEXT NOT NULL,
            description TEXT,
            payload TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            resolved_at TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            action_type TEXT NOT NULL,
            description TEXT,
            payload TEXT,
            result TEXT,
            autonomy_level TEXT
        );

        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS interaction_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_type TEXT NOT NULL,
            context TEXT,
            value REAL DEFAULT 1.0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS insights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            summary TEXT NOT NULL,
            evidence TEXT,
            recommendation TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            resolved_at TIMESTAMP,
            resolved_by TEXT
        );

        CREATE TABLE IF NOT EXISTS learned_preferences (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            source_insight_id INTEGER,
            confidence REAL DEFAULT 0.5,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            due_at TIMESTAMP NOT NULL,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            fired_at TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS recurring_reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            cron_expression TEXT NOT NULL,
            next_fire_at TIMESTAMP NOT NULL,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS approval_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action_type TEXT NOT NULL,
            command_pattern TEXT NOT NULL,
            approved_count INTEGER DEFAULT 0,
            denied_count INTEGER DEFAULT 0,
            auto_tier TEXT DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(action_type, command_pattern)
        );

        CREATE TABLE IF NOT EXISTS activity_timing (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_type TEXT NOT NULL,
            hour INTEGER NOT NULL,
            day_of_week INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def tmp_db_with_learning(tmp_db):
    """tmp_db with learning module wired to use it."""
    import learning
    original = learning._db_conn
    learning._db_conn = tmp_db
    yield tmp_db
    learning._db_conn = original


# --- Telegram Mock Fixtures ---

class FakeMessage:
    """Minimal Telegram Message mock."""
    def __init__(self, text="", chat_id=12345):
        self.text = text
        self.chat = MagicMock()
        self.chat.id = chat_id
        self.from_user = MagicMock()
        self.from_user.id = chat_id
        self.from_user.first_name = "Ahmed"
        self.reply_text = AsyncMock()
        self.reply_html = AsyncMock()


class FakeUpdate:
    """Minimal Telegram Update mock."""
    def __init__(self, text="", chat_id=12345):
        self.message = FakeMessage(text, chat_id)
        self.effective_chat = MagicMock()
        self.effective_chat.id = chat_id
        self.callback_query = None
        self.update_id = 1


class FakeContext:
    """Minimal Telegram CallbackContext mock."""
    def __init__(self, args=None):
        self.args = args or []
        self.bot = MagicMock()
        self.bot.send_message = AsyncMock()
        self.bot_data = {}
        self.user_data = {}
        self.chat_data = {}


@pytest.fixture
def mock_update():
    """Create a fake Telegram Update for testing action handlers."""
    def _factory(text="test", chat_id=12345):
        return FakeUpdate(text, chat_id)
    return _factory


@pytest.fixture
def mock_context():
    """Create a fake Telegram Context for testing action handlers."""
    def _factory(args=None):
        return FakeContext(args)
    return _factory


# --- LLM Mock Fixtures ---

@pytest.fixture
def mock_ask_llm():
    """Configurable mock LLM. Returns canned responses or a callable.

    Usage:
        async def test_something(mock_ask_llm):
            ask = mock_ask_llm("fixed response")
            result = await ask("any query", "any context")
            assert result == "fixed response"

        # Or with a callable:
            ask = mock_ask_llm(lambda q, c, s="": f"Response to: {q}")
    """
    def _factory(response="Mock LLM response"):
        if callable(response):
            return AsyncMock(side_effect=response)
        return AsyncMock(return_value=response)
    return _factory


# --- Autonomy Fixtures ---

@pytest.fixture
def autonomy_controller(tmp_db):
    """AutonomyController backed by tmp_db."""
    from autonomy import AutonomyController
    return AutonomyController(tmp_db)
