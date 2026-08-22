"""Tests for autonomy classification and approval decisions."""

import ast
from pathlib import Path
from types import SimpleNamespace

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

    def test_unknown_defaults_to_dangerous(self, tmp_db):
        ctrl = AutonomyController(tmp_db)
        assert ctrl.classify_action("unknown_action") == ActionType.DANGEROUS

    def test_declared_type_classifies_registered_action(self, tmp_db):
        ctrl = AutonomyController(tmp_db)
        assert ctrl.classify_action(
            "calendar", declared_type=ActionType.DANGEROUS,
        ) == ActionType.DANGEROUS

    def test_shell_classification_uses_command_payload(self, tmp_db):
        ctrl = AutonomyController(tmp_db)
        assert ctrl.classify_action(
            "shell", {"command": "ls -la"}, ActionType.WRITE,
        ) == ActionType.READ
        assert ctrl.classify_action(
            "shell", {"command": "touch demo.txt"}, ActionType.WRITE,
        ) == ActionType.WRITE
        assert ctrl.classify_action(
            "shell", {"command": "rm -rf /"}, ActionType.WRITE,
        ) == ActionType.DANGEROUS

    def test_trust_does_not_change_intrinsic_classification(self, tmp_db):
        ctrl = AutonomyController(tmp_db)
        ctrl._trust_scores["send_email"] = {
            "successes": 50, "failures": 0, "promoted": True,
        }

        assert ctrl.classify_action("send_email") == ActionType.WRITE
        assert ctrl.needs_approval("send_email") is False


class TestSkillRiskMetadata:
    def test_build_skill_requires_valid_default_risk(self):
        from skills import _build_skill

        missing = SimpleNamespace(SKILL={
            "name": "missing",
            "actions": [],
        })
        invalid = SimpleNamespace(SKILL={
            "name": "invalid",
            "risk": "maybe",
            "actions": [],
        })

        with pytest.raises(ValueError, match="default risk"):
            _build_skill("missing", missing)
        with pytest.raises(ValueError, match="invalid default risk"):
            _build_skill("invalid", invalid)

    def test_action_override_is_typed_and_queryable(self):
        from skills import SkillRegistry, _build_skill

        module = SimpleNamespace(
            handle=lambda *args: True,
            SKILL={
                "name": "demo",
                "description": "Demo",
                "risk": "read",
                "actions": [
                    {"type": "demo_read", "handler": "handle"},
                    {"type": "demo_write", "handler": "handle", "risk": "write"},
                ],
            },
        )
        registry = SkillRegistry()
        registry.register(_build_skill("demo", module))

        assert registry.get_action_type("demo_read") == ActionType.READ
        assert registry.get_action_type("demo_write") == ActionType.WRITE
        assert registry.get_action_type("missing") is None

    def test_all_literal_skill_manifests_declare_valid_risk(self):
        valid = {action_type.value for action_type in ActionType}
        actions_dir = Path(__file__).parents[1] / "actions"
        actions_seen = 0
        declared = {}

        for path in actions_dir.glob("*.py"):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in tree.body:
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if not any(
                    isinstance(target, ast.Name) and target.id == "SKILL"
                    for target in targets
                ) or not isinstance(node.value, ast.Dict):
                    continue
                manifest = {
                    key.value: value
                    for key, value in zip(node.value.keys, node.value.values)
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                }
                default = manifest.get("risk")
                assert isinstance(default, ast.Constant), f"{path.name} lacks default risk"
                assert default.value in valid, f"{path.name} has invalid risk {default.value}"
                actions = manifest.get("actions")
                if not isinstance(actions, (ast.List, ast.Tuple)):
                    continue
                for action in actions.elts:
                    if not isinstance(action, ast.Dict):
                        continue
                    action_fields = {
                        key.value: value
                        for key, value in zip(action.keys, action.values)
                        if isinstance(key, ast.Constant) and isinstance(key.value, str)
                    }
                    risk = action_fields.get("risk", default)
                    assert isinstance(risk, ast.Constant)
                    assert risk.value in valid
                    action_name = action_fields["type"].value
                    declared[(path.stem, action_name)] = risk.value
                    actions_seen += 1

        assert actions_seen >= 200
        assert declared[("weather", "weather")] == "read"
        assert declared[("reminders", "reminder")] == "write"
        assert declared[("calendar", "calendar")] == "dangerous"
        assert declared[("gmail", "email_personal")] == "dangerous"
        assert declared[("machine", "screenshot")] == "dangerous"
        assert declared[("github_api", "github_merge_pr")] == "dangerous"


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
