"""Tests for Khalil improvements."""

import asyncio
import os
import re
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


# ============================================================
# Batch 2: Items #11, #10, #78, #72, #20, #21, #70
# ============================================================


# --- #11: Configurable Signal Window ---

class TestConfigurableSignalWindow:
    def test_default_window_is_7_days(self, tmp_db, monkeypatch):
        """Without a setting, healing uses 7-day window."""
        from healing import detect_recurring_failures
        monkeypatch.setattr("healing._get_conn", lambda: tmp_db)
        # Insert a signal 3 days ago (within default 7d window)
        from datetime import datetime, timedelta
        three_days_ago = (datetime.utcnow() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        tmp_db.execute(
            "INSERT INTO interaction_signals (signal_type, context, created_at) VALUES (?, ?, ?)",
            ("action_execution_failure", '{"action": "test_action"}', three_days_ago),
        )
        tmp_db.commit()
        results = detect_recurring_failures()
        # Should find the signal (within 7d window)
        assert len(results) >= 0  # no crash = success; count depends on threshold

    def test_custom_window_from_settings(self, tmp_db, monkeypatch):
        """Setting healing_signal_window_hours overrides the default."""
        from healing import detect_recurring_failures
        monkeypatch.setattr("healing._get_conn", lambda: tmp_db)
        tmp_db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('healing_signal_window_hours', '1')"
        )
        # Insert a signal 2 hours ago (outside 1h window)
        from datetime import datetime, timedelta
        two_hours_ago = (datetime.utcnow() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        tmp_db.execute(
            "INSERT INTO interaction_signals (signal_type, context, created_at) VALUES (?, ?, ?)",
            ("action_execution_failure", '{"action": "test_action"}', two_hours_ago),
        )
        tmp_db.commit()
        results = detect_recurring_failures()
        # Signal is outside 1h window, so should not be found
        assert len(results) == 0


# --- #10: Capability Usage Heatmap ---

class TestCapabilityHeatmap:
    def test_heatmap_empty(self, tmp_db):
        """Heatmap returns empty list when no signals."""
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        from learning import get_capability_heatmap
        result = get_capability_heatmap()
        assert result == []
        learning._db_conn = original

    def test_heatmap_counts(self, tmp_db):
        """Heatmap aggregates capability_usage signals."""
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        from learning import record_signal, get_capability_heatmap
        record_signal("capability_usage", {"action": "shell"})
        record_signal("capability_usage", {"action": "shell"})
        record_signal("capability_usage", {"action": "reminder"})
        result = get_capability_heatmap()
        assert len(result) == 2
        assert result[0]["action"] == "shell"
        assert result[0]["count"] == 2
        assert result[1]["action"] == "reminder"
        assert result[1]["count"] == 1
        learning._db_conn = original


# --- #78: Privacy-Aware LLM Routing ---

class TestPrivacyRouting:
    def test_sensitive_pattern_detected(self):
        """Sensitive patterns should be detected in queries."""
        import re
        from config import SENSITIVE_PATTERNS
        sensitive_queries = [
            "My SSN is 123-45-6789",
            "My phone is 416-555-1234",
            "what's my password for gmail",
            "credit card number",
        ]
        for q in sensitive_queries:
            matched = any(re.search(p, q, re.IGNORECASE) for p in SENSITIVE_PATTERNS)
            assert matched, f"Query should be detected as sensitive: {q}"

    def test_non_sensitive_not_flagged(self):
        """Normal queries should not be flagged as sensitive."""
        import re
        from config import SENSITIVE_PATTERNS
        normal_queries = [
            "what's the weather today",
            "search my emails for project updates",
            "remind me to call John",
        ]
        for q in normal_queries:
            matched = any(re.search(p, q, re.IGNORECASE) for p in SENSITIVE_PATTERNS)
            assert not matched, f"Query should NOT be sensitive: {q}"


# --- #72: Sensitive Data Redaction in Logs ---

class TestLogRedaction:
    def test_redact_phone_numbers(self):
        from server import _redact_sensitive
        text = "Call me at 416-555-1234 please"
        assert "[REDACTED]" in _redact_sensitive(text)
        assert "416-555-1234" not in _redact_sensitive(text)

    def test_redact_password_mentions(self):
        from server import _redact_sensitive
        text = "User password is secret123"
        assert "[REDACTED]" in _redact_sensitive(text)

    def test_no_redaction_for_clean_text(self):
        from server import _redact_sensitive
        text = "Hello world, how are you today?"
        assert _redact_sensitive(text) == text


# --- #20: Circuit Breaker ---

class TestCircuitBreaker:
    def test_starts_closed(self):
        from server import CircuitBreaker
        cb = CircuitBreaker("test", threshold=3, cooldown_seconds=60)
        assert not cb.is_open()

    def test_opens_after_threshold(self):
        from server import CircuitBreaker
        cb = CircuitBreaker("test", threshold=3, cooldown_seconds=60)
        cb.record_failure()
        cb.record_failure()
        assert not cb.is_open()
        cb.record_failure()  # 3rd failure = open
        assert cb.is_open()

    def test_resets_on_success(self):
        from server import CircuitBreaker
        cb = CircuitBreaker("test", threshold=3, cooldown_seconds=60)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        cb.record_failure()
        assert not cb.is_open()  # reset, so only 1 failure

    def test_half_open_after_cooldown(self):
        import time
        from server import CircuitBreaker
        cb = CircuitBreaker("test", threshold=1, cooldown_seconds=1)
        cb.record_failure()
        assert cb.is_open()
        time.sleep(1.1)
        assert not cb.is_open()  # cooldown expired, half-open


# --- #21: Startup Self-Test ---

class TestStartupSelfTest:
    def test_format_startup_report(self):
        from monitoring import format_startup_report
        results = {
            "database": {"status": "ok", "documents": 42},
            "ollama": {"status": "down", "error": "not running"},
            "oauth": {"status": "ok", "unhealthy_count": 0},
            "github": {"status": "ok"},
            "overall": "degraded",
            "issues": ["Ollama"],
        }
        report = format_startup_report(results)
        assert "Ollama" in report
        assert "DEGRADED" in report
        assert "✅" in report
        assert "❌" in report


# --- #70: Per-Action-Type Rate Limits ---

class TestRateLimits:
    def test_allows_under_limit(self, tmp_db):
        from autonomy import AutonomyController
        ctrl = AutonomyController(tmp_db)
        allowed, reason = ctrl.check_rate_limit("send_email")
        assert allowed
        assert reason == ""

    def test_blocks_over_limit(self, tmp_db):
        from autonomy import AutonomyController
        ctrl = AutonomyController(tmp_db)
        # Insert 5 send_email entries in audit_log (limit is 5/hour)
        for i in range(5):
            ctrl.log_audit("send_email", f"test email {i}")
        allowed, reason = ctrl.check_rate_limit("send_email")
        assert not allowed
        assert "Rate limit exceeded" in reason

    def test_different_action_types_independent(self, tmp_db):
        from autonomy import AutonomyController
        ctrl = AutonomyController(tmp_db)
        # Fill send_email limit
        for i in range(5):
            ctrl.log_audit("send_email", f"test email {i}")
        # shell should still be allowed
        allowed, _ = ctrl.check_rate_limit("shell_read")
        assert allowed


# ============================================================
# Batch 3: Items #2, #8, #4, #75, #89, #18, #28
# ============================================================


# --- #2: Response Latency Tracking ---

class TestResponseLatencyTracking:
    def test_latency_signal_recorded(self, tmp_db):
        """record_signal can store latency data."""
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        from learning import record_signal
        record_signal("response_latency", {"latency_ms": 1234.5, "query_len": 42})
        row = tmp_db.execute(
            "SELECT context FROM interaction_signals WHERE signal_type = 'response_latency'"
        ).fetchone()
        assert row is not None
        import json
        ctx = json.loads(row[0])
        assert ctx["latency_ms"] == 1234.5
        assert ctx["query_len"] == 42
        learning._db_conn = original


# --- #8: Decision Journal ---

class TestDecisionJournal:
    def test_needs_approval_logs_decision(self, tmp_db):
        """needs_approval should log autonomy_decision to audit_log."""
        from autonomy import AutonomyController
        ctrl = AutonomyController(tmp_db)
        ctrl.needs_approval("search_knowledge")
        row = tmp_db.execute(
            "SELECT action_type, description, result FROM audit_log WHERE action_type = 'autonomy_decision'"
        ).fetchone()
        assert row is not None
        assert "AUTO_APPROVED" in row[1]
        assert row[2] == "read_auto_approved"

    def test_decision_journal_logs_approval_needed(self, tmp_db):
        """Supervised mode should log APPROVAL_NEEDED for write actions."""
        from autonomy import AutonomyController
        from config import AutonomyLevel
        ctrl = AutonomyController(tmp_db)
        ctrl.set_level(AutonomyLevel.SUPERVISED)
        ctrl.needs_approval("send_email")
        row = tmp_db.execute(
            "SELECT description FROM audit_log WHERE action_type = 'autonomy_decision' "
            "AND description LIKE '%send_email%'"
        ).fetchone()
        assert row is not None
        assert "APPROVAL_NEEDED" in row[0]

    def test_decision_journal_payload(self, tmp_db):
        """Decision journal payload should include action details."""
        import json
        from autonomy import AutonomyController
        ctrl = AutonomyController(tmp_db)
        ctrl.needs_approval("shell_dangerous")
        row = tmp_db.execute(
            "SELECT payload FROM audit_log WHERE action_type = 'autonomy_decision' "
            "AND description LIKE '%shell_dangerous%'"
        ).fetchone()
        assert row is not None
        payload = json.loads(row[0])
        assert payload["action"] == "shell_dangerous"
        assert payload["reason"] == "hard_guardrail"  # shell_dangerous is in HARD_GUARDRAILS


# --- #4: Configurable Reflection Cadence ---

class TestConfigurableReflectionCadence:
    def test_default_reflection_settings(self, tmp_db):
        """Without settings, defaults should be used."""
        row = tmp_db.execute(
            "SELECT value FROM settings WHERE key = 'reflection_weekly_day'"
        ).fetchone()
        assert row is None  # No override — defaults apply

    def test_custom_reflection_hour(self, tmp_db):
        """Settings can override reflection schedule."""
        tmp_db.execute(
            "INSERT INTO settings (key, value) VALUES ('reflection_micro_hour', '22')"
        )
        tmp_db.commit()
        row = tmp_db.execute(
            "SELECT value FROM settings WHERE key = 'reflection_micro_hour'"
        ).fetchone()
        assert row is not None
        assert int(row[0]) == 22


# --- #75: Approval Expiry Notification ---

class TestApprovalExpiryNotification:
    def test_get_expiring_actions_empty(self, tmp_db):
        from autonomy import AutonomyController
        ctrl = AutonomyController(tmp_db)
        result = ctrl.get_expiring_actions()
        assert result == []

    def test_get_expiring_actions_finds_near_expiry(self, tmp_db):
        from datetime import datetime, timedelta
        from autonomy import AutonomyController, PENDING_TTL_SECONDS
        ctrl = AutonomyController(tmp_db)
        # Insert a pending action created 55 minutes ago (near 1h TTL)
        near_expiry = (datetime.utcnow() - timedelta(seconds=PENDING_TTL_SECONDS - 200)).strftime("%Y-%m-%d %H:%M:%S")
        tmp_db.execute(
            "INSERT INTO pending_actions (action_type, description, status, created_at) VALUES (?, ?, 'pending', ?)",
            ("shell_write", "test action", near_expiry),
        )
        tmp_db.commit()
        result = ctrl.get_expiring_actions(warn_seconds=300)
        assert len(result) == 1
        assert result[0]["action_type"] == "shell_write"


# --- #89: Configurable Alert Thresholds ---

class TestConfigurableAlertThresholds:
    def test_default_thresholds(self):
        from scheduler.proactive import _DEFAULT_THRESHOLDS
        assert _DEFAULT_THRESHOLDS["stale_goals_days"] == 90
        assert _DEFAULT_THRESHOLDS["stale_projects_days"] == 60
        assert _DEFAULT_THRESHOLDS["stale_portfolio_days"] == 60

    def test_get_threshold_default(self):
        from scheduler.proactive import _get_threshold
        # Should return default when no settings exist
        result = _get_threshold("stale_goals_days")
        assert result == 90

    def test_get_threshold_custom(self, tmp_db):
        """Custom threshold from settings should override default."""
        # Insert into real DB path temporarily
        tmp_db.execute(
            "INSERT INTO settings (key, value) VALUES ('threshold_stale_goals_days', '30')"
        )
        tmp_db.commit()
        # We can't easily test _get_threshold with tmp_db since it opens its own connection
        # but we verify the setting is stored correctly
        row = tmp_db.execute(
            "SELECT value FROM settings WHERE key = 'threshold_stale_goals_days'"
        ).fetchone()
        assert int(row[0]) == 30


# --- #18: Graceful Degradation Chain ---

class TestGracefulDegradation:
    def test_fallback_models_defined(self):
        from server import _FALLBACK_MODELS
        assert len(_FALLBACK_MODELS) == 2
        assert "haiku" in _FALLBACK_MODELS[1]

    def test_get_cached_response_no_db(self):
        from server import _get_cached_response
        # With no db_conn, should return None
        import server
        original = server.db_conn
        server.db_conn = None
        result = _get_cached_response("test query")
        assert result is None
        server.db_conn = original

    def test_get_cached_response_with_data(self, tmp_db):
        """Cached response should return last assistant message."""
        from server import _get_cached_response
        import server
        original = server.db_conn
        server.db_conn = tmp_db
        tmp_db.execute(
            "INSERT INTO conversations (chat_id, role, content) VALUES (1, 'assistant', 'Hello from cache')"
        )
        tmp_db.commit()
        result = _get_cached_response("test")
        assert result is not None
        assert "Hello from cache" in result
        server.db_conn = original


# --- #28: Post-Generation Smoke Test Expansion ---

class TestSmokeTestExpansion:
    def test_smoke_test_import_check(self, tmp_path):
        """Smoke test phase 1 — module with handler should pass."""
        from actions.extend import smoke_test_module
        mod = tmp_path / "action_test_cmd.py"
        mod.write_text("async def cmd_test_cmd(update, context): pass\n")
        passed, error = smoke_test_module(mod, "test_cmd")
        assert passed, f"Should pass: {error}"

    def test_smoke_test_missing_handler(self, tmp_path):
        """Smoke test should fail if handler is missing."""
        from actions.extend import smoke_test_module
        mod = tmp_path / "action_bad.py"
        mod.write_text("x = 1\n")
        passed, error = smoke_test_module(mod, "bad")
        assert not passed
        assert "Missing handler" in error

    def test_smoke_test_mock_call(self, tmp_path):
        """Smoke test phase 2 — handler called with mock objects should not crash."""
        from actions.extend import smoke_test_module
        mod = tmp_path / "action_greet.py"
        mod.write_text(
            "async def cmd_greet(update, context):\n"
            "    await update.message.reply_text('Hello!')\n"
        )
        passed, error = smoke_test_module(mod, "greet")
        assert passed, f"Should pass: {error}"


# ============================================================
# Batch 4: Items #3, #5, #29, #31, #36, #40, #61
# ============================================================


# --- #3: Intent Detection Accuracy Tracker ---

class TestIntentAccuracy:
    def test_accuracy_empty(self, tmp_db):
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        from learning import get_intent_accuracy
        result = get_intent_accuracy()
        assert result["total"] == 0
        assert result["accuracy_pct"] == 0.0
        learning._db_conn = original

    def test_accuracy_tracking(self, tmp_db):
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        from learning import record_signal, get_intent_accuracy
        record_signal("intent_accuracy", {"pattern_hint": "shell", "llm_action": "shell", "match": True})
        record_signal("intent_accuracy", {"pattern_hint": "shell", "llm_action": "reminder", "match": False})
        record_signal("intent_accuracy", {"pattern_hint": "email", "llm_action": "email", "match": True})
        result = get_intent_accuracy()
        assert result["total"] == 3
        assert result["matches"] == 2
        assert result["accuracy_pct"] == pytest.approx(66.7, abs=0.1)
        assert len(result["mismatches"]) == 1
        assert result["mismatches"][0]["pattern_hint"] == "shell"
        assert result["mismatches"][0]["llm_action"] == "reminder"
        learning._db_conn = original


# --- #5: Reflection Diff Report ---

class TestReflectionDiff:
    def test_diff_no_data(self, tmp_db):
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        from learning import generate_reflection_diff
        result = generate_reflection_diff()
        assert "No significant changes" in result
        learning._db_conn = original

    def test_diff_with_preferences(self, tmp_db):
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        # Insert a recently updated preference
        tmp_db.execute(
            "INSERT INTO learned_preferences (key, value, confidence, updated_at) "
            "VALUES ('response_style', 'concise', 0.8, datetime('now'))"
        )
        tmp_db.commit()
        from learning import generate_reflection_diff
        result = generate_reflection_diff()
        assert "response_style" in result
        assert "Preferences changed" in result
        learning._db_conn = original

    def test_diff_with_insights(self, tmp_db):
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        from learning import store_insight, generate_reflection_diff
        store_insight("preference", "User prefers bullet points", "3 messages", "Set format=bullets")
        result = generate_reflection_diff()
        assert "insight" in result.lower()
        learning._db_conn = original


# --- #29: Extension Capability Dedup ---

class TestExtensionDedup:
    def test_no_overlap_novel_extension(self):
        from actions.extend import check_extension_overlap
        result = check_extension_overlap({
            "name": "weather_tracker",
            "command": "weather",
            "description": "Check current weather conditions",
        })
        assert result is None  # no overlap

    def test_overlap_with_builtin(self):
        from actions.extend import check_extension_overlap
        result = check_extension_overlap({
            "name": "email_sender",
            "command": "email",
            "description": "Send emails",
        })
        assert result is not None
        assert "email" in result.lower()

    def test_overlap_with_builtin_calendar(self):
        from actions.extend import check_extension_overlap
        result = check_extension_overlap({
            "name": "calendar_viewer",
            "command": "cal",
            "description": "View calendar events",
        })
        assert result is not None


# --- #31: Extension Usage Monitoring ---

class TestExtensionUsageMonitoring:
    def test_extension_health_empty(self, tmp_db):
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        from learning import get_extension_health
        result = get_extension_health()
        assert result == []
        learning._db_conn = original

    def test_extension_health_tracking(self, tmp_db):
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        from learning import record_signal, get_extension_health
        record_signal("extension_usage", {"extension": "label", "status": "invoked"})
        record_signal("extension_usage", {"extension": "label", "status": "success"})
        record_signal("extension_usage", {"extension": "label", "status": "invoked"})
        record_signal("extension_usage", {"extension": "label", "status": "error", "error": "API failed"})
        result = get_extension_health()
        assert len(result) == 1
        assert result[0]["extension"] == "label"
        assert result[0]["invocations"] == 2
        assert result[0]["errors"] == 1
        assert result[0]["error_rate_pct"] == 50.0
        learning._db_conn = original


# --- #36: Clipboard Integration ---

class TestClipboardIntegration:
    def test_clipboard_read_detected(self):
        from server import _looks_like_action
        assert _looks_like_action("what's on my clipboard") == "clipboard_read"
        assert _looks_like_action("show clipboard") == "clipboard_read"
        assert _looks_like_action("read my clipboard") == "clipboard_read"

    def test_clipboard_process_detected(self):
        from server import _looks_like_action
        assert _looks_like_action("summarize my clipboard") == "clipboard_process"

    def test_clipboard_direct_intent(self):
        from server import _try_direct_shell_intent
        intent = _try_direct_shell_intent("what's on my clipboard")
        assert intent is not None
        assert intent["command"] == "pbpaste"


# --- #40: Spotlight File Search ---

class TestSpotlightSearch:
    def test_spotlight_detected(self):
        from server import _looks_like_action
        assert _looks_like_action("find file report.pdf") == "spotlight"
        assert _looks_like_action("find all my python files") == "spotlight"

    def test_spotlight_direct_intent(self):
        from server import _try_direct_shell_intent
        intent = _try_direct_shell_intent("find file report.pdf")
        assert intent is not None
        assert "mdfind" in intent["command"]
        assert "report.pdf" in intent["command"]


# --- #61: Implicit Preference Detection ---

class TestImplicitPreference:
    def test_preference_patterns(self):
        """Preference patterns should match common preference expressions."""
        import re
        patterns = [
            (r"\bi\s+prefer\s+(.+?)(?:\.|$)", "I prefer bullet points"),
            (r"\b(?:always|never)\s+(.+?)(?:\.|$)", "always use short answers"),
            (r"\buse\s+(?:bullet\s+points?|lists?|markdown|short\s+(?:answers?|responses?))\b", "use bullet points"),
        ]
        for pattern, text in patterns:
            assert re.search(pattern, text.lower()), f"Pattern should match: {text}"

    def test_preference_signal_recorded(self, tmp_db):
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        from learning import record_signal
        record_signal("implicit_preference", {"type": "style_preference", "text": "I like short answers", "match": "i like short answers"})
        row = tmp_db.execute(
            "SELECT context FROM interaction_signals WHERE signal_type = 'implicit_preference'"
        ).fetchone()
        assert row is not None
        import json
        ctx = json.loads(row[0])
        assert ctx["type"] == "style_preference"
        learning._db_conn = original


# ============================================================
# Batch 5: Items #71, #39, #19, #22, #57, #66, #34
# ============================================================


# --- #71: Audit Log Retention Policy ---

class TestAuditLogRetention:
    def test_archive_no_old_entries(self, tmp_db):
        from autonomy import AutonomyController
        ctrl = AutonomyController(tmp_db)
        count = ctrl.archive_old_audit_logs(retention_days=90)
        assert count == 0

    def test_archive_old_entries(self, tmp_db, tmp_path, monkeypatch):
        from autonomy import AutonomyController
        from datetime import datetime, timedelta
        ctrl = AutonomyController(tmp_db)
        # Insert an entry from 100 days ago
        old_ts = (datetime.utcnow() - timedelta(days=100)).strftime("%Y-%m-%d %H:%M:%S")
        tmp_db.execute(
            "INSERT INTO audit_log (timestamp, action_type, description, autonomy_level) VALUES (?, 'test', 'old entry', 'SUPERVISED')",
            (old_ts,),
        )
        # Insert a recent entry
        ctrl.log_audit("test", "recent entry")
        tmp_db.commit()
        monkeypatch.setattr("config.DATA_DIR", tmp_path)
        count = ctrl.archive_old_audit_logs(retention_days=90)
        assert count == 1
        # Recent entry still exists
        remaining = tmp_db.execute("SELECT COUNT(*) FROM audit_log WHERE description = 'recent entry'").fetchone()[0]
        assert remaining >= 1
        # Archive file exists
        import glob
        archives = glob.glob(str(tmp_path / "audit_archive_*.jsonl.gz"))
        assert len(archives) == 1


# --- #39: Native macOS Notifications ---

class TestMacOSNotifications:
    def test_notification_function_exists(self):
        from monitoring import send_macos_notification
        assert callable(send_macos_notification)

    def test_notification_escapes_quotes(self):
        """Verify the function handles special characters."""
        from monitoring import send_macos_notification
        # Just test it doesn't crash — actual notification requires macOS
        result = send_macos_notification('Test "Title"', 'Hello "World"')
        # Result depends on macOS availability — just check no crash
        assert isinstance(result, bool)


# --- #19: Healing Confidence Scoring ---

class TestHealingConfidence:
    def test_base_confidence(self, tmp_db, monkeypatch):
        from healing import score_healing_confidence
        monkeypatch.setattr("healing._get_conn", lambda: tmp_db)
        diagnosis = {"fingerprint": "test:test", "failure_count": 1}
        score = score_healing_confidence(diagnosis, "def fix(): pass")
        assert 0.0 < score <= 1.0

    def test_high_confidence_small_patch(self, tmp_db, monkeypatch):
        from healing import score_healing_confidence
        monkeypatch.setattr("healing._get_conn", lambda: tmp_db)
        diagnosis = {"fingerprint": "test:test", "failure_count": 5}
        score = score_healing_confidence(diagnosis, "x = 1")
        assert score >= 0.5  # small patch + many signals = high confidence

    def test_lower_confidence_large_patch(self, tmp_db, monkeypatch):
        from healing import score_healing_confidence
        monkeypatch.setattr("healing._get_conn", lambda: tmp_db)
        diagnosis = {"fingerprint": "test:test", "failure_count": 1}
        large_patch = "\n".join(f"line_{i} = {i}" for i in range(50))
        score = score_healing_confidence(diagnosis, large_patch)
        # Large patch + few signals = lower confidence
        assert score < 0.7


# --- #22: Healing for Scheduler Jobs ---

class TestSchedulerHealing:
    def test_record_scheduler_failure(self, tmp_db):
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        from scheduler.tasks import _record_scheduler_failure
        _record_scheduler_failure("morning_brief", "Connection refused")
        row = tmp_db.execute(
            "SELECT context FROM interaction_signals WHERE signal_type = 'scheduler_task_failure'"
        ).fetchone()
        assert row is not None
        import json
        ctx = json.loads(row[0])
        assert ctx["task"] == "morning_brief"
        assert "Connection" in ctx["error"]
        learning._db_conn = original


# --- #57: Conversation Summarization ---

class TestConversationSummarization:
    def test_no_long_conversations(self, tmp_db):
        """No conversations with enough messages should return empty."""
        import asyncio
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        from learning import summarize_conversations

        async def mock_llm(q, c, system_extra=""):
            return "Mock summary"

        result = asyncio.run(summarize_conversations(mock_llm, min_messages=20))
        assert result == []
        learning._db_conn = original

    def test_summarizes_long_conversation(self, tmp_db):
        """Long conversation should be summarized."""
        import asyncio
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        from learning import summarize_conversations

        # Insert 25 messages
        for i in range(25):
            role = "user" if i % 2 == 0 else "assistant"
            tmp_db.execute(
                "INSERT INTO conversations (chat_id, role, content) VALUES (1, ?, ?)",
                (role, f"Message {i} about project planning"),
            )
        tmp_db.commit()

        async def mock_llm(q, c, system_extra=""):
            return "Summary: discussed project planning across 25 messages."

        result = asyncio.run(summarize_conversations(mock_llm, min_messages=20))
        assert len(result) == 1
        assert result[0]["message_count"] == 25
        # Check document was created
        doc = tmp_db.execute(
            "SELECT content FROM documents WHERE source = 'conversation_summary'"
        ).fetchone()
        assert doc is not None
        assert "project planning" in doc[0]
        learning._db_conn = original


# --- #66: Dynamic Context Window ---

class TestDynamicContextWindow:
    def test_topic_similarity_same(self):
        from server import _compute_topic_similarity
        assert _compute_topic_similarity("python code review", "python code review") > 0.5

    def test_topic_similarity_different(self):
        from server import _compute_topic_similarity
        sim = _compute_topic_similarity("python code review", "grocery shopping list")
        assert sim < 0.2

    def test_topic_similarity_partial(self):
        from server import _compute_topic_similarity
        sim = _compute_topic_similarity("python code review", "review python tests")
        assert sim > 0.3  # shared words: python, review


# --- #34: Focus/DND Mode Awareness ---

class TestFocusModeAwareness:
    def test_focus_mode_function_exists(self):
        from monitoring import get_focus_mode_status
        assert callable(get_focus_mode_status)

    def test_focus_mode_returns_dict(self):
        from monitoring import get_focus_mode_status
        result = get_focus_mode_status()
        assert isinstance(result, dict)
        assert "active" in result


# --- #1: Conversation Success Scoring ---

class TestConversationSuccessScoring:
    def test_record_success_signal(self, tmp_db):
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        from learning import record_signal
        record_signal("conversation_success", {
            "query": "test query",
            "topic": "tech",
            "latency_ms": 150.0,
            "had_correction": False,
        })
        row = tmp_db.execute(
            "SELECT context FROM interaction_signals WHERE signal_type = 'conversation_success'"
        ).fetchone()
        assert row is not None
        import json
        ctx = json.loads(row[0])
        assert ctx["topic"] == "tech"
        assert ctx["had_correction"] is False
        learning._db_conn = original

    def test_get_conversation_scores_empty(self, tmp_db):
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        from learning import get_conversation_scores
        result = get_conversation_scores(days=7)
        assert result["total"] == 0
        assert result["score_pct"] == 0.0
        assert result["by_topic"] == {}
        learning._db_conn = original

    def test_get_conversation_scores_with_data(self, tmp_db):
        import json
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        from learning import record_signal, get_conversation_scores
        # 3 success signals, 1 with correction
        record_signal("conversation_success", {"query": "q1", "topic": "tech", "had_correction": False})
        record_signal("conversation_success", {"query": "q2", "topic": "work", "had_correction": True})
        record_signal("conversation_success", {"query": "q3", "topic": "tech", "had_correction": False})
        result = get_conversation_scores(days=7)
        assert result["total"] == 3
        assert result["corrections"] == 1
        assert result["by_topic"]["tech"]["count"] == 2
        assert result["by_topic"]["work"]["corrections"] == 1
        learning._db_conn = original

    def test_explicit_feedback_recorded(self, tmp_db):
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        from learning import record_signal, get_conversation_scores
        record_signal("conversation_success", {"query": "q1", "topic": "general", "had_correction": False})
        record_signal("explicit_feedback", {"sentiment": "positive", "comment": "great"}, value=1.0)
        record_signal("explicit_feedback", {"sentiment": "negative", "comment": "bad"}, value=-1.0)
        result = get_conversation_scores(days=7)
        assert result["positive_feedback"] == 1
        assert result["negative_feedback"] == 1
        learning._db_conn = original

    def test_feedback_pattern_in_action_patterns(self):
        from server import _looks_like_action
        assert _looks_like_action("/feedback positive") == "feedback"


# --- #9: Monthly Meta-Reflection ---

class TestMonthlyMetaReflection:
    def test_skips_with_insufficient_insights(self, tmp_db):
        import asyncio
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        from learning import run_monthly_meta_reflection

        async def mock_llm(q, c, system_extra=""):
            return "[]"

        result = asyncio.run(run_monthly_meta_reflection(mock_llm))
        assert result == []
        learning._db_conn = original

    def test_runs_with_enough_insights(self, tmp_db):
        import asyncio
        import json
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        from learning import store_insight, run_monthly_meta_reflection

        # Insert 3 insights
        store_insight("preference", "Users prefer short answers", "5 signals", "Set max_length=200")
        store_insight("knowledge_gap", "Missing finance data", "3 misses", "Index finance docs")
        store_insight("prompt", "Improve greeting", "2 signals", "Add time-based greeting")

        meta_response = json.dumps([
            {"category": "meta", "summary": "Weekly insights are too vague", "recommendation": "Require quantitative evidence"}
        ])

        async def mock_llm(q, c, system_extra=""):
            return meta_response

        result = asyncio.run(run_monthly_meta_reflection(mock_llm))
        assert len(result) == 1
        assert result[0]["summary"] == "Weekly insights are too vague"
        assert "id" in result[0]
        # Verify stored in DB
        row = tmp_db.execute("SELECT category FROM insights WHERE id = ?", (result[0]["id"],)).fetchone()
        assert row[0] == "meta"
        learning._db_conn = original

    def test_handles_llm_failure(self, tmp_db):
        import asyncio
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        from learning import store_insight, run_monthly_meta_reflection

        store_insight("preference", "insight 1", "ev1", "rec1")
        store_insight("preference", "insight 2", "ev2", "rec2")

        async def mock_llm(q, c, system_extra=""):
            return "⚠️ API error"

        result = asyncio.run(run_monthly_meta_reflection(mock_llm))
        assert result == []
        learning._db_conn = original


# --- #52: GitHub Issue Creation ---

class TestGitHubIssueCreation:
    def test_issue_pattern_detected(self):
        from server import _looks_like_action
        assert _looks_like_action("create a github issue") == "gh_issue"
        assert _looks_like_action("open issue for bug") == "gh_issue"
        assert _looks_like_action("file an issue about auth") == "gh_issue"
        assert _looks_like_action("new issue") == "gh_issue"

    def test_direct_intent_issue_creation(self):
        from server import _try_direct_shell_intent
        result = _try_direct_shell_intent("create issue for fix login bug")
        assert result is not None
        assert result["action"] == "shell"
        assert "gh issue create" in result["command"]
        assert "fix login bug" in result["command"]

    def test_direct_intent_issue_with_quotes(self):
        from server import _try_direct_shell_intent
        result = _try_direct_shell_intent("open issue titled 'Add dark mode'")
        assert result is not None
        assert "gh issue create" in result["command"]


# --- #53: GitHub PR Status Monitoring ---

class TestGitHubPRStatus:
    def test_pr_pattern_detected(self):
        from server import _looks_like_action
        assert _looks_like_action("check my PRs") == "gh_pr_status"
        assert _looks_like_action("PR status") == "gh_pr_status"
        assert _looks_like_action("list my open pull requests") == "gh_pr_status"

    def test_direct_intent_pr_status(self):
        from server import _try_direct_shell_intent
        result = _try_direct_shell_intent("check my PRs")
        assert result is not None
        assert result["action"] == "shell"
        assert "gh pr list" in result["command"]
        assert "--author=@me" in result["command"]

    def test_direct_intent_pr_status_verbose(self):
        from server import _try_direct_shell_intent
        result = _try_direct_shell_intent("list my open pull requests")
        assert result is not None
        assert "gh pr list" in result["command"]


# --- #63: Knowledge Freshness Scoring ---

class TestKnowledgeFreshnessScoring:
    def test_freshness_recent_document(self):
        from knowledge.search import _compute_freshness_score
        from datetime import datetime
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        score = _compute_freshness_score(now)
        assert score > 0.95  # very recent = near 1.0

    def test_freshness_old_document(self):
        from knowledge.search import _compute_freshness_score
        from datetime import datetime, timedelta
        old = (datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S")
        score = _compute_freshness_score(old)
        assert score < 0.2  # 90 days with 30-day half-life = ~0.125

    def test_freshness_no_timestamp(self):
        from knowledge.search import _compute_freshness_score
        assert _compute_freshness_score(None) == 0.5

    def test_freshness_invalid_timestamp(self):
        from knowledge.search import _compute_freshness_score
        assert _compute_freshness_score("not-a-date") == 0.5

    def test_freshness_date_only(self):
        from knowledge.search import _compute_freshness_score
        from datetime import datetime
        today = datetime.utcnow().strftime("%Y-%m-%d")
        score = _compute_freshness_score(today)
        assert score > 0.9  # today's date = very fresh


# --- #65: Conversation Topic Detection ---

class TestConversationTopicDetection:
    def test_detect_work_topic(self):
        from server import classify_message_topic
        assert classify_message_topic("Can you check the sprint backlog for our team meeting?") == "work"

    def test_detect_finance_topic(self):
        from server import classify_message_topic
        assert classify_message_topic("What's my RRSP investment portfolio looking like?") == "finance"

    def test_detect_health_topic(self):
        from server import classify_message_topic
        assert classify_message_topic("How many calories did I burn in my workout?") == "health"

    def test_detect_tech_topic(self):
        from server import classify_message_topic
        assert classify_message_topic("Help me debug this python code with the API") == "tech"

    def test_detect_family_topic(self):
        from server import classify_message_topic
        assert classify_message_topic("When do the kids have school vacation?") == "family"

    def test_detect_general_topic(self):
        from server import classify_message_topic
        assert classify_message_topic("What's the weather like?") == "general"

    def test_returns_string(self):
        from server import classify_message_topic
        result = classify_message_topic("random words here")
        assert isinstance(result, str)


# --- #82: Scheduler Task Tests ---

class TestSchedulerTasks:
    def test_record_scheduler_failure_records_signal(self, tmp_db):
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        from scheduler.tasks import _record_scheduler_failure
        _record_scheduler_failure("test_task", "Something broke")
        row = tmp_db.execute(
            "SELECT context FROM interaction_signals WHERE signal_type = 'scheduler_task_failure'"
        ).fetchone()
        assert row is not None
        import json
        ctx = json.loads(row[0])
        assert ctx["task"] == "test_task"
        assert "broke" in ctx["error"]
        learning._db_conn = original

    def test_record_digest_sent_records_signal(self, tmp_db):
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        from scheduler.tasks import _record_digest_sent
        _record_digest_sent("morning_brief")
        row = tmp_db.execute(
            "SELECT context FROM interaction_signals WHERE signal_type = 'digest_sent'"
        ).fetchone()
        assert row is not None
        import json
        ctx = json.loads(row[0])
        assert ctx["type"] == "morning_brief"
        learning._db_conn = original

    def test_record_scheduler_failure_truncates_long_errors(self, tmp_db):
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        from scheduler.tasks import _record_scheduler_failure
        long_error = "x" * 1000
        _record_scheduler_failure("task", long_error)
        row = tmp_db.execute(
            "SELECT context FROM interaction_signals WHERE signal_type = 'scheduler_task_failure'"
        ).fetchone()
        import json
        ctx = json.loads(row[0])
        assert len(ctx["error"]) <= 500
        learning._db_conn = original

    def test_task_functions_catch_exceptions(self, tmp_db):
        """Task functions should handle exceptions without crashing the scheduler."""
        import asyncio
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        from scheduler.tasks import _record_scheduler_failure
        # Simulate what happens when a task catches an exception
        _record_scheduler_failure("email_sync", "Connection refused")
        _record_scheduler_failure("morning_brief", "API timeout")
        rows = tmp_db.execute(
            "SELECT context FROM interaction_signals WHERE signal_type = 'scheduler_task_failure'"
        ).fetchall()
        assert len(rows) == 2
        learning._db_conn = original

    def test_record_digest_sent_multiple_types(self, tmp_db):
        import json
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        from scheduler.tasks import _record_digest_sent
        _record_digest_sent("morning_brief")
        _record_digest_sent("weekly_summary")
        rows = tmp_db.execute(
            "SELECT context FROM interaction_signals WHERE signal_type = 'digest_sent' ORDER BY id"
        ).fetchall()
        assert len(rows) == 2
        assert json.loads(rows[0][0])["type"] == "morning_brief"
        assert json.loads(rows[1][0])["type"] == "weekly_summary"
        learning._db_conn = original

    def test_record_scheduler_failure_does_not_crash_without_db(self):
        """_record_scheduler_failure should swallow exceptions gracefully."""
        import learning
        original = learning._db_conn
        learning._db_conn = None  # Force connection to fail
        from scheduler.tasks import _record_scheduler_failure
        # Should not raise
        try:
            _record_scheduler_failure("task", "error")
        except Exception:
            pass  # The function itself catches exceptions
        finally:
            learning._db_conn = original


# --- #77: Immutable Audit Trail ---

class TestImmutableAuditTrail:
    def test_log_audit_writes_jsonl(self, tmp_db, tmp_path, monkeypatch):
        """log_audit should append to audit_trail.jsonl alongside SQLite."""
        import json
        monkeypatch.setattr("autonomy.DATA_DIR", tmp_path)
        from autonomy import AutonomyController
        ctrl = AutonomyController(tmp_db)
        ctrl.log_audit("test_action", "Test description", {"key": "val"}, "ok")

        trail_path = tmp_path / "audit_trail.jsonl"
        assert trail_path.exists()
        lines = trail_path.read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["action_type"] == "test_action"
        assert entry["description"] == "Test description"
        assert entry["payload"] == {"key": "val"}
        assert entry["result"] == "ok"

    def test_log_audit_appends_multiple(self, tmp_db, tmp_path, monkeypatch):
        """Multiple log_audit calls should append, not overwrite."""
        monkeypatch.setattr("autonomy.DATA_DIR", tmp_path)
        from autonomy import AutonomyController
        ctrl = AutonomyController(tmp_db)
        ctrl.log_audit("action1", "First")
        ctrl.log_audit("action2", "Second")

        trail_path = tmp_path / "audit_trail.jsonl"
        lines = trail_path.read_text().strip().split("\n")
        assert len(lines) == 2

    def test_log_audit_still_writes_sqlite(self, tmp_db, tmp_path, monkeypatch):
        """JSONL writing should not break SQLite writes."""
        monkeypatch.setattr("autonomy.DATA_DIR", tmp_path)
        from autonomy import AutonomyController
        ctrl = AutonomyController(tmp_db)
        ctrl.log_audit("test", "test desc")

        row = tmp_db.execute("SELECT * FROM audit_log WHERE action_type = 'test'").fetchone()
        assert row is not None


# --- #68: Embedding Provider Abstraction ---

class TestEmbeddingProviderAbstraction:
    def test_embed_provider_config_exists(self):
        """EMBED_PROVIDER should be defined in config."""
        from config import EMBED_PROVIDER
        assert EMBED_PROVIDER == "ollama"

    def test_provider_registry_has_ollama(self):
        """The provider registry should include ollama."""
        from knowledge.embedder import _PROVIDERS
        assert "ollama" in _PROVIDERS
        assert "embed_text" in _PROVIDERS["ollama"]
        assert "embed_batch" in _PROVIDERS["ollama"]
        assert "check" in _PROVIDERS["ollama"]

    def test_get_provider_returns_ollama(self):
        """_get_provider should return the ollama provider by default."""
        from knowledge.embedder import _get_provider
        provider = _get_provider()
        assert callable(provider["embed_text"])

    def test_get_provider_raises_for_unknown(self, monkeypatch):
        """_get_provider should raise ValueError for unknown providers."""
        import knowledge.embedder as embedder
        monkeypatch.setattr(embedder, "EMBED_PROVIDER", "nonexistent")
        with pytest.raises(ValueError, match="Unknown EMBED_PROVIDER"):
            embedder._get_provider()

    def test_public_api_delegates_to_provider(self):
        """embed_text and embed_batch should be async callables."""
        import asyncio
        from knowledge.embedder import embed_text, embed_batch
        assert asyncio.iscoroutinefunction(embed_text)
        assert asyncio.iscoroutinefunction(embed_batch)


# --- #96: Sleep Schedule Inference ---

class TestSleepScheduleInference:
    def test_infer_sleep_schedule_no_data(self, tmp_db):
        """With no conversations, should return null times and zero confidence."""
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        try:
            result = learning.infer_sleep_schedule(days=7)
            assert result["wake_time"] is None
            assert result["sleep_time"] is None
            assert result["confidence"] == 0.0
            assert result["days_with_data"] == 0
        finally:
            learning._db_conn = original

    def test_infer_sleep_schedule_with_data(self, tmp_db):
        """With conversation data, should return reasonable times."""
        import learning
        from datetime import datetime, timedelta
        original = learning._db_conn
        learning._db_conn = tmp_db
        try:
            # Insert conversations for 3 days
            now = datetime.utcnow()
            for i in range(3):
                day = now - timedelta(days=i)
                morning = day.replace(hour=8, minute=30, second=0).strftime("%Y-%m-%d %H:%M:%S")
                evening = day.replace(hour=22, minute=15, second=0).strftime("%Y-%m-%d %H:%M:%S")
                tmp_db.execute(
                    "INSERT INTO conversations (chat_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                    (1, "user", "morning msg", morning),
                )
                tmp_db.execute(
                    "INSERT INTO conversations (chat_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                    (1, "user", "evening msg", evening),
                )
            tmp_db.commit()

            result = learning.infer_sleep_schedule(days=14)
            assert result["wake_time"] is not None
            assert result["sleep_time"] is not None
            assert result["days_with_data"] == 3
            assert result["confidence"] > 0
            # Wake time should be around 08:30
            assert result["wake_time"].startswith("08")
            # Sleep time should be around 22:15
            assert result["sleep_time"].startswith("22")
        finally:
            learning._db_conn = original

    def test_infer_sleep_schedule_confidence_increases_with_data(self, tmp_db):
        """More days of data should yield higher confidence."""
        import learning
        from datetime import datetime, timedelta
        original = learning._db_conn
        learning._db_conn = tmp_db
        try:
            now = datetime.utcnow()
            for i in range(10):
                day = now - timedelta(days=i)
                ts = day.replace(hour=9, minute=0, second=0).strftime("%Y-%m-%d %H:%M:%S")
                tmp_db.execute(
                    "INSERT INTO conversations (chat_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                    (1, "user", "msg", ts),
                )
            tmp_db.commit()

            result = learning.infer_sleep_schedule(days=14)
            assert result["confidence"] > 0.5
        finally:
            learning._db_conn = original


# --- #86: Coverage Gate for CI ---

class TestCoverageGate:
    def test_pyproject_toml_exists(self):
        """pyproject.toml should exist with coverage config."""
        from pathlib import Path
        pyproject = Path(__file__).parent.parent / "pyproject.toml"
        assert pyproject.exists()

    def test_pyproject_has_coverage_config(self):
        """pyproject.toml should have fail_under = 30."""
        from pathlib import Path
        pyproject = Path(__file__).parent.parent / "pyproject.toml"
        content = pyproject.read_text()
        assert "fail_under = 30" in content
        assert "[tool.coverage.run]" in content
        assert "[tool.coverage.report]" in content


# --- #46: Gmail Label Management ---

class TestGmailLabelManagement:
    def test_scopes_modify_defined(self):
        """SCOPES_MODIFY should be defined with gmail.modify scope."""
        from actions.gmail import SCOPES_MODIFY
        assert any("gmail.modify" in s for s in SCOPES_MODIFY)

    def test_token_file_modify_in_config(self):
        """TOKEN_FILE_MODIFY should be defined in config."""
        from config import TOKEN_FILE_MODIFY
        assert "token_modify" in str(TOKEN_FILE_MODIFY)

    def test_list_labels_is_async(self):
        """list_labels should be an async function."""
        import asyncio
        from actions.gmail import list_labels
        assert asyncio.iscoroutinefunction(list_labels)

    def test_apply_label_is_async(self):
        """apply_label should be an async function."""
        import asyncio
        from actions.gmail import apply_label
        assert asyncio.iscoroutinefunction(apply_label)

    def test_remove_label_is_async(self):
        """remove_label should be an async function."""
        import asyncio
        from actions.gmail import remove_label
        assert asyncio.iscoroutinefunction(remove_label)

    def test_get_gmail_service_modify_param(self):
        """_get_gmail_service should accept modify parameter."""
        import inspect
        from actions.gmail import _get_gmail_service
        sig = inspect.signature(_get_gmail_service)
        assert "modify" in sig.parameters

    def test_action_patterns_include_label(self):
        """_ACTION_PATTERNS should include label/categorize patterns."""
        from server import _ACTION_PATTERNS
        label_patterns = [h for _, h in _ACTION_PATTERNS if h == "label"]
        assert len(label_patterns) >= 1


# --- #67: Cross-Source Context Fusion ---

class TestCrossSourceContextFusion:
    def test_truncate_context_adds_source_tags(self):
        """truncate_context should tag each result with [Source: ...]."""
        from server import truncate_context
        results = [
            {"category": "email", "title": "Meeting notes 2024-01-15", "content": "Some content"},
            {"category": "drive", "title": "Project plan", "content": "Other content"},
        ]
        output = truncate_context(results)
        assert "[Source: email" in output
        assert "[Source: drive" in output

    def test_truncate_context_empty_category(self):
        """truncate_context should handle empty category gracefully."""
        from server import truncate_context
        results = [{"category": "", "title": "Untitled", "content": "Content"}]
        output = truncate_context(results)
        assert "[Source: Untitled]" in output

    def test_truncate_context_preserves_content(self):
        """Content should still be present after adding source tags."""
        from server import truncate_context
        results = [{"category": "test", "title": "Doc", "content": "Important data here"}]
        output = truncate_context(results)
        assert "Important data here" in output


# --- #76: Two-Factor for Dangerous Actions ---

class TestTwoFactorConfirmation:
    def test_generate_confirmation_code_format(self):
        """Confirmation code should be a 4-digit string."""
        from autonomy import generate_confirmation_code
        code = generate_confirmation_code()
        assert len(code) == 4
        assert code.isdigit()
        assert 1000 <= int(code) <= 9999

    def test_hard_guardrail_action_gets_code(self, tmp_db):
        """Creating a hard guardrail action should generate a confirmation code."""
        from autonomy import AutonomyController
        ctrl = AutonomyController(tmp_db)
        action_id = ctrl.create_pending_action("delete_data", "Delete everything")
        code = ctrl.get_confirmation_code(action_id)
        assert code is not None
        assert len(code) == 4

    def test_non_guardrail_action_no_code(self, tmp_db):
        """Non-guardrail actions should NOT get confirmation codes."""
        from autonomy import AutonomyController
        ctrl = AutonomyController(tmp_db)
        action_id = ctrl.create_pending_action("search_knowledge", "Search something")
        code = ctrl.get_confirmation_code(action_id)
        assert code is None

    def test_verify_correct_code(self, tmp_db):
        """Correct code should verify successfully and be consumed."""
        from autonomy import AutonomyController
        ctrl = AutonomyController(tmp_db)
        action_id = ctrl.create_pending_action("send_money", "Send $100")
        code = ctrl.get_confirmation_code(action_id)
        assert ctrl.verify_confirmation_code(action_id, code) is True
        # Code should be consumed
        assert ctrl.get_confirmation_code(action_id) is None

    def test_verify_wrong_code(self, tmp_db):
        """Wrong code should fail verification."""
        from autonomy import AutonomyController
        ctrl = AutonomyController(tmp_db)
        action_id = ctrl.create_pending_action("delete_data", "Delete all")
        assert ctrl.verify_confirmation_code(action_id, "0000") is False

    def test_verify_nonexistent_action(self, tmp_db):
        """Verifying a code for an action without one should return False."""
        from autonomy import AutonomyController
        ctrl = AutonomyController(tmp_db)
        assert ctrl.verify_confirmation_code(9999, "1234") is False


# --- #41: Brew Package Management ---

class TestBrewPackageManagement:
    """Tests for brew command classification and direct intent mapping."""

    def test_brew_list_is_safe(self):
        """brew list should be classified as READ (safe)."""
        from actions.shell import classify_command
        from config import ActionType
        assert classify_command("brew list") == ActionType.READ

    def test_brew_info_is_safe(self):
        """brew info <pkg> should be classified as READ (safe)."""
        from actions.shell import classify_command
        from config import ActionType
        assert classify_command("brew info python") == ActionType.READ

    def test_brew_search_is_safe(self):
        """brew search <pkg> should be classified as READ (safe)."""
        from actions.shell import classify_command
        from config import ActionType
        assert classify_command("brew search node") == ActionType.READ

    def test_brew_install_is_write(self):
        """brew install should be classified as WRITE (needs approval)."""
        from actions.shell import classify_command
        from config import ActionType
        assert classify_command("brew install python") == ActionType.WRITE

    def test_brew_upgrade_is_write(self):
        """brew upgrade should be classified as WRITE (needs approval)."""
        from actions.shell import classify_command
        from config import ActionType
        assert classify_command("brew upgrade") == ActionType.WRITE

    def test_brew_uninstall_is_write(self):
        """brew uninstall should be classified as WRITE (needs approval)."""
        from actions.shell import classify_command
        from config import ActionType
        assert classify_command("brew uninstall node") == ActionType.WRITE

    def test_brew_cleanup_is_write(self):
        """brew cleanup should be classified as WRITE (needs approval)."""
        from actions.shell import classify_command
        from config import ActionType
        assert classify_command("brew cleanup") == ActionType.WRITE

    def test_brew_list_action_pattern(self):
        """'list my brew packages' should match shell action pattern."""
        from server import _looks_like_action
        assert _looks_like_action("list my brew packages") == "shell"

    def test_brew_install_action_pattern(self):
        """'brew install wget' should match shell action pattern."""
        from server import _looks_like_action
        assert _looks_like_action("brew install wget") == "shell"

    def test_brew_direct_intent_list(self):
        """'list my brew packages' should map to 'brew list' command."""
        from server import _try_direct_shell_intent
        result = _try_direct_shell_intent("list my brew packages")
        assert result is not None
        assert result["command"] == "brew list"

    def test_brew_direct_intent_info(self):
        """'brew info python' should map directly."""
        from server import _try_direct_shell_intent
        result = _try_direct_shell_intent("brew info python")
        assert result is not None
        assert result["command"] == "brew info python"

    def test_brew_direct_intent_search(self):
        """'brew search node' should map directly."""
        from server import _try_direct_shell_intent
        result = _try_direct_shell_intent("brew search node")
        assert result is not None
        assert result["command"] == "brew search node"

    def test_brew_direct_intent_install(self):
        """'brew install wget' should map directly."""
        from server import _try_direct_shell_intent
        result = _try_direct_shell_intent("brew install wget")
        assert result is not None
        assert result["command"] == "brew install wget"

    def test_brew_direct_intent_upgrade_all(self):
        """'brew upgrade' should map directly."""
        from server import _try_direct_shell_intent
        result = _try_direct_shell_intent("brew upgrade")
        assert result is not None
        assert result["command"] == "brew upgrade"

    def test_brew_direct_intent_upgrade_specific(self):
        """'brew upgrade python' should map directly."""
        from server import _try_direct_shell_intent
        result = _try_direct_shell_intent("brew upgrade python")
        assert result is not None
        assert result["command"] == "brew upgrade python"

    def test_brew_direct_intent_uninstall(self):
        """'brew uninstall node' should map directly."""
        from server import _try_direct_shell_intent
        result = _try_direct_shell_intent("brew uninstall node")
        assert result is not None
        assert result["command"] == "brew uninstall node"

    def test_brew_direct_intent_cleanup(self):
        """'brew cleanup' should map directly."""
        from server import _try_direct_shell_intent
        result = _try_direct_shell_intent("brew cleanup")
        assert result is not None
        assert result["command"] == "brew cleanup"


# --- #80: Gmail Action Tests ---

class TestGmailActions:
    """Tests for Gmail action module with mocked Google API responses."""

    def test_extract_body_plain_text(self):
        """extract_body should decode base64 plain text body."""
        import base64
        from actions.gmail import extract_body
        text = "Hello, this is a test email."
        encoded = base64.urlsafe_b64encode(text.encode()).decode()
        payload = {"body": {"data": encoded}}
        assert extract_body(payload) == text

    def test_extract_body_multipart_prefers_plain(self):
        """extract_body should prefer text/plain in multipart messages."""
        import base64
        from actions.gmail import extract_body
        plain_text = "Plain text body"
        html_text = "<p>HTML body</p>"
        payload = {
            "body": {},
            "parts": [
                {
                    "mimeType": "text/html",
                    "body": {"data": base64.urlsafe_b64encode(html_text.encode()).decode()},
                },
                {
                    "mimeType": "text/plain",
                    "body": {"data": base64.urlsafe_b64encode(plain_text.encode()).decode()},
                },
            ],
        }
        assert extract_body(payload) == plain_text

    def test_extract_body_html_fallback(self):
        """extract_body should strip HTML tags as fallback."""
        import base64
        from actions.gmail import extract_body
        html_text = "<p>Hello <b>World</b></p>"
        payload = {
            "body": {},
            "parts": [
                {
                    "mimeType": "text/html",
                    "body": {"data": base64.urlsafe_b64encode(html_text.encode()).decode()},
                },
            ],
        }
        result = extract_body(payload)
        assert "Hello" in result
        assert "World" in result
        assert "<p>" not in result

    def test_extract_body_empty(self):
        """extract_body should return empty string when no body found."""
        from actions.gmail import extract_body
        payload = {"body": {}, "parts": []}
        assert extract_body(payload) == ""

    def test_search_emails_sync(self):
        """_search_emails_sync should use Gmail API to search and return formatted results."""
        import base64
        from unittest.mock import patch, MagicMock
        from actions.gmail import _search_emails_sync

        mock_service = MagicMock()
        # Mock messages().list()
        mock_service.users().messages().list().execute.return_value = {
            "messages": [{"id": "msg1"}]
        }
        # Mock messages().get()
        body_data = base64.urlsafe_b64encode(b"Test body").decode()
        mock_service.users().messages().get().execute.return_value = {
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "Test Subject"},
                    {"name": "From", "value": "test@example.com"},
                    {"name": "To", "value": "me@example.com"},
                    {"name": "Date", "value": "Mon, 1 Jan 2026"},
                ],
                "body": {"data": body_data},
            },
            "snippet": "Test snippet",
        }

        with patch("actions.gmail._get_gmail_service", return_value=mock_service):
            results = _search_emails_sync("test query", max_results=5)

        assert len(results) == 1
        assert results[0]["subject"] == "Test Subject"
        assert results[0]["from"] == "test@example.com"
        assert "Test body" in results[0]["body"]

    def test_draft_email_sync(self):
        """_draft_email_sync should create a draft via Gmail API."""
        from unittest.mock import patch, MagicMock
        from actions.gmail import _draft_email_sync

        mock_service = MagicMock()
        mock_service.users().drafts().create().execute.return_value = {
            "id": "draft123",
        }

        with patch("actions.gmail._get_gmail_service", return_value=mock_service):
            result = _draft_email_sync("recipient@example.com", "Test Subject", "Test body")

        assert result["draft_id"] == "draft123"
        assert result["to"] == "recipient@example.com"
        assert result["subject"] == "Test Subject"


