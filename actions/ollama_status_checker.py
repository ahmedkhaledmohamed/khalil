"""Check real-time status of Ollama and Claude services.

No API keys required — Ollama status is checked via its local HTTP API
(default http://localhost:11434). Claude API availability is checked via
a lightweight models endpoint using the API key stored in keyring.

Keyring keys used:
- anthropic-api-key  — Anthropic API key (for Claude status check)
"""

import asyncio
import logging
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import keyring

from config import DB_PATH, KEYRING_SERVICE, OLLAMA_URL, OLLAMA_LLM_MODEL, TIMEZONE

log = logging.getLogger("khalil.actions.ollama_status_checker")

_tables_ensured = False


def ensure_tables(conn: sqlite3.Connection):
    """Create status check log table. Called once at startup."""
    global _tables_ensured
    if _tables_ensured:
        return
    conn.execute(
        """CREATE TABLE IF NOT EXISTS ollama_status_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checked_at TEXT NOT NULL,
            ollama_up INTEGER NOT NULL,
            ollama_models TEXT,
            claude_up INTEGER,
            notes TEXT
        )"""
    )
    conn.commit()
    _tables_ensured = True


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


# --- Core sync functions (called via asyncio.to_thread) ---


def _check_ollama() -> dict:
    """Check Ollama server status and list loaded models."""
    result = {"up": False, "version": None, "models": []}
    try:
        with httpx.Client(timeout=5) as client:
            # Health check
            resp = client.get(f"{OLLAMA_URL}/api/version")
            if resp.status_code == 200:
                result["up"] = True
                data = resp.json()
                result["version"] = data.get("version", "unknown")

            # List models
            resp = client.get(f"{OLLAMA_URL}/api/tags")
            if resp.status_code == 200:
                models = resp.json().get("models", [])
                result["models"] = [
                    {
                        "name": m.get("name", "?"),
                        "size_gb": round(m.get("size", 0) / 1e9, 1),
                    }
                    for m in models
                ]
    except httpx.ConnectError:
        log.debug("Ollama not reachable at %s", OLLAMA_URL)
    except Exception as exc:
        log.warning("Ollama check error: %s", exc)
        result["notes"] = str(exc)
    return result


def _check_claude() -> dict:
    """Check Claude API availability via the models endpoint."""
    result = {"up": False, "model": None}
    api_key = keyring.get_password(KEYRING_SERVICE, "anthropic-api-key")
    if not api_key:
        result["notes"] = "No API key configured (keyring: anthropic-api-key)"
        return result
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                "https://api.anthropic.com/v1/models",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                },
            )
            if resp.status_code == 200:
                result["up"] = True
                result["model"] = "available"
            else:
                result["notes"] = f"HTTP {resp.status_code}"
    except Exception as exc:
        log.warning("Claude API check error: %s", exc)
        result["notes"] = str(exc)
    return result


def _log_check(ollama: dict, claude: dict):
    """Log status check result to DB."""
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz).isoformat()
    model_names = ", ".join(m["name"] for m in ollama.get("models", []))
    notes_parts = []
    if ollama.get("notes"):
        notes_parts.append(f"ollama: {ollama['notes']}")
    if claude.get("notes"):
        notes_parts.append(f"claude: {claude['notes']}")

    conn = _get_conn()
    try:
        ensure_tables(conn)
        conn.execute(
            "INSERT INTO ollama_status_log (checked_at, ollama_up, ollama_models, claude_up, notes) "
            "VALUES (?, ?, ?, ?, ?)",
            (now, int(ollama["up"]), model_names, int(claude["up"]), "; ".join(notes_parts) or None),
        )
        conn.commit()
    finally:
        conn.close()


def _get_history(limit: int = 10) -> list[dict]:
    """Fetch recent status check history."""
    conn = _get_conn()
    try:
        ensure_tables(conn)
        rows = conn.execute(
            "SELECT checked_at, ollama_up, ollama_models, claude_up, notes "
            "FROM ollama_status_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --- Async wrappers ---


async def _async_check_all() -> tuple[dict, dict]:
    """Run Ollama and Claude checks concurrently."""
    ollama, claude = await asyncio.gather(
        asyncio.to_thread(_check_ollama),
        asyncio.to_thread(_check_claude),
    )
    await asyncio.to_thread(_log_check, ollama, claude)
    return ollama, claude


async def _async_get_history(limit: int = 10) -> list[dict]:
    return await asyncio.to_thread(_get_history, limit)


def _format_status(ollama: dict, claude: dict) -> str:
    """Format status results for Telegram."""
    lines = ["--- Service Status ---", ""]

    # Ollama
    icon = "UP" if ollama["up"] else "DOWN"
    lines.append(f"Ollama: {icon}")
    if ollama.get("version"):
        lines.append(f"  Version: {ollama['version']}")
    lines.append(f"  URL: {OLLAMA_URL}")
    lines.append(f"  Configured model: {OLLAMA_LLM_MODEL}")
    if ollama["models"]:
        lines.append(f"  Available models ({len(ollama['models'])}):")
        for m in ollama["models"]:
            lines.append(f"    - {m['name']} ({m['size_gb']} GB)")
    elif ollama["up"]:
        lines.append("  No models downloaded")

    lines.append("")

    # Claude
    icon = "UP" if claude["up"] else "DOWN"
    lines.append(f"Claude API: {icon}")
    if claude.get("notes"):
        lines.append(f"  Note: {claude['notes']}")

    return "\n".join(lines)


def _format_history(records: list[dict]) -> str:
    """Format history records for Telegram."""
    if not records:
        return "No status check history yet."
    lines = ["--- Recent Checks ---", ""]
    for r in records:
        ollama_s = "UP" if r["ollama_up"] else "DOWN"
        claude_s = "UP" if r["claude_up"] else "DOWN"
        ts = r["checked_at"][:19]  # trim timezone for readability
        lines.append(f"{ts}  Ollama={ollama_s}  Claude={claude_s}")
        if r.get("notes"):
            lines.append(f"  {r['notes']}")
    return "\n".join(lines)


async def handle_ollama_status(update, context):
    """Handle /ollama_status command.

    Subcommands:
        /ollama_status          — check current status of all services
        /ollama_status history   — show recent check history
        /ollama_status history N — show last N checks (default 10)
    """
    args = context.args or []

    if args and args[0].lower() == "history":
        limit = 10
        if len(args) > 1:
            try:
                limit = min(int(args[1]), 50)
            except ValueError:
                await update.message.reply_text("Usage: /ollama_status history [N]")
                return
        records = await _async_get_history(limit)
        await update.message.reply_text(_format_history(records))
        return

    # Default: run status check
    await update.message.reply_text("Checking services...")
    ollama, claude = await _async_check_all()
    await update.message.reply_text(_format_status(ollama, claude))
