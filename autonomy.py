"""Autonomy controller — classifies actions and manages approval flow."""

import json
import logging
import random
import sqlite3
from datetime import datetime, timedelta, timezone

from config import AutonomyLevel, ActionType, HARD_GUARDRAILS, DATA_DIR

log = logging.getLogger("khalil.autonomy")


# Action classification rules
ACTION_RULES = {
    # Read actions — always safe
    "search_knowledge": ActionType.READ,
    "get_context": ActionType.READ,
    "search_email": ActionType.READ,
    "search_drive": ActionType.READ,
    "get_timeline": ActionType.READ,
    "summarize": ActionType.READ,
    # Write actions — need approval in supervised mode
    "send_email": ActionType.WRITE,
    "draft_email": ActionType.WRITE,
    "create_reminder": ActionType.WRITE,
    "modify_file": ActionType.WRITE,
    # Dangerous actions — always need approval
    "send_money": ActionType.DANGEROUS,
    "delete_data": ActionType.DANGEROUS,
    "share_externally": ActionType.DANGEROUS,
    "modify_financial_account": ActionType.DANGEROUS,
    "generate_capability": ActionType.DANGEROUS,
    # Shell command tiers
    "shell_read": ActionType.READ,
    "shell_write": ActionType.WRITE,
    "shell_dangerous": ActionType.DANGEROUS,
    # Terminal / Cursor control
    "cursor_status": ActionType.READ,
    "cursor_extensions": ActionType.READ,
    "terminal_status": ActionType.READ,
    "cursor_open": ActionType.READ,      # navigates existing window
    "cursor_diff": ActionType.READ,      # opens diff view
    "cursor_open_project": ActionType.WRITE,  # opens new window
    "terminal_exec": ActionType.WRITE,   # injects command into live session
    "terminal_new_tab": ActionType.WRITE,
    # Cursor integrated terminal (via bridge extension)
    "cursor_terminal_status": ActionType.READ,
    "cursor_terminal_exec": ActionType.WRITE,   # injects command into Cursor terminal
    "cursor_terminal_new": ActionType.WRITE,
    # Multi-step task plans
    "task_plan": ActionType.WRITE,
}

# Safe writes: auto-approved in GUIDED mode (low risk, easily reversible)
SAFE_WRITES = {"create_reminder", "draft_email", "shell_read"}

# Pending action TTL: expire after 1 hour
PENDING_TTL_SECONDS = 3600

# #70: Per-action-type rate limits (action_prefix -> (max_count, window_seconds))
DEFAULT_RATE_LIMITS = {
    "send_email": (5, 3600),       # 5 emails per hour
    "shell": (20, 60),             # 20 shell commands per minute
    "create_reminder": (10, 3600), # 10 reminders per hour
    "generate_capability": (2, 3600),  # 2 extensions per hour
}


