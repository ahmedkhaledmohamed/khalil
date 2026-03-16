"""Tests for shell error classification and retry safety."""

import pytest

from actions.shell import classify_error, would_escalate, classify_command
from config import ActionType


class TestClassifyError:
    @pytest.mark.parametrize("stderr", [
        "Connection refused",
        "Resource temporarily unavailable",
        "Resource busy",
        "try again later",
        "Command timed out after 30s",
    ])
    def test_transient(self, stderr):
        assert classify_error(1, stderr) == "transient"

    @pytest.mark.parametrize("stderr", [
        "permission denied",
        "Operation not permitted",
        "command not found",
        "No such file or directory",
        "Not a directory",
    ])
    def test_permanent(self, stderr):
        assert classify_error(1, stderr) == "permanent"

    @pytest.mark.parametrize("stderr", [
        "execution error: The variable cursor is not defined. (-2753)",
        "syntax error: expected end of line but found identifier",
        "invalid option -- 'z'",
        "unrecognized arguments: --foo",
        "error: unknown command 'bloop'",
    ])
    def test_correctable(self, stderr):
        assert classify_error(1, stderr) == "correctable"

    def test_empty_stderr_is_correctable(self):
        assert classify_error(1, "") == "correctable"


class TestWouldEscalate:
    def test_read_to_read(self):
        assert would_escalate("ls -la", "ls -la /tmp") is False

    def test_read_to_write(self):
        assert would_escalate("ls -la", "pip install requests") is True

    def test_read_to_dangerous(self):
        assert would_escalate("ls -la", "sudo rm -rf /") is True

    def test_write_to_dangerous(self):
        assert would_escalate("pip install foo", "sudo rm -rf /") is True

    def test_write_to_read(self):
        assert would_escalate("pip install foo", "ls -la") is False

    def test_same_level(self):
        assert would_escalate("pip install foo", "npm install bar") is False

    def test_osascript_to_osascript(self):
        """osascript corrections should not escalate."""
        orig = "osascript -e 'tell application \"System Events\" to count windows of desktop where class is cursor'"
        corrected = "osascript -e 'tell application \"System Events\" to count windows of process \"Cursor\"'"
        assert would_escalate(orig, corrected) is False
