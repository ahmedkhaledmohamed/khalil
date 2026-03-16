"""Tests for autonomy classification and approval decisions."""

import pytest

from autonomy import AutonomyController, ACTION_RULES, SAFE_WRITES
from config import AutonomyLevel, ActionType, HARD_GUARDRAILS


class TestClassifyAction:
    def test_read_actions(self, tmp_db):
        ctrl = AutonomyController(tmp_db)
        for action in ["search_knowledge", "get_context", "search_email", "search_drive", "get_timeline", "summarize", "shell_read"]:
            assert ctrl.classify_action(action) == ActionType.READ, f"{action} should be READ"

    def test_write_actions(self, tmp_db):
        ctrl = AutonomyController(tmp_db)
        for action in ["send_email", "draft_email", "create_reminder", "modify_file", "shell_write"]:
            assert ctrl.classify_action(action) == ActionType.WRITE, f"{action} should be WRITE"

    def test_dangerous_actions(self, tmp_db):
        ctrl = AutonomyController(tmp_db)
        for action in ["send_money", "delete_data", "share_externally", "modify_financial_account", "generate_capability", "shell_dangerous"]:
            assert ctrl.classify_action(action) == ActionType.DANGEROUS, f"{action} should be DANGEROUS"

    def test_unknown_defaults_to_write(self, tmp_db):
        ctrl = AutonomyController(tmp_db)
        assert ctrl.classify_action("unknown_action") == ActionType.WRITE


class TestHardGuardrails:
    """Hard guardrails ALWAYS need approval, regardless of autonomy level."""

    @pytest.mark.parametrize("guardrail", HARD_GUARDRAILS)
    def test_supervised(self, tmp_db, guardrail):
        ctrl = AutonomyController(tmp_db)
        ctrl.set_level(AutonomyLevel.SUPERVISED)
        assert ctrl.needs_approval(guardrail) is True

    @pytest.mark.parametrize("guardrail", HARD_GUARDRAILS)
    def test_guided(self, tmp_db, guardrail):
        ctrl = AutonomyController(tmp_db)
        ctrl.set_level(AutonomyLevel.GUIDED)
        assert ctrl.needs_approval(guardrail) is True

    @pytest.mark.parametrize("guardrail", HARD_GUARDRAILS)
    def test_autonomous(self, tmp_db, guardrail):
        ctrl = AutonomyController(tmp_db)
        ctrl.set_level(AutonomyLevel.AUTONOMOUS)
        assert ctrl.needs_approval(guardrail) is True


class TestReadActions:
    """READ actions never need approval."""

    @pytest.mark.parametrize("level", list(AutonomyLevel))
    def test_read_never_needs_approval(self, tmp_db, level):
        ctrl = AutonomyController(tmp_db)
        ctrl.set_level(level)
        assert ctrl.needs_approval("search_knowledge") is False
        assert ctrl.needs_approval("search_email") is False


class TestWriteActions:
    def test_supervised_always_needs_approval(self, tmp_db):
        ctrl = AutonomyController(tmp_db)
        ctrl.set_level(AutonomyLevel.SUPERVISED)
        assert ctrl.needs_approval("send_email") is True
        assert ctrl.needs_approval("create_reminder") is True
        assert ctrl.needs_approval("draft_email") is True

    def test_guided_safe_writes_auto_approved(self, tmp_db):
        ctrl = AutonomyController(tmp_db)
        ctrl.set_level(AutonomyLevel.GUIDED)
        for action in SAFE_WRITES:
            assert ctrl.needs_approval(action) is False, f"{action} should be auto-approved in GUIDED"

    def test_guided_risky_writes_need_approval(self, tmp_db):
        ctrl = AutonomyController(tmp_db)
        ctrl.set_level(AutonomyLevel.GUIDED)
        assert ctrl.needs_approval("send_email") is True
        assert ctrl.needs_approval("modify_file") is True

    def test_autonomous_writes_auto_approved(self, tmp_db):
        ctrl = AutonomyController(tmp_db)
        ctrl.set_level(AutonomyLevel.AUTONOMOUS)
        assert ctrl.needs_approval("send_email") is False
        assert ctrl.needs_approval("modify_file") is False
