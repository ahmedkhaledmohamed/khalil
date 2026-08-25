from __future__ import annotations

"""Dev tools status — check coding-agent sessions and terminal activity.

Answers questions like "is Codex working?" by inspecting running processes.
"""

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from config import DB_PATH

log = logging.getLogger("khalil.actions.dev_tools")

_SESSION_STATE_KEY = "coding_session_bridge"
_PROMPT_STABILITY_POLLS = 2
_NATIVE_APPROVAL_CONFIRMATION_SECONDS = 3.0
_NATIVE_EVENT_TTL_SECONDS = 2 * 60 * 60
_BRIDGE_STATE_LOCK = asyncio.Lock()
_CONFIRMATION_TASKS: set[asyncio.Task] = set()
_LSOF_PATH = "/usr/sbin/lsof" if Path("/usr/sbin/lsof").exists() else "lsof"
_ANSI_ESCAPE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_CODEX_TUI_QUESTION = re.compile(
    r"(?:^|\n)\s*[•●]\s+((?:what|which|how|where|when|why|would|should|do you|"
    r"are you|can you|could you)[^\n?]{3,240}\?)",
    re.I,
)
_APPROVAL_PATTERNS = (
    re.compile(r"\bdo you want to (?:proceed|continue|allow|run|execute)\b", re.I),
    re.compile(r"\b(?:allow|approve|permit|authorize)\b.{0,100}\?", re.I | re.S),
    re.compile(r"\bpermission (?:required|request(?:ed)?)\b", re.I),
    re.compile(r"\byes,? allow\b", re.I),
    re.compile(r"\bpress (?:enter|return) to (?:confirm|approve|continue)\b", re.I),
)
_INPUT_PATTERNS = (
    re.compile(r"\b(?:needs?|requires?) (?:your )?input\b", re.I),
    re.compile(r"\b(?:please )?(?:choose|select) (?:an? )?(?:option|answer)\b", re.I),
    re.compile(
        r"(?:^|\n)\s*(?:what|which|how|where|when|why|would|should|do you|are you|"
        r"can you|could you)[^\n?]{3,240}\?\s*(?:[>❯›]\s*)?$",
        re.I,
    ),
)

SKILL = {
    "name": "dev_tools",
    "description": "Check developer tool status — Codex, Claude Code, git, terminals",
    "category": "development",
    "risk": "read",
    "patterns": [
        (r"\bcodex\s+(?:status|session|instance|process)", "claude_code_status"),
        (r"\b(?:is|any)\s+codex\s+(?:waiting|running|active|idle|working)\b", "claude_code_status"),
        (r"\bclaude\s*code\b", "claude_code_status"),
        (r"\bclaude\s+(?:session|instance|process)", "claude_code_status"),
        (r"\b(?:is|any)\s+claude\s+(?:waiting|running|active|idle)\b", "claude_code_status"),
        (r"\bcoding?\s+(?:session|agent)s?\s+(?:status|running|waiting|active)\b", "claude_code_status"),
    ],
    "actions": [
        {
            "type": "claude_code_status",
            "handler": "handle_intent",
            "keywords": "codex claude code coding agent session waiting running active idle terminal",
            "description": "Check coding-agent session status",
        },
    ],
    "examples": [
        "Is Codex working?",
        "Any active coding agent sessions?",
        "Is Claude Code waiting on me?",
    ],
}