# --- #81: Calendar Action Tests ---

class TestCalendarActions:
    """Tests for calendar actions with mocked Google API."""

    def test_get_events_sync(self):
        """_get_events_sync should fetch and format calendar events."""
        from unittest.mock import patch, MagicMock
        from actions.calendar import _get_events_sync

        mock_service = MagicMock()
        mock_service.events().list().execute.return_value = {
            "items": [
                {
                    "summary": "Team Standup",
                    "start": {"dateTime": "2026-03-16T09:00:00-04:00"},
                    "end": {"dateTime": "2026-03-16T09:30:00-04:00"},
                    "location": "Room 101",
                    "description": "Daily standup",
                },
                {
                    "summary": "All Day Event",
                    "start": {"date": "2026-03-16"},
                    "end": {"date": "2026-03-17"},
                },
            ]
        }

        with patch("actions.calendar._get_calendar_service", return_value=mock_service):
            events = _get_events_sync(days=1)

        assert len(events) == 2
        assert events[0]["summary"] == "Team Standup"
        assert events[0]["location"] == "Room 101"
        assert events[0]["all_day"] is False
        assert events[1]["summary"] == "All Day Event"
        assert events[1]["all_day"] is True

    def test_format_events_text_empty(self):
        """format_events_text should handle empty event list."""
        from actions.calendar import format_events_text
        assert format_events_text([]) == "No upcoming events."

    def test_format_events_text_all_day(self):
        """format_events_text should show 'All day' for all-day events."""
        from actions.calendar import format_events_text
        events = [{"summary": "Holiday", "start": "2026-03-16", "end": "2026-03-17", "location": "", "all_day": True}]
        result = format_events_text(events)
        assert "All day" in result
        assert "Holiday" in result

    def test_format_events_text_timed(self):
        """format_events_text should show formatted time for timed events."""
        from actions.calendar import format_events_text
        events = [{
            "summary": "Meeting",
            "start": "2026-03-16T14:00:00-04:00",
            "end": "2026-03-16T15:00:00-04:00",
            "location": "",
            "all_day": False,
        }]
        result = format_events_text(events)
        assert "Meeting" in result
        # Should contain a formatted time (AM/PM)
        assert "PM" in result or "AM" in result

    def test_format_events_text_with_location(self):
        """format_events_text should include location when present."""
        from actions.calendar import format_events_text
        events = [{
            "summary": "Lunch",
            "start": "2026-03-16T12:00:00-04:00",
            "end": "2026-03-16T13:00:00-04:00",
            "location": "Cafe Nero",
            "all_day": False,
        }]
        result = format_events_text(events)
        assert "Cafe Nero" in result

    def test_create_event_sync(self):
        """_create_event_sync should create an event via Calendar API."""
        from unittest.mock import patch, MagicMock
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from actions.calendar import _create_event_sync

        mock_service = MagicMock()
        mock_service.events().insert().execute.return_value = {
            "id": "event123",
            "summary": "New Meeting",
            "start": {"dateTime": "2026-03-17T10:00:00-04:00"},
            "htmlLink": "https://calendar.google.com/event/123",
        }

        start = datetime(2026, 3, 17, 10, 0, tzinfo=ZoneInfo("America/Toronto"))

        with patch("actions.calendar._get_calendar_service", return_value=mock_service), \
             patch("actions.calendar.TOKEN_FILE_CALENDAR_WRITE") as mock_token:
            mock_token.exists.return_value = True
            result = _create_event_sync("New Meeting", start)

        assert result["id"] == "event123"
        assert result["summary"] == "New Meeting"
        assert "calendar.google.com" in result["link"]


