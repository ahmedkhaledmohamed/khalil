"""Tests for heal verification loop."""

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

# Stub heavy imports before loading healing.py
for mod_name in ["anthropic", "httpx", "keyring"]:
    if mod_name not in sys.modules:
        sys.modules[mod_name] = MagicMock()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from healing import check_heal_outcomes


@pytest.fixture
def heal_db(tmp_path):
    """Create a test database with signals and insights tables."""
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
    yield conn
    conn.close()


class TestHealVerification:
    def test_detects_recurrence(self, heal_db, monkeypatch):
        """If a heal was applied but failures recur, mark as failed_heal."""
        import healing
        monkeypatch.setattr(healing, "_get_conn", lambda: heal_db)

        # Insert a self_heal insight from 2 days ago
        two_days_ago = (datetime.utcnow() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        heal_db.execute(
            "INSERT INTO insights (category, summary, evidence, created_at) VALUES (?, ?, ?, ?)",
            ("self_heal", "Fixed shell intent", "Fingerprint: action_execution_failure:shell", two_days_ago),
        )
        # Insert a failure signal AFTER the heal
        one_day_ago = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        heal_db.execute(
            "INSERT INTO interaction_signals (signal_type, context, created_at) VALUES (?, ?, ?)",
            ("action_execution_failure", json.dumps({"action": "shell"}), one_day_ago),
        )
        heal_db.commit()

        failed = check_heal_outcomes()
        assert len(failed) == 1
        assert failed[0]["fingerprint"] == "action_execution_failure:shell"

        # Verify the insight was marked as failed
        row = heal_db.execute("SELECT summary FROM insights WHERE id = ?", (failed[0]["insight_id"],)).fetchone()
        assert "[failed_heal]" in row["summary"]

    def test_no_recurrence_stays_clean(self, heal_db, monkeypatch):
        """If no failures recur after heal, don't mark as failed."""
        import healing
        monkeypatch.setattr(healing, "_get_conn", lambda: heal_db)

        two_days_ago = (datetime.utcnow() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        heal_db.execute(
            "INSERT INTO insights (category, summary, evidence, created_at) VALUES (?, ?, ?, ?)",
            ("self_heal", "Fixed shell intent", "Fingerprint: action_execution_failure:shell", two_days_ago),
        )
        heal_db.commit()

        failed = check_heal_outcomes()
        assert len(failed) == 0

    def test_already_failed_not_rechecked(self, heal_db, monkeypatch):
        """Insights already marked as failed_heal are skipped."""
        import healing
        monkeypatch.setattr(healing, "_get_conn", lambda: heal_db)

        heal_db.execute(
            "INSERT INTO insights (category, summary, evidence) VALUES (?, ?, ?)",
            ("self_heal", "[failed_heal] Fixed shell intent", "Fingerprint: action_execution_failure:shell"),
        )
        heal_db.commit()

        failed = check_heal_outcomes()
        assert len(failed) == 0
