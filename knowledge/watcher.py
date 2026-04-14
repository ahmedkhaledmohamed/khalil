"""Knowledge freshness watcher — tiered re-indexing for file changes.

Tier 1: trigger_reindex_files() — called by webhook on push events (seconds)
Tier 2: scan_and_reindex() — called by scheduler every 5 min (minutes)

Only processes .md and .csv files. Skips .git, node_modules, etc.
Rate-limited to max N files per cycle to avoid overloading Ollama.
"""

import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path

from config import (
    DB_PATH, WATCH_PATHS, WATCH_MAX_FILES_PER_CYCLE, WATCH_ACTIVE_HOURS,
)

log = logging.getLogger("khalil.watcher")

SUPPORTED_EXTENSIONS = {".md", ".csv"}
SKIP_PARTS = {".git", ".github", "node_modules", "__pycache__", ".next", ".venv", "out"}

# Prevent concurrent reindex runs
_reindex_lock = asyncio.Lock()

# Files that exceeded per-cycle limit — processed next cycle
_pending_queue: list[str] = []


def is_active_hours() -> bool:
    """Check if current time is within configured active hours."""
    hour = datetime.now().hour
    start, end = WATCH_ACTIVE_HOURS
    return start <= hour < end


def _should_skip(filepath: Path) -> bool:
    """Check if a file path should be skipped (in .git, node_modules, etc.)."""
    return bool(SKIP_PARTS.intersection(filepath.parts))


def _scan_changed_files(watch_paths: list[Path]) -> list[str]:
    """Scan directories for files with mtime newer than last indexed.

    Uses the file_freshness table for per-file tracking.
    Returns list of absolute file paths that need reindexing.
    """
    import sqlite3

    if not DB_PATH.exists():
        return []

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Load all known file freshness entries
    known = {}
    try:
        for row in conn.execute("SELECT file_path, last_mtime, content_hash FROM file_freshness"):
            known[row["file_path"]] = (row["last_mtime"], row["content_hash"])
    except Exception:
        pass  # Table may not exist yet

    changed = []
    for watch_dir in watch_paths:
        if not watch_dir.exists():
            continue
        for filepath in watch_dir.rglob("*"):
            if not filepath.is_file():
                continue
            if filepath.suffix not in SUPPORTED_EXTENSIONS:
                continue
            if _should_skip(filepath):
                continue
            fp = str(filepath)
            try:
                mtime = filepath.stat().st_mtime
                if fp in known:
                    if mtime <= known[fp][0]:
                        continue  # Not modified since last index
                changed.append(fp)
            except OSError:
                continue

    conn.close()
    return changed


async def scan_and_reindex() -> dict:
    """Tier 2: scan watched paths for changed files and reindex them.

    Called by the scheduler every 5 minutes during active hours.
    Returns {"scanned": N, "changed": N, "indexed": N, "queued": N}
    """
    if not is_active_hours():
        return {"scanned": 0, "changed": 0, "indexed": 0, "queued": 0}

    async with _reindex_lock:
        from knowledge.indexer import reindex_files

        # Process pending queue first
        to_process = list(_pending_queue)
        _pending_queue.clear()

        # Scan for new changes
        new_changed = _scan_changed_files(WATCH_PATHS)
        to_process.extend(new_changed)

        # Deduplicate
        to_process = list(dict.fromkeys(to_process))

        if not to_process:
            return {"scanned": len(WATCH_PATHS), "changed": 0, "indexed": 0, "queued": 0}

        result = await reindex_files(to_process, max_files=WATCH_MAX_FILES_PER_CYCLE)

        # Queue overflow for next cycle
        if result.get("queued"):
            _pending_queue.extend(result["queued"])

        return {
            "scanned": len(WATCH_PATHS),
            "changed": len(to_process),
            "indexed": result.get("indexed", 0),
            "queued": len(_pending_queue),
        }


async def trigger_reindex_files(file_paths: list[str]) -> dict:
    """Tier 1: immediately reindex specific files.

    Called by the webhook handler on push events.
    Respects the reindex lock and max files limit.
    """
    async with _reindex_lock:
        from knowledge.indexer import reindex_files
        return await reindex_files(file_paths, max_files=WATCH_MAX_FILES_PER_CYCLE)


async def remove_indexed_files(file_paths: list[str]) -> int:
    """Remove documents and embeddings for deleted files.

    Called when webhook reports removed files.
    """
    import sqlite3

    if not DB_PATH.exists():
        return 0

    conn = sqlite3.connect(str(DB_PATH))
    removed = 0
    for fp in file_paths:
        old_ids = [r[0] for r in conn.execute(
            "SELECT id FROM documents WHERE source_path = ?", (fp,)
        ).fetchall()]
        if old_ids:
            placeholders = ",".join("?" * len(old_ids))
            conn.execute(f"DELETE FROM document_embeddings WHERE id IN ({placeholders})", old_ids)
            conn.execute(f"DELETE FROM documents WHERE id IN ({placeholders})", old_ids)
            removed += len(old_ids)
        conn.execute("DELETE FROM file_freshness WHERE file_path = ?", (fp,))
    conn.commit()
    conn.close()
    log.info("Removed %d chunks for %d deleted files", removed, len(file_paths))
    return removed
