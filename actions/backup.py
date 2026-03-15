"""Backup and restore — export/import Khalil state as JSON."""

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from config import DB_PATH, DATA_DIR, TIMEZONE

log = logging.getLogger("khalil.actions.backup")

BACKUP_DIR = DATA_DIR / "backups"

# Tables to export (excludes documents/embeddings — those are re-indexable)
BACKUP_TABLES = ["reminders", "conversations", "audit_log", "pending_actions", "settings"]


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
