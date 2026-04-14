"""Ingest markdown archives into SQLite with embeddings."""

import csv
import re
import sqlite3
import struct
import asyncio
from pathlib import Path

from config import (
    DB_PATH, DATA_DIR, GMAIL_DIR, DRIVE_DIR, TIMELINE_FILE, CONTEXT_FILE, EMBED_DIM,
    WORK_DIR, CAREER_DIR, FINANCE_DIR, PROJECTS_DIR, GOALS_DIR, LEARNING_DIR,
    SIDE_PROJECT_DIRS, KHALIL_DIR, WORK_PROJECT_DOCS, WORK_PROJECT_FILES,
    CURSOR_TRANSCRIPTS_DIR, CURSOR_CATALOG_FILE,
)
from knowledge.embedder import embed_batch


def init_db() -> sqlite3.Connection:
    """Initialize SQLite database with sqlite-vec for vector search."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.enable_load_extension(True)
    import sqlite_vec
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    # Enable WAL mode for concurrent read/write without "database is locked" errors
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_documents_category ON documents(category);
        CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source);

        CREATE TABLE IF NOT EXISTS pending_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action_type TEXT NOT NULL,
            description TEXT NOT NULL,
            payload TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            resolved_at TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            due_at TIMESTAMP NOT NULL,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            fired_at TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_reminders_status ON reminders(status);
        CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(due_at);

        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            action_type TEXT NOT NULL,
            description TEXT NOT NULL,
            payload TEXT,
            result TEXT,
            autonomy_level TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp ON audit_log(timestamp);

        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            message_type TEXT DEFAULT 'text',
            metadata TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_conversations_chat ON conversations(chat_id, timestamp);

        -- Self-improvement: interaction signals for reflection analysis
        CREATE TABLE IF NOT EXISTS interaction_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_type TEXT NOT NULL,
            context TEXT,
            value REAL DEFAULT 1.0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_signals_type ON interaction_signals(signal_type);
        CREATE INDEX IF NOT EXISTS idx_signals_created ON interaction_signals(created_at);

        -- Self-improvement: LLM-generated insights from periodic reflection
        CREATE TABLE IF NOT EXISTS insights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            summary TEXT NOT NULL,
            evidence TEXT,
            recommendation TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            resolved_at TIMESTAMP,
            resolved_by TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_insights_status ON insights(status);

        -- Self-improvement: active preferences derived from insights
        CREATE TABLE IF NOT EXISTS learned_preferences (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            source_insight_id INTEGER,
            confidence REAL DEFAULT 0.5,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS recurring_reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            cron_expression TEXT NOT NULL,
            next_fire_at TIMESTAMP NOT NULL,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_recurring_status ON recurring_reminders(status);
        CREATE INDEX IF NOT EXISTS idx_recurring_next ON recurring_reminders(next_fire_at);

        -- M9: Adaptive autonomy — learned approval patterns
        CREATE TABLE IF NOT EXISTS approval_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action_type TEXT NOT NULL,
            command_pattern TEXT NOT NULL,
            approved_count INTEGER DEFAULT 0,
            denied_count INTEGER DEFAULT 0,
            auto_tier TEXT DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(action_type, command_pattern)
        );

        CREATE INDEX IF NOT EXISTS idx_approval_patterns_action ON approval_patterns(action_type);

        -- M9: Adaptive autonomy — activity timing signals
        CREATE TABLE IF NOT EXISTS activity_timing (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_type TEXT NOT NULL,
            hour INTEGER NOT NULL,
            day_of_week INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_activity_timing ON activity_timing(signal_type, day_of_week, hour);

        -- Follow-up persistence: track surfaced alerts and nudge if unaddressed
        CREATE TABLE IF NOT EXISTS follow_ups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            source TEXT NOT NULL,
            summary TEXT NOT NULL,
            action_type TEXT,
            payload TEXT DEFAULT '{}',
            status TEXT DEFAULT 'pending',
            follow_up_at TEXT,
            nudge_count INTEGER DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_follow_ups_status ON follow_ups(status);
        CREATE INDEX IF NOT EXISTS idx_follow_ups_at ON follow_ups(follow_up_at);

        -- Conversation memory: rolling summaries for context continuity
        CREATE TABLE IF NOT EXISTS conversation_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            summary TEXT NOT NULL,
            message_range_start INTEGER NOT NULL,
            message_range_end INTEGER NOT NULL,
            message_count INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_conv_summaries_chat
            ON conversation_summaries(chat_id, created_at);

        -- Conversation memory: extracted facts, decisions, action items, preferences
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            memory_type TEXT NOT NULL,
            content TEXT NOT NULL,
            source_context TEXT,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(memory_type, status);

        -- Tool analytics: track tool usage, success rates, latency (#54)
        CREATE TABLE IF NOT EXISTS tool_analytics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tool_name TEXT NOT NULL,
            params TEXT,
            success BOOLEAN NOT NULL,
            latency_s REAL,
            error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_tool_analytics_name ON tool_analytics(tool_name, created_at);
        CREATE INDEX IF NOT EXISTS idx_tool_analytics_success ON tool_analytics(success, created_at);
    """)

    # Create virtual tables for vector search
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS document_embeddings USING vec0(id INTEGER PRIMARY KEY, embedding float[{EMBED_DIM}])"
    )
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS memory_embeddings USING vec0(id INTEGER PRIMARY KEY, embedding float[{EMBED_DIM}])"
    )

    # Migrations for existing databases
    try:
        conn.execute("SELECT message_type FROM conversations LIMIT 0")
    except Exception:
        conn.execute("ALTER TABLE conversations ADD COLUMN message_type TEXT DEFAULT 'text'")
        conn.execute("ALTER TABLE conversations ADD COLUMN metadata TEXT")

    # File freshness tracking for tiered re-indexing
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS file_freshness (
            file_path TEXT PRIMARY KEY,
            last_indexed_at REAL NOT NULL,
            last_mtime REAL NOT NULL,
            content_hash TEXT NOT NULL,
            chunk_count INTEGER DEFAULT 0,
            source TEXT,
            category TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_file_freshness_source ON file_freshness(source);
    """)

    # Migration: add source_path to documents for per-file delete-and-reinsert
    try:
        conn.execute("SELECT source_path FROM documents LIMIT 0")
    except Exception:
        conn.execute("ALTER TABLE documents ADD COLUMN source_path TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_source_path ON documents(source_path)")

    conn.commit()
    return conn


def serialize_float32(vec: list[float]) -> bytes:
    """Serialize a float vector to bytes for sqlite-vec."""
    return struct.pack(f"{len(vec)}f", *vec)


def chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """Split text into overlapping chunks by character count."""
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]
        if chunk.strip():
            chunks.append(chunk.strip())
        start = end - overlap
    return chunks


def parse_email_file(filepath: Path) -> list[dict]:
    """Parse a markdown email archive into individual entries."""
    content = filepath.read_text(encoding="utf-8")
    entries = []

    # Split by email separator (### or ---)
    parts = re.split(r"\n---\n", content)

    current_entry = None
    for part in parts:
        part = part.strip()
        if not part:
            continue

        # Look for email headers
        subject_match = re.search(r"###\s+(.+)", part)
        from_match = re.search(r"\*\*From\*\*:\s*(.+)", part)
        date_match = re.search(r"\*\*Date\*\*:\s*(.+)", part)

        if subject_match:
            title = subject_match.group(1).strip()
            from_addr = from_match.group(1).strip() if from_match else ""
            date = date_match.group(1).strip() if date_match else ""
            # Get the snippet (blockquote text)
            snippet_match = re.search(r">\s*(.+)", part)
            snippet = snippet_match.group(1).strip() if snippet_match else ""

            entries.append({
                "title": title,
                "content": f"From: {from_addr}\nDate: {date}\n{title}\n{snippet}",
                "metadata": f"from={from_addr}; date={date}",
            })
        elif len(part) > 50:
            # Non-email content chunk (e.g. headers, summaries)
            entries.append({
                "title": part[:80],
                "content": part,
                "metadata": "",
            })

    return entries


def parse_markdown_file(filepath: Path) -> list[dict]:
    """Parse a general markdown file into sections."""
    content = filepath.read_text(encoding="utf-8")
    entries = []

    # Split by headers
    sections = re.split(r"\n(?=#{1,3}\s)", content)
    for section in sections:
        section = section.strip()
        if not section or len(section) < 30:
            continue

        header_match = re.match(r"#{1,3}\s+(.+)", section)
        title = header_match.group(1).strip() if header_match else section[:80]

        # For long sections, chunk them
        if len(section) > 600:
            chunks = chunk_text(section)
            for i, chunk in enumerate(chunks):
                entries.append({
                    "title": f"{title} (part {i+1})",
                    "content": chunk,
                    "metadata": f"file={filepath.name}",
                })
        else:
            entries.append({
                "title": title,
                "content": section,
                "metadata": f"file={filepath.name}",
            })

    return entries


def parse_csv_file(filepath: Path) -> list[dict]:
    """Parse a CSV file into one document per row (e.g. sprint planning data).

    Title = Description column, content = all columns as key: value pairs.
    Skips rows with #NAME? errors or entirely empty rows.
    """
    entries = []
    with open(filepath, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Skip error rows (#NAME?, #REF!)
            values = list(row.values())
            if any("#NAME?" in str(v) for v in values if v):
                continue

            # Use Description column as title
            title = (
                row.get("Description of Work (Squad Internal)", "").strip()
                or row.get("Groove Title", "").strip()
            )
            if not title:
                continue

            # Build content as key: value pairs (skip empty values)
            content_lines = []
            for k, v in row.items():
                v = (v or "").strip()
                if v and "#NAME?" not in v and "#REF!" not in v:
                    content_lines.append(f"{k}: {v}")

            if content_lines:
                entries.append({
                    "title": title[:200],
                    "content": "\n".join(content_lines),
                    "metadata": f"file={filepath.name}",
                })

    return entries


# Category mapping for repo content directories
_REPO_DIRS = {
    "work": WORK_DIR,
    "career": CAREER_DIR,
    "finance": FINANCE_DIR,
    "projects": PROJECTS_DIR,
    "goals": GOALS_DIR,
    "learning": LEARNING_DIR,
}


def _categorize_repo_file(filepath: Path) -> tuple[str, str]:
    """Determine (source, category) for a repo content file based on its path."""
    parts = filepath.parts
    # Find which top-level directory this belongs to
    for dirname in ("work", "career", "finance", "projects", "goals", "learning"):
        if dirname in parts:
            break
    else:
        return "repo", "repo:other"

    name = filepath.stem.lower()

    if dirname == "work":
        return "work", "work:employer"
    elif dirname == "career":
        if "resume" in name:
            return "career", "career:resume"
        elif any(k in name for k in ("narrative", "profile", "linkedin")):
            return "career", "career:narrative"
        elif "cover" in str(filepath).lower():
            return "career", "career:cover-letter"
        elif any(k in name for k in ("interview", "prep")):
            return "career", "career:interview"
        return "career", "career:general"
    elif dirname == "finance":
        if "portfolio" in name:
            return "finance", "finance:portfolio"
        elif "rsu" in name or "wts" in name:
            return "finance", "finance:rsu"
        return "finance", "finance:general"
    elif dirname == "projects":
        if "zia" in name:
            return "projects", "projects:zia"
        elif "bezier" in name:
            return "projects", "projects:bezier"
        elif "tiny" in name or "tiny-grounds" in str(filepath):
            return "projects", "projects:tiny-grounds"
        return "projects", f"projects:{name}"
    elif dirname == "goals":
        return "goals", "goals:annual"
    elif dirname == "learning":
        return "learning", f"learning:{name}"

    return "repo", f"repo:{dirname}"


def load_cursor_catalog(catalog_path: Path) -> dict[str, dict]:
    """Parse the cursor-conversations.md catalog to map conversation IDs to metadata.

    Returns {short_id: {title, workspace, date}} where short_id is the first 8 chars
    of the conversation UUID.
    """
    if not catalog_path.exists():
        return {}

    catalog = {}
    content = catalog_path.read_text(encoding="utf-8")
    current_date = ""

    for line in content.splitlines():
        # Date headers like "#### 2026-03-14"
        date_match = re.match(r"####\s+(\d{4}-\d{2}-\d{2})", line)
        if date_match:
            current_date = date_match.group(1)
            continue

        # Table rows with conversation data: | # | **Title** | Workspace | Msgs | Size | `id` |
        row_match = re.match(
            r"\|\s*\d+\s*\|\s*\*\*(.+?)\*\*\s*\|\s*(\w+)\s*\|.*\|\s*`(\w+)`\s*\|",
            line,
        )
        if row_match:
            title = row_match.group(1).strip()
            workspace = row_match.group(2).strip()
            short_id = row_match.group(3).strip()
            catalog[short_id] = {
                "title": title,
                "workspace": workspace,
                "date": current_date,
            }

    return catalog


def parse_cursor_transcript(
    filepath: Path, title: str, workspace: str, date: str,
) -> list[dict]:
    """Parse a Cursor agent transcript JSONL into conversation-segment chunks.

    Groups consecutive messages into ~2000-char segments that preserve
    conversational coherence (user question + assistant answer together).
    Filters out short noise messages (<50 chars).
    """
    import json as _json

    messages = []
    with open(filepath, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msg = _json.loads(line)
            except _json.JSONDecodeError:
                continue

            role = msg.get("role", "")
            content_parts = msg.get("message", {}).get("content", [])
            text_parts = []
            for part in content_parts:
                if isinstance(part, dict) and part.get("type") == "text":
                    text_parts.append(part.get("text", ""))

            text = "\n".join(text_parts).strip()
            if not text:
                continue

            # Strip <user_query> tags
            text = re.sub(r"</?user_query>", "", text).strip()

            # Skip noise: very short messages
            if len(text) < 50:
                continue

            prefix = "User" if role == "user" else "Assistant"
            messages.append(f"{prefix}: {text}")

    if not messages:
        return []

    # Group into ~2000-char segments respecting message boundaries
    entries = []
    segment = []
    segment_len = 0
    part_num = 1
    conv_id = filepath.stem[:8]

    for msg in messages:
        msg_len = len(msg)
        if segment and segment_len + msg_len > 2000:
            # Flush current segment
            entries.append({
                "title": f"{title} (part {part_num})" if len(messages) > 5 else title,
                "content": "\n\n".join(segment),
                "metadata": f"workspace={workspace}; date={date}; conversation_id={conv_id}",
            })
            segment = []
            segment_len = 0
            part_num += 1

        segment.append(msg)
        segment_len += msg_len

    # Flush remaining
    if segment:
        entries.append({
            "title": f"{title} (part {part_num})" if part_num > 1 else title,
            "content": "\n\n".join(segment),
            "metadata": f"workspace={workspace}; date={date}; conversation_id={conv_id}",
        })

    return entries


async def index_source(conn: sqlite3.Connection, source: str, category: str, entries: list[dict],
                       source_path: str | None = None):
    """Index entries: store in documents table and generate embeddings."""
    if not entries:
        return 0

    # Insert documents
    doc_ids = []
    for entry in entries:
        cursor = conn.execute(
            "INSERT INTO documents (source, category, title, content, metadata, source_path) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (source, category, entry["title"], entry["content"], entry.get("metadata", ""), source_path),
        )
        doc_ids.append(cursor.lastrowid)

    # Generate embeddings in batch
    texts = [e["content"] for e in entries]
    embeddings = await embed_batch(texts)

    # Store embeddings
    for doc_id, embedding in zip(doc_ids, embeddings):
        conn.execute(
            "INSERT INTO document_embeddings (id, embedding) VALUES (?, ?)",
            (doc_id, serialize_float32(embedding)),
        )

    conn.commit()
    return len(entries)


async def reindex_files(file_paths: list[str], max_files: int = 20) -> dict:
    """Reindex specific files: delete old chunks, parse, embed, insert.

    Used by the freshness watcher (Tier 2 polling) and webhook handler (Tier 1).
    Content hash check skips files that were touched but not changed.

    Returns {"indexed": N, "skipped": N, "errors": N, "queued": [...]}
    """
    import hashlib
    import time as _time

    conn = init_db()
    result = {"indexed": 0, "skipped": 0, "errors": 0, "queued": []}

    SUPPORTED = {".md", ".csv"}
    to_process = [p for p in file_paths if Path(p).suffix in SUPPORTED and Path(p).exists()]

    # Rate limit: queue excess files for next cycle
    if len(to_process) > max_files:
        result["queued"] = to_process[max_files:]
        to_process = to_process[:max_files]

    for fp in to_process:
        try:
            filepath = Path(fp)
            content = filepath.read_text(encoding="utf-8", errors="replace")
            content_hash = hashlib.sha256(content.encode()).hexdigest()

            # Check if content actually changed
            row = conn.execute(
                "SELECT content_hash FROM file_freshness WHERE file_path = ?", (fp,)
            ).fetchone()
            if row and row[0] == content_hash:
                result["skipped"] += 1
                continue

            # Delete old chunks for this file
            old_ids = [r[0] for r in conn.execute(
                "SELECT id FROM documents WHERE source_path = ?", (fp,)
            ).fetchall()]
            if old_ids:
                placeholders = ",".join("?" * len(old_ids))
                conn.execute(f"DELETE FROM document_embeddings WHERE id IN ({placeholders})", old_ids)
                conn.execute(f"DELETE FROM documents WHERE id IN ({placeholders})", old_ids)

            # Parse file
            if filepath.suffix == ".md":
                entries = parse_markdown_file(filepath)
            elif filepath.suffix == ".csv":
                entries = parse_csv_file(filepath)
            else:
                continue

            # Categorize
            source, category = _categorize_repo_file(filepath)

            # Index with source_path
            n = await index_source(conn, source, category, entries, source_path=fp)

            # Update freshness tracking
            conn.execute(
                "INSERT OR REPLACE INTO file_freshness "
                "(file_path, last_indexed_at, last_mtime, content_hash, chunk_count, source, category) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (fp, _time.time(), filepath.stat().st_mtime, content_hash, n, source, category),
            )
            conn.commit()
            result["indexed"] += 1

        except Exception as e:
            log.warning("reindex_files: error on %s: %s", fp, e)
            result["errors"] += 1

    return result


async def index_all(force: bool = False):
    """Index all archive sources into the database."""
    conn = init_db()

    # Check if already indexed
    count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    if count > 0 and not force:
        print(f"Database already has {count} documents. Use force=True to re-index.")
        return conn

    if force:
        conn.execute("DELETE FROM documents")
        conn.execute("DELETE FROM document_embeddings")
        conn.commit()

    total = 0

    # Index Gmail archives
    if GMAIL_DIR.exists():
        for md_file in sorted(GMAIL_DIR.rglob("*.md")):
            category = str(md_file.relative_to(GMAIL_DIR)).replace(".md", "").replace("/", ":")
            entries = parse_email_file(md_file)
            n = await index_source(conn, "gmail", f"email:{category}", entries)
            print(f"  gmail/{category}: {n} entries")
            total += n

    # Index Drive index
    if DRIVE_DIR.exists():
        for md_file in sorted(DRIVE_DIR.glob("*.md")):
            entries = parse_markdown_file(md_file)
            n = await index_source(conn, "drive", "drive:index", entries)
            print(f"  drive/{md_file.name}: {n} entries")
            total += n

    # Index timeline
    if TIMELINE_FILE.exists():
        entries = parse_markdown_file(TIMELINE_FILE)
        n = await index_source(conn, "timeline", "life:timeline", entries)
        print(f"  timeline: {n} entries")
        total += n

    # Index CONTEXT.md
    if CONTEXT_FILE.exists():
        entries = parse_markdown_file(CONTEXT_FILE)
        n = await index_source(conn, "context", "personal:context", entries)
        print(f"  context: {n} entries")
        total += n

    # Index repo content directories (work, career, finance, projects)
    for dirname, repo_dir in _REPO_DIRS.items():
        if not repo_dir.exists():
            continue
        for md_file in sorted(repo_dir.rglob("*.md")):
            source, category = _categorize_repo_file(md_file)
            entries = parse_markdown_file(md_file)
            n = await index_source(conn, source, category, entries)
            print(f"  {source}/{md_file.name}: {n} entries")
            total += n
        # CSV files (sprint planning, etc.)
        for csv_file in sorted(repo_dir.rglob("*.csv")):
            entries = parse_csv_file(csv_file)
            n = await index_source(conn, dirname, "work:planning", entries)
            print(f"  {dirname}/{csv_file.name} (csv): {n} entries")
            total += n

    # Index side project READMEs
    for project_dir in SIDE_PROJECT_DIRS:
        if not project_dir.exists():
            continue
        readme = project_dir / "README.md"
        if readme.exists():
            entries = parse_markdown_file(readme)
            proj_name = project_dir.name.lower()
            n = await index_source(conn, "side_project", f"projects:{proj_name}", entries)
            print(f"  side_project/{proj_name}: {n} entries")
            total += n

    # Index Khalil's own capabilities
    khalil_readme = KHALIL_DIR / "README.md"
    if khalil_readme.exists():
        entries = parse_markdown_file(khalil_readme)
        n = await index_source(conn, "khalil", "khalil:capabilities", entries)
        print(f"  khalil/capabilities: {n} entries")
        total += n

    # Index work project documentation
    _SKIP_PARTS = {'.git', '.github', 'node_modules', 'out', '__pycache__', '.next'}
    for project_dir, category_prefix in WORK_PROJECT_DOCS:
        if not project_dir.exists():
            continue
        for md_file in sorted(project_dir.rglob("*.md")):
            if any(part in _SKIP_PARTS or part.startswith('.') for part in md_file.parts):
                continue
            # For CM_Backend, only index files under */docs/
            if "CM_Backend" in str(project_dir) and "/docs/" not in str(md_file):
                continue
            rel = md_file.relative_to(project_dir)
            subcategory = f"{category_prefix}:{rel.parent}" if rel.parent != Path('.') else category_prefix
            entries = parse_markdown_file(md_file)
            n = await index_source(conn, "work_project", subcategory, entries)
            print(f"  work_project/{rel}: {n} entries")
            total += n

    # Index standalone work project files
    for filepath, category in WORK_PROJECT_FILES:
        if filepath.exists():
            entries = parse_markdown_file(filepath)
            n = await index_source(conn, "work_project", category, entries)
            print(f"  work_project/{filepath.name}: {n} entries")
            total += n

    # Index Cursor conversation transcripts
    if CURSOR_TRANSCRIPTS_DIR.exists():
        catalog = load_cursor_catalog(CURSOR_CATALOG_FILE)
        for jsonl in sorted(CURSOR_TRANSCRIPTS_DIR.rglob("*/agent-transcripts/*/*.jsonl")):
            conv_id = jsonl.stem[:8]
            meta = catalog.get(conv_id, {})
            conv_title = meta.get("title", conv_id)
            ws = meta.get("workspace", "unknown")
            dt = meta.get("date", "")
            entries = parse_cursor_transcript(jsonl, conv_title, ws, dt)
            category = f"cursor:{ws.lower()}"
            n = await index_source(conn, "cursor", category, entries)
            print(f"  cursor/{ws}/{conv_id}: {n} entries")
            total += n

    print(f"\nTotal indexed: {total} documents")
    return conn


async def index_incremental():
    """Index only archive files modified since the last indexing run.

    Checks file mtime against a stored timestamp in settings.
    Much faster than index_all(force=True) for regular refreshes.
    """
    conn = init_db()

    # Get last index timestamp
    row = conn.execute("SELECT value FROM settings WHERE key = 'last_index_time'").fetchone()
    last_index = float(row[0]) if row else 0.0

    import time
    now = time.time()
    total = 0

    def _should_index(filepath: Path) -> bool:
        return filepath.stat().st_mtime > last_index

    # Index Gmail archives (only modified files)
    if GMAIL_DIR.exists():
        for md_file in sorted(GMAIL_DIR.rglob("*.md")):
            if not _should_index(md_file):
                continue
            category = str(md_file.relative_to(GMAIL_DIR)).replace(".md", "").replace("/", ":")
            entries = parse_email_file(md_file)
            n = await index_source(conn, "gmail", f"email:{category}", entries)
            print(f"  gmail/{category}: {n} entries (updated)")
            total += n

    # Index Drive index (only modified files)
    if DRIVE_DIR.exists():
        for md_file in sorted(DRIVE_DIR.glob("*.md")):
            if not _should_index(md_file):
                continue
            entries = parse_markdown_file(md_file)
            n = await index_source(conn, "drive", "drive:index", entries)
            print(f"  drive/{md_file.name}: {n} entries (updated)")
            total += n

    # Index timeline (if modified)
    if TIMELINE_FILE.exists() and _should_index(TIMELINE_FILE):
        entries = parse_markdown_file(TIMELINE_FILE)
        n = await index_source(conn, "timeline", "life:timeline", entries)
        print(f"  timeline: {n} entries (updated)")
        total += n

    # Index CONTEXT.md (if modified)
    if CONTEXT_FILE.exists() and _should_index(CONTEXT_FILE):
        entries = parse_markdown_file(CONTEXT_FILE)
        n = await index_source(conn, "context", "personal:context", entries)
        print(f"  context: {n} entries (updated)")
        total += n

    # Index repo content directories (only modified files)
    for dirname, repo_dir in _REPO_DIRS.items():
        if not repo_dir.exists():
            continue
        for md_file in sorted(repo_dir.rglob("*.md")):
            if not _should_index(md_file):
                continue
            source, category = _categorize_repo_file(md_file)
            entries = parse_markdown_file(md_file)
            n = await index_source(conn, source, category, entries)
            print(f"  {source}/{md_file.name}: {n} entries (updated)")
            total += n
        # CSV files (only modified)
        for csv_file in sorted(repo_dir.rglob("*.csv")):
            if not _should_index(csv_file):
                continue
            entries = parse_csv_file(csv_file)
            n = await index_source(conn, dirname, "work:planning", entries)
            print(f"  {dirname}/{csv_file.name} (csv): {n} entries (updated)")
            total += n

    # Index side project READMEs (only modified)
    for project_dir in SIDE_PROJECT_DIRS:
        if not project_dir.exists():
            continue
        readme = project_dir / "README.md"
        if readme.exists() and _should_index(readme):
            entries = parse_markdown_file(readme)
            proj_name = project_dir.name.lower()
            n = await index_source(conn, "side_project", f"projects:{proj_name}", entries)
            print(f"  side_project/{proj_name}: {n} entries (updated)")
            total += n

    # Index Khalil capabilities (only if modified)
    khalil_readme = KHALIL_DIR / "README.md"
    if khalil_readme.exists() and _should_index(khalil_readme):
        entries = parse_markdown_file(khalil_readme)
        n = await index_source(conn, "khalil", "khalil:capabilities", entries)
        print(f"  khalil/capabilities: {n} entries (updated)")
        total += n

    # Index work project documentation (only modified files)
    _SKIP_PARTS = {'.git', '.github', 'node_modules', 'out', '__pycache__', '.next'}
    for project_dir, category_prefix in WORK_PROJECT_DOCS:
        if not project_dir.exists():
            continue
        for md_file in sorted(project_dir.rglob("*.md")):
            if not _should_index(md_file):
                continue
            if any(part in _SKIP_PARTS or part.startswith('.') for part in md_file.parts):
                continue
            if "CM_Backend" in str(project_dir) and "/docs/" not in str(md_file):
                continue
            rel = md_file.relative_to(project_dir)
            subcategory = f"{category_prefix}:{rel.parent}" if rel.parent != Path('.') else category_prefix
            entries = parse_markdown_file(md_file)
            n = await index_source(conn, "work_project", subcategory, entries)
            print(f"  work_project/{rel}: {n} entries (updated)")
            total += n

    # Index standalone work project files (only if modified)
    for filepath, category in WORK_PROJECT_FILES:
        if filepath.exists() and _should_index(filepath):
            entries = parse_markdown_file(filepath)
            n = await index_source(conn, "work_project", category, entries)
            print(f"  work_project/{filepath.name}: {n} entries (updated)")
            total += n

    # Index Cursor conversation transcripts (only modified files)
    if CURSOR_TRANSCRIPTS_DIR.exists():
        catalog = load_cursor_catalog(CURSOR_CATALOG_FILE)
        for jsonl in sorted(CURSOR_TRANSCRIPTS_DIR.rglob("*/agent-transcripts/*/*.jsonl")):
            if not _should_index(jsonl):
                continue
            conv_id = jsonl.stem[:8]
            meta = catalog.get(conv_id, {})
            conv_title = meta.get("title", conv_id)
            ws = meta.get("workspace", "unknown")
            dt = meta.get("date", "")
            entries = parse_cursor_transcript(jsonl, conv_title, ws, dt)
            category = f"cursor:{ws.lower()}"
            n = await index_source(conn, "cursor", category, entries)
            print(f"  cursor/{ws}/{conv_id}: {n} entries (updated)")
            total += n

    # Update last index timestamp
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('last_index_time', ?)",
        (str(now),),
    )
    conn.commit()

    print(f"\nIncremental index: {total} new documents")
    return conn


if __name__ == "__main__":
    import sys
    if "--incremental" in sys.argv:
        asyncio.run(index_incremental())
    else:
        asyncio.run(index_all(force=True))