async def _get_coding_agent_processes() -> list[dict]:
    """Get running Codex and Claude Code CLI processes with their state."""
    proc = await asyncio.create_subprocess_exec(
        "ps", "aux",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()

    processes = []
    for line in stdout.decode().splitlines():
        line_lower = line.lower()
        if "claude" not in line_lower and "codex" not in line_lower:
            continue
        parts = line.split(None, 10)
        if len(parts) < 11:
            continue

        cpu = float(parts[2])
        stat = parts[7]  # e.g. S+, R+, S
        tty = parts[6]   # e.g. s057, s131, ??
        started = parts[8]
        command = parts[10]
        command_lower = command.lower()
        # Only interactive CLIs are controllable. Desktop helpers, app servers,
        # sandboxes and code-mode hosts may share a TTY but do not accept input.
        if tty == "??" or any(skip in command_lower for skip in (
            "claude.app", "claude helper", "codex.app", "crashpad", "shipit",
            "grep", "ps aux", "app-server", "code-mode-host", "codex sandbox",
            "features.code_mode_host",
        )):
            continue
        executable = command.split(None, 1)[0].rsplit("/", 1)[-1].lower()
        if executable not in ("codex", "claude"):
            continue
        agent = "Codex" if "codex" in command.lower() else "Claude Code"

        # CPU can distinguish activity from idleness, but cannot prove that an
        # agent needs input. The reconciler below makes that determination from
        # a stable explicit terminal prompt.
        if cpu > 5.0:
            status = "actively working"
        elif "S+" in stat:
            status = "idle (foreground)"
        else:
            status = "background"

        processes.append({
            "pid": int(parts[1]),
            "tty": tty,
            "cpu": cpu,
            "stat": stat,
            "started": started,
            "status": status,
            "agent": agent,
            "command": command[:60],
        })

    # Resolve process metadata concurrently; lsof and ps can each take seconds
    # when many sessions are open.
    metadata = await asyncio.gather(*(
        asyncio.gather(_resolve_cwd(p["pid"]), _resolve_parent_pid(p["pid"]))
        for p in processes
    ))
    for process, (cwd, ppid) in zip(processes, metadata):
        process["cwd"] = cwd
        process["ppid"] = ppid

    bridge_sessions = _load_bridge_state().get("sessions", {})
    for process in processes:
        if bridge_sessions.get(_session_key(process), {}).get("status") == "needs_input":
            process["status"] = "waiting for input"

    return processes


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalise_tty(tty: str) -> str:
    if tty.startswith("/dev/"):
        return tty
    if tty.startswith("s") and tty[1:].isdigit():
        return f"/dev/tty{tty}"
    return tty


def _session_key(process: dict) -> str:
    identity = ":".join((
        process["agent"], str(process["pid"]), process.get("started", ""),
        _normalise_tty(process.get("tty", "")),
    ))
    return hashlib.sha256(identity.encode()).hexdigest()[:16]


def _load_bridge_state() -> dict:
    empty = {"sessions": {}, "message_index": {}, "event_index": {}}
    try:
        conn = sqlite3.connect(str(DB_PATH))
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (_SESSION_STATE_KEY,)).fetchone()
        conn.close()
        if not row:
            return empty
        state = json.loads(row[0])
        if not isinstance(state.get("sessions"), dict) or not isinstance(state.get("message_index"), dict):
            return empty
        if not isinstance(state.get("event_index"), dict):
            state["event_index"] = {}
        return state
    except Exception as exc:
        log.debug("Failed to load coding-session bridge state: %s", exc)
        return empty


def _save_bridge_state(state: dict) -> None:
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (_SESSION_STATE_KEY, json.dumps(state)),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        log.warning("Failed to save coding-session bridge state: %s", exc)


async def _get_tmux_panes() -> list[dict]:
    """Return addressable tmux panes without depending on formatted user output."""
    from actions.tmux_control import _run_tmux

    output, rc = await _run_tmux(
        "list-panes", "-a", "-F",
        "#{session_name}|#{window_index}.#{pane_index}|#{pane_tty}|#{pane_pid}",
    )
    if rc != 0:
        return []
    panes = []
    for line in output.splitlines():
        parts = line.split("|", 3)
        if len(parts) != 4:
            continue
        session, pane, tty, pid = parts
        panes.append({
            "session": session,
            "target": f"{session}:{pane}",
            "tty": tty,
            "pid": int(pid) if pid.isdigit() else None,
        })
    return panes


async def _resolve_parent_pid(pid: int) -> int | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ps", "-o", "ppid=", "-p", str(pid),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        value = stdout.decode().strip()
        return int(value) if value.isdigit() else None
    except Exception:
        return None


async def _resolve_control_targets(processes: list[dict]) -> dict[str, dict]:
    """Map process keys to exact iTerm or tmux input targets."""
    from actions.terminal import bridge_list_instances, get_iterm_sessions

    iterm_ttys = {session["tty"] for session in await get_iterm_sessions() if session.get("tty")}
    tmux_by_tty = {pane["tty"]: pane for pane in await _get_tmux_panes() if pane.get("tty")}
    cursor_terminals = []
    for instance in await bridge_list_instances():
        for terminal in instance["terminals"]:
            cursor_terminals.append({**terminal, "bridge_url": instance["base_url"]})
    cursor_by_pid = {terminal.get("pid"): terminal for terminal in cursor_terminals if terminal.get("pid")}
    targets = {}
    for process in processes:
        key = _session_key(process)
        tty = _normalise_tty(process["tty"])
        if tty in tmux_by_tty:
            pane = tmux_by_tty[tty]
            targets[key] = {
                "kind": "tmux", "target": pane["target"],
                "identity": f"tmux:{pane['target']}", "tty": tty,
            }
        elif tty in iterm_ttys:
            targets[key] = {
                "kind": "iterm", "target": tty, "identity": f"iterm:{tty}", "tty": tty,
            }
        elif process.get("ppid") in cursor_by_pid:
            terminal = cursor_by_pid[process["ppid"]]
            name = terminal.get("name", "terminal")
            targets[key] = {
                "kind": "cursor",
                "target": str(terminal["id"]),
                "identity": f"cursor:{terminal['pid']}",
                "name": name,
                "bridge_url": terminal["bridge_url"],
                "read_target": str(terminal["id"]),
                "tty": tty,
            }
    return targets


