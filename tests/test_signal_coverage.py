"""Tests for signal coverage and healing trigger improvements."""

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from healing import detect_recurring_failures, _extract_targets_from_traceback, CRITICAL_ERROR_PATTERNS


@pytest.fixture
def signal_db(tmp_path):
    """Create a test database with the interaction_signals and insights tables."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE interaction_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_type TEXT NOT NULL,
            context TEXT,
            value REAL DEFAULT 1.0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE insights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            summary TEXT,
            evidence TEXT,
            recommendation TEXT,
            auto_apply INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    yield conn, db_path
    conn.close()


class TestExtensionFailureTrigger:
    def test_extension_failure_detected(self, signal_db, monkeypatch):
        conn, db_path = signal_db
        # Insert 3 extension_runtime_failure signals
        for i in range(3):
            conn.execute(
                "INSERT INTO interaction_signals (signal_type, context) VALUES (?, ?)",
                ("extension_runtime_failure", json.dumps({"extension": "slack_reader", "error": "KeyError: 'channel'"})),
            )
        conn.commit()

        # Monkeypatch _get_conn to return our test DB
        import healing
        monkeypatch.setattr(healing, "_get_conn", lambda: conn)
        monkeypatch.setattr(healing, "HEALING_FAILURE_THRESHOLD", 3)

        triggers = detect_recurring_failures()
        assert len(triggers) >= 1
        assert any("extension_runtime_failure" in t["fingerprint"] for t in triggers)


class TestCriticalErrorThreshold:
    def test_import_error_triggers_after_one(self, signal_db, monkeypatch):
        conn, db_path = signal_db
        conn.execute(
            "INSERT INTO interaction_signals (signal_type, context) VALUES (?, ?)",
            ("extension_runtime_failure", json.dumps({
                "extension": "slack_reader",
                "error": "ImportError: No module named 'slack_sdk'",
            })),
        )
        conn.commit()

        import healing
        monkeypatch.setattr(healing, "_get_conn", lambda: conn)
        monkeypatch.setattr(healing, "HEALING_FAILURE_THRESHOLD", 3)

        triggers = detect_recurring_failures()
        assert len(triggers) == 1, "ImportError should trigger healing after 1 occurrence"

    def test_non_critical_needs_threshold(self, signal_db, monkeypatch):
        conn, db_path = signal_db
        # Just 1 non-critical failure — should NOT trigger
        conn.execute(
            "INSERT INTO interaction_signals (signal_type, context) VALUES (?, ?)",
            ("action_execution_failure", json.dumps({"action": "shell", "error": "exit code 1"})),
        )
        conn.commit()

        import healing
        monkeypatch.setattr(healing, "_get_conn", lambda: conn)
        monkeypatch.setattr(healing, "HEALING_FAILURE_THRESHOLD", 3)

        triggers = detect_recurring_failures()
        assert len(triggers) == 0


class TestDynamicCodeMap:
    def test_extract_from_traceback(self):
        signals = [{
            "context": {
                "error": (
                    'Traceback (most recent call last):\n'
                    '  File "/Users/ahmed/scripts/khalil/actions/slack_reader.py", line 45, in cmd_slack\n'
                    '    channel = get_channel_id(name)\n'
                    'KeyError: "channel"'
                ),
            },
        }]
        targets = _extract_targets_from_traceback(signals)
        assert targets is not None
        assert targets[0] == ("actions/slack_reader.py", "cmd_slack")

    def test_ignores_wrapper_frames(self):
        signals = [{
            "context": {
                "error": (
                    'Traceback (most recent call last):\n'
                    '  File "/Users/ahmed/scripts/khalil/server.py", line 100, in wrapper\n'
                    '    return await handler(update, context)\n'
                    '  File "/Users/ahmed/scripts/khalil/actions/timer.py", line 20, in cmd_timer\n'
                    '    raise ValueError("bad")\n'
                    'ValueError: bad'
                ),
            },
        }]
        targets = _extract_targets_from_traceback(signals)
        assert targets is not None
        assert targets[0] == ("actions/timer.py", "cmd_timer")

    def test_no_traceback_returns_none(self):
        signals = [{"context": {"error": "something went wrong"}}]
        assert _extract_targets_from_traceback(signals) is None

    def test_empty_signals(self):
        assert _extract_targets_from_traceback([]) is None
