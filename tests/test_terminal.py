"""Tests for actions/terminal.py — Cursor IDE and iTerm2 awareness."""

import asyncio
import pytest
from actions.terminal import (
    parse_cursor_status,
    parse_iterm_sessions,
    format_cursor_status,
    format_terminal_status,
    diff_dev_state,
    format_state_changes,
    _escape_applescript,
)

# --- Fixtures: real output from cursor --status ---

CURSOR_STATUS_OUTPUT = """\
Version:          Cursor 2.6.19 (224838f96445be37e3db643a163a817c15b36060, 2026-03-12T04:07:27.435Z)
OS Version:       Darwin arm64 25.3.0
CPUs:             Apple M1 Max (10 x 2400)
Memory (System):  64.00GB (9.84GB free)
Load (avg):       3, 4, 4
VM:               0%
Screen Reader:    no
Process Argv:
GPU Status:       2d_canvas:                              enabled
                  gpu_compositing:                        enabled

CPU %\tMem MB\t   PID\tProcess
    2\t   983\t  1346\tcursor main
    4\t   131\t  1503\t   gpu-process
    0\t    66\t  1505\t   utility-network-service
    0\t   524\t  1618\twindow [1] (.env — compass-AI)
    0\t   524\t  1619\twindow [2] (LOOP.md — ParentingAssistant)
   80\t   590\t  1620\twindow [3] (distribution-playbook.md — PM-AI-Partner-Framework)
    0\t   131\t  1653\tshared-process
"""

CURSOR_STATUS_NO_WINDOWS = """\
Version:          Cursor 2.6.19 (abc123, 2026-03-12)
OS Version:       Darwin arm64 25.3.0
CPUs:             Apple M1 Max (10 x 2400)
Memory (System):  64.00GB (32.00GB free)

CPU %\tMem MB\t   PID\tProcess
    1\t   500\t  1000\tcursor main
"""

CURSOR_STATUS_EMPTY = ""


class TestParseCursorStatus:
    def test_parses_version(self):
        result = parse_cursor_status(CURSOR_STATUS_OUTPUT)
        assert result["version"] == "Cursor"  # First token after ":"
        # Actually let's check it parses the version string
        assert result["version"] is not None

    def test_parses_memory(self):
        result = parse_cursor_status(CURSOR_STATUS_OUTPUT)
        assert result["memory_system"] == "64.00GB"
        assert result["memory_free"] == "9.84GB"

    def test_parses_cpus(self):
        result = parse_cursor_status(CURSOR_STATUS_OUTPUT)
        assert "M1 Max" in result["cpus"]

    def test_parses_windows(self):
        result = parse_cursor_status(CURSOR_STATUS_OUTPUT)
        assert len(result["windows"]) == 3

    def test_window_details(self):
        result = parse_cursor_status(CURSOR_STATUS_OUTPUT)
        w1 = result["windows"][0]
        assert w1["id"] == 1
        assert w1["name"] == ".env"
        assert w1["project"] == "compass-AI"
        assert w1["pid"] == 1618
        assert w1["cpu_pct"] == 0
        assert w1["mem_mb"] == 524

    def test_window_high_cpu(self):
        result = parse_cursor_status(CURSOR_STATUS_OUTPUT)
        w3 = result["windows"][2]
        assert w3["cpu_pct"] == 80
        assert w3["project"] == "PM-AI-Partner-Framework"

    def test_no_windows(self):
        result = parse_cursor_status(CURSOR_STATUS_NO_WINDOWS)
        assert result["windows"] == []
        assert result["memory_free"] == "32.00GB"

    def test_empty_output(self):
        result = parse_cursor_status(CURSOR_STATUS_EMPTY)
        assert result["windows"] == []
        assert result["version"] is None

    def test_window_without_project(self):
        """Window title without ' — project' separator."""
        raw = "    0\t   100\t  999\twindow [1] (untitled)"
        result = parse_cursor_status(raw)
        assert len(result["windows"]) == 1
        assert result["windows"][0]["name"] == "untitled"
        assert result["windows"][0]["project"] is None


