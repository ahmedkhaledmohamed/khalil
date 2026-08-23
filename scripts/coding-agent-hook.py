#!/usr/bin/env python3
"""Fail-open relay from Codex or Claude hooks to Khalil on localhost."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import uuid


KEYCHAIN_SERVICE = "khalil-assistant"
KEYCHAIN_ACCOUNT = "webhook-secret-coding-agent"
DEFAULT_URL = "http://127.0.0.1:8033/webhook/coding-agent"
MAX_INPUT_BYTES = 64 * 1024


def _read_input() -> dict:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        return {}
    try:
        value = json.loads(raw or b"{}")
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _first(payload: dict, *names: str):
    for name in names:
        value = payload.get(name)
        if value not in (None, ""):
            return value
    return None


def _tool_name(payload: dict) -> str:
    value = _first(payload, "tool_name", "toolName", "tool")
    if isinstance(value, dict):
        value = _first(value, "name", "type")
    return str(value or "")


def _tool_input(payload: dict) -> dict:
    value = _first(payload, "tool_input", "toolInput", "input")
    return value if isinstance(value, dict) else {}


def _format_questions(tool_input: dict) -> str:
    questions = tool_input.get("questions")
    if not isinstance(questions, list):
        return ""
    rendered = []
    for item in questions[:3]:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or item.get("prompt") or "").strip()
        options = item.get("options")
        labels = []
        if isinstance(options, list):
            for option in options[:5]:
                if isinstance(option, dict):
                    label = option.get("label") or option.get("value")
                else:
                    label = option
                if label:
                    labels.append(str(label))
        if question:
            rendered.append(question + (f"\nOptions: {', '.join(labels)}" if labels else ""))
    return "\n\n".join(rendered)


def _format_tool_request(payload: dict, *, permission: bool) -> str:
    tool_name = _tool_name(payload) or "tool"
    tool_input = _tool_input(payload)
    questions = _format_questions(tool_input)
    if questions:
        return questions
    summary = json.dumps(tool_input, ensure_ascii=False, separators=(",", ":"))
    if summary and summary != "{}":
        action = "requested permission" if permission else "needs your input"
        return f"{tool_name} {action}:\n{summary[:1800]}"
    action = "requested permission" if permission else "needs your input"
    return f"{tool_name} {action}."


def _normalise_event(agent: str, raw_event: str, payload: dict) -> tuple[str, str, bool] | None:
    event = raw_event.lower().replace("-", "_")
    if event in {"stop", "session_end", "sessionend", "after_agent_response", "afteragentresponse", "completed"}:
        return "completed", "", False
    if event in {"permission_request", "permissionrequest"}:
        return "needs_input", _format_tool_request(payload, permission=True), True
    if event in {"pre_tool_use", "pretooluse"}:
        compact_name = "".join(character for character in _tool_name(payload).lower() if character.isalnum())
        if not compact_name.endswith(("askuserquestion", "requestuserinput")):
            return None
        return "needs_input", _format_tool_request(payload, permission=False), False
    if event == "notification":
        notification_type = str(_first(
            payload, "notification_type", "notificationType", "type",
        ) or "").lower()
        if notification_type in {"agent_completed", "completed"}:
            return "completed", "", False
        if notification_type not in {
            "agent_needs_input", "permission_prompt", "elicitation_dialog", "idle_prompt",
        }:
            return None
        prompt = str(_first(payload, "message", "prompt", "reason") or "").strip()
        return "needs_input", prompt or f"{agent} needs your input.", notification_type == "permission_prompt"
    if event == "needs_input":
        prompt = str(_first(payload, "message", "prompt", "reason") or "").strip()
        return "needs_input", prompt, bool(payload.get("approval"))
    return None


def _process_row(pid: int) -> tuple[int | None, str, str]:
    try:
        result = subprocess.run(
            ["/bin/ps", "-o", "ppid=", "-o", "tty=", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=1, check=False,
        )
        parts = result.stdout.strip().split(None, 2)
        if len(parts) != 3:
            return None, "", ""
        return int(parts[0]), parts[1], parts[2]
    except (OSError, subprocess.SubprocessError, ValueError):
        return None, "", ""


def _agent_process(agent: str) -> tuple[int | None, str]:
    pid = os.getppid()
    expected = "codex" if agent == "codex" else "claude"
    fallback_tty = ""
    for _ in range(16):
        parent, tty, command = _process_row(pid)
        if tty and tty != "??" and not fallback_tty:
            fallback_tty = tty
        executable = Path(command.split(None, 1)[0]).name.lower() if command else ""
        if executable == expected:
            return pid, tty if tty != "??" else fallback_tty
        if parent is None or parent <= 1 or parent == pid:
            break
        pid = parent
    return None, fallback_tty


def _secret() -> str | None:
    configured = os.environ.get("KHALIL_AGENT_HOOK_SECRET")
    if configured:
        return configured
    try:
        result = subprocess.run(
            [
                "/usr/bin/security", "find-generic-password",
                "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w",
            ],
            capture_output=True, text=True, timeout=2, check=False,
        )
        return result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _event_id(payload: dict) -> str:
    native_id = _first(
        payload, "event_id", "eventId", "hook_event_id", "tool_use_id", "toolUseId",
    )
    return str(native_id or uuid.uuid4())


def _relay(agent: str, raw_event: str, payload: dict) -> None:
    normalised = _normalise_event(agent, raw_event, payload)
    secret = _secret()
    if not normalised or not secret:
        return
    event, prompt, approval = normalised
    pid, tty = _agent_process(agent)
    session_id = _first(
        payload, "session_id", "sessionId", "conversation_id", "conversationId", "thread_id",
    )
    body = json.dumps({
        "schema_version": 1,
        "event_id": _event_id(payload),
        "agent": agent,
        "event": event,
        "session_id": str(session_id or ""),
        "pid": pid,
        "tty": tty,
        "cwd": str(_first(payload, "cwd", "working_directory", "workingDirectory") or os.getcwd()),
        "prompt": prompt,
        "approval": approval,
    }, ensure_ascii=False, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(
        secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256,
    ).hexdigest()
    request = urllib.request.Request(
        DEFAULT_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Khalil-Timestamp": timestamp,
            "X-Khalil-Signature": f"sha256={signature}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            response.read(1024)
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", choices=("codex", "claude"), required=True)
    parser.add_argument("--event", required=True)
    args = parser.parse_args()
    try:
        _relay(args.agent, args.event, _read_input())
    finally:
        # Hook failures must never block the coding agent.
        print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
