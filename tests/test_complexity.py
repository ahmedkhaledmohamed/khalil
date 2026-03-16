"""Tests for capability complexity classification."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from actions.extend import classify_complexity, SIMPLE_CAPABILITIES, COMPLEX_SIGNALS


class TestComplexCapabilities:
    @pytest.mark.parametrize("name,description", [
        ("slack_reader", "Read Slack messages"),
        ("jira_tracker", "Track Jira issues"),
        ("github_pr", "Manage GitHub pull requests"),
        ("notion_sync", "Sync with Notion"),
        ("linear_issues", "Track Linear issues"),
        ("twitter_bot", "Post tweets via Twitter API"),
        ("spotify_player", "Control Spotify playback"),
    ])
    def test_complex_by_name(self, name, description):
        assert classify_complexity({"name": name, "description": description}) == "complex"

    @pytest.mark.parametrize("name,description", [
        ("reader", "Read Slack messages and channels"),
        ("tracker", "OAuth-based issue tracker"),
        ("scraper", "Scrape web pages for data"),
        ("monitor", "Real-time monitoring dashboard"),
    ])
    def test_complex_by_description(self, name, description):
        assert classify_complexity({"name": name, "description": description}) == "complex"


class TestSimpleCapabilities:
    @pytest.mark.parametrize("name", list(SIMPLE_CAPABILITIES))
    def test_each_simple_capability(self, name):
        assert classify_complexity({"name": name, "description": f"A {name} tool"}) == "simple"


class TestDefaultBehavior:
    def test_unknown_defaults_to_complex(self):
        assert classify_complexity({"name": "weather", "description": "Check weather"}) == "complex"

    def test_empty_spec(self):
        assert classify_complexity({}) == "complex"

    def test_missing_fields(self):
        assert classify_complexity({"name": "timer"}) == "simple"
        assert classify_complexity({"description": "something"}) == "complex"