class TestParseItermSessions:
    def test_parses_sessions(self):
        raw = "ahmedm@P296|||zsh|||/dev/ttys003|||true\nahmedm@P296|||python|||/dev/ttys004|||false\n"
        sessions = parse_iterm_sessions(raw)
        assert len(sessions) == 2
        assert sessions[0]["window"] == "ahmedm@P296"
        assert sessions[0]["tty"] == "/dev/ttys003"
        assert sessions[0]["is_current"] is True
        assert sessions[1]["is_current"] is False

    def test_empty_output(self):
        assert parse_iterm_sessions("") == []
        assert parse_iterm_sessions("\n") == []

    def test_partial_line(self):
        """Lines with fewer than 4 parts are skipped."""
        raw = "incomplete|||data\nvalid|||name|||/dev/ttys001|||true\n"
        sessions = parse_iterm_sessions(raw)
        assert len(sessions) == 1


class TestFormatCursorStatus:
    def test_formats_with_windows(self):
        status = parse_cursor_status(CURSOR_STATUS_OUTPUT)
        text = format_cursor_status(status)
        assert "Cursor Status" in text
        assert "compass-AI" in text
        assert "ParentingAssistant" in text
        assert "80% CPU" in text  # high CPU warning

    def test_formats_error(self):
        text = format_cursor_status({"error": "not found", "windows": []})
        assert "not found" in text

    def test_formats_no_windows(self):
        status = parse_cursor_status(CURSOR_STATUS_NO_WINDOWS)
        text = format_cursor_status(status)
        assert "No windows open" in text


class TestFormatTerminalStatus:
    def test_formats_sessions(self):
        status = {
            "sessions": [
                {"window": "w1", "name": "zsh", "tty": "/dev/ttys003", "is_current": True,
                 "process": "python server.py", "pid": 123, "elapsed": "02:30"},
                {"window": "w1", "name": "zsh", "tty": "/dev/ttys004", "is_current": False,
                 "process": "idle (zsh)", "pid": None, "elapsed": None},
            ],
            "count": 2,
        }
        text = format_terminal_status(status)
        assert "Terminal Sessions (2)" in text
        assert "python server.py" in text
        assert "idle (zsh)" in text

    def test_formats_empty(self):
        text = format_terminal_status({"sessions": [], "count": 0})
        assert "No iTerm2 sessions" in text


# --- Milestone 3: Proactive Polling & State Diffing ---

