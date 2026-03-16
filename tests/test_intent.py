"""Tests for direct shell intent mapping (no LLM).

server.py has heavy top-level imports (anthropic, telegram, etc.) that aren't
available in the test environment. We mock them so we can import just the
pure-logic helper we need.
"""

import os
import re
import sys
import types
from unittest.mock import MagicMock

import pytest

# Stub out heavy dependencies before importing server
_STUBS = [
    "anthropic", "telegram", "telegram.ext", "telegram.constants",
    "google.oauth2.credentials", "google.auth.transport.requests",
    "googleapiclient.discovery", "googleapiclient", "google.oauth2",
    "google.auth", "google.auth.transport", "google",
    "apscheduler", "apscheduler.schedulers", "apscheduler.schedulers.asyncio",
    "apscheduler.triggers", "apscheduler.triggers.cron",
    "fastapi", "fastapi.responses", "pydantic_settings", "pydantic",
    "uvicorn", "httpx", "keyring", "mcp", "mcp.server", "mcp.server.fastmcp",
    "sqlite_vec", "croniter",
]
for mod_name in _STUBS:
    if mod_name not in sys.modules:
        sys.modules[mod_name] = MagicMock()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from server import _try_direct_shell_intent, _extract_shell_from_response


class TestOpenApp:
    @pytest.mark.parametrize("text,expected_app", [
        ("open slack", "Slack"),
        ("open the safari", "Safari"),
        ("open chrome", "Google Chrome"),
        ("open vs code", "Visual Studio Code"),
        ("open vscode", "Visual Studio Code"),
        ("open finder", "Finder"),
        ("open terminal", "Terminal"),
        ("open music", "Music"),
        ("open notes", "Notes"),
        ("open calendar", "Calendar"),
        ("open spotify", "Spotify"),
        ("open mail", "Mail"),
        ("open discord", "Discord"),
        ("open zoom", "zoom.us"),
        ("open arc", "Arc"),
        ("open firefox", "Firefox"),
        ("open brave", "Brave Browser"),
    ])
    def test_app_opening(self, text, expected_app):
        result = _try_direct_shell_intent(text)
        assert result is not None
        assert result["action"] == "shell"
        assert result["command"] == f"open -a '{expected_app}'"

    def test_open_the_prefix(self):
        result = _try_direct_shell_intent("open the safari")
        assert result is not None
        assert result["command"] == "open -a 'Safari'"


class TestOpenURL:
    def test_http_url(self):
        result = _try_direct_shell_intent("open http://example.com")
        assert result is not None
        assert result["command"] == "open http://example.com"

    def test_https_url(self):
        result = _try_direct_shell_intent("open https://google.com")
        assert result is not None
        assert result["command"] == "open https://google.com"

    def test_url_preserves_case(self):
        result = _try_direct_shell_intent("open https://GitHub.com/MyRepo")
        assert result is not None
        assert "GitHub.com/MyRepo" in result["command"]


class TestDiskSpace:
    @pytest.mark.parametrize("text", [
        "check disk space",
        "check storage",
        "check space",
    ])
    def test_disk_space_variants(self, text):
        result = _try_direct_shell_intent(text)
        assert result is not None
        assert result["command"] == "df -h"


class TestNoMatch:
    @pytest.mark.parametrize("text", [
        "what's the weather",
        "remind me to buy milk",
        "send an email to John",
        "search my emails",
        "hello",
        "",
    ])
    def test_returns_none(self, text):
        assert _try_direct_shell_intent(text) is None


class TestDirectShellTemplates:
    """Direct intent templates bypass the LLM for common system queries."""

    @pytest.mark.parametrize("text,expected_fragment", [
        ("how many cursor windows open on my machine", 'count windows of process "Cursor"'),
        ("how many Safari windows are open", 'count windows of process "Safari"'),
        ("How many Chrome windows open", 'count windows of process "Chrome"'),
    ])
    def test_window_count(self, text, expected_fragment):
        result = _try_direct_shell_intent(text)
        assert result is not None
        assert expected_fragment in result["command"]

    def test_running_processes(self):
        result = _try_direct_shell_intent("what apps are running")
        assert result is not None
        assert "ps" in result["command"]

    def test_battery(self):
        result = _try_direct_shell_intent("what's my battery")
        assert result is not None
        assert "pmset" in result["command"]

    def test_battery_status(self):
        result = _try_direct_shell_intent("battery status level")
        assert result is not None
        assert "pmset" in result["command"]

    def test_ip_address(self):
        result = _try_direct_shell_intent("what's my ip")
        assert result is not None
        assert "ipconfig" in result["command"]

    def test_uptime(self):
        result = _try_direct_shell_intent("how long has my mac been running")
        assert result is not None
        assert "uptime" in result["command"]


class TestExtractShellFromResponse:
    """Detect shell commands the LLM suggests instead of executing."""

    def test_extracts_from_sh_block(self):
        response = (
            "I can help with that. Run this command:\n"
            "```sh\n"
            "osascript -e 'tell application \"System Events\" to get the name of every window of process \"Cursor\"'\n"
            "```\n"
            "This will list all windows."
        )
        cmd = _extract_shell_from_response(response)
        assert cmd is not None
        assert "osascript" in cmd

    def test_extracts_from_bare_block(self):
        response = "Try:\n```\ndf -h\n```"
        assert _extract_shell_from_response(response) == "df -h"

    def test_ignores_multiline_scripts(self):
        response = "```sh\ncd /tmp\nls -la\nrm file\n```"
        assert _extract_shell_from_response(response) is None

    def test_ignores_no_code_block(self):
        response = "You have 5 Cursor windows open on your machine."
        assert _extract_shell_from_response(response) is None

    def test_ignores_comment_only_blocks(self):
        response = "```sh\n# This is just a comment\n```"
        assert _extract_shell_from_response(response) is None

    def test_extracts_from_bash_block(self):
        response = "```bash\npmset -g batt\n```"
        assert _extract_shell_from_response(response) == "pmset -g batt"
