"""Tests for improved gap detection (semantic gate) and smoke test."""

import os
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from actions.extend import detect_capability_gap, GAP_GATE_PATTERNS, smoke_test_module


class TestSemanticGate:
    """The semantic gate should catch all known refusal patterns plus novel variants."""

    @pytest.mark.parametrize("response", [
        # Original phrase list entries
        "Sorry, I can't do that right now.",
        "I don't have the ability to read Slack.",
        "That capability isn't available yet.",
        "I can't currently access your device.",
        "Not something I can do yet.",
        "I don't have a feature for that.",
        "I don't have that capability.",
        "That's not something I support.",
        "I'm not able to check that.",
        "No built-in support for Jira integration.",
        "I would need direct access to your device.",
        "I don't have real-time monitoring.",
        "I can't determine the exact number.",
        "I don't have access to your Slack.",
        "I can't access your calendar directly.",
        "I'm unable to perform that action.",
        "That's beyond my current capabilities.",
        "Please check your Mac manually.",
        # NOVEL patterns the old phrase list would miss
        "I cannot read your Slack messages directly.",
        "I won't be able to do that without an API key.",
        "I couldn't access that service.",
        "Unfortunately, I do not have the ability to track Jira issues.",
        "This is not possible with my current setup.",
    ])
    def test_catches_refusals(self, response):
        assert detect_capability_gap(response) is True, f"Should detect gap in: {response!r}"

    @pytest.mark.parametrize("response", [
        "Here are your emails from last week.",
        "The meeting is at 3pm tomorrow.",
        "Your portfolio is up 3% this month.",
        "I found 5 matching documents.",
        "Reminder created for tomorrow at 9am.",
        "",
        "Sure, I can help with that!",
        "The weather today is sunny.",
        "Done. File has been saved.",
    ])
    def test_no_false_positives(self, response):
        assert detect_capability_gap(response) is False, f"False positive on: {response!r}"


class TestSmokeTest:
    def test_valid_module_passes(self, tmp_path):
        module = tmp_path / "good_module.py"
        module.write_text(textwrap.dedent("""\
            async def cmd_test(update, context):
                pass
        """))
        ok, err = smoke_test_module(module, "test")
        assert ok, f"Should pass but got: {err}"

    def test_missing_handler_fails(self, tmp_path):
        module = tmp_path / "bad_handler.py"
        module.write_text(textwrap.dedent("""\
            async def cmd_wrong_name(update, context):
                pass
        """))
        ok, err = smoke_test_module(module, "test")
        assert not ok
        assert "Missing handler" in err

    def test_import_error_fails(self, tmp_path):
        module = tmp_path / "bad_import.py"
        module.write_text(textwrap.dedent("""\
            import nonexistent_module_xyz
            async def cmd_test(update, context):
                pass
        """))
        ok, err = smoke_test_module(module, "test")
        assert not ok
        assert "ModuleNotFoundError" in err or "No module named" in err

    def test_syntax_error_fails(self, tmp_path):
        module = tmp_path / "bad_syntax.py"
        module.write_text("def broken(\n")
        ok, err = smoke_test_module(module, "test")
        assert not ok