async def _read_target(target: dict, lines: int = 50) -> str | None:
    if target["kind"] == "iterm":
        from actions.terminal import read_iterm_session
        result = await read_iterm_session(target["target"], lines=lines)
        return result.get("content") if result.get("success") else None

    if target["kind"] == "tmux":
        from actions.tmux_control import _run_tmux
        output, rc = await _run_tmux(
            "capture-pane", "-t", target["target"], "-p", "-S", f"-{lines}",
        )
        return output if rc == 0 else None

    read_target = target.get("read_target")
    if not read_target:
        return None
    from actions.terminal import bridge_get_output
    result = await bridge_get_output(read_target, lines=lines, base_url=target["bridge_url"])
    if result.get("error") or result.get("note"):
        return None
    output = result.get("output")
    return "\n".join(output) if isinstance(output, list) else output


def _extract_input_prompt(output: str | None) -> tuple[str | None, bool]:
    """Extract an explicit input request from the terminal tail.

    A sleeping foreground process alone is not evidence of an input request.
    The terminal must contain an explicit question, choice, or approval prompt.
    """
    if not output:
        return None, False
    cleaned = _ANSI_ESCAPE.sub("", output).replace("\r", "")
    lines = [line.rstrip() for line in cleaned.splitlines()]
    tail = "\n".join(lines[-18:]).strip()
    if not tail:
        return None, False
    if "Ask Codex to do anything" in tail:
        questions = _CODEX_TUI_QUESTION.findall(tail)
        if questions:
            return questions[-1].strip(), False
    # Approval UI is actionable only while it remains at the bottom of the
    # terminal. Searching the full scrollback re-detects prompts that an
    # automatic reviewer already resolved.
    actionable_tail = "\n".join(lines[-8:]).strip()
    approval = any(pattern.search(actionable_tail) for pattern in _APPROVAL_PATTERNS)
    if not approval and not any(pattern.search(tail) for pattern in _INPUT_PATTERNS):
        return None, False
    return tail[-1800:], approval


def _redact_prompt(prompt: str) -> str:
    """Redact Khalil's known sensitive patterns before external notification."""
    from config import SENSITIVE_PATTERNS

    redacted = prompt
    for pattern in SENSITIVE_PATTERNS:
        redacted = re.sub(pattern, "[REDACTED]", redacted, flags=re.IGNORECASE)
    return redacted


def _format_session_notification(session: dict) -> str:
    project = session.get("cwd") or "Unknown project"
    interaction_id = session["interaction_id"]
    kind = "approval" if session.get("approval") else "input"
    controllable = session.get("target_kind") != "unavailable"
    if not controllable:
        instructions = "This terminal is not remotely controllable; answer in the original session."
    elif session.get("approval"):
        instructions = f"Reply with `approve <exact choice>` or `deny <exact choice>` (ID {interaction_id})."
    else:
        instructions = f"Reply directly to this message with your answer (ID {interaction_id})."
    return (
        f"Coding session needs {kind}\n\n"
        f"Agent: {session['agent']}\n"
        f"Project: {project}\n"
        f"Terminal: {session['target_kind']} {session.get('target_name') or session['target']}\n\n"
        f"{session['prompt']}\n\n{instructions}"
    )


def _native_event_age_seconds(session: dict) -> float:
    try:
        updated = datetime.fromisoformat(session["native_event_at"])
        age = datetime.now(timezone.utc) - updated.astimezone(timezone.utc)
        return age.total_seconds()
    except (KeyError, TypeError, ValueError):
        return float("inf")


def _native_event_is_pending(session: dict) -> bool:
    return bool(
        session.get("source") == "native_hook"
        and session.get("status") == "needs_input"
        and _native_event_age_seconds(session) < _NATIVE_EVENT_TTL_SECONDS
    )


def _same_pending_native_session(
    session: dict, agent: str, external_session_id: str | None,
) -> bool:
    return bool(
        external_session_id
        and session.get("source") == "native_hook"
        and session.get("status") in {"pending_confirmation", "needs_input"}
        and session.get("agent") == agent
        and session.get("external_session_id") == external_session_id
    )


