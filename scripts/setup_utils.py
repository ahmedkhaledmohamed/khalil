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
"""

import sys

KEYRING_SERVICE = "khalil-assistant"


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

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