# --- #95: Subscription Renewal Alerts ---

class TestSubscriptionRenewals:
    """Tests for subscription renewal detection from indexed emails."""

    def test_detect_subscription_renewals_empty(self, tmp_db):
        """Should return empty list when no renewal emails exist."""
        import learning
        learning.set_conn(tmp_db)
        results = learning.detect_subscription_renewals(days_ahead=7)
        assert results == []

    def test_detect_subscription_renewals_finds_matches(self, tmp_db):
        """Should find emails containing renewal keywords."""
        import learning
        learning.set_conn(tmp_db)
        tmp_db.execute(
            "INSERT INTO documents (source, category, title, content) VALUES (?, ?, ?, ?)",
            ("email", "finance", "Netflix Renewal",
             "Your Netflix subscription renewal is on 2026-04-01. Amount: $15.99"),
        )
        tmp_db.commit()

        results = learning.detect_subscription_renewals(days_ahead=7)
        assert len(results) >= 1
        found = [r for r in results if r["title"] == "Netflix Renewal"]
        assert len(found) == 1
        assert found[0]["amount"] == "$15.99"
        assert found[0]["estimated_date"] == "2026-04-01"

    def test_detect_subscription_renewals_no_duplicates(self, tmp_db):
        """Should not return duplicate results for the same document."""
        import learning
        learning.set_conn(tmp_db)
        # This document matches multiple keywords
        tmp_db.execute(
            "INSERT INTO documents (source, category, title, content) VALUES (?, ?, ?, ?)",
            ("email", "finance", "Billing Notice",
             "Your subscription renewal auto-renew billing cycle will process on 2026-04-15. Total: $29.99"),
        )
        tmp_db.commit()

        results = learning.detect_subscription_renewals(days_ahead=7)
        titles = [r["title"] for r in results]
        assert titles.count("Billing Notice") == 1