async def _poll_coding_sessions(channel, chat_id: int | str) -> int:
    """Reconcile controllable coding sessions and send idempotent input alerts."""
    processes = await _get_coding_agent_processes()
    targets = await _resolve_control_targets(processes)
    state = _load_bridge_state()
    old_sessions = state["sessions"]
    active_sessions = {}
    notifications = 0

    for process in processes:
        key = _session_key(process)
        target = targets.get(key)
        previous = old_sessions.get(key, {})
        # Only an explicit prompt in a foreground interactive CLI is eligible.
        if not target or "+" not in process.get("stat", ""):
            if (
                previous.get("status") == "pending_confirmation"
                and _native_event_age_seconds(previous) < _NATIVE_EVENT_TTL_SECONDS
            ):
                active_sessions[key] = {
                    **previous,
                    "cwd": process.get("cwd") or previous.get("cwd"),
                    "updated_at": _utc_now(),
                }
            continue

        if (
            previous.get("status") == "pending_confirmation"
            and _native_event_age_seconds(previous) < 10
        ):
            active_sessions[key] = {
                **previous,
                "cwd": process.get("cwd") or previous.get("cwd"),
                "target_kind": target["kind"],
                "target": target["target"],
                "target_identity": target["identity"],
                "target_name": target.get("name"),
                "bridge_url": target.get("bridge_url"),
                "updated_at": _utc_now(),
            }
            continue

        output = await _read_target(target)
        if output is None and previous.get("status") == "pending_confirmation":
            active_sessions[key] = {
                **previous,
                "cwd": process.get("cwd") or previous.get("cwd"),
                "target_kind": target["kind"],
                "target": target["target"],
                "target_identity": target["identity"],
                "target_name": target.get("name"),
                "bridge_url": target.get("bridge_url"),
                "updated_at": _utc_now(),
            }
            continue
        prompt, approval = _extract_input_prompt(output)
        native_pending = _native_event_is_pending(previous)
        prompt_hash = hashlib.sha256((prompt or "").encode()).hexdigest()[:16] if prompt else None
        if native_pending:
            prompt_hash = previous.get("candidate_hash")
            approval = previous.get("approval", False)
            prompt = prompt or previous.get("prompt")
        same_candidate = prompt_hash and prompt_hash == previous.get("candidate_hash")
        candidate_polls = previous.get("candidate_polls", 0) + 1 if same_candidate else (1 if prompt else 0)
        answered_same_prompt = (
            previous.get("status") == "responded"
            and previous.get("candidate_hash") == prompt_hash
        )
        if native_pending:
            status = "needs_input"
        elif previous.get("status") == "pending_confirmation" and not prompt:
            status = "auto_resolved"
        elif answered_same_prompt:
            status = "responded"
        else:
            status = "needs_input" if prompt and candidate_polls >= _PROMPT_STABILITY_POLLS else "running"
        session = {
            "key": key,
            "interaction_id": key[:8],
            "agent": process["agent"],
            "pid": process["pid"],
            "started": process.get("started"),
            "tty": _normalise_tty(process["tty"]),
            "cwd": process.get("cwd") or previous.get("cwd"),
            "target_kind": target["kind"],
            "target": target["target"],
            "target_identity": target["identity"],
            "target_name": target.get("name"),
            "bridge_url": target.get("bridge_url"),
            "status": status,
            "approval": approval,
            "candidate_hash": prompt_hash,
            "candidate_polls": candidate_polls,
            "notification_message_id": previous.get("notification_message_id"),
            "source": previous.get("source") if native_pending else "terminal_poll",
            "external_session_id": previous.get("external_session_id"),
            "hook_event": previous.get("hook_event") if native_pending else None,
            "notified_at": previous.get("notified_at"),
            "native_event_at": previous.get("native_event_at"),
            "updated_at": _utc_now(),
        }

        already_notified = (
            previous.get("status") == "needs_input"
            and previous.get("candidate_hash") == prompt_hash
            and previous.get("notification_message_id") is not None
        )
        if status == "needs_input" and not already_notified:
            sent = await channel.send_message(
                chat_id, _format_session_notification({**session, "prompt": _redact_prompt(prompt)}),
            )
            session["notification_message_id"] = str(sent.message_id)
            state["message_index"][str(sent.message_id)] = key
            notifications += 1
        active_sessions[key] = session

    active_keys = set(active_sessions)
    state["message_index"] = {
        message_id: key for message_id, key in state["message_index"].items()
        if key in active_keys and active_sessions[key].get("status") == "needs_input"
    }
    state["sessions"] = active_sessions
    _save_bridge_state(state)
    return notifications


def _normalise_event_agent(value: object) -> str | None:
    agent = str(value or "").strip().lower()
    if agent == "codex":
        return "Codex"
    if agent in {"claude", "claude-code", "claude_code", "claude code"}:
        return "Claude Code"
    return None


