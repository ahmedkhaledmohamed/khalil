"""Tests for capability gap detection — phrase matching and structured tags."""

import re

import pytest

from actions.extend import detect_capability_gap, CAPABILITY_GAP_PHRASES

# The regex used in server.py for structured gap tags
GAP_TAG_PATTERN = r'\[CAPABILITY_GAP:\s*(\w+)\s*\|\s*(/\w+)\s*\|\s*(.+?)\]'


class TestPhraseDetection:
    @pytest.mark.parametrize("phrase", CAPABILITY_GAP_PHRASES)
    def test_each_phrase_triggers(self, phrase):
        response = f"Sorry, {phrase} for now."
        assert detect_capability_gap(response) is True

    def test_case_insensitive(self):
        assert detect_capability_gap("I CAN'T DO THAT") is True
        assert detect_capability_gap("I Don't Have The Ability") is True

    def test_device_access_refusal(self):
        """Regression: Khalil said it can't access the device when it can run shell commands."""
        assert detect_capability_gap("I would need direct access to your device") is True
        assert detect_capability_gap("I don't have real-time monitoring capabilities") is True
        assert detect_capability_gap("I can't determine the exact number") is True
        assert detect_capability_gap("please check your Mac manually") is True

    @pytest.mark.parametrize("response", [
        "Here are your emails from last week.",
        "The meeting is at 3pm tomorrow.",
        "Your portfolio is up 3% this month.",
        "I found 5 matching documents.",
        "Reminder created for tomorrow at 9am.",
        "",
    ])
    def test_no_false_positives(self, response):
        assert detect_capability_gap(response) is False


class TestStructuredTagParsing:
    def test_valid_tag(self):
        text = "I can't do that. [CAPABILITY_GAP: slack_reader | /slack | Read and search Slack messages] Would you like me to try?"
        m = re.search(GAP_TAG_PATTERN, text)
        assert m is not None
        assert m.group(1) == "slack_reader"
        assert m.group(2) == "/slack"
        assert m.group(3) == "Read and search Slack messages"

    def test_no_tag(self):
        assert re.search(GAP_TAG_PATTERN, "normal response text") is None

    def test_extra_whitespace(self):
        m = re.search(GAP_TAG_PATTERN, "[CAPABILITY_GAP:  timer  |  /timer  |  Set a timer]")
        assert m is not None
        assert m.group(1) == "timer"
        assert m.group(2) == "/timer"
        assert m.group(3) == "Set a timer"

    def test_tag_with_surrounding_text(self):
        text = "Sorry, I can't do that yet.\n[CAPABILITY_GAP: jira_tracker | /jira | Track Jira issues]\nBut I can help you set it up."
        m = re.search(GAP_TAG_PATTERN, text)
        assert m is not None
        assert m.group(1) == "jira_tracker"

    def test_partial_tag_no_match(self):
        assert re.search(GAP_TAG_PATTERN, "[CAPABILITY_GAP: incomplete") is None
        assert re.search(GAP_TAG_PATTERN, "[CAPABILITY_GAP: name | /cmd") is None

    def test_name_must_be_word_chars(self):
        # Spaces in name should not match
        assert re.search(GAP_TAG_PATTERN, "[CAPABILITY_GAP: slack reader | /slack | desc]") is None

    def test_command_must_start_with_slash(self):
        assert re.search(GAP_TAG_PATTERN, "[CAPABILITY_GAP: timer | timer | desc]") is None