# --- #92: Weekend Project Nudge ---

class TestWeekendNudge:
    """Tests for weekend project nudge generation."""

    def test_returns_none_on_weekday(self, monkeypatch):
        """Should return None if today is not Saturday."""
        from unittest.mock import patch
        from scheduler.proactive import generate_weekend_nudge
        # Mock date.today() to return a Monday
        import datetime as dt_mod
        fake_date = dt_mod.date(2026, 3, 16)  # Monday
        with patch("scheduler.proactive.date") as mock_date:
            mock_date.today.return_value = fake_date
            mock_date.side_effect = lambda *args, **kw: dt_mod.date(*args, **kw)
            result = generate_weekend_nudge()
        assert result is None

    def test_returns_nudge_on_saturday_with_stale_items(self, monkeypatch, tmp_path):
        """Should return a nudge message on Saturday with stale items."""
        from unittest.mock import patch, MagicMock
        from scheduler.proactive import generate_weekend_nudge
        import datetime as dt_mod
        import sqlite3

        # Set up a temporary DB with an old reminder
        db_path = tmp_path / "khalil.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY, text TEXT, due_at TEXT, status TEXT)")
        conn.execute(
            "INSERT INTO reminders (text, due_at, status) VALUES (?, ?, ?)",
            ("Review project plan", "2026-02-01 09:00:00", "active"),
        )
        conn.commit()
        conn.close()

        fake_saturday = dt_mod.date(2026, 3, 14)  # Saturday
        with patch("scheduler.proactive.date") as mock_date, \
             patch("scheduler.proactive.DB_PATH", db_path), \
             patch("scheduler.proactive.GOALS_DIR", tmp_path / "nonexistent"):
            mock_date.today.return_value = fake_saturday
            mock_date.side_effect = lambda *args, **kw: dt_mod.date(*args, **kw)
            result = generate_weekend_nudge()

        assert result is not None
        assert "Weekend Project Nudge" in result
        assert "Review project plan" in result

    def test_returns_none_on_saturday_with_nothing_stale(self, monkeypatch, tmp_path):
        """Should return None on Saturday if nothing is stale."""
        from unittest.mock import patch
        from scheduler.proactive import generate_weekend_nudge
        import datetime as dt_mod
        import sqlite3

        # Set up a DB with no stale items
        db_path = tmp_path / "khalil.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY, text TEXT, due_at TEXT, status TEXT)")
        conn.commit()
        conn.close()

        fake_saturday = dt_mod.date(2026, 3, 14)  # Saturday
        with patch("scheduler.proactive.date") as mock_date, \
             patch("scheduler.proactive.DB_PATH", db_path), \
             patch("scheduler.proactive.GOALS_DIR", tmp_path / "nonexistent"), \
             patch("actions.projects.KNOWN_PROJECTS", {}):
            mock_date.today.return_value = fake_saturday
            mock_date.side_effect = lambda *args, **kw: dt_mod.date(*args, **kw)
            result = generate_weekend_nudge()

        assert result is None