def _event_process(payload: dict, processes: list[dict], agent: str) -> dict | None:
    candidates = [process for process in processes if process["agent"] == agent]
    try:
        pid = int(payload.get("pid"))
    except (TypeError, ValueError):
        pid = None
    if pid is not None:
        exact = [process for process in candidates if process["pid"] == pid]
        if len(exact) == 1:
            return exact[0]

    tty = _normalise_tty(str(payload.get("tty") or ""))
    if tty:
        exact = [
            process for process in candidates
            if _normalise_tty(process.get("tty", "")) == tty
        ]
        if len(exact) == 1:
            return exact[0]

    cwd = str(payload.get("cwd") or "").strip()
    if cwd:
        exact = [process for process in candidates if process.get("cwd") == cwd]
        if len(exact) == 1:
            return exact[0]
    return None


def _event_session_key(payload: dict, agent: str, process: dict | None) -> str:
    if process:
        return _session_key(process)
    identity = ":".join((
        agent,
        str(payload.get("session_id") or ""),
        str(payload.get("pid") or ""),
        _normalise_tty(str(payload.get("tty") or "")),
        str(payload.get("cwd") or ""),
    ))
    return hashlib.sha256(identity.encode()).hexdigest()[:16]


def _remember_event(state: dict, event_id: str) -> None:
    state["event_index"][event_id] = _utc_now()
    if len(state["event_index"]) > 500:
        newest = sorted(
            state["event_index"].items(), key=lambda item: item[1], reverse=True,
        )[:500]
        state["event_index"] = dict(newest)


async def _confirm_pending_approval(
    key: str,
    native_candidate_hash: str,
    channel,
    chat_id: int | str,
) -> dict:
    """Notify only when a native approval remains visible in the exact terminal."""
    state = _load_bridge_state()
    session = state["sessions"].get(key)
    if (
        not session
        or session.get("status") != "pending_confirmation"
        or session.get("native_candidate_hash") != native_candidate_hash
    ):
        return {"confirmed": False, "reason": "no_longer_pending"}

    processes = await _get_coding_agent_processes()
    live = next((process for process in processes if _session_key(process) == key), None)
    if not live or "+" not in live.get("stat", ""):
        session["status"] = "auto_resolved" if live else "stale"
        session["resolution"] = "agent_not_waiting" if live else "session_ended"
        session["updated_at"] = _utc_now()
        _save_bridge_state(state)
        return {"confirmed": False, "reason": session["resolution"]}

    targets = await _resolve_control_targets([live])
    target = targets.get(key)
    if not target:
        session["confirmation_error"] = "control_target_unavailable"
        session["updated_at"] = _utc_now()
        _save_bridge_state(state)
        return {"confirmed": False, "pending": True, "reason": "target_unavailable"}

    output = await _read_target(target)
    if output is None:
        session["confirmation_error"] = "terminal_output_unavailable"
        session["updated_at"] = _utc_now()
        _save_bridge_state(state)
        return {"confirmed": False, "pending": True, "reason": "output_unavailable"}

    prompt, approval = _extract_input_prompt(output)
    if not prompt or not approval:
        session["status"] = "auto_resolved"
        session["resolution"] = "approval_prompt_cleared"
        session["updated_at"] = _utc_now()
        _save_bridge_state(state)
        return {"confirmed": False, "reason": "approval_prompt_cleared"}

    confirmation_hash = hashlib.sha256(prompt.encode()).hexdigest()[:16]
    same_confirmation = confirmation_hash == session.get("confirmation_hash")
    confirmation_polls = session.get("confirmation_polls", 0) + 1 if same_confirmation else 1
    session.update({
        "cwd": live.get("cwd") or session.get("cwd"),
        "target_kind": target["kind"],
        "target": target["target"],
        "target_identity": target["identity"],
        "target_name": target.get("name"),
        "bridge_url": target.get("bridge_url"),
        "candidate_hash": confirmation_hash,
        "confirmation_hash": confirmation_hash,
        "confirmation_polls": confirmation_polls,
        "confirmation_error": None,
        "prompt": _redact_prompt(prompt),
        "updated_at": _utc_now(),
    })
    if confirmation_polls < _PROMPT_STABILITY_POLLS:
        _save_bridge_state(state)
        return {"confirmed": False, "pending": True, "reason": "awaiting_stability"}

    sent = await channel.send_message(
        chat_id,
        _format_session_notification({**session, "prompt": _redact_prompt(prompt)}),
    )
    session["status"] = "needs_input"
    session["notification_message_id"] = str(sent.message_id)
    session["notified_at"] = _utc_now()
    state["message_index"] = {
        message_id: indexed_key
        for message_id, indexed_key in state["message_index"].items()
        if indexed_key != key
    }
    state["message_index"][str(sent.message_id)] = key
    _save_bridge_state(state)
    return {"confirmed": True, "notified": True}


