"""Tests for the batch 1 improvements (items #17, #33, #59, #73, #15, #16, #69, #79, #23, #45)."""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# --- #17: SQLite WAL Mode ---

class TestSQLiteWAL:
    def test_init_db_sets_wal_mode(self, tmp_path, monkeypatch):
        """init_db should enable WAL journal mode."""
        db_path = tmp_path / "data" / "khalil.db"
        monkeypatch.setattr("config.DB_PATH", db_path)
        monkeypatch.setattr("config.DATA_DIR", tmp_path / "data")
        from knowledge.indexer import init_db
        conn = init_db()
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
        conn.close()

    def test_init_db_sets_row_factory(self, tmp_path, monkeypatch):
        db_path = tmp_path / "data" / "khalil.db"
        monkeypatch.setattr("config.DB_PATH", db_path)
        monkeypatch.setattr("config.DATA_DIR", tmp_path / "data")
        from knowledge.indexer import init_db
        conn = init_db()
        assert conn.row_factory == sqlite3.Row
        conn.close()


# --- #33: Expanded SAFE_PREFIXES ---

class TestExpandedSafePrefixes:
    @pytest.mark.parametrize("cmd", [
        "grep -r pattern .",
        "awk '{print $1}' file.txt",
        "sed -n '1,5p' file.txt",
        "mdfind 'kMDItemFSName == *.py'",
        "mdls ~/file.txt",
        "lsof -i :8080",
        "netstat -an",
        "softwareupdate --list",
        "sort data.txt",
        "uniq -c lines.txt",
        "cut -d',' -f1 data.csv",
        "diff file1.txt file2.txt",
        "pbcopy",
    ])
    def test_new_safe_commands(self, cmd):
        from actions.shell import classify_command
        from config import ActionType
        result = classify_command(cmd)
        assert result == ActionType.READ, f"{cmd} should be READ, got {result}"


# --- #73: Shell Command Injection Prevention ---

class TestShellSanitization:
    def test_rejects_chained_commands(self):
        from actions.shell import sanitize_command
        cmd, reason = sanitize_command("ls; rm -rf /")
        assert cmd is None
        assert "chaining" in reason.lower()

    def test_rejects_double_ampersand(self):
        from actions.shell import sanitize_command
        cmd, reason = sanitize_command("echo hi && rm -rf /")
        assert cmd is None

    def test_rejects_backticks(self):
        from actions.shell import sanitize_command
        cmd, reason = sanitize_command("echo `whoami`")
        assert cmd is None
        assert "subshell" in reason.lower()

    def test_rejects_dollar_paren(self):
        from actions.shell import sanitize_command
        cmd, reason = sanitize_command("echo $(cat /etc/passwd)")
        assert cmd is None

    def test_rejects_null_bytes(self):
        from actions.shell import sanitize_command
        cmd, reason = sanitize_command("ls\x00-la")
        assert cmd is None

    def test_allows_pipes(self):
        """Pipes are allowed for read-only pipelines like 'ps aux | grep python'."""
        from actions.shell import sanitize_command
        cmd, reason = sanitize_command("ps aux | grep python")
        assert cmd is not None
        assert reason == ""

    def test_allows_normal_commands(self):
        from actions.shell import sanitize_command
        cmd, reason = sanitize_command("ls -la ~/Desktop")
        assert cmd == "ls -la ~/Desktop"
        assert reason == ""

    def test_execute_shell_rejects_injected(self):
        import asyncio
        from actions.shell import execute_shell
        result = asyncio.run(execute_shell("echo hi; rm -rf /"))
        assert result["returncode"] == -2
        assert "rejected" in result["stderr"].lower()


# --- #69: Context-Aware Autonomy ---

class TestContextAwareAutonomy:
    def test_default_no_context_awareness(self, tmp_db):
        """Without the setting enabled, autonomy level should not change."""
        from autonomy import AutonomyController
        from config import AutonomyLevel
        ctrl = AutonomyController(tmp_db)
        ctrl.set_level(AutonomyLevel.AUTONOMOUS)
        # Default: context_aware_autonomy not set
        assert ctrl._effective_level() == AutonomyLevel.AUTONOMOUS

    def test_context_aware_enabled(self, tmp_db):
        """With context_aware_autonomy=1, effective level depends on time."""
        from autonomy import AutonomyController
        from config import AutonomyLevel
        tmp_db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('context_aware_autonomy', '1')")
        tmp_db.commit()
        ctrl = AutonomyController(tmp_db)
        ctrl.set_level(AutonomyLevel.AUTONOMOUS)
        # Just verify it returns a valid level (actual result depends on time of day)
        level = ctrl._effective_level()
        assert isinstance(level, AutonomyLevel)


# --- #79: Test Harness Fixtures ---

class TestHarnessFixtures:
    def test_tmp_db_has_all_tables(self, tmp_db):
        """tmp_db should have all Khalil tables."""
        tables = [row[0] for row in tmp_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()]
        expected = [
            "audit_log", "conversations", "documents", "insights",
            "interaction_signals", "learned_preferences", "pending_actions",
            "recurring_reminders", "reminders", "settings",
        ]
        for table in expected:
            assert table in tables, f"Missing table: {table}"

    def test_mock_update_factory(self, mock_update):
        update = mock_update("hello world", chat_id=99)
        assert update.message.text == "hello world"
        assert update.effective_chat.id == 99

    def test_mock_context_factory(self, mock_context):
        ctx = mock_context(args=["add", "test"])
        assert ctx.args == ["add", "test"]

    def test_mock_ask_llm(self, mock_ask_llm):
        import asyncio
        ask = mock_ask_llm("test response")
        result = asyncio.run(ask("query", "context"))
        assert result == "test response"

    def test_autonomy_controller_fixture(self, autonomy_controller):
        from config import AutonomyLevel
        assert autonomy_controller.level == AutonomyLevel.SUPERVISED

    def test_tmp_db_with_learning(self, tmp_db_with_learning):
        """learning module should use the test DB."""
        from learning import record_signal
        record_signal("test_signal", {"test": True})
        row = tmp_db_with_learning.execute(
            "SELECT signal_type FROM interaction_signals WHERE signal_type = 'test_signal'"
        ).fetchone()
        assert row is not None


# --- #15: OAuth Token Health Check ---

class TestOAuthUtils:
    def test_check_token_health_missing(self, tmp_path):
        from oauth_utils import check_token_health
        result = check_token_health(tmp_path / "nonexistent.json")
        assert result["status"] == "missing"

    def test_check_all_tokens_returns_list(self):
        from oauth_utils import check_all_tokens
        results = check_all_tokens()
        assert isinstance(results, list)
        assert len(results) == 3  # gmail_readonly, gmail_compose, calendar
        for r in results:
            assert "name" in r
            assert "status" in r
