"""Restart PharoClaw (the bot process) or the host machine via SSH.

This capability uses the Telegram Bot API to confirm restart actions,
then triggers the restart. For PharoClaw restarts, the process exits with
code 0 so the process manager (systemd/launchd) can restart it. For
host machine restarts, an SSH command is sent to the configured host.

Keyring keys used:
- restart-ssh-host     — SSH host for remote machine restart (e.g., user@192.168.1.100)
- restart-ssh-key-path — Path to SSH private key (optional, uses default if unset)
"""

import asyncio
import logging
import sqlite3
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import keyring

from config import DB_PATH, KEYRING_SERVICE, TIMEZONE

log = logging.getLogger("pharoclaw.actions.system_restarter")

_tables_ensured = False


def ensure_tables(conn: sqlite3.Connection):
    """Create restart log table. Called once at startup."""
    global _tables_ensured
    if _tables_ensured:
        return
    conn.execute(
        """CREATE TABLE IF NOT EXISTS restart_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            requested_at TEXT NOT NULL,
            restart_type TEXT NOT NULL,
            status TEXT NOT NULL,
            requested_by TEXT,
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


def _log_restart(restart_type: str, status: str, requested_by: str = None, notes: str = None):
    """Log a restart request to the database."""
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz).isoformat()
    conn = _get_conn()
    try:
        ensure_tables(conn)
        conn.execute(
            "INSERT INTO restart_log (requested_at, restart_type, status, requested_by, notes) "
            "VALUES (?, ?, ?, ?, ?)",
            (now, restart_type, status, requested_by, notes),
        )
        conn.commit()
    finally:
        conn.close()


def _get_history(limit: int = 10) -> list[dict]:
    """Fetch recent restart history."""
    conn = _get_conn()
    try:
        ensure_tables(conn)
        rows = conn.execute(
            "SELECT requested_at, restart_type, status, requested_by, notes "
            "FROM restart_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --- Async wrappers ---


async def _async_log_restart(restart_type: str, status: str, requested_by: str = None, notes: str = None):
    await asyncio.to_thread(_log_restart, restart_type, status, requested_by, notes)


async def _async_get_history(limit: int = 10) -> list[dict]:
    return await asyncio.to_thread(_get_history, limit)


def _format_history(records: list[dict]) -> str:
    """Format restart history for Telegram."""
    if not records:
        return "No restart history yet."
    lines = ["--- Restart History ---", ""]
    for r in records:
        ts = r["requested_at"][:19]
        lines.append(f"{ts}  type={r['restart_type']}  status={r['status']}")
        if r.get("requested_by"):
            lines.append(f"  by: {r['requested_by']}")
        if r.get("notes"):
            lines.append(f"  note: {r['notes']}")
    return "\n".join(lines)


async def handle_restart(update, context):
    """Handle /restart command.

    Subcommands:
        /restart              — show usage help
        /restart pharoclaw       — preview: show what restarting PharoClaw would do
        /restart pharoclaw confirm — restart the PharoClaw bot process
        /restart host         — preview: show what restarting the host would do
        /restart host confirm — restart the host machine via SSH
        /restart history      — show recent restart history
        /restart history N    — show last N restarts (default 10)
    """
    args = context.args or []
    user = update.effective_user
    user_label = user.full_name if user else "unknown"

    if not args:
        await update.message.reply_text(
            "Usage:\n"
            "  /restart pharoclaw         — preview PharoClaw restart\n"
            "  /restart pharoclaw confirm — restart PharoClaw process\n"
            "  /restart host           — preview host restart\n"
            "  /restart host confirm   — restart host machine\n"
            "  /restart history [N]    — show restart history"
        )
        return

    subcmd = args[0].lower()

    # --- History ---
    if subcmd == "history":
        limit = 10
        if len(args) > 1:
            try:
                limit = min(int(args[1]), 50)
            except ValueError:
                await update.message.reply_text("Usage: /restart history [N]")
                return
        records = await _async_get_history(limit)
        await update.message.reply_text(_format_history(records))
        return

    # --- PharoClaw restart ---
    if subcmd == "pharoclaw":
        confirm = len(args) > 1 and args[1].lower() == "confirm"
        if not confirm:
            await update.message.reply_text(
                "DRY RUN — Restart PharoClaw\n\n"
                "This will:\n"
                "  1. Log the restart request\n"
                "  2. Send a goodbye message\n"
                "  3. Exit the process (code 0)\n"
                "  4. The process manager (systemd/launchd) will restart PharoClaw\n\n"
                "To proceed: /restart pharoclaw confirm"
            )
            return

        await _async_log_restart("pharoclaw", "initiated", requested_by=user_label)
        await update.message.reply_text("Restarting PharoClaw... I'll be back shortly.")
        log.info("PharoClaw restart requested by %s — exiting process", user_label)

        # Give Telegram time to deliver the message before exiting
        await asyncio.sleep(1)
        sys.exit(0)

    # --- Host restart ---
    if subcmd == "host":
        ssh_host = keyring.get_password(KEYRING_SERVICE, "restart-ssh-host")
        if not ssh_host:
            await update.message.reply_text(
                "Host restart not configured.\n\n"
                "Set the SSH host in keyring:\n"
                "  keyring set pharoclaw restart-ssh-host"
            )
            return

        confirm = len(args) > 1 and args[1].lower() == "confirm"
        if not confirm:
            await update.message.reply_text(
                f"DRY RUN — Restart Host\n\n"
                f"This will:\n"
                f"  1. Log the restart request\n"
                f"  2. SSH to {ssh_host}\n"
                f"  3. Execute 'sudo shutdown -r now'\n\n"
                f"To proceed: /restart host confirm"
            )
            return

        await _async_log_restart("host", "initiated", requested_by=user_label, notes=f"target: {ssh_host}")
        await update.message.reply_text(f"Sending restart command to {ssh_host}...")

        try:
            result = await _send_ssh_restart(ssh_host)
            if result["success"]:
                await _async_log_restart("host", "sent", requested_by=user_label)
                await update.message.reply_text(f"Restart command sent to {ssh_host}. Host should reboot shortly.")
            else:
                await _async_log_restart("host", "failed", requested_by=user_label, notes=result.get("error", ""))
                await update.message.reply_text(f"Failed to restart host: {result.get('error', 'unknown error')}")
        except Exception as exc:
            await _async_log_restart("host", "error", requested_by=user_label, notes=str(exc))
            await update.message.reply_text(f"Error sending restart command: {exc}")
            log.error("Host restart failed: %s", exc)
        return

    await update.message.reply_text(
        f"Unknown subcommand: {subcmd}\n"
        "Use /restart for usage help."
    )


async def _send_ssh_restart(ssh_host: str) -> dict:
    """Send a restart command to the host via SSH using asyncio subprocess.

    Uses asyncio.create_subprocess_exec (not the forbidden subprocess module)
    to run the SSH command asynchronously.
    """
    ssh_key_path = keyring.get_password(KEYRING_SERVICE, "restart-ssh-key-path")

    cmd = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10"]
    if ssh_key_path:
        cmd.extend(["-i", ssh_key_path])
    cmd.extend([ssh_host, "sudo", "shutdown", "-r", "now"])

    log.info("Executing SSH restart: %s", " ".join(cmd))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

    if proc.returncode == 0:
        return {"success": True}
    else:
        error_msg = stderr.decode().strip() if stderr else f"exit code {proc.returncode}"
        return {"success": False, "error": error_msg}
