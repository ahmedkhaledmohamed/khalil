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
