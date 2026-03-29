"""Telegram command handler eval — tests /commands through mock Update objects.

Calls cmd_* handlers directly with mocked Telegram Update/Context,
captures responses via InstrumentedChannel.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@dataclass
class CommandTestCase:
    id: str
    command: str            # e.g. "health", "goals", "search quantum"
    handler_name: str       # e.g. "cmd_health", "cmd_goals"
    args: list[str]         # parsed args after command name
    expected_contains: list[str] = field(default_factory=list)
    expected_not_contains: list[str] = field(default_factory=list)
    expect_error: bool = False
    description: str = ""


@dataclass
class CommandTestResult:
    case_id: str
    passed: bool
    response: str
    latency_s: float
    checks: list[str] = field(default_factory=list)  # "pass: X" or "fail: X"
    error: str | None = None


# ---------------------------------------------------------------------------
# Test cases — commands that don't require external APIs
# ---------------------------------------------------------------------------

COMMAND_CASES: list[CommandTestCase] = [
    # === No-arg commands (should always produce output) ===
    CommandTestCase(
        "cmd-health-001", "health", "cmd_health", [],
        expected_contains=["health"],
        description="health check status",
    ),
    CommandTestCase(
        "cmd-help-001", "help", "cmd_help", [],
        expected_contains=["help"],
        description="help text",
    ),
    CommandTestCase(
        "cmd-start-001", "start", "cmd_start", [],
        description="start/welcome message",
    ),
    CommandTestCase(
        "cmd-stats-001", "stats", "cmd_stats", [],
        description="usage statistics",
    ),
    CommandTestCase(
        "cmd-goals-001", "goals", "cmd_goals", [],
        description="current goals",
    ),
    CommandTestCase(
        "cmd-remind-list", "remind", "cmd_remind", ["list"],
        description="list reminders",
    ),
    CommandTestCase(
        "cmd-audit-001", "audit", "cmd_audit", [],
        description="system audit",
    ),
    CommandTestCase(
        "cmd-dev-001", "dev", "cmd_dev", [],
        description="dev info",
    ),

    # === Commands with args ===
    CommandTestCase(
        "cmd-search-001", "search test", "cmd_search", ["test"],
        expected_contains=["search"],
        description="search with query",
    ),
    CommandTestCase(
        "cmd-search-empty", "search", "cmd_search", [],
        expected_contains=["usage"],
        description="search without query shows usage",
    ),
    CommandTestCase(
        "cmd-goals-all", "goals all", "cmd_goals", ["all"],
        description="all goals",
    ),
    CommandTestCase(
        "cmd-mode-001", "mode", "cmd_mode", [],
        description="current mode",
    ),

    # === Commands that should produce structured output ===
    CommandTestCase(
        "cmd-commitments-001", "commitments", "cmd_commitments", [],
        description="commitments list",
    ),
    CommandTestCase(
        "cmd-tasks-001", "tasks", "cmd_tasks", [],
        description="tasks list",
    ),
    CommandTestCase(
        "cmd-learn-001", "learn", "cmd_learn", [],
        description="learning stats",
    ),
    CommandTestCase(
        "cmd-extensions-001", "extensions", "cmd_extensions", [],
        description="extension management",
    ),
]


# ---------------------------------------------------------------------------
# Mock infrastructure
# ---------------------------------------------------------------------------

def _make_mock_update(chat_id: int = 12345, user_id: int = 12345) -> MagicMock:
    """Create a minimal mock Telegram Update."""
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user.id = user_id
    update.message.text = ""
    update.message.reply_text = AsyncMock()
    return update


def _make_mock_context(args: list[str]) -> MagicMock:
    """Create a minimal mock Telegram context."""
    ctx = MagicMock()
    ctx.args = args
    return ctx


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run_command_tests() -> dict:
    """Run all command test cases. Returns summary dict."""
    # Import server init directly to avoid yaml dependency from eval.cases
    import server
    server.db_conn = server.init_db()
    server.autonomy = server.AutonomyController(server.db_conn)
    from learning import set_conn as set_learning_conn
    set_learning_conn(server.db_conn)
    row = server.db_conn.execute(
        "SELECT value FROM settings WHERE key = 'owner_chat_id'"
    ).fetchone()
    if row:
        server.OWNER_CHAT_ID = int(row[0])
    from skills import get_registry
    get_registry()
    server_mod = server

    # Use InstrumentedChannel from runner (import the class directly)
    from channels import ActionButton, Channel, ChannelType, IncomingMessage, SentMessage

    class InstrumentedChannel(Channel):
        channel_type = ChannelType.TELEGRAM
        def __init__(self):
            self.messages: list[str] = []
            self._msg_counter: int = 0
        async def send_message(self, chat_id, text, *, buttons=None, parse_mode=None):
            self._msg_counter += 1
            if text.strip() != "Thinking...":
                self.messages.append(text)
            return SentMessage(chat_id=chat_id, message_id=self._msg_counter, channel=self)
        async def edit_message(self, chat_id, message_id, text, *, parse_mode=None):
            if text.strip() != "Thinking...":
                self.messages.append(text)
        async def delete_message(self, chat_id, message_id):
            pass
        async def send_photo(self, chat_id, photo_path, caption=""):
            self._msg_counter += 1
            self.messages.append(caption or f"[photo: {photo_path}]")
            return SentMessage(chat_id=chat_id, message_id=self._msg_counter, channel=self)
        async def send_typing(self, chat_id):
            pass

    channel = InstrumentedChannel()

    # Create an InstrumentedChannel and patch it into channel_registry
    channel = InstrumentedChannel()

    results: list[CommandTestResult] = []
    start = time.monotonic()

    for case in COMMAND_CASES:
        channel.messages.clear()
        channel._msg_counter = 0

        handler = getattr(server_mod, case.handler_name, None)
        if handler is None:
            results.append(CommandTestResult(
                case_id=case.id, passed=False, response="",
                latency_s=0, error=f"Handler {case.handler_name} not found",
            ))
            continue

        update = _make_mock_update()
        update.message.text = f"/{case.command}"
        context = _make_mock_context(case.args)

        # Patch _ctx_from_update to use our InstrumentedChannel
        from channels.message_context import MessageContext
        from channels import ChannelType, IncomingMessage

        def _mock_ctx_from_update(u, _ch=channel, _cmd=case.command):
            return MessageContext(
                channel=_ch,
                chat_id="eval",
                user_id="eval",
                channel_type=ChannelType.TELEGRAM,
                incoming=IncomingMessage(
                    text=f"/{_cmd}",
                    chat_id="eval",
                    user_id="eval",
                    channel_type=ChannelType.TELEGRAM,
                ),
            )

        case_start = time.monotonic()
        error = None

        try:
            with patch.object(server_mod, "_ctx_from_update", _mock_ctx_from_update):
                await asyncio.wait_for(handler(update, context), timeout=30.0)
        except asyncio.TimeoutError:
            error = "Timeout after 30s"
        except Exception as e:
            error = f"{type(e).__name__}: {e}"

        elapsed = time.monotonic() - case_start
        response = "\n".join(channel.messages)

        # Evaluate
        checks = []
        passed = True

        # Non-empty response (unless error expected)
        if not case.expect_error:
            if len(response) > 0:
                checks.append("pass: non_empty")
            else:
                checks.append("fail: response empty")
                passed = False

        # Expected contains
        response_lower = response.lower()
        for s in case.expected_contains:
            if s.lower() in response_lower:
                checks.append(f"pass: contains '{s}'")
            else:
                checks.append(f"fail: missing '{s}'")
                passed = False

        # Expected not contains
        for s in case.expected_not_contains:
            if s.lower() not in response_lower:
                checks.append(f"pass: excludes '{s}'")
            else:
                checks.append(f"fail: unexpected '{s}'")
                passed = False

        # No error
        if error:
            checks.append(f"fail: {error}")
            passed = False
        else:
            checks.append("pass: no_error")

        results.append(CommandTestResult(
            case_id=case.id,
            passed=passed,
            response=response[:200],
            latency_s=round(elapsed, 3),
            checks=checks,
            error=error,
        ))

    total_elapsed = time.monotonic() - start
    passed_count = sum(1 for r in results if r.passed)
    failed = [r for r in results if not r.passed]

    # Print results
    print(f"\n{'=' * 60}")
    print(f"COMMAND HANDLER EVAL — {len(COMMAND_CASES)} cases in {total_elapsed:.2f}s")
    print(f"  Passed: {passed_count}/{len(COMMAND_CASES)} ({passed_count/len(COMMAND_CASES):.1%})")
    print(f"{'=' * 60}")

    if failed:
        print(f"\nFAILURES ({len(failed)}):")
        for r in failed:
            fail_checks = [c for c in r.checks if c.startswith("fail")]
            print(f"  {r.case_id}: {'; '.join(fail_checks)}")
            if r.response:
                print(f"    response: {r.response[:100]}")

    return {
        "total": len(COMMAND_CASES),
        "passed": passed_count,
        "failed": len(failed),
        "pass_rate": passed_count / len(COMMAND_CASES),
        "elapsed_s": round(total_elapsed, 3),
        "failures": [{"id": r.case_id, "checks": r.checks} for r in failed],
    }


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(run_command_tests())