async def _confirm_pending_approval_after_delay(
    key: str,
    native_candidate_hash: str,
    channel,
    chat_id: int | str,
) -> None:
    """Run two bounded probes after the hook returns so auto-review can finish."""
    for delay in (_NATIVE_APPROVAL_CONFIRMATION_SECONDS, 2.0):
        await asyncio.sleep(delay)
        async with _BRIDGE_STATE_LOCK:
            result = await _confirm_pending_approval(
                key, native_candidate_hash, channel, chat_id,
            )
        if not result.get("pending"):
            return


def _schedule_pending_approval_confirmation(
    key: str,
    native_candidate_hash: str,
    channel,
    chat_id: int | str,
) -> None:
    task = asyncio.create_task(_confirm_pending_approval_after_delay(
        key, native_candidate_hash, channel, chat_id,
    ))
    _CONFIRMATION_TASKS.add(task)
    task.add_done_callback(_pending_confirmation_done)


def _pending_confirmation_done(task: asyncio.Task) -> None:
    _CONFIRMATION_TASKS.discard(task)
    if task.cancelled():
        return
    try:
        error = task.exception()
    except asyncio.CancelledError:
        return
    if error:
        log.warning("Coding-session approval confirmation failed: %s", error)


async def _record_coding_agent_event(payload: dict, channel, chat_id: int | str) -> dict:
    """Apply one authenticated native hook event to the session bridge."""
    if payload.get("schema_version") != 1:
        return {"accepted": False, "reason": "unsupported_schema"}
    agent = _normalise_event_agent(payload.get("agent"))
    event = str(payload.get("event") or "")
    if not agent or event not in {"needs_input", "completed"}:
        return {"accepted": False, "reason": "unsupported_event"}

    event_id = str(payload.get("event_id") or "")[:200]
    if not event_id:
        return {"accepted": False, "reason": "missing_event_id"}
    state = _load_bridge_state()
    if event_id in state["event_index"]:
        return {"accepted": True, "duplicate": True}

    external_session_id = str(payload.get("session_id") or "")[:300] or None
    hook_event = str(payload.get("hook_event") or "").strip().lower()[:100] or None
    existing_key = next((
        key for key, session in state["sessions"].items()
        if external_session_id
        and session.get("agent") == agent
        and session.get("external_session_id") == external_session_id
    ), None)
    processes = await _get_coding_agent_processes()
    process = _event_process(payload, processes, agent)
    if existing_key:
        key = existing_key
    else:
        key = _event_session_key(payload, agent, process)
    previous = state["sessions"].get(key, {})

    if event == "completed":
        _remember_event(state, event_id)
        if previous:
            state["message_index"] = {
                message_id: indexed_key
                for message_id, indexed_key in state["message_index"].items()
                if indexed_key != key
            }
            previous["status"] = "responded"
            previous["notification_message_id"] = None
            previous["completed_at"] = _utc_now()
            previous["updated_at"] = _utc_now()
            state["sessions"][key] = previous
        _save_bridge_state(state)
        return {"accepted": True, "cleared": bool(previous)}

    prompt = str(payload.get("prompt") or "").strip()[:8000]
    if not prompt:
        return {"accepted": False, "reason": "missing_prompt"}
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:16]
    if (
        previous.get("status") in {"pending_confirmation", "needs_input"}
        and (
            previous.get("native_candidate_hash") == prompt_hash
            or previous.get("candidate_hash") == prompt_hash
            or (
                hook_event == "notification"
                and _same_pending_native_session(previous, agent, external_session_id)
            )
        )
        and (
            previous.get("status") == "pending_confirmation"
            or previous.get("notified_at")
        )
    ):
        current_message_id = str(previous.get("notification_message_id") or "")
        state["message_index"] = {
            message_id: indexed_key
            for message_id, indexed_key in state["message_index"].items()
            if indexed_key != key or message_id == current_message_id
        }
        if hook_event == "notification":
            previous["native_event_at"] = _utc_now()
            previous["updated_at"] = _utc_now()
            state["sessions"][key] = previous
        _remember_event(state, event_id)
        _save_bridge_state(state)
        return {"accepted": True, "duplicate": True}

    # A background process may still share a TTY with an unrelated foreground
    # command. Notify for it, but expose reply routing only while the coding
    # agent itself owns the foreground terminal.
    controllable_process = process if process and "+" in process.get("stat", "") else None
    targets = await _resolve_control_targets([controllable_process] if controllable_process else [])
    target = targets.get(_session_key(process)) if process else None
    approval = bool(payload.get("approval"))
    session = {
        "key": key,
        "interaction_id": key[:8],
        "agent": agent,
        "pid": process["pid"] if process else payload.get("pid"),
        "started": process.get("started") if process else None,
        "tty": _normalise_tty(process["tty"] if process else str(payload.get("tty") or "")),
        "cwd": (
            (process.get("cwd") if process else None)
            or str(payload.get("cwd") or "").strip()
            or previous.get("cwd")
        ),
        "target_kind": target["kind"] if target else "unavailable",
        "target": target["target"] if target else "original terminal",
        "target_identity": target["identity"] if target else None,
        "target_name": target.get("name") if target else None,
        "bridge_url": target.get("bridge_url") if target else None,
        "status": "pending_confirmation" if approval else "needs_input",
        "approval": approval,
        "prompt": _redact_prompt(prompt),
        "candidate_hash": prompt_hash,
        "native_candidate_hash": prompt_hash,
        "candidate_polls": _PROMPT_STABILITY_POLLS,
        "notification_message_id": None,
        "external_session_id": external_session_id,
        "hook_event": hook_event,
        "source": "native_hook",
        "notified_at": None,
        "native_event_at": _utc_now(),
        "updated_at": _utc_now(),
    }
    state["sessions"][key] = session
    _remember_event(state, event_id)
    if approval:
        _save_bridge_state(state)
        return {
            "accepted": True,
            "pending_confirmation": True,
            "key": key,
            "native_candidate_hash": prompt_hash,
        }

    sent = await channel.send_message(
        chat_id, _format_session_notification({**session, "prompt": _redact_prompt(prompt)}),
    )
    session["notification_message_id"] = str(sent.message_id)
    session["notified_at"] = _utc_now()
    state["message_index"] = {
        message_id: indexed_key
        for message_id, indexed_key in state["message_index"].items()
        if indexed_key != key
    }
    if target:
        state["message_index"][str(sent.message_id)] = key
    _save_bridge_state(state)
    return {"accepted": True, "notified": True, "controllable": bool(target)}