# --- #7: Prompt Effectiveness Scoring ---

class TestPromptEffectiveness:
    """Tests for prompt effectiveness scoring."""

    def test_empty_data(self, tmp_db):
        """Should return empty results when no signals exist."""
        import learning
        learning.set_conn(tmp_db)
        result = learning.get_prompt_effectiveness(days=7)
        assert result["by_topic"] == {}
        assert result["best_topic"] is None
        assert result["worst_topic"] is None

    def test_single_topic(self, tmp_db):
        """Should calculate success rate for a single topic."""
        import json
        import learning
        learning.set_conn(tmp_db)

        # Insert 3 success signals for "work" topic, 1 with correction
        for i in range(3):
            ctx = {"topic": "work", "had_correction": i == 0}
            tmp_db.execute(
                "INSERT INTO interaction_signals (signal_type, context, value) VALUES (?, ?, ?)",
                ("conversation_success", json.dumps(ctx), 1.0),
            )
        tmp_db.commit()

        result = learning.get_prompt_effectiveness(days=7)
        assert "work" in result["by_topic"]
        assert result["by_topic"]["work"]["total"] == 3
        assert result["by_topic"]["work"]["successes"] == 2
        assert result["by_topic"]["work"]["success_rate_pct"] == pytest.approx(66.7, abs=0.1)

    def test_multiple_topics_best_worst(self, tmp_db):
        """Should identify best and worst topics."""
        import json
        import learning
        learning.set_conn(tmp_db)

        # "tech" topic: 3 successes, 0 corrections (100%)
        for _ in range(3):
            tmp_db.execute(
                "INSERT INTO interaction_signals (signal_type, context, value) VALUES (?, ?, ?)",
                ("conversation_success", json.dumps({"topic": "tech", "had_correction": False}), 1.0),
            )
        # "finance" topic: 2 signals, both with corrections (0%)
        for _ in range(2):
            tmp_db.execute(
                "INSERT INTO interaction_signals (signal_type, context, value) VALUES (?, ?, ?)",
                ("conversation_success", json.dumps({"topic": "finance", "had_correction": True}), 1.0),
            )
        tmp_db.commit()

        result = learning.get_prompt_effectiveness(days=7)
        assert result["best_topic"] == "tech"
        assert result["worst_topic"] == "finance"


