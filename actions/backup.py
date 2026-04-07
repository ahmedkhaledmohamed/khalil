"""Backup and restore — export/import Khalil state as JSON.

Two export modes:
- Full backup: all operational tables → data/backups/ (local only)
- Knowledge export: portable knowledge tables → git-synced directory (for cross-machine persistence)
"""

import json
import logging
import os
import re
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from config import DB_PATH, DATA_DIR, TIMEZONE, KNOWLEDGE_EXPORT_DIR

log = logging.getLogger("khalil.actions.backup")

BACKUP_DIR = DATA_DIR / "backups"

# Tables to export (excludes documents/embeddings — those are re-indexable)
BACKUP_TABLES = ["reminders", "conversations", "audit_log", "pending_actions", "settings"]

# Portable knowledge tables — the stuff worth preserving forever
KNOWLEDGE_TABLES = [
    "memories",
    "conversation_summaries",
    "learned_preferences",
    "insights",
    "approval_patterns",
    "workflows",
    "reminders",
    "settings",
]

# Fields that should be redacted in exports
_SENSITIVE_FIELD_PATTERNS = re.compile(r"key|token|secret|password|credential", re.IGNORECASE)


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def export_backup() -> Path:
    """Export conversations, reminders, audit log, pending actions, and settings as JSON.

    Returns the path to the backup file.
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    conn = _get_conn()
    backup_data = {"exported_at": datetime.now(ZoneInfo(TIMEZONE)).isoformat()}

    for table in BACKUP_TABLES:
        try:
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            backup_data[table] = [dict(r) for r in rows]
        except sqlite3.OperationalError as e:
            log.warning("Skipping table %s: %s", table, e)
            backup_data[table] = []

    conn.close()

    timestamp = datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUP_DIR / f"khalil_backup_{timestamp}.json"

    with open(backup_path, "w", encoding="utf-8") as f:
        json.dump(backup_data, f, indent=2, default=str)

    log.info("Backup exported to %s", backup_path)
    return backup_path


def import_backup(backup_path: Path) -> dict:
    """Import state from a backup JSON file.

    Returns dict with counts of imported rows per table.
    """
    with open(backup_path, "r", encoding="utf-8") as f:
        backup_data = json.load(f)

    conn = _get_conn()
    counts = {}

    for table in BACKUP_TABLES:
        rows = backup_data.get(table, [])
        if not rows:
            counts[table] = 0
            continue

        columns = list(rows[0].keys())
        placeholders = ", ".join("?" for _ in columns)
        col_names = ", ".join(columns)

        imported = 0
        for row in rows:
            values = [row.get(c) for c in columns]
            try:
                conn.execute(
                    f"INSERT OR IGNORE INTO {table} ({col_names}) VALUES ({placeholders})",
                    values,
                )
                imported += 1
            except sqlite3.Error as e:
                log.warning("Failed to import row into %s: %s", table, e)

        counts[table] = imported

    conn.commit()
    conn.close()
    log.info("Backup imported from %s: %s", backup_path, counts)
    return counts


def list_backups() -> list[dict]:
    """List available backup files."""
    if not BACKUP_DIR.exists():
        return []

    backups = []
    for f in sorted(BACKUP_DIR.glob("khalil_backup_*.json"), reverse=True):
        stat = f.stat()
        backups.append({
            "filename": f.name,
            "path": str(f),
            "size_kb": round(stat.st_size / 1024, 1),
            "created": datetime.fromtimestamp(stat.st_mtime).isoformat()[:16],
        })

    return backups


def format_backup_summary(backup_path: Path) -> str:
    """Format a summary of a backup file for display."""
    with open(backup_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    lines = [f"📦 Backup: {backup_path.name}\n"]
    lines.append(f"Exported: {data.get('exported_at', 'unknown')}\n")

    for table in BACKUP_TABLES:
        rows = data.get(table, [])
        lines.append(f"  {table}: {len(rows)} rows")

    return "\n".join(lines)


# --- Knowledge Export (portable, git-synced) ---


def _redact_row(row: dict) -> dict:
    """Redact sensitive fields in a row dict."""
    redacted = {}
    for k, v in row.items():
        if _SENSITIVE_FIELD_PATTERNS.search(k) and v and isinstance(v, str):
            redacted[k] = "[REDACTED]"
        else:
            redacted[k] = v
    return redacted


def export_knowledge(export_dir: Path = None, git_sync: bool = True) -> dict:
    """Export portable knowledge tables as individual JSON files.

    Args:
        export_dir: Target directory (defaults to KNOWLEDGE_EXPORT_DIR).
        git_sync: Whether to commit and push to git after export.

    Returns dict with table names and row counts exported.
    """
    export_dir = export_dir or KNOWLEDGE_EXPORT_DIR
    export_dir.mkdir(parents=True, exist_ok=True)

    conn = _get_conn()
    counts = {}
    now = datetime.now(ZoneInfo(TIMEZONE))

    for table in KNOWLEDGE_TABLES:
        try:
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            data = [_redact_row(dict(r)) for r in rows]
            counts[table] = len(data)

            out_path = export_dir / f"{table}.json"
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str, ensure_ascii=False)

        except sqlite3.OperationalError as e:
            log.warning("Skipping table %s: %s", table, e)
            counts[table] = 0

    conn.close()

    # Write metadata
    meta = {
        "exported_at": now.isoformat(),
        "tables": counts,
        "total_rows": sum(counts.values()),
    }
    with open(export_dir / "_meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    log.info("Knowledge exported to %s: %s total rows", export_dir, meta["total_rows"])

    # Git sync
    if git_sync:
        _git_sync(export_dir, now)

    return counts


def _git_sync(export_dir: Path, timestamp: datetime):
    """Commit and push knowledge export to git."""
    try:
        # Initialize repo if needed
        git_dir = export_dir / ".git"
        if not git_dir.exists():
            subprocess.run(
                ["git", "init"], cwd=str(export_dir),
                capture_output=True, timeout=10,
            )
            # Create .gitignore
            gitignore = export_dir / ".gitignore"
            if not gitignore.exists():
                gitignore.write_text(".DS_Store\n")
            log.info("Initialized git repo in %s", export_dir)

        # Stage all changes
        subprocess.run(
            ["git", "add", "-A"], cwd=str(export_dir),
            capture_output=True, timeout=10,
        )

        # Check if there are changes to commit
        status = subprocess.run(
            ["git", "diff", "--cached", "--quiet"], cwd=str(export_dir),
            capture_output=True, timeout=10,
        )
        if status.returncode == 0:
            log.info("No knowledge changes to commit")
            return

        # Commit
        msg = f"Knowledge export {timestamp.strftime('%Y-%m-%d %H:%M')}"
        subprocess.run(
            ["git", "commit", "-m", msg], cwd=str(export_dir),
            capture_output=True, timeout=10,
        )
        log.info("Knowledge committed: %s", msg)

        # Push (if remote configured)
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"], cwd=str(export_dir),
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            push_result = subprocess.run(
                ["git", "push"], cwd=str(export_dir),
                capture_output=True, text=True, timeout=30,
            )
            if push_result.returncode == 0:
                log.info("Knowledge pushed to remote")
            else:
                log.warning("Git push failed: %s", push_result.stderr[:200])
        else:
            log.info("No git remote configured — export is local only")

    except Exception as e:
        log.warning("Git sync failed (non-fatal): %s", e)


def import_knowledge(source_dir: Path = None) -> dict:
    """Import knowledge from JSON files into the database.

    Uses INSERT OR IGNORE to merge without overwriting existing data.
    Returns dict with table names and rows imported.
    """
    source_dir = source_dir or KNOWLEDGE_EXPORT_DIR

    if not source_dir.exists():
        return {"error": f"Source directory not found: {source_dir}"}

    conn = _get_conn()
    counts = {}

    for table in KNOWLEDGE_TABLES:
        json_path = source_dir / f"{table}.json"
        if not json_path.exists():
            counts[table] = 0
            continue

        with open(json_path, "r", encoding="utf-8") as f:
            rows = json.load(f)

        if not rows:
            counts[table] = 0
            continue

        # Filter out redacted rows for import
        columns = [k for k in rows[0].keys() if not _SENSITIVE_FIELD_PATTERNS.search(k) or rows[0][k] != "[REDACTED]"]
        if not columns:
            counts[table] = 0
            continue

        placeholders = ", ".join("?" for _ in columns)
        col_names = ", ".join(columns)

        imported = 0
        for row in rows:
            # Skip rows with redacted values in key columns
            values = []
            skip = False
            for c in columns:
                v = row.get(c)
                if v == "[REDACTED]":
                    skip = True
                    break
                values.append(v)
            if skip:
                continue

            try:
                conn.execute(
                    f"INSERT OR IGNORE INTO {table} ({col_names}) VALUES ({placeholders})",
                    values,
                )
                imported += 1
            except sqlite3.Error as e:
                log.warning("Failed to import row into %s: %s", table, e)

        counts[table] = imported

    conn.commit()
    conn.close()
    log.info("Knowledge imported from %s: %s", source_dir, counts)
    return counts


def format_knowledge_summary(counts: dict) -> str:
    """Format knowledge export/import results for display."""
    total = sum(v for v in counts.values() if isinstance(v, int))
    lines = [f"📚 Knowledge: {total} total rows\n"]
    for table, count in counts.items():
        lines.append(f"  {table}: {count} rows")
    return "\n".join(lines)