async def record_coding_agent_event(payload: dict, channel, chat_id: int | str) -> dict:
    """Serialize native hook delivery with polling and Telegram replies."""
    async with _BRIDGE_STATE_LOCK:
        result = await _record_coding_agent_event(payload, channel, chat_id)
    if result.get("pending_confirmation"):
        _schedule_pending_approval_confirmation(
            result["key"], result["native_candidate_hash"], channel, chat_id,
        )
    return result


async def poll_coding_sessions(channel, chat_id: int | str) -> int:
    """Serialize reconciliation with Telegram reply handling."""
    async with _BRIDGE_STATE_LOCK:
        return await _poll_coding_sessions(channel, chat_id)


async def _send_to_target(session: dict, text: str) -> tuple[bool, str | None]:
    # The agent could terminate in the narrow interval after live-process
    # validation, leaving a shell at the same TTY. Never relay a response that
    # would be classified as a blocked shell command in that race.
    from actions.shell import classify_command
    from config import ActionType
    if len(text) > 2000:
        return False, "Response is too long (maximum 2,000 characters)"
    if classify_command(text) == ActionType.DANGEROUS:
        return False, "Response resembles a blocked shell command"

    if session["target_kind"] == "iterm":
        from actions.terminal import send_input_to_iterm
        result = await send_input_to_iterm(text, session["target"])
        return result.get("success", False), result.get("error")

    if session["target_kind"] == "cursor":
        from actions.terminal import bridge_send_command
        result = await bridge_send_command(
            session["target"], text, show=False, base_url=session["bridge_url"],
        )
        return not result.get("error") and bool(result.get("sent")), result.get("error")

    from actions.tmux_control import send_input
    result = await send_input(session["target"], text)
    return result.get("success", False), result.get("error")