class AutonomyController:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._level = self._load_level()
        self._confirmation_codes: dict[int, str] = {}  # #76: action_id -> 4-digit code

    def _load_level(self) -> AutonomyLevel:
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key = 'autonomy_level'"
        ).fetchone()
        if row:
            return AutonomyLevel(int(row[0]))
        # Default to supervised
        self.conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('autonomy_level', ?)",
            (str(AutonomyLevel.SUPERVISED.value),),
        )
        self.conn.commit()
        return AutonomyLevel.SUPERVISED

    @property
    def level(self) -> AutonomyLevel:
        return self._level

    def set_level(self, level: AutonomyLevel):
        self._level = level
        self.conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('autonomy_level', ?)",
            (str(level.value),),
        )
        self.conn.commit()

    def log_audit(self, action_type: str, description: str, payload: dict | None = None, result: str | None = None):
        """Write an entry to the audit log and append to immutable JSONL trail (#77)."""
        self.conn.execute(
            "INSERT INTO audit_log (action_type, description, payload, result, autonomy_level) VALUES (?, ?, ?, ?, ?)",
            (action_type, description, json.dumps(payload) if payload else None, result, self._level.name),
        )
        self.conn.commit()

        # #77: Append to immutable JSONL audit trail (tamper-resistant)
        try:
            trail_path = DATA_DIR / "audit_trail.jsonl"
            trail_path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "action_type": action_type,
                "description": description,
                "payload": payload,
                "result": result,
                "autonomy_level": self._level.name,
            }
            with open(trail_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            log.warning("Failed to write audit trail JSONL: %s", e)

    def get_audit_log(self, limit: int = 10) -> list[dict]:
        """Get recent audit log entries."""
        rows = self.conn.execute(
            "SELECT id, timestamp, action_type, description, result, autonomy_level FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {"id": r[0], "timestamp": r[1], "action_type": r[2], "description": r[3], "result": r[4], "autonomy_level": r[5]}
            for r in rows
        ]

    def archive_old_audit_logs(self, retention_days: int = 90) -> int:
        """#71: Archive audit log entries older than retention_days. Returns count archived."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).strftime("%Y-%m-%d %H:%M:%S")

        # Count entries to archive
        count = self.conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE timestamp < ?", (cutoff,)
        ).fetchone()[0]

        if count == 0:
            return 0

        # Write to archive file before deleting
        rows = self.conn.execute(
            "SELECT * FROM audit_log WHERE timestamp < ? ORDER BY timestamp", (cutoff,)
        ).fetchall()

        import gzip
        from config import DATA_DIR
        archive_path = DATA_DIR / f"audit_archive_{datetime.now(timezone.utc).strftime('%Y%m')}.jsonl.gz"
        with gzip.open(archive_path, "at") as f:
            for r in rows:
                f.write(json.dumps({
                    "id": r[0], "timestamp": r[1], "action_type": r[2],
                    "description": r[3], "payload": r[4], "result": r[5],
                    "autonomy_level": r[6],
                }) + "\n")

        # Delete archived entries
        self.conn.execute("DELETE FROM audit_log WHERE timestamp < ?", (cutoff,))
        self.conn.commit()
        log.info("Archived %d audit log entries to %s", count, archive_path.name)
        return count

    def classify_action(self, action_name: str) -> ActionType:
        """Classify an action into read/write/dangerous."""
        return ACTION_RULES.get(action_name, ActionType.WRITE)

    def _effective_level(self) -> AutonomyLevel:
        """Return the effective autonomy level, adjusted by time-of-day context.

        During work hours (8 AM - 6 PM weekdays), use configured level.
        Outside work hours, cap at GUIDED (more conservative).
        """
        # Check if context-aware autonomy is enabled
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key = 'context_aware_autonomy'"
        ).fetchone()
        if not row or row[0] != "1":
            return self._level

        from datetime import datetime
        import zoneinfo
        from config import TIMEZONE
        now = datetime.now(zoneinfo.ZoneInfo(TIMEZONE))
        is_work_hours = now.weekday() < 5 and 8 <= now.hour < 18

        if is_work_hours:
            return self._level
        # Outside work hours, cap at GUIDED
        if self._level == AutonomyLevel.AUTONOMOUS:
            return AutonomyLevel.GUIDED
        return self._level

    def check_rate_limit(self, action_name: str) -> tuple[bool, str]:
        """Check if an action exceeds its rate limit. Returns (allowed, reason)."""
        for prefix, (max_count, window_seconds) in DEFAULT_RATE_LIMITS.items():
            if action_name.startswith(prefix):
                cutoff = (datetime.now(timezone.utc) - timedelta(seconds=window_seconds)).strftime("%Y-%m-%d %H:%M:%S")
                count = self.conn.execute(
                    "SELECT COUNT(*) FROM audit_log WHERE action_type LIKE ? AND timestamp > ?",
                    (f"{prefix}%", cutoff),
                ).fetchone()[0]
                if count >= max_count:
                    return False, f"Rate limit exceeded: {count}/{max_count} {prefix} actions in {window_seconds}s"
                break
        return True, ""

    def needs_approval(self, action_name: str, payload: dict | None = None) -> bool:
        """Check if an action needs user approval given current autonomy level."""
        action_type = self.classify_action(action_name)
        effective_level = self._effective_level()

        # Hard guardrails always need approval
        if action_name in HARD_GUARDRAILS:
            needs = True
            reason = "hard_guardrail"
        elif action_type == ActionType.DANGEROUS:
            needs = True
            reason = "dangerous_action"
        elif action_type == ActionType.READ:
            needs = False
            reason = "read_auto_approved"
        elif effective_level == AutonomyLevel.SUPERVISED:
            needs = True
            reason = "supervised_mode"
        elif effective_level == AutonomyLevel.GUIDED:
            needs = action_name not in SAFE_WRITES
            reason = "guided_risky" if needs else "guided_safe_write"
        else:  # AUTONOMOUS
            needs = False
            reason = "autonomous_mode"

        # M9: Auto-escalation from learned approval patterns
        if needs and reason not in ("hard_guardrail", "dangerous_action"):
            try:
                from learning import check_auto_escalation
                if check_auto_escalation(action_name, payload):
                    needs = False
                    reason = "learned_auto_approve"
                    log.info("Auto-approved via learned pattern: %s", action_name)
            except Exception:
                pass  # Table may not exist yet

        # #8: Decision journal — log every autonomy decision with reasoning
        try:
            self.conn.execute(
                "INSERT INTO audit_log (action_type, description, payload, result, autonomy_level) "
                "VALUES ('autonomy_decision', ?, ?, ?, ?)",
                (
                    f"{'APPROVAL_NEEDED' if needs else 'AUTO_APPROVED'}: {action_name}",
                    json.dumps({"action": action_name, "action_type": action_type.value,
                                "effective_level": effective_level.name, "reason": reason}),
                    reason,
                    effective_level.name,
                ),
            )
            self.conn.commit()
        except Exception:
            pass  # Non-critical — don't block the decision

        return needs

    def create_pending_action(self, action_name: str, description: str, payload: dict | None = None) -> int:
        """Queue an action for approval. Returns the action ID.

        For hard guardrail actions (#76), a confirmation code is generated
        that must be verified before approval.
        """
        cursor = self.conn.execute(
            "INSERT INTO pending_actions (action_type, description, payload, status) VALUES (?, ?, ?, 'pending')",
            (action_name, description, json.dumps(payload) if payload else None),
        )
        self.conn.commit()
        action_id = cursor.lastrowid

        # #76: Generate confirmation code for hard guardrail actions
        if action_name in HARD_GUARDRAILS:
            code = generate_confirmation_code()
            self._confirmation_codes[action_id] = code
            log.info("Confirmation code generated for hard guardrail action #%d", action_id)

        return action_id

    def approve_action(self, action_id: int) -> dict | None:
        """Approve a pending action. Returns the action details."""
        row = self.conn.execute(
            "SELECT * FROM pending_actions WHERE id = ? AND status = 'pending'", (action_id,)
        ).fetchone()
        if not row:
            return None

        self.conn.execute(
            "UPDATE pending_actions SET status = 'approved', resolved_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), action_id),
        )
        self.conn.commit()
        action = {
            "id": row[0],
            "action_type": row[1],
            "description": row[2],
            "payload": json.loads(row[3]) if row[3] else None,
        }
        self.log_audit(action["action_type"], action["description"], action.get("payload"), "approved")
        # Record signal for self-improvement reflection
        try:
            from learning import record_signal, record_approval_pattern
            record_signal("action_decision", {"action_type": action["action_type"], "decision": "approved"}, value=1.0)
            record_approval_pattern(action["action_type"], action.get("payload"), approved=True)
        except Exception:
            pass
        return action

    def deny_action(self, action_id: int) -> bool:
        """Deny a pending action."""
        # Fetch details before updating for audit
        row = self.conn.execute(
            "SELECT action_type, description FROM pending_actions WHERE id = ?", (action_id,)
        ).fetchone()
        result = self.conn.execute(
            "UPDATE pending_actions SET status = 'denied', resolved_at = ? WHERE id = ? AND status = 'pending'",
            (datetime.now(timezone.utc).isoformat(), action_id),
        )
        self.conn.commit()
        if result.rowcount > 0 and row:
            self.log_audit(row[0], row[1], result="denied")
            # Record signal for self-improvement reflection
            try:
                from learning import record_signal, record_approval_pattern
                record_signal("action_decision", {"action_type": row[0], "decision": "denied"}, value=0.0)
                payload_row = self.conn.execute(
                    "SELECT payload FROM pending_actions WHERE id = ?", (action_id,)
                ).fetchone()
                _payload = json.loads(payload_row[0]) if payload_row and payload_row[0] else None
                record_approval_pattern(row[0], _payload, approved=False)
            except Exception:
                pass
        return result.rowcount > 0

    def get_expiring_actions(self, warn_seconds: int = 300) -> list[dict]:
        """Get pending actions that will expire within warn_seconds. For pre-expiry reminders."""
        now = datetime.utcnow()
        expiry_cutoff = now - timedelta(seconds=PENDING_TTL_SECONDS - warn_seconds)
        expiry_str = expiry_cutoff.strftime("%Y-%m-%d %H:%M:%S")
        too_old = (now - timedelta(seconds=PENDING_TTL_SECONDS)).strftime("%Y-%m-%d %H:%M:%S")
        rows = self.conn.execute(
            "SELECT id, action_type, description, created_at FROM pending_actions "
            "WHERE status = 'pending' AND created_at < ? AND created_at > ?",
            (expiry_str, too_old),
        ).fetchall()
        return [{"id": r[0], "action_type": r[1], "description": r[2], "created_at": r[3]} for r in rows]

    def expire_stale_actions(self) -> int:
        """Expire pending actions older than PENDING_TTL_SECONDS. Returns count expired."""
        # SQLite CURRENT_TIMESTAMP uses "YYYY-MM-DD HH:MM:SS" format (space, no TZ)
        # so cutoff must match that format for correct string comparison
        cutoff = datetime.utcnow() - timedelta(seconds=PENDING_TTL_SECONDS)
        cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
        result = self.conn.execute(
            "UPDATE pending_actions SET status = 'expired', resolved_at = ? WHERE status = 'pending' AND created_at < ?",
            (datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), cutoff_str),
        )
        if result.rowcount:
            self.conn.commit()
            self.log_audit("system", f"Expired {result.rowcount} stale pending action(s)", result="expired")
            log.info("Expired %d stale pending actions", result.rowcount)
        return result.rowcount

    def get_pending_actions(self) -> list[dict]:
        """Get all pending actions (expires stale ones first)."""
        self.expire_stale_actions()
        rows = self.conn.execute(
            "SELECT id, action_type, description, payload, created_at FROM pending_actions WHERE status = 'pending' ORDER BY created_at DESC"
        ).fetchall()
        return [
            {
                "id": r[0],
                "action_type": r[1],
                "description": r[2],
                "payload": json.loads(r[3]) if r[3] else None,
                "created_at": r[4],
            }
            for r in rows
        ]

    def get_latest_pending(self) -> dict | None:
        """Get the most recent pending action."""
        pending = self.get_pending_actions()
        return pending[0] if pending else None

    async def execute_action(self, action: dict) -> str:
        """Execute an approved action by dispatching to the appropriate handler.

        Returns a status message string.
        """
        action_type = action["action_type"]
        payload = action.get("payload") or {}

        if action_type == "send_email":
            from actions.gmail import draft_email, send_draft
            # Create draft then send it
            draft = await draft_email(
                to=payload["to"],
                subject=payload["subject"],
                body=payload["body"],
            )
            await send_draft(draft["draft_id"])
            return f"📧 Email sent to {payload['to']}: {payload['subject']}"

        elif action_type == "create_reminder":
            from actions.reminders import create_reminder
            from datetime import datetime
            due_at = datetime.fromisoformat(payload["due_at"])
            result = create_reminder(payload["text"], due_at)
            return f"⏰ Reminder #{result['id']} created: {result['text']} (due {result['due_at']})"

        elif action_type in ("shell_write", "shell_read"):
            from actions.shell import execute_shell, format_output
            cmd = payload["command"]
            result = await execute_shell(cmd, cwd=payload.get("cwd"))
            self.log_audit(action_type, f"Executed: {cmd}", payload, f"exit={result['returncode']}")
            return format_output(result, cmd)



        elif action_type == "generate_capability":
            from actions.extend import generate_and_pr
            return await generate_and_pr(payload)

        else:
            return f"Unknown action type: {action_type}. No executor available."

    def get_confirmation_code(self, action_id: int) -> str | None:
        """Get the confirmation code for a pending action, if one exists."""
        return self._confirmation_codes.get(action_id)

    def verify_confirmation_code(self, action_id: int, code: str) -> bool:
        """Verify a confirmation code for a hard guardrail action (#76).

        Returns True if the code matches. Removes the code on success.
        """
        expected = self._confirmation_codes.get(action_id)
        if expected is None:
            return False
        if code == expected:
            del self._confirmation_codes[action_id]
            return True
        return False

    def format_level(self) -> str:
        """Format current autonomy level for display."""
        icons = {
            AutonomyLevel.SUPERVISED: "🔒",
            AutonomyLevel.GUIDED: "🔓",
            AutonomyLevel.AUTONOMOUS: "⚡",
        }
        return f"{icons[self._level]} {self._level.name.title()} (Level {self._level.value})"


    def format_patterns(self) -> str:
        """Format learned approval patterns for /mode patterns display."""
        try:
            from learning import get_approval_patterns, AUTO_ESCALATE_THRESHOLD
            patterns = get_approval_patterns()
        except Exception:
            return "No learned patterns yet."

        if not patterns:
            return "No learned patterns yet. Patterns are recorded as you approve/deny actions."

        lines = [f"Learned Approval Patterns (auto-approve threshold: {AUTO_ESCALATE_THRESHOLD}):"]
        for p in patterns:
            status = "AUTO" if p["auto_approve"] else "LEARNING"
            lines.append(
                f"  [{status}] {p['action_type']}/{p['command_pattern']}"
                f" | Approved: {p['approved_count']} | Denied: {p['denied_count']}"
            )
        return chr(10).join(lines)


def generate_confirmation_code() -> str:
    """Generate a random 4-digit confirmation code (#76)."""
    return f"{random.randint(1000, 9999)}"