class TestDiffDevState:
    def test_empty_old_state(self):
        """First snapshot — no changes."""
        new = {"cursor_projects": ["foo"], "cursor_window_count": 1,
               "cursor_high_cpu": [], "iterm_session_count": 2,
               "iterm_ttys": [], "frontmost_app": "Cursor"}
        assert diff_dev_state({}, new) == []

    def test_new_project_opened(self):
        old = {"cursor_projects": ["foo"], "cursor_window_count": 1,
               "cursor_high_cpu": [], "iterm_session_count": 1,
               "iterm_ttys": [], "frontmost_app": "Cursor"}
        new = {**old, "cursor_projects": ["foo", "bar"], "cursor_window_count": 2}
        changes = diff_dev_state(old, new)
        assert any("opened project bar" in c for c in changes)

    def test_project_closed(self):
        old = {"cursor_projects": ["foo", "bar"], "cursor_window_count": 2,
               "cursor_high_cpu": [], "iterm_session_count": 1,
               "iterm_ttys": [], "frontmost_app": "Cursor"}
        new = {**old, "cursor_projects": ["foo"], "cursor_window_count": 1}
        changes = diff_dev_state(old, new)
        assert any("closed project bar" in c for c in changes)

    def test_all_cursor_windows_closed(self):
        old = {"cursor_projects": ["foo"], "cursor_window_count": 2,
               "cursor_high_cpu": [], "iterm_session_count": 1,
               "iterm_ttys": [], "frontmost_app": "Cursor"}
        new = {**old, "cursor_projects": [], "cursor_window_count": 0}
        changes = diff_dev_state(old, new)
        assert any("all windows closed" in c for c in changes)

    def test_cursor_opened_from_zero(self):
        old = {"cursor_projects": [], "cursor_window_count": 0,
               "cursor_high_cpu": [], "iterm_session_count": 1,
               "iterm_ttys": [], "frontmost_app": "Finder"}
        new = {**old, "cursor_projects": ["proj"], "cursor_window_count": 2, "frontmost_app": "Cursor"}
        changes = diff_dev_state(old, new)
        assert any("opened" in c and "2 windows" in c for c in changes)

    def test_high_cpu_alert(self):
        old = {"cursor_projects": ["foo"], "cursor_window_count": 1,
               "cursor_high_cpu": [], "iterm_session_count": 1,
               "iterm_ttys": [], "frontmost_app": "Cursor"}
        new = {**old, "cursor_high_cpu": [{"project": "foo", "cpu": 85}]}
        changes = diff_dev_state(old, new)
        assert any("85% CPU" in c for c in changes)

    def test_high_cpu_no_repeat(self):
        """Don't re-alert if same project was already high CPU."""
        old = {"cursor_projects": ["foo"], "cursor_window_count": 1,
               "cursor_high_cpu": [{"project": "foo", "cpu": 85}],
               "iterm_session_count": 1, "iterm_ttys": [], "frontmost_app": "Cursor"}
        new = {**old, "cursor_high_cpu": [{"project": "foo", "cpu": 90}]}
        changes = diff_dev_state(old, new)
        assert not any("CPU" in c for c in changes)

    def test_new_terminal_sessions(self):
        old = {"cursor_projects": [], "cursor_window_count": 0,
               "cursor_high_cpu": [], "iterm_session_count": 2,
               "iterm_ttys": [], "frontmost_app": "iTerm2"}
        new = {**old, "iterm_session_count": 4}
        changes = diff_dev_state(old, new)
        assert any("2 new session(s)" in c for c in changes)

    def test_terminal_sessions_closed(self):
        old = {"cursor_projects": [], "cursor_window_count": 0,
               "cursor_high_cpu": [], "iterm_session_count": 3,
               "iterm_ttys": [], "frontmost_app": "iTerm2"}
        new = {**old, "iterm_session_count": 1}
        changes = diff_dev_state(old, new)
        assert any("2 session(s) closed" in c for c in changes)

    def test_frontmost_app_change(self):
        old = {"cursor_projects": [], "cursor_window_count": 0,
               "cursor_high_cpu": [], "iterm_session_count": 1,
               "iterm_ttys": [], "frontmost_app": "Cursor"}
        new = {**old, "frontmost_app": "Safari"}
        changes = diff_dev_state(old, new)
        assert any("Switched to Safari" in c for c in changes)

    def test_no_changes(self):
        state = {"cursor_projects": ["foo"], "cursor_window_count": 1,
                 "cursor_high_cpu": [], "iterm_session_count": 2,
                 "iterm_ttys": ["/dev/ttys003"], "frontmost_app": "Cursor"}
        assert diff_dev_state(state, state) == []


class TestFormatStateChanges:
    def test_formats_changes(self):
        changes = ["🖥 Cursor: opened project foo", "📟 Terminal: 1 new session(s) opened"]
        text = format_state_changes(changes)
        assert "Dev Environment Update" in text
        assert "opened project foo" in text

    def test_empty_changes(self):
        assert format_state_changes([]) == ""


# --- Milestone 4+5: Terminal & Cursor Control ---

class TestEscapeApplescript:
    def test_escapes_quotes(self):
        assert _escape_applescript('say "hello"') == 'say \\"hello\\"'

    def test_escapes_backslash(self):
        assert _escape_applescript("path\\to") == "path\\\\to"

    def test_plain_string(self):
        assert _escape_applescript("ls -la") == "ls -la"

    def test_combined(self):
        assert _escape_applescript('echo "test\\n"') == 'echo \\"test\\\\n\\"'