async def _handle_session_reply(ctx, query: str) -> bool:
    """Route a reply-to-message response to its exact pending coding session."""
    incoming = getattr(ctx, "incoming", None)
    reply_to = str(incoming.reply_to_msg_id) if incoming and incoming.reply_to_msg_id is not None else None
    if not reply_to:
        if _looks_like_uncorrelated_approval_reply(query):
            await ctx.reply(
                "That approval isn't linked to a coding-session request. Reply directly to "
                "the Telegram alert with `approve <exact terminal choice>` or "
                "`deny <exact terminal choice>`."
            )
            return True
        return False

    state = _load_bridge_state()
    key = state["message_index"].get(reply_to)
    if not key:
        if _looks_like_uncorrelated_approval_reply(query):
            await ctx.reply(
                "That approval isn't linked to an active coding-session request. Reply to "
                "the latest Telegram alert and include the exact terminal choice."
            )
            return True
        return False
    session = state["sessions"].get(key)
    if not session or session.get("status") != "needs_input":
        await ctx.reply("That coding-session request is no longer active.")
        return True

    response = query.strip()
    if session.get("approval"):
        lowered = response.lower()
        if lowered.startswith("approve ") and response[8:].strip():
            response = response[8:].strip()
        elif lowered.startswith("deny ") and response[5:].strip():
            response = response[5:].strip()
        else:
            await ctx.reply(
                "This is an approval request. Reply with `approve <exact terminal choice>` "
                "or `deny <exact terminal choice>`; I won't infer a choice from ordinary text."
            )
            return True

    # Re-resolve live processes and exact targets to reject PID reuse, closed
    # terminals, and moved sessions before writing anything.
    processes = await _get_coding_agent_processes()
    live = next((process for process in processes if _session_key(process) == key), None)
    targets = await _resolve_control_targets([live] if live else [])
    live_target = targets.get(key)
    if (
        not live or not live_target
        or "+" not in live.get("stat", "")
        or live_target["kind"] != session["target_kind"]
        or live_target["identity"] != session.get("target_identity")
    ):
        state["message_index"].pop(reply_to, None)
        session["status"] = "stale"
        _save_bridge_state(state)
        await ctx.reply("I didn't send that response because the original coding session is no longer controllable.")
        return True

    before = await _read_target(live_target, lines=20)
    delivery_session = {
        **session,
        "target": live_target["target"],
        "bridge_url": live_target.get("bridge_url"),
    }
    success, error = await _send_to_target(delivery_session, response)
    if not success:
        await ctx.reply(f"I couldn't deliver the response to {session['agent']}: {error or 'unknown error'}")
        return True

    await asyncio.sleep(0.5)
    after = await _read_target(live_target, lines=20)
    state["message_index"].pop(reply_to, None)
    session["status"] = "responded"
    session["responded_at"] = _utc_now()
    session["notification_message_id"] = None
    _save_bridge_state(state)
    if before != after:
        await ctx.reply(f"Response delivered to {session['agent']} in {session.get('cwd') or session['target']}.")
    else:
        await ctx.reply(
            f"Response was written to {session['agent']}, but its terminal output has not changed yet."
        )
    return True


def _looks_like_uncorrelated_approval_reply(query: str) -> bool:
    """Recognize control replies that must never fall through to an LLM."""
    lowered = query.strip().lower()
    if lowered in {"approved", "approves", "denied", "denies"}:
        return True
    if lowered.startswith("approve ") and lowered != "approve plan":
        return True
    return lowered.startswith("deny ")


async def handle_session_reply(ctx, query: str) -> bool:
    """Serialize reply delivery with session-state reconciliation."""
    async with _BRIDGE_STATE_LOCK:
        return await _handle_session_reply(ctx, query)


async def _get_claude_processes() -> list[dict]:
    """Compatibility helper for features that specifically target Claude TTYs."""
    return [
        process for process in await _get_coding_agent_processes()
        if process["agent"] == "Claude Code"
    ]


async def _resolve_cwd(pid: int) -> str | None:
    """Resolve the current working directory of a process via lsof."""
    try:
        proc = await asyncio.create_subprocess_exec(
            _LSOF_PATH, "-a", "-d", "cwd", "-p", str(pid), "-Fn",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode != 0:
            return None
        for line in stdout.decode().splitlines():
            if line.startswith("n/"):
                return line[1:]  # Strip the 'n' prefix
        return None
    except Exception:
        return None


def _format_processes(processes: list[dict]) -> str:
    """Format process list for Telegram display."""
    if not processes:
        return "No coding-agent sessions running."

    waiting = [p for p in processes if "waiting" in p["status"]]
    working = [p for p in processes if "working" in p["status"]]

    lines = [f"**Coding Agent Sessions** ({len(processes)} total)\n"]

    if waiting:
        lines.append(f"⏳ **{len(waiting)} waiting for your input:**")
        for p in waiting:
            cwd = f"\n    📂 {p['cwd']}" if p.get("cwd") else ""
            lines.append(f"  • {p['agent']} on {p['tty']} (started {p['started']}){cwd}")

    if working:
        lines.append(f"🔄 **{len(working)} actively working:**")
        for p in working:
            cwd = f"\n    📂 {p['cwd']}" if p.get("cwd") else ""
            lines.append(f"  • {p['agent']} on {p['tty']} — CPU {p['cpu']:.0f}%{cwd}")

    idle = [p for p in processes if p not in waiting and p not in working]
    if idle:
        lines.append(f"💤 **{len(idle)} idle:**")
        for p in idle:
            cwd = f"\n    📂 {p['cwd']}" if p.get("cwd") else ""
            lines.append(f"  • {p['agent']} on {p['tty']} (started {p['started']}){cwd}")

    return "\n".join(lines)


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle dev tools queries."""
    if action == "claude_code_status":
        processes = await _get_coding_agent_processes()
        response = _format_processes(processes)
        await ctx.reply(response)
        return True

    return False
