#!/usr/bin/env python3
"""Setup utilities for Khalil installer.

Invoked by install.sh via the venv Python for operations that need
the keyring library (which requires pip install first).

Usage:
    .venv/bin/python3 scripts/setup_utils.py check_secret <key>
    .venv/bin/python3 scripts/setup_utils.py set_secret <key> <value>
    .venv/bin/python3 scripts/setup_utils.py validate_secret <key> <value>
    .venv/bin/python3 scripts/setup_utils.py check_imports
    .venv/bin/python3 scripts/setup_utils.py db_doc_count
    .venv/bin/python3 scripts/setup_utils.py install_coding_agent_hooks <repo_root> [hooks_path]
"""

import json
import os
from pathlib import Path
import secrets
import shlex
import sys
import tempfile

KEYRING_SERVICE = "khalil-assistant"
CODING_AGENT_SECRET_KEY = "webhook-secret-coding-agent"


def check_secret(key: str) -> bool:
    """Check if a secret exists in the macOS keychain."""
    import keyring
    val = keyring.get_password(KEYRING_SERVICE, key)
    return val is not None and len(val) > 0


def set_secret(key: str, value: str):
    """Store a secret in the macOS keychain."""
    import keyring
    keyring.set_password(KEYRING_SERVICE, key, value)


def validate_secret(key: str, value: str) -> tuple[bool, str]:
    """Basic format validation for known secret types."""
    if not value or not value.strip():
        return False, "empty value"
    value = value.strip()
    if key == "telegram-bot-token":
        if ":" not in value:
            return False, "Telegram tokens contain a colon (e.g., 123456:ABC-DEF...)"
        return True, "ok"
    if key == "anthropic-api-key":
        if not value.startswith("sk-ant-"):
            return False, "Anthropic keys start with sk-ant-"
        return True, "ok"
    if key in ("spotify-client-id", "spotify-client-secret"):
        if len(value) < 10:
            return False, "value too short"
        return True, "ok"
    # Default: accept any non-empty value
    return True, "ok"


def check_imports() -> list[str]:
    """Verify critical Python imports work. Returns list of failures."""
    failures = []
    for mod in [
        "anthropic", "keyring", "httpx", "fastapi",
        "telegram", "sqlite3", "apscheduler",
    ]:
        try:
            __import__(mod)
        except ImportError as e:
            failures.append(f"{mod}: {e}")
    # sqlite-vec is a C extension loaded at runtime, not imported directly
    return failures


def db_doc_count() -> int:
    """Count documents in the database. Returns 0 if DB doesn't exist."""
    import sqlite3
    from pathlib import Path
    db_path = Path(__file__).parent.parent / "data" / "khalil.db"
    if not db_path.exists():
        return 0
    try:
        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        conn.close()
        return count
    except Exception:
        return 0


def ensure_coding_agent_secret() -> bool:
    """Create the local hook signing secret if it does not already exist."""
    import keyring

    if keyring.get_password(KEYRING_SERVICE, CODING_AGENT_SECRET_KEY):
        return False
    keyring.set_password(
        KEYRING_SERVICE,
        CODING_AGENT_SECRET_KEY,
        secrets.token_urlsafe(32),
    )
    return True


def _upsert_command_hook(hooks: dict, event: str, relay_event: str, command: str) -> bool:
    desired = {"type": "command", "command": command, "timeout": 3}
    marker = f"coding-agent-hook.py --agent codex --event {relay_event}"
    entries = hooks.setdefault(event, [])
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        handlers = entry.get("hooks")
        if not isinstance(handlers, list):
            continue
        for index, handler in enumerate(handlers):
            if isinstance(handler, dict) and marker in str(handler.get("command", "")):
                if handler == desired:
                    return False
                handlers[index] = desired
                return True
    entries.append({"hooks": [desired]})
    return True


def install_coding_agent_hooks(repo_root: str, hooks_path: str | None = None) -> bool:
    """Merge Khalil's Codex lifecycle hooks into the user's hook config."""
    root = Path(repo_root).expanduser().resolve()
    python = root / ".venv" / "bin" / "python"
    relay = root / "scripts" / "coding-agent-hook.py"
    if not python.exists() or not relay.exists():
        raise FileNotFoundError("Khalil virtualenv or coding-agent hook relay is missing")

    path = Path(hooks_path).expanduser() if hooks_path else Path.home() / ".codex" / "hooks.json"
    if path.exists():
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("Codex hooks configuration must be a JSON object")
    else:
        data = {}
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Codex hooks configuration has an invalid hooks field")

    base = f"{shlex.quote(str(python))} {shlex.quote(str(relay))} --agent codex --event"
    changed = False
    for config_event, relay_event in (
        ("PreToolUse", "pre_tool_use"),
        ("PermissionRequest", "permission_request"),
        ("Stop", "stop"),
    ):
        changed |= _upsert_command_hook(
            hooks, config_event, relay_event, f"{base} {relay_event}",
        )
    secret_created = ensure_coding_agent_secret()

    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent, delete=False,
            ) as output:
                temporary = Path(output.name)
                json.dump(data, output, indent=2)
                output.write("\n")
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
    return changed or secret_created


def main():
    if len(sys.argv) < 2:
        print("Usage: setup_utils.py <command> [args...]", file=sys.stderr)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "check_secret":
        key = sys.argv[2]
        if check_secret(key):
            sys.exit(0)
        else:
            sys.exit(1)

    elif cmd == "set_secret":
        key, value = sys.argv[2], sys.argv[3]
        set_secret(key, value)
        print(f"Stored {key} in keychain")

    elif cmd == "validate_secret":
        key, value = sys.argv[2], sys.argv[3]
        ok, msg = validate_secret(key, value)
        if ok:
            sys.exit(0)
        else:
            print(msg, file=sys.stderr)
            sys.exit(1)

    elif cmd == "check_imports":
        failures = check_imports()
        if failures:
            for f in failures:
                print(f"FAIL: {f}", file=sys.stderr)
            sys.exit(1)
        print("All critical imports OK")
        sys.exit(0)

    elif cmd == "db_doc_count":
        print(db_doc_count())

    elif cmd == "install_coding_agent_hooks":
        repo_root = sys.argv[2]
        hooks_path = sys.argv[3] if len(sys.argv) > 3 else None
        changed = install_coding_agent_hooks(repo_root, hooks_path)
        print("updated" if changed else "already configured")

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