class TestIntentPatterns:
    """Test that _ACTION_PATTERNS and _try_direct_shell_intent catch terminal/cursor intents."""

    def _action_hint(self, text):
        """Return the _ACTION_PATTERNS hint for text."""
        import re
        # Import at test time to get the latest patterns
        import importlib
        import server
        importlib.reload(server)
        for pattern, hint in server._ACTION_PATTERNS:
            if re.search(pattern, text.lower()):
                return hint
        return None

    def _direct_intent(self, text):
        """Return direct intent dict."""
        import importlib
        import server
        importlib.reload(server)
        return server._try_direct_shell_intent(text)

    def test_cursor_status_pattern(self):
        assert self._action_hint("cursor status") == "cursor_status"
        assert self._action_hint("what's open in cursor") == "cursor_status"

    def test_terminal_status_pattern(self):
        assert self._action_hint("terminal sessions") == "terminal_status"
        assert self._action_hint("what's running in my terminal") == "terminal_status"

    def test_terminal_exec_pattern(self):
        assert self._action_hint("run npm test in terminal") == "terminal_exec"

    def test_terminal_new_tab_pattern(self):
        assert self._action_hint("new terminal tab") == "terminal_new_tab"
        assert self._action_hint("open a new terminal") == "terminal_new_tab"

    def test_cursor_open_pattern(self):
        assert self._action_hint("open server.py in cursor") == "cursor_open"
        assert self._action_hint("cursor open config.py") == "cursor_open"

    def test_cursor_diff_pattern(self):
        assert self._action_hint("cursor diff") == "cursor_diff"

    # Direct intent tests
    def test_direct_cursor_status(self):
        intent = self._direct_intent("cursor status")
        assert intent["action"] == "cursor_status"

    def test_direct_terminal_status(self):
        intent = self._direct_intent("terminal sessions")
        assert intent["action"] == "terminal_status"

    def test_direct_run_in_terminal(self):
        intent = self._direct_intent("run npm test in terminal")
        assert intent["action"] == "terminal_exec"
        assert intent["command"] == "npm test"

    def test_direct_send_to_terminal(self):
        intent = self._direct_intent("send ls -la to terminal")
        assert intent["action"] == "terminal_exec"
        assert intent["command"] == "ls -la"

    def test_direct_new_tab(self):
        intent = self._direct_intent("new terminal tab")
        assert intent["action"] == "terminal_new_tab"

    def test_direct_open_in_cursor(self):
        intent = self._direct_intent("open server.py in cursor")
        assert intent["action"] == "cursor_open"
        assert intent["path"] == "server.py"

    def test_direct_cursor_open(self):
        intent = self._direct_intent("cursor open config.py")
        assert intent["action"] == "cursor_open"
        assert intent["path"] == "config.py"

    def test_direct_jump_to_line(self):
        intent = self._direct_intent("jump to line 42 in server.py")
        assert intent["action"] == "cursor_open"
        assert intent["line"] == 42
        assert intent["path"] == "server.py"

    def test_direct_cursor_diff(self):
        intent = self._direct_intent("cursor diff file1.py file2.py")
        assert intent["action"] == "cursor_diff"
        assert intent["file1"] == "file1.py"
        assert intent["file2"] == "file2.py"


class TestAutonomyRules:
    """Test that terminal/cursor actions have correct autonomy classification."""

    def test_terminal_exec_is_write(self):
        from autonomy import ACTION_RULES
        from config import ActionType
        assert ACTION_RULES["terminal_exec"] == ActionType.WRITE

    def test_cursor_open_is_read(self):
        from autonomy import ACTION_RULES
        from config import ActionType
        assert ACTION_RULES["cursor_open"] == ActionType.READ

    def test_cursor_open_project_is_write(self):
        from autonomy import ACTION_RULES
        from config import ActionType
        assert ACTION_RULES["cursor_open_project"] == ActionType.WRITE

    def test_terminal_new_tab_is_write(self):
        from autonomy import ACTION_RULES
        from config import ActionType
        assert ACTION_RULES["terminal_new_tab"] == ActionType.WRITE

    def test_cursor_diff_is_read(self):
        from autonomy import ACTION_RULES
        from config import ActionType
        assert ACTION_RULES["cursor_diff"] == ActionType.READ


# --- Milestone 6: MCP Tools ---

class TestMcpToolsExist:
    """Verify MCP dev tools are defined in mcp_server.py source."""

    def test_mcp_tools_defined_in_source(self):
        """Check that all dev environment MCP tools exist in mcp_server.py source."""
        from pathlib import Path
        source = (Path(__file__).parent.parent / "mcp_server.py").read_text()
        expected_tools = [
            "dev_environment_status",
            "cursor_open_file",
            "list_terminal_sessions",
            "run_in_terminal",
            "cursor_diff_files",
        ]
        for tool_name in expected_tools:
            assert f"async def {tool_name}" in source, f"MCP tool {tool_name} not found in mcp_server.py"
            assert f"@mcp.tool()" in source  # At least one decorator exists
