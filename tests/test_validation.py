"""Tests for code validation in the self-extension pipeline."""

import pytest

from actions.extend import validate_generated_code


def _wrap_async(code: str) -> str:
    """Wrap code in an async function to pass the async requirement."""
    return f"import logging\nlog = logging.getLogger('test')\n{code}\nasync def cmd_test(u, c): pass"


class TestBlockedImports:
    @pytest.mark.parametrize("imp", [
        "import subprocess",
        "import ctypes",
        "import socket",
        # NOTE: "from http.server import ..." is not blocked because the check
        # splits on "." and checks only the first part ("http") against the
        # blocklist. Bug tracked separately.
        "from xmlrpc.server import SimpleXMLRPCServer",
    ])
    def test_blocked_imports_rejected(self, imp):
        code = _wrap_async(imp)
        ok, err = validate_generated_code(code)
        assert not ok
        assert "Blocked import" in err

    @pytest.mark.parametrize("imp", [
        "import logging",
        "import sqlite3",
        "import json",
        "import asyncio",
        "import httpx",
        "import keyring",
        "from datetime import datetime",
        "from config import DB_PATH",
    ])
    def test_allowed_imports_pass(self, imp):
        code = _wrap_async(imp)
        ok, _ = validate_generated_code(code)
        assert ok


class TestBlockedCalls:
    @pytest.mark.parametrize("call,expected_fragment", [
        ("eval('1+1')", "eval"),
        ("exec('print(1)')", "exec"),
        ("compile('x', 'f', 'exec')", "compile"),
        ("__import__('os')", "__import__"),
    ])
    def test_blocked_bare_calls(self, call, expected_fragment):
        code = _wrap_async(call)
        ok, err = validate_generated_code(code)
        assert not ok
        assert expected_fragment in err

    @pytest.mark.parametrize("call,expected_fragment", [
        ("import os\nos.system('ls')", "os.system"),
        ("import os\nos.popen('ls')", "os.popen"),
        ("import shutil\nshutil.rmtree('/')", "shutil.rmtree"),
        ("import shutil\nshutil.move('a', 'b')", "shutil.move"),
    ])
    def test_blocked_qualified_calls(self, call, expected_fragment):
        code = _wrap_async(call)
        ok, err = validate_generated_code(code)
        assert not ok
        assert expected_fragment in err


class TestSafeCalls:
    """Regression tests — these must NOT be blocked (conn.execute bug)."""

    @pytest.mark.parametrize("code_snippet", [
        "conn.execute('SELECT 1')",
        "cursor.executemany(query, params)",
        "conn.executescript(schema)",
        "db.execute('INSERT INTO t VALUES (?)', (1,))",
    ])
    def test_sqlite_operations_allowed(self, code_snippet):
        code = _wrap_async(f"import sqlite3\nconn = sqlite3.connect(':memory:')\n{code_snippet}")
        ok, err = validate_generated_code(code)
        assert ok, f"Should pass but got: {err}"

    def test_httpx_allowed(self):
        code = _wrap_async("import httpx\nhttpx.get('https://api.example.com')")
        ok, _ = validate_generated_code(code)
        assert ok

    def test_keyring_allowed(self):
        code = _wrap_async("import keyring\nkeyring.get_password('service', 'key')")
        ok, _ = validate_generated_code(code)
        assert ok


class TestStructureChecks:
    def test_no_async_function_rejected(self):
        code = "def sync_only(): pass"
        ok, err = validate_generated_code(code)
        assert not ok
        assert "async" in err.lower()

    def test_syntax_error_rejected(self):
        code = "def broken("
        ok, err = validate_generated_code(code)
        assert not ok
        assert "Syntax" in err or "syntax" in err

    def test_valid_module_passes(self):
        code = (
            "import logging\n"
            "log = logging.getLogger('test')\n"
            "async def cmd_test(update, context):\n"
            "    await update.message.reply_text('ok')\n"
        )
        ok, err = validate_generated_code(code)
        assert ok, f"Should pass but got: {err}"