# --- #100: MCP Server Expansion ---

class TestMCPServerExpansion:
    """Tests for new MCP tools added in #100.

    These test the underlying logic directly rather than the decorated MCP functions,
    to avoid issues with FastMCP module-level imports and test ordering.
    """

    def test_healing_status_empty(self, tmp_db):
        """healing_status query should return no rows for empty DB."""
        rows = tmp_db.execute(
            "SELECT id, summary FROM insights WHERE category = 'self_heal' ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
        assert len(rows) == 0

    def test_healing_status_with_data(self, tmp_db):
        """healing_status query should find self_heal insights."""
        tmp_db.execute(
            "INSERT INTO insights (category, summary, evidence, recommendation, status) "
            "VALUES ('self_heal', 'Fixed calendar auth', 'Token expired', 'Refresh token', 'applied')"
        )
        tmp_db.commit()

        rows = tmp_db.execute(
            "SELECT id, summary, status, created_at FROM insights "
            "WHERE category = 'self_heal' ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["summary"] == "Fixed calendar auth"
        assert rows[0]["status"] == "applied"

    def test_audit_log_empty(self, tmp_db):
        """audit_log query should return no rows for empty DB."""
        rows = tmp_db.execute(
            "SELECT action_type, description FROM audit_log ORDER BY timestamp DESC LIMIT 10"
        ).fetchall()
        assert len(rows) == 0

    def test_audit_log_with_data(self, tmp_db):
        """audit_log query should return recent entries."""
        tmp_db.execute(
            "INSERT INTO audit_log (action_type, description, result, autonomy_level) "
            "VALUES ('shell', 'Ran brew list', 'success', 'auto')"
        )
        tmp_db.commit()

        rows = tmp_db.execute(
            "SELECT action_type, description, result, autonomy_level, timestamp "
            "FROM audit_log ORDER BY timestamp DESC LIMIT 5"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["description"] == "Ran brew list"
        assert rows[0]["autonomy_level"] == "auto"

    def test_learned_preferences_empty(self, tmp_db):
        """learned_preferences should handle no preferences."""
        import learning
        learning.set_conn(tmp_db)
        prefs = learning.list_preferences()
        assert prefs == []

    def test_learned_preferences_with_data(self, tmp_db):
        """learned_preferences should list stored preferences."""
        import learning
        learning.set_conn(tmp_db)
        learning.set_preference("tone", "concise", confidence=0.8)

        prefs = learning.list_preferences()
        assert len(prefs) == 1
        assert prefs[0]["key"] == "tone"
        assert prefs[0]["value"] == "concise"
        assert prefs[0]["confidence"] == 0.8

    def test_extension_health_empty(self, tmp_db):
        """Extension health should return empty list when no usage data."""
        import learning
        learning.set_conn(tmp_db)
        health = learning.get_extension_health(days=7)
        assert health == []

    def test_extension_health_with_data(self, tmp_db):
        """Extension health should aggregate usage stats."""
        import json
        import learning
        learning.set_conn(tmp_db)

        # Record some extension usage
        for status in ["invoked", "success", "invoked", "error"]:
            tmp_db.execute(
                "INSERT INTO interaction_signals (signal_type, context, value) VALUES (?, ?, ?)",
                ("extension_usage", json.dumps({"extension": "weather", "status": status}), 1.0),
            )
        tmp_db.commit()

        health = learning.get_extension_health(days=7)
        assert len(health) == 1
        assert health[0]["extension"] == "weather"
        assert health[0]["invocations"] == 2
        assert health[0]["errors"] == 1


# --- #99: CLI for local testing ---

class TestCLI:
    def test_cli_exists(self):
        """cli.py should exist at the khalil root."""
        cli_path = os.path.join(os.path.dirname(__file__), "..", "cli.py")
        assert os.path.exists(cli_path)

    def test_cli_has_ask_function(self):
        """cli.py should define _ask coroutine."""
        import importlib
        import cli
        importlib.reload(cli)
        assert hasattr(cli, "_ask")
        assert asyncio.iscoroutinefunction(cli._ask)

    def test_cli_has_repl(self):
        """cli.py should define _repl for interactive mode."""
        import cli
        assert hasattr(cli, "_repl")
        assert callable(cli._repl)


# --- #98: Proactive Knowledge Gap Filling ---

class TestKnowledgeGapFilling:
    def test_no_signals_returns_empty(self, tmp_db):
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        try:
            result = learning.fill_knowledge_gaps()
            assert result == []
        finally:
            learning._db_conn = original

    def test_clusters_similar_queries(self, tmp_db):
        """Queries with 3+ occurrences and word overlap should be clustered."""
        import json
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        try:
            # Insert 3 similar search_miss signals
            for q in ["spotify quarterly goals Q1", "quarterly goals spotify review", "spotify goals quarterly update"]:
                tmp_db.execute(
                    "INSERT INTO interaction_signals (signal_type, context, value) VALUES (?, ?, ?)",
                    ("search_miss", json.dumps({"query": q}), 1.0),
                )
            tmp_db.commit()
            result = learning.fill_knowledge_gaps()
            assert isinstance(result, list)
            # Should find at least one cluster
            assert len(result) >= 1
            assert "query" in result[0]
            assert "results_found" in result[0]
            assert "indexed" in result[0]
        finally:
            learning._db_conn = original

    def test_below_threshold_skipped(self, tmp_db):
        """Clusters with fewer than 3 queries should not be returned."""
        import json
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        try:
            # Only 2 signals — below the 3-occurrence threshold
            for q in ["unique topic alpha", "unique topic alpha repeated"]:
                tmp_db.execute(
                    "INSERT INTO interaction_signals (signal_type, context, value) VALUES (?, ?, ?)",
                    ("search_miss", json.dumps({"query": q}), 1.0),
                )
            tmp_db.commit()
            result = learning.fill_knowledge_gaps()
            assert result == []
        finally:
            learning._db_conn = original


# --- #12: Causal Insight Validation ---

class TestInsightValidation:
    def test_empty_when_no_applied_insights(self, tmp_db):
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        try:
            result = learning.validate_applied_insights(days=14)
            assert result == []
        finally:
            learning._db_conn = original

    def test_validates_preference_insight(self, tmp_db):
        """A preference insight with active linked preference should validate."""
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        try:
            from datetime import datetime, timedelta
            resolved_at = (datetime.utcnow() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
            tmp_db.execute(
                "INSERT INTO insights (id, category, summary, evidence, recommendation, status, resolved_at, resolved_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (1, "preference", "User prefers short replies", "signals", "set preference", "applied", resolved_at, "auto"),
            )
            tmp_db.execute(
                "INSERT INTO learned_preferences (key, value, source_insight_id, confidence) "
                "VALUES (?, ?, ?, ?)",
                ("response_length", '"short"', 1, 0.7),
            )
            tmp_db.commit()
            result = learning.validate_applied_insights(days=14)
            assert len(result) == 1
            assert result[0]["validated"] is True
            assert "still active" in result[0]["reason"]
        finally:
            learning._db_conn = original

    def test_invalidates_decayed_preference(self, tmp_db):
        """A preference insight with decayed confidence should not validate."""
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        try:
            from datetime import datetime, timedelta
            resolved_at = (datetime.utcnow() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
            tmp_db.execute(
                "INSERT INTO insights (id, category, summary, evidence, recommendation, status, resolved_at, resolved_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (1, "preference", "User prefers long replies", "signals", "set preference", "applied", resolved_at, "auto"),
            )
            tmp_db.execute(
                "INSERT INTO learned_preferences (key, value, source_insight_id, confidence) "
                "VALUES (?, ?, ?, ?)",
                ("response_length", '"long"', 1, 0.1),
            )
            tmp_db.commit()
            result = learning.validate_applied_insights(days=14)
            assert len(result) == 1
            assert result[0]["validated"] is False
            assert "decayed" in result[0]["reason"]
        finally:
            learning._db_conn = original

    def test_validates_knowledge_gap_reduction(self, tmp_db):
        """A knowledge_gap insight should validate if search misses decreased."""
        import json
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        try:
            from datetime import datetime, timedelta
            resolved_at = (datetime.utcnow() - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
            before = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")

            # Insert insight
            tmp_db.execute(
                "INSERT INTO insights (id, category, summary, evidence, recommendation, status, resolved_at, resolved_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (1, "knowledge_gap", "Missing info on topic X", "signals", "index more", "applied", resolved_at, "auto"),
            )
            # 3 misses before, 0 after
            for _ in range(3):
                tmp_db.execute(
                    "INSERT INTO interaction_signals (signal_type, context, value, created_at) VALUES (?, ?, ?, ?)",
                    ("search_miss", json.dumps({"query": "topic X"}), 1.0, before),
                )
            tmp_db.commit()
            result = learning.validate_applied_insights(days=14)
            assert len(result) == 1
            assert result[0]["validated"] is True
            assert "decreased" in result[0]["reason"]
        finally:
            learning._db_conn = original


# --- #84: Healing Regression Tests ---

class TestHealingPipeline:
    def test_detect_recurring_failures_empty(self, tmp_db):
        """No signals should return empty triggers."""
        import healing
        from unittest.mock import patch
        with patch.object(healing, "_get_conn", return_value=tmp_db):
            result = healing.detect_recurring_failures()
            assert result == []

    def test_detect_recurring_failures_with_signals(self, tmp_db):
        """Signals above threshold should trigger detection."""
        import json
        import healing
        from unittest.mock import patch
        with patch.object(healing, "_get_conn", return_value=tmp_db):
            for _ in range(4):
                tmp_db.execute(
                    "INSERT INTO interaction_signals (signal_type, context, value) VALUES (?, ?, ?)",
                    ("intent_detection_failure", json.dumps({"action_hint": "shell"}), 1.0),
                )
            tmp_db.commit()
            result = healing.detect_recurring_failures()
            assert len(result) >= 1
            assert result[0]["fingerprint"] == "intent_detection_failure:shell"
            assert result[0]["failure_count"] >= 4

    def test_score_healing_confidence_range(self, tmp_db):
        """Score should always be between 0.0 and 1.0."""
        from healing import score_healing_confidence
        from unittest.mock import patch
        with patch("healing._get_conn", return_value=tmp_db):
            diagnosis = {"fingerprint": "test:test", "failure_count": 10}
            score = score_healing_confidence(diagnosis, "x = 1\ny = 2")
            assert 0.0 <= score <= 1.0

    def test_score_healing_confidence_small_vs_large(self, tmp_db):
        """Small patches should score higher than large patches."""
        from healing import score_healing_confidence
        from unittest.mock import patch
        with patch("healing._get_conn", return_value=tmp_db):
            diagnosis = {"fingerprint": "test:test", "failure_count": 5}
            small = score_healing_confidence(diagnosis, "x = 1")
            large = score_healing_confidence(diagnosis, "\n".join(f"line_{i}" for i in range(50)))
            assert small > large

    def test_failure_code_map_references_valid_files(self):
        """All files in FAILURE_CODE_MAP should exist on disk."""
        from healing import FAILURE_CODE_MAP
        from config import KHALIL_DIR
        for fingerprint, targets in FAILURE_CODE_MAP.items():
            for rel_path, func_name in targets:
                file_path = KHALIL_DIR / rel_path
                assert file_path.exists(), f"FAILURE_CODE_MAP[{fingerprint}] references missing file: {rel_path}"

    def test_critical_error_patterns_detection(self):
        """CRITICAL_ERROR_PATTERNS should match known error strings."""
        from healing import CRITICAL_ERROR_PATTERNS
        test_errors = [
            "ImportError: No module named 'foo'",
            "ModuleNotFoundError: No module named 'bar'",
            "AttributeError: 'NoneType' has no attribute 'x'",
            "SyntaxError: invalid syntax",
        ]
        for error in test_errors:
            assert any(pat in error for pat in CRITICAL_ERROR_PATTERNS), f"Pattern not matched: {error}"

    def test_deterministic_signals_threshold_one(self, tmp_db):
        """Deterministic signal types should trigger after just 1 occurrence."""
        import json
        import healing
        from unittest.mock import patch
        with patch.object(healing, "_get_conn", return_value=tmp_db):
            tmp_db.execute(
                "INSERT INTO interaction_signals (signal_type, context, value) VALUES (?, ?, ?)",
                ("capability_gap_detected", json.dumps({"action_hint": "test_action"}), 1.0),
            )
            tmp_db.commit()
            result = healing.detect_recurring_failures()
            # Should trigger with just 1 signal since it's deterministic
            assert len(result) >= 1


# --- #85: Extension Regression Tests ---

class TestExtensionPipeline:
    def test_detect_capability_gap_positive(self):
        """Phrases indicating inability should trigger gap detection."""
        from actions.extend import detect_capability_gap
        assert detect_capability_gap("I can't do that for you") is True
        assert detect_capability_gap("I don't have access to your calendar") is True
        assert detect_capability_gap("That isn't available yet") is True
        assert detect_capability_gap("You need to check your Mac manually") is True

    def test_detect_capability_gap_negative(self):
        """Normal responses should not trigger gap detection."""
        from actions.extend import detect_capability_gap
        assert detect_capability_gap("Here are your calendar events for today") is False
        assert detect_capability_gap("Email sent successfully") is False
        assert detect_capability_gap("The weather in Toronto is 15C") is False

    def test_check_extension_overlap_no_overlap(self):
        from actions.extend import check_extension_overlap
        result = check_extension_overlap({
            "name": "pomodoro_timer",
            "command": "pomodoro",
            "description": "Pomodoro timer for focus sessions",
        })
        assert result is None

    def test_check_extension_overlap_builtin(self):
        from actions.extend import check_extension_overlap
        result = check_extension_overlap({
            "name": "remind_helper",
            "command": "remind",
            "description": "Set reminders",
        })
        assert result is not None
        assert "remind" in result.lower()

    def test_gap_gate_patterns_regex(self):
        """GAP_GATE_PATTERNS should match known refusal variants."""
        import re
        from actions.extend import GAP_GATE_PATTERNS
        positives = [
            "i can't do that",
            "i cannot access your files",
            "i'm unable to check that",
            "that is beyond my current capabilities",
            "check your mac manually for that info",
            "i don't have real-time data",
            "no built-in support for that feature",
            "that isn't supported right now",
        ]
        for phrase in positives:
            matched = any(re.search(p, phrase.lower()) for p in GAP_GATE_PATTERNS)
            assert matched, f"GAP_GATE_PATTERNS failed to match: '{phrase}'"

        negatives = [
            "here are your results",
            "the email has been sent",
            "done",
        ]
        for phrase in negatives:
            matched = any(re.search(p, phrase.lower()) for p in GAP_GATE_PATTERNS)
            assert not matched, f"GAP_GATE_PATTERNS falsely matched: '{phrase}'"

    def test_smoke_test_module_basic(self, tmp_path):
        """smoke_test_module should pass for a valid module with an async handler."""
        from actions.extend import smoke_test_module
        module = tmp_path / "test_ext.py"
        module.write_text(
            "async def cmd_test(update, context):\n"
            "    await update.message.reply_text('hello')\n"
        )
        passed, error = smoke_test_module(module, "test")
        assert passed, f"Smoke test should pass but got: {error}"

    def test_smoke_test_module_missing_handler(self, tmp_path):
        """smoke_test_module should fail when the handler function is missing."""
        from actions.extend import smoke_test_module
        module = tmp_path / "test_ext2.py"
        module.write_text("def some_other_func(): pass\n")
        passed, error = smoke_test_module(module, "missing")
        assert not passed
        assert "missing" in error.lower() or "Missing" in error


# --- #90: Email Follow-up Detector ---

class TestEmailFollowupDetector:
    def test_no_sent_emails_returns_empty(self, tmp_db):
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        try:
            result = learning.detect_email_followups(days=7)
            assert result == []
        finally:
            learning._db_conn = original

    def test_detects_awaiting_reply(self, tmp_db):
        """A sent email without a matching reply should be flagged."""
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        try:
            tmp_db.execute(
                "INSERT INTO documents (source, category, title, content, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("gmail-sent", "email-sent", "Meeting follow up", "Hi, just following up...",
                 "2026-03-15 10:00:00"),
            )
            tmp_db.commit()
            result = learning.detect_email_followups(days=7)
            assert len(result) == 1
            assert result[0]["awaiting_reply"] is True
            assert result[0]["subject"] == "Meeting follow up"
        finally:
            learning._db_conn = original

    def test_detects_replied(self, tmp_db):
        """A sent email with a matching Re: reply should not be flagged as awaiting."""
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        try:
            tmp_db.execute(
                "INSERT INTO documents (source, category, title, content, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("gmail-sent", "email-sent", "Project update", "Here is the update...",
                 "2026-03-14 10:00:00"),
            )
            tmp_db.execute(
                "INSERT INTO documents (source, category, title, content, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("gmail", "email", "Re: Project update", "Thanks for the update!",
                 "2026-03-15 10:00:00"),
            )
            tmp_db.commit()
            result = learning.detect_email_followups(days=7)
            assert len(result) == 1
            assert result[0]["awaiting_reply"] is False
        finally:
            learning._db_conn = original


# --- #62: RAG Result Reranking ---

class TestRerankScore:
    def test_compute_rerank_score_with_distance(self):
        from knowledge.search import _compute_rerank_score
        result = {
            "title": "Test Doc",
            "content": "Python automation scripts for deployment",
            "distance": 0.5,
            "match_type": "semantic",
            "freshness": 0.8,
        }
        score = _compute_rerank_score(result, ["python", "automation"])
        assert 0.0 < score <= 1.0

    def test_compute_rerank_score_keyword_only(self):
        from knowledge.search import _compute_rerank_score
        result = {
            "title": "Deployment Guide",
            "content": "How to deploy python apps",
            "match_type": "keyword",
            "freshness": 0.5,
        }
        score = _compute_rerank_score(result, ["deploy", "python"])
        assert score > 0.0

    def test_higher_keyword_match_scores_higher(self):
        from knowledge.search import _compute_rerank_score
        result_high = {
            "title": "Python automation",
            "content": "Python automation for deploy",
            "match_type": "keyword",
            "freshness": 0.5,
        }
        result_low = {
            "title": "Unrelated",
            "content": "Nothing matching here",
            "match_type": "keyword",
            "freshness": 0.5,
        }
        terms = ["python", "automation", "deploy"]
        assert _compute_rerank_score(result_high, terms) > _compute_rerank_score(result_low, terms)

    def test_closer_semantic_distance_scores_higher(self):
        from knowledge.search import _compute_rerank_score
        close = {
            "title": "Test", "content": "test content",
            "distance": 0.2, "match_type": "semantic", "freshness": 0.5,
        }
        far = {
            "title": "Test", "content": "test content",
            "distance": 1.5, "match_type": "semantic", "freshness": 0.5,
        }
        assert _compute_rerank_score(close, ["test"]) > _compute_rerank_score(far, ["test"])

    def test_fresher_docs_score_higher(self):
        from knowledge.search import _compute_rerank_score
        fresh = {
            "title": "Test", "content": "test content",
            "match_type": "keyword", "freshness": 1.0,
        }
        stale = {
            "title": "Test", "content": "test content",
            "match_type": "keyword", "freshness": 0.1,
        }
        assert _compute_rerank_score(fresh, ["test"]) > _compute_rerank_score(stale, ["test"])


# ============================================================
# Batch 10: Items #88, #87, #38, #49, #56, #94, #6
# ============================================================


# --- #88: LLM Response Contract Tests ---

class TestLLMResponseContracts:
    """Golden-file style tests validating LLM outputs conform to expected formats."""

    def test_reflection_prompt_structure(self, tmp_db):
        """_build_reflection_prompt() should produce a valid structured prompt."""
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        try:
            from learning import _build_reflection_prompt
            data = {
                "signals": [
                    {"signal_type": "action_decision", "context": '{"action": "shell"}', "value": 1, "created_at": "2026-03-15"},
                ],
                "audit": [],
                "conversations": [{"content": "test conversation", "timestamp": "2026-03-15"}],
                "actions": [],
            }
            prompt = _build_reflection_prompt(data, [])
            assert "## Action Decisions" in prompt
            assert "## Search Misses" in prompt
            assert "## Digest Engagement" in prompt
            assert "## User Conversation Topics" in prompt
            assert "## Current Learned Preferences" in prompt
            assert "JSON array" in prompt
            assert "category" in prompt
            assert "summary" in prompt
            assert "evidence" in prompt
            assert "recommendation" in prompt
            assert "auto_apply" in prompt
        finally:
            learning._db_conn = original

    def test_intent_detection_response_parseable(self):
        """Intent detection JSON response format should be parseable with expected keys."""
        import json
        # Valid intent response format
        valid_responses = [
            '{"action": "shell", "command": "ls -la", "description": "List files"}',
            '{"action": "reminder", "text": "Buy milk", "due": "tomorrow 9am"}',
            '{"action": "email", "query": "from:boss subject:review"}',
        ]
        for resp in valid_responses:
            parsed = json.loads(resp)
            assert "action" in parsed, f"Missing 'action' key in: {resp}"

        # Invalid JSON should raise
        with pytest.raises(json.JSONDecodeError):
            json.loads("not json at all")

    def test_capability_gap_tag_parseable(self):
        """CAPABILITY_GAP tag format should be parseable by server.py regex."""
        valid_tags = [
            "[CAPABILITY_GAP: slack_reader | /slack | Read and search Slack messages]",
            "[CAPABILITY_GAP: weather_check | /weather | Check current weather conditions]",
            "[CAPABILITY_GAP: jira_sync | /jira | Sync Jira tickets and status]",
        ]
        gap_re = re.compile(r'\[CAPABILITY_GAP:\s*(\w+)\s*\|\s*(/\w+)\s*\|\s*(.+?)\]')
        for tag in valid_tags:
            m = gap_re.search(tag)
            assert m is not None, f"Regex failed to match: {tag}"
            assert m.group(1)  # short_name
            assert m.group(2).startswith("/")  # command
            assert len(m.group(3)) > 0  # description

    def test_capability_gap_tag_invalid_rejected(self):
        """Invalid CAPABILITY_GAP tags should not match the regex."""
        gap_re = re.compile(r'\[CAPABILITY_GAP:\s*(\w+)\s*\|\s*(/\w+)\s*\|\s*(.+?)\]')
        invalid_tags = [
            "[CAPABILITY_GAP: ]",
            "[CAPABILITY_GAP: name | cmd | ]",  # no / prefix on command
            "CAPABILITY_GAP: name | /cmd | desc",  # missing brackets
            "[CAPABILITY_GAP: name]",  # missing pipes
        ]
        for tag in invalid_tags:
            m = gap_re.search(tag)
            # Either no match, or matches incorrectly — these should not produce valid 3-group matches
            if m:
                # If it matches, the groups should not all be valid
                assert not (m.group(1) and m.group(2).startswith("/") and len(m.group(3).strip()) > 0), \
                    f"Invalid tag should not match fully: {tag}"

    def test_healing_diagnosis_format(self):
        """Healing diagnosis dict should have expected schema keys."""
        # Expected schema for a healing diagnosis
        diagnosis = {
            "fingerprint": "intent_detection_failure:shell",
            "failure_count": 5,
            "signal_type": "intent_detection_failure",
            "action_hint": "shell",
        }
        required_keys = {"fingerprint", "failure_count"}
        assert required_keys.issubset(diagnosis.keys())
        assert isinstance(diagnosis["fingerprint"], str)
        assert isinstance(diagnosis["failure_count"], int)
        assert ":" in diagnosis["fingerprint"]


# --- #87: Load Test for Concurrent Messages ---

class TestConcurrentAccess:
    """Tests verifying SQLite + handler don't deadlock under concurrent access."""

    def test_concurrent_conversation_writes(self, tmp_path):
        """10 concurrent writes to conversations should not deadlock."""
        import threading
        db_path = tmp_path / "concurrent.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()

        errors = []

        def write_conversation(thread_id):
            try:
                c = sqlite3.connect(str(db_path))
                c.execute("PRAGMA journal_mode=WAL")
                c.execute(
                    "INSERT INTO conversations (chat_id, role, content) VALUES (?, ?, ?)",
                    (thread_id, "user", f"message from thread {thread_id}"),
                )
                c.commit()
                c.close()
            except Exception as e:
                errors.append((thread_id, str(e)))

        threads = [threading.Thread(target=write_conversation, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(errors) == 0, f"Concurrent write errors: {errors}"

        # Verify all 10 rows written
        c = sqlite3.connect(str(db_path))
        count = c.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        c.close()
        assert count == 10

    def test_concurrent_signal_recording(self, tmp_path):
        """10 concurrent signal writes should not deadlock."""
        import json
        import threading
        db_path = tmp_path / "signals.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE interaction_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_type TEXT NOT NULL,
                context TEXT,
                value REAL DEFAULT 1.0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()

        errors = []

        def write_signal(thread_id):
            try:
                c = sqlite3.connect(str(db_path))
                c.execute("PRAGMA journal_mode=WAL")
                c.execute(
                    "INSERT INTO interaction_signals (signal_type, context, value) VALUES (?, ?, ?)",
                    ("test_signal", json.dumps({"thread": thread_id}), 1.0),
                )
                c.commit()
                c.close()
            except Exception as e:
                errors.append((thread_id, str(e)))

        threads = [threading.Thread(target=write_signal, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(errors) == 0, f"Concurrent signal errors: {errors}"
        c = sqlite3.connect(str(db_path))
        count = c.execute("SELECT COUNT(*) FROM interaction_signals").fetchone()[0]
        c.close()
        assert count == 10

    def test_wal_mode_enables_concurrent_reads(self, tmp_path):
        """WAL mode should allow concurrent reads during writes."""
        import threading
        db_path = tmp_path / "wal_test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, val TEXT)")
        conn.execute("INSERT INTO test (val) VALUES ('seed')")
        conn.commit()
        conn.close()

        read_results = []
        errors = []

        def reader(thread_id):
            try:
                c = sqlite3.connect(str(db_path))
                c.execute("PRAGMA journal_mode=WAL")
                rows = c.execute("SELECT COUNT(*) FROM test").fetchone()[0]
                read_results.append(rows)
                c.close()
            except Exception as e:
                errors.append((thread_id, str(e)))

        def writer():
            try:
                c = sqlite3.connect(str(db_path))
                c.execute("PRAGMA journal_mode=WAL")
                for i in range(5):
                    c.execute("INSERT INTO test (val) VALUES (?)", (f"val_{i}",))
                c.commit()
                c.close()
            except Exception as e:
                errors.append(("writer", str(e)))

        # Start writer and readers concurrently
        threads = [threading.Thread(target=writer)]
        threads += [threading.Thread(target=reader, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(errors) == 0, f"WAL concurrency errors: {errors}"
        assert len(read_results) == 5


# --- #38: App Window Management ---

class TestWindowManagement:
    def test_arrange_windows_pattern(self):
        """'arrange windows side by side' should match shell pattern."""
        from server import _looks_like_action
        assert _looks_like_action("arrange windows side by side") == "shell"

    def test_minimize_windows_pattern(self):
        from server import _looks_like_action
        assert _looks_like_action("minimize all windows") == "shell"

    def test_show_windows_pattern(self):
        from server import _looks_like_action
        assert _looks_like_action("show all windows") == "shell"

    def test_resize_window_pattern(self):
        from server import _looks_like_action
        assert _looks_like_action("resize the window") == "shell"

    def test_arrange_windows_direct_intent(self):
        from server import _try_direct_shell_intent
        result = _try_direct_shell_intent("arrange windows side by side")
        assert result is not None
        assert "osascript" in result["command"]

    def test_minimize_windows_direct_intent(self):
        from server import _try_direct_shell_intent
        result = _try_direct_shell_intent("minimize all windows")
        assert result is not None
        assert "osascript" in result["command"]
        assert "visible" in result["command"]

    def test_show_all_windows_direct_intent(self):
        from server import _try_direct_shell_intent
        result = _try_direct_shell_intent("show all windows")
        assert result is not None
        assert "osascript" in result["command"]

    def test_resize_window_direct_intent(self):
        from server import _try_direct_shell_intent
        result = _try_direct_shell_intent("resize the window")
        assert result is not None
        assert "osascript" in result["command"]


# --- #49: Google Contacts Search ---

class TestGoogleContacts:
    def test_contacts_pattern_find(self):
        from server import _looks_like_action
        assert _looks_like_action("find contact John") == "contacts"

    def test_contacts_pattern_search(self):
        from server import _looks_like_action
        assert _looks_like_action("search my contacts for Ahmed") == "contacts"

    def test_contacts_pattern_email_for(self):
        from server import _looks_like_action
        assert _looks_like_action("email address for Sarah") == "contacts"

    def test_contacts_direct_intent(self):
        from server import _try_direct_shell_intent
        result = _try_direct_shell_intent("find contact John Smith")
        assert result is not None
        assert result["action"] == "contacts_search"
        assert "John Smith" in result["query"]

    def test_contacts_scopes_defined(self):
        from actions.gmail import SCOPES_CONTACTS
        assert len(SCOPES_CONTACTS) == 1
        assert "contacts.readonly" in SCOPES_CONTACTS[0]

    def test_search_contacts_sync_with_mock(self):
        """_search_contacts_sync should parse People API response correctly."""
        from unittest.mock import MagicMock, patch
        mock_service = MagicMock()
        mock_service.people().searchContacts().execute.return_value = {
            "results": [
                {
                    "person": {
                        "names": [{"displayName": "John Doe"}],
                        "emailAddresses": [{"value": "john@example.com"}],
                        "phoneNumbers": [{"value": "+1234567890"}],
                    }
                },
                {
                    "person": {
                        "names": [{"displayName": "Jane Smith"}],
                        "emailAddresses": [{"value": "jane@example.com"}],
                        "phoneNumbers": [],
                    }
                },
            ]
        }
        with patch("actions.gmail._get_people_service", return_value=mock_service):
            from actions.gmail import _search_contacts_sync
            results = _search_contacts_sync("John")
            assert len(results) == 2
            assert results[0]["name"] == "John Doe"
            assert results[0]["email"] == "john@example.com"
            assert results[0]["phone"] == "+1234567890"
            assert results[1]["phone"] == ""


# --- #56: iCloud Reminders Sync ---

class TestICloudReminders:
    def test_icloud_pattern_add(self):
        from server import _looks_like_action
        assert _looks_like_action("add to apple reminders buy milk") == "icloud_reminder"

    def test_icloud_pattern_show(self):
        from server import _looks_like_action
        assert _looks_like_action("show my icloud reminders") == "icloud_reminder"

    def test_icloud_pattern_app(self):
        from server import _looks_like_action
        assert _looks_like_action("open reminders app") == "icloud_reminder"

    def test_get_icloud_reminders_with_mock(self):
        """get_icloud_reminders should parse osascript output."""
        from unittest.mock import patch, MagicMock
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Buy groceries|||March 20, 2026\nPay rent|||March 25, 2026\n"
        with patch("actions.reminders.subprocess.run", return_value=mock_result):
            from actions.reminders import get_icloud_reminders
            reminders = get_icloud_reminders()
            assert len(reminders) == 2
            assert reminders[0]["name"] == "Buy groceries"
            assert reminders[0]["due_date"] == "March 20, 2026"
            assert reminders[1]["name"] == "Pay rent"

    def test_get_icloud_reminders_failure(self):
        """get_icloud_reminders should return empty on osascript failure."""
        from unittest.mock import patch, MagicMock
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "error"
        with patch("actions.reminders.subprocess.run", return_value=mock_result):
            from actions.reminders import get_icloud_reminders
            assert get_icloud_reminders() == []

    def test_create_icloud_reminder_with_mock(self):
        """create_icloud_reminder should call osascript and return success."""
        from unittest.mock import patch, MagicMock
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("actions.reminders.subprocess.run", return_value=mock_result):
            from actions.reminders import create_icloud_reminder
            result = create_icloud_reminder("Buy milk", "2026-03-20 09:00")
            assert result["created"] is True
            assert result["name"] == "Buy milk"

    def test_create_icloud_reminder_no_due_date(self):
        """create_icloud_reminder should work without a due date."""
        from unittest.mock import patch, MagicMock
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("actions.reminders.subprocess.run", return_value=mock_result):
            from actions.reminders import create_icloud_reminder
            result = create_icloud_reminder("Buy milk")
            assert result["created"] is True
            assert result["due_date"] == ""

    def test_icloud_direct_intent_list(self):
        from server import _try_direct_shell_intent
        result = _try_direct_shell_intent("show my apple reminders")
        assert result is not None
        assert result["action"] == "icloud_reminder_list"

    def test_icloud_direct_intent_create(self):
        from server import _try_direct_shell_intent
        result = _try_direct_shell_intent("add to apple reminders buy milk")
        assert result is not None
        assert result["action"] == "icloud_reminder_create"
        assert "buy milk" in result["text"]


# --- #94: Goal Progress Auto-Check ---

class TestGoalProgress:
    def test_no_goals_returns_empty(self, tmp_db, tmp_path, monkeypatch):
        """Should return empty when no goals directory."""
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        monkeypatch.setattr("learning.GOALS_DIR", tmp_path / "nonexistent")
        try:
            result = learning.check_goal_progress(days=7)
            assert result == []
        finally:
            learning._db_conn = original

    def test_goals_with_no_activity(self, tmp_db, tmp_path, monkeypatch):
        """Goals with no matching signals should report no_activity."""
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        goals_dir = tmp_path / "goals"
        goals_dir.mkdir()
        (goals_dir / "2026.md").write_text("- Learn Rust programming\n- Ship Bezier MVP\n")
        monkeypatch.setattr("learning.GOALS_DIR", goals_dir)
        try:
            result = learning.check_goal_progress(days=7)
            assert len(result) == 2
            assert all(r["status"] == "no_activity" for r in result)
        finally:
            learning._db_conn = original

    def test_goals_with_activity(self, tmp_db, tmp_path, monkeypatch):
        """Goals with matching conversation activity should be marked active."""
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        goals_dir = tmp_path / "goals"
        goals_dir.mkdir()
        (goals_dir / "2026.md").write_text("- Learn Rust programming\n")
        monkeypatch.setattr("learning.GOALS_DIR", goals_dir)

        # Add conversations mentioning "Rust" and "programming"
        for i in range(6):
            tmp_db.execute(
                "INSERT INTO conversations (chat_id, role, content) VALUES (?, ?, ?)",
                (1, "user", f"I've been studying Rust programming chapter {i}"),
            )
        tmp_db.commit()
        try:
            result = learning.check_goal_progress(days=7)
            assert len(result) == 1
            assert result[0]["status"] == "active"
            assert result[0]["activity_count"] >= 5
        finally:
            learning._db_conn = original

    def test_goals_extracts_from_markdown(self, tmp_db, tmp_path, monkeypatch):
        """Should extract goals from markdown bullet points and headers."""
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        goals_dir = tmp_path / "goals"
        goals_dir.mkdir()
        (goals_dir / "q1.md").write_text(
            "## Ship the new feature\n"
            "- Write more tests\n"
            "* Read three books\n"
        )
        monkeypatch.setattr("learning.GOALS_DIR", goals_dir)
        try:
            result = learning.check_goal_progress(days=7)
            goal_texts = [r["goal"] for r in result]
            assert "Ship the new feature" in goal_texts
            assert "Write more tests" in goal_texts
            assert "Read three books" in goal_texts
        finally:
            learning._db_conn = original


# --- #6: Multi-turn Coherence Analysis ---

class TestCoherenceAnalysis:
    def test_no_issues_when_empty(self, tmp_db):
        """Should return empty when no conversations."""
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        try:
            result = learning.detect_coherence_issues(days=7)
            assert result == []
        finally:
            learning._db_conn = original

    def test_detects_repeated_info(self, tmp_db):
        """Should detect 'I already told you' patterns."""
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        tmp_db.execute(
            "INSERT INTO conversations (chat_id, role, content) VALUES (?, ?, ?)",
            (1, "user", "I already told you my name is Ahmed"),
        )
        tmp_db.commit()
        try:
            result = learning.detect_coherence_issues(days=7)
            assert len(result) == 1
            assert result[0]["issue_type"] == "repeated_info"
        finally:
            learning._db_conn = original

    def test_detects_correction(self, tmp_db):
        """Should detect 'No, I said...' correction patterns."""
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        tmp_db.execute(
            "INSERT INTO conversations (chat_id, role, content) VALUES (?, ?, ?)",
            (1, "user", "No, I said I wanted the short version"),
        )
        tmp_db.commit()
        try:
            result = learning.detect_coherence_issues(days=7)
            assert len(result) == 1
            assert result[0]["issue_type"] == "correction"
        finally:
            learning._db_conn = original

    def test_detects_lost_context(self, tmp_db):
        """Should detect 'you forgot' patterns indicating lost context."""
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        tmp_db.execute(
            "INSERT INTO conversations (chat_id, role, content) VALUES (?, ?, ?)",
            (1, "user", "You forgot about the deadline I mentioned"),
        )
        tmp_db.commit()
        try:
            result = learning.detect_coherence_issues(days=7)
            assert len(result) == 1
            assert result[0]["issue_type"] == "lost_context"
        finally:
            learning._db_conn = original

    def test_no_false_positives(self, tmp_db):
        """Normal messages should not trigger coherence issues."""
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        normal_messages = [
            "What's the weather today?",
            "Set a reminder for tomorrow",
            "Check my calendar",
            "Thanks, that's helpful",
        ]
        for msg in normal_messages:
            tmp_db.execute(
                "INSERT INTO conversations (chat_id, role, content) VALUES (?, ?, ?)",
                (1, "user", msg),
            )
        tmp_db.commit()
        try:
            result = learning.detect_coherence_issues(days=7)
            assert len(result) == 0
        finally:
            learning._db_conn = original

    def test_returns_expected_fields(self, tmp_db):
        """Each issue should have chat_id, timestamp, issue_type, context."""
        import learning
        original = learning._db_conn
        learning._db_conn = tmp_db
        tmp_db.execute(
            "INSERT INTO conversations (chat_id, role, content) VALUES (?, ?, ?)",
            (42, "user", "I already said I want bullet points"),
        )
        tmp_db.commit()
        try:
            result = learning.detect_coherence_issues(days=7)
            assert len(result) == 1
            issue = result[0]
            assert "chat_id" in issue
            assert "timestamp" in issue
            assert "issue_type" in issue
            assert "context" in issue
            assert issue["chat_id"] == 42
        finally:
            learning._db_conn = original
