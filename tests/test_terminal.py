"""Tests for actions/terminal.py — Cursor IDE and iTerm2 awareness."""

import pytest
from actions.terminal import (
    parse_cursor_status,
    parse_iterm_sessions,
    format_cursor_status,
    format_terminal_status,
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
