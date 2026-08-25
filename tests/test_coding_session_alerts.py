"""Focused behavior checks for coding-session approval notifications."""

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from actions import dev_tools


class RecordingChannel:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))
        return SimpleNamespace(message_id=len(self.messages))


def _process(*, cwd=None):
    return {
        "agent": "Codex",
        "pid": 123,
        "started": "10:00AM",
        "tty": "s001",
        "stat": "S+",
        "cwd": cwd,
        "ppid": 456,
    }


def _target():
    return {
        "kind": "cursor",
        "target": "2",
        "identity": "cursor:456",
        "name": "codex",
        "bridge_url": "http://127.0.0.1:8034",
        "read_target": "2",
    }


def _payload(*, approval=True):
    return {
        "schema_version": 1,
        "event_id": "event-1",
        "agent": "codex",
        "event": "needs_input",
        "hook_event": "permission_request" if approval else "pre_tool_use",
        "session_id": "session-1",
        "pid": 123,
        "tty": "s001",
        "cwd": "/work/project",
        "prompt": "Shell requested permission.",
        "approval": approval,
    }


def _configure_state(monkeypatch, tmp_path):
    db_path = tmp_path / "bridge.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(dev_tools, "DB_PATH", db_path)


def _configure_session_runtime(monkeypatch, process):
    async def processes():
        return [process]

    async def targets(_processes):
        return {dev_tools._session_key(process): _target()}

    monkeypatch.setattr(dev_tools, "_get_coding_agent_processes", processes)
    monkeypatch.setattr(dev_tools, "_resolve_control_targets", targets)


def test_native_approval_waits_for_confirmation_and_preserves_hook_project(monkeypatch, tmp_path):
    _configure_state(monkeypatch, tmp_path)
    process = _process(cwd=None)
    _configure_session_runtime(monkeypatch, process)
    channel = RecordingChannel()

    result = asyncio.run(dev_tools._record_coding_agent_event(_payload(), channel, 7))

    state = dev_tools._load_bridge_state()
    session = state["sessions"][result["key"]]
    assert result["pending_confirmation"] is True
    assert channel.messages == []
    assert session["status"] == "pending_confirmation"
    assert session["cwd"] == "/work/project"


def test_auto_resolved_approval_does_not_notify(monkeypatch, tmp_path):
    _configure_state(monkeypatch, tmp_path)
    process = _process(cwd="/work/project")
    _configure_session_runtime(monkeypatch, process)
    channel = RecordingChannel()
    result = asyncio.run(dev_tools._record_coding_agent_event(_payload(), channel, 7))

    async def cleared_output(_target, lines=50):
        return "Running the approved command now"

    monkeypatch.setattr(dev_tools, "_read_target", cleared_output)
    confirmation = asyncio.run(dev_tools._confirm_pending_approval(
        result["key"], result["native_candidate_hash"], channel, 7,
    ))

    session = dev_tools._load_bridge_state()["sessions"][result["key"]]
    assert confirmation["reason"] == "approval_prompt_cleared"
    assert session["status"] == "auto_resolved"
    assert channel.messages == []


def test_persistent_approval_notifies_once_after_two_matching_probes(monkeypatch, tmp_path):
    _configure_state(monkeypatch, tmp_path)
    process = _process(cwd="/work/project")
    _configure_session_runtime(monkeypatch, process)
    channel = RecordingChannel()
    result = asyncio.run(dev_tools._record_coding_agent_event(_payload(), channel, 7))

    async def waiting_output(_target, lines=50):
        return "Do you want to proceed?\n1. Yes, allow\n2. No"

    monkeypatch.setattr(dev_tools, "_read_target", waiting_output)
    first = asyncio.run(dev_tools._confirm_pending_approval(
        result["key"], result["native_candidate_hash"], channel, 7,
    ))
    second = asyncio.run(dev_tools._confirm_pending_approval(
        result["key"], result["native_candidate_hash"], channel, 7,
    ))
    third = asyncio.run(dev_tools._confirm_pending_approval(
        result["key"], result["native_candidate_hash"], channel, 7,
    ))

    state = dev_tools._load_bridge_state()
    session = state["sessions"][result["key"]]
    assert first["reason"] == "awaiting_stability"
    assert second["notified"] is True
    assert third["reason"] == "no_longer_pending"
    assert len(channel.messages) == 1
    assert "Project: /work/project" in channel.messages[0][1]
    assert session["status"] == "needs_input"
    assert state["message_index"] == {"1": result["key"]}


def test_non_approval_input_still_notifies_immediately(monkeypatch, tmp_path):
    _configure_state(monkeypatch, tmp_path)
    process = _process(cwd="/work/project")
    _configure_session_runtime(monkeypatch, process)
    channel = RecordingChannel()

    result = asyncio.run(dev_tools._record_coding_agent_event(
        _payload(approval=False), channel, 7,
    ))

    assert result["notified"] is True
    assert len(channel.messages) == 1
    assert "needs input" in channel.messages[0][1]


def test_polling_confirms_pending_approval_after_restart(monkeypatch, tmp_path):
    _configure_state(monkeypatch, tmp_path)
    process = _process(cwd="/work/project")
    _configure_session_runtime(monkeypatch, process)
    channel = RecordingChannel()
    result = asyncio.run(dev_tools._record_coding_agent_event(_payload(), channel, 7))
    state = dev_tools._load_bridge_state()
    state["sessions"][result["key"]]["native_event_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=15)
    ).isoformat()
    dev_tools._save_bridge_state(state)

    async def waiting_output(_target, lines=50):
        return "Do you want to proceed?\n1. Yes, allow\n2. No"

    monkeypatch.setattr(dev_tools, "_read_target", waiting_output)
    first = asyncio.run(dev_tools._poll_coding_sessions(channel, 7))
    second = asyncio.run(dev_tools._poll_coding_sessions(channel, 7))

    assert first == 0
    assert second == 1
    assert len(channel.messages) == 1


def test_cursor_targets_read_output_by_terminal_id(monkeypatch):
    process = _process(cwd="/work/project")

    async def no_iterm():
        return []

    async def no_tmux():
        return []

    async def cursor_instances():
        return [{
            "base_url": "http://127.0.0.1:8034",
            "terminals": [
                {"id": 1, "name": "codex", "pid": 111},
                {"id": 2, "name": "codex", "pid": 456},
            ],
        }]

    monkeypatch.setattr("actions.terminal.get_iterm_sessions", no_iterm)
    monkeypatch.setattr(dev_tools, "_get_tmux_panes", no_tmux)
    monkeypatch.setattr("actions.terminal.bridge_list_instances", cursor_instances)

    targets = asyncio.run(dev_tools._resolve_control_targets([process]))

    assert targets[dev_tools._session_key(process)]["read_target"] == "2"


def test_lsof_uses_daemon_safe_executable_path(monkeypatch):
    captured = {}

    class Process:
        returncode = 0

        async def communicate(self):
            return b"p123\nfcwd\nn/work/project\n", b""

    async def create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess_exec)

    cwd = asyncio.run(dev_tools._resolve_cwd(123))

    assert captured["args"][0] == dev_tools._LSOF_PATH
    assert cwd == "/work/project"
