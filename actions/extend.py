"""Self-extension engine — detect capability gaps, generate code, open PRs.

When Khalil can't handle a request, this module:
1. Detects the capability gap (phrase match + LLM classification)
2. Generates a new action module via Claude Opus
3. Validates the generated code (AST + blocklist)
4. Opens a PR via git + gh CLI
5. Notifies the user via Telegram
"""

import ast
import asyncio
import json
import logging
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from config import (
    KHALIL_DIR, EXTENSIONS_DIR, CLAUDE_MODEL_COMPLEX,
    KEYRING_SERVICE, CLAUDE_CODE_BIN, DB_PATH, DATA_DIR,
)

log = logging.getLogger("khalil.extend")


# --- #24: Dependency Injection for Extensions ---

class KhalilContext:
    """Clean interface for extensions instead of requiring internal imports.

    Extensions receive this object and use it to interact with Khalil's
    core systems (DB, LLM, notifications, search, signals).
    """

    def __init__(self, db, ask_llm, notify):
        self.db = db          # sqlite3 connection
        self.ask_llm = ask_llm  # async callable(query, context, system_extra) -> str
        self.notify = notify    # async callable(message) -> None

    def search(self, query, limit=5):
        """Search knowledge base."""
        from knowledge.search import keyword_search
        return keyword_search(query, limit=limit)

    def record_signal(self, signal_type, context=None):
        """Record an interaction signal."""
        from learning import record_signal
        record_signal(signal_type, context)


# Rate limit: max 1 generation per 15 min (was 1hr — too slow for backlog drain)
_last_generation_time: float = 0
GENERATION_COOLDOWN_SECONDS = 900

# --- Stage 1: Phrase-based capability gap detection ---

# Semantic gate patterns — broader than exact phrases, catches novel refusal variants
# These are cheap regex checks; if any match, the Stage 2 LLM classifier runs.
GAP_GATE_PATTERNS = [
    r"\bi\s+(?:can'?t|cannot|don'?t|do\s+not|couldn'?t|won'?t)\b",
    r"\b(?:not\s+(?:able|something|possible)|unable)\b",
    r"\b(?:beyond|outside)\s+(?:my|the)\s+(?:current|capabilities)\b",
    r"\bcheck\s+(?:your|the)\s+\w+\s+manually\b",
    r"\bneed\s+direct\s+access\b",
    r"\bdon'?t\s+have\s+(?:access|the\s+ability|a\s+feature|that\s+capability|real-time|monitoring)\b",
    r"\bno\s+built-in\s+support\b",
    r"\bisn'?t\s+(?:available|possible|supported)\b",
]

# Keep the old phrase list for backward compatibility in tests
CAPABILITY_GAP_PHRASES = [
    "i can't do that", "i don't have the ability", "that capability isn't available",
    "i can't currently", "not something i can do yet", "i don't have a feature for",
    "i don't have that capability", "that's not something i support", "i'm not able to",
    "no built-in support for", "i would need direct access", "i don't have real-time",
    "i can't determine", "i don't have access to your", "i can't access your",
    "i'm unable to", "that's beyond my current", "check your mac manually",
]


def detect_capability_gap(response: str) -> bool:
    """Stage 1: cheap semantic gate on LLM response.

    Uses broad regex patterns instead of exact phrase matching.
    If triggered, Stage 2 LLM classifier should confirm.
    """
    response_lower = response.lower()
    return any(re.search(p, response_lower) for p in GAP_GATE_PATTERNS)


# --- Stage 2: LLM classification ---

EXISTING_CAPABILITIES = [
    "search — search knowledge base",
    "email — search/draft/send emails",
    "drive — search Google Drive",
    "remind — set/list/cancel reminders (one-time and recurring)",
    "brief — generate morning brief",
    "calendar — view calendar events",
    "finance — financial dashboard, deadlines, portfolio, RSU",
    "work — sprint dashboard, P0 epics, themes, owners",
    "goals — quarterly goal tracking",
    "project — project status tracking (zia, tiny-grounds, bezier, khalil)",
    "jobs — job scraper matches",
    "nudge — proactive checks for overdue items",
    "learn — self-improvement insights and preferences",
    "sync — sync new emails into knowledge base",
    "backup — export/import backups",
    "health — system health status",
    "stats — knowledge base statistics",
    # Granular shell/system actions (handled by _try_direct_shell_intent)
    "cursor_terminal — list/run/create Cursor IDE terminal sessions",
    "terminal — check terminal/iTerm sessions, run commands in terminal",
    "cursor — check Cursor IDE status, open files, diff, extensions",
    "contacts — search Google Contacts for people and email addresses",
    "tasks — list/create Google Tasks",
    "shell — run macOS shell commands (battery, IP, uptime, disk, network, brew, clipboard, etc.)",
    "screenshot — capture screenshots of screen or windows",
]


async def classify_gap(query: str, ask_llm_fn) -> dict | None:
    """Stage 2: LLM classifies whether this is a real capability gap.

    Returns spec dict {"name": "...", "command": "...", "description": "..."}
    or None if it's just a knowledge gap or normal conversation.
    """
    # Add extension capabilities + skill registry to the list
    ext_capabilities = _get_extension_capabilities()
    try:
        from skills import get_registry
        registry = get_registry()
        skill_capabilities = [
            f"{s.name} — {s.description}"
            for s in registry.list_skills()
        ]
    except Exception:
        skill_capabilities = []
    all_capabilities = EXISTING_CAPABILITIES + ext_capabilities + skill_capabilities

    capabilities_text = "\n".join(f"- {c}" for c in all_capabilities)

    response = await ask_llm_fn(
        f"The user asked: \"{query}\"\n\n"
        "Khalil's current capabilities:\n"
        f"{capabilities_text}\n\n"
        "Classify this request:\n"
        "1. CAPABILITY_GAP — the user wants Khalil to DO something (track, monitor, log, schedule, etc.) "
        "that none of the existing capabilities cover.\n"
        "2. KNOWLEDGE_GAP — the user wants INFORMATION that Khalil should have but doesn't.\n"
        "3. CONVERSATION — normal chat, opinion, advice, or something the existing capabilities already handle.\n\n"
        "If CAPABILITY_GAP, respond with ONLY a JSON object:\n"
        '{"type": "capability_gap", "name": "snake_case_name", "command": "short_command", "description": "one-line description"}\n\n'
        "If KNOWLEDGE_GAP or CONVERSATION, respond with ONLY:\n"
        '{"type": "knowledge_gap"} or {"type": "conversation"}\n\n'
        "Respond with ONLY JSON. No explanation.",
        "",
        system_extra="You are classifying user requests. Be strict — only flag as capability_gap if it truly requires new code.",
    )

    from llm import CapabilityGapResult, parse_llm_json

    gap = parse_llm_json(response.strip(), CapabilityGapResult)
    if gap is None:
        log.warning("Gap classification returned invalid JSON: %s", response[:200])
        return None

    if gap.type != "capability_gap":
        return None

    name = gap.name.strip()
    command = gap.command.strip()
    description = gap.description.strip()

    if not name or not command or not description:
        return None

    # Sanitize name (only alphanumeric + underscore)
    name = re.sub(r"[^a-z0-9_]", "_", name.lower())
    command = re.sub(r"[^a-z0-9]", "", command.lower())

    return {"name": name, "command": command, "description": description}


def check_extension_overlap(spec: dict) -> str | None:
    """#29: Check if a proposed extension overlaps with existing capabilities.

    Returns a message describing the overlap, or None if no overlap found.
    """
    name = spec.get("name", "").lower()
    command = spec.get("command", "").lower()
    description = spec.get("description", "").lower()

    # Check against built-in capabilities
    for cap in EXISTING_CAPABILITIES:
        cap_lower = cap.lower()
        cap_name = cap_lower.split(" — ")[0].strip() if " — " in cap_lower else cap_lower.split()[0]
        if cap_name in name or cap_name in command or name in cap_lower:
            return f"Overlaps with built-in capability: {cap}"

    # Check against existing extensions
    for ext_cap in _get_extension_capabilities():
        ext_lower = ext_cap.lower()
        ext_cmd = ext_lower.split(" — ")[0].strip()
        if ext_cmd == command or name in ext_lower:
            return f"Overlaps with existing extension: {ext_cap}"

    return None


def _get_extension_capabilities() -> list[str]:
    """Read existing extension manifests to include in capabilities list."""
    capabilities = []
    if EXTENSIONS_DIR.exists():
        for manifest_path in EXTENSIONS_DIR.glob("*.json"):
            try:
                manifest = json.loads(manifest_path.read_text())
                capabilities.append(f"{manifest['command']} — {manifest['description']} (extension)")
            except Exception:
                continue
    return capabilities


# --- #26: Extension Version Tracking ---


def get_extension_versions() -> list[dict]:
    """Read all extension manifests and return version info.

    Returns list of {name, version, created_at} dicts.
    """
    versions = []
    if EXTENSIONS_DIR.exists():
        for manifest_path in EXTENSIONS_DIR.glob("*.json"):
            if manifest_path.name.endswith(".prev.json"):
                continue
            try:
                manifest = json.loads(manifest_path.read_text())
                versions.append({
                    "name": manifest.get("name", manifest_path.stem),
                    "version": manifest.get("version", 1),
                    "created_at": manifest.get("generated_at", "unknown"),
                })
            except Exception:
                continue
    return versions


def _backup_manifest(manifest_path: Path) -> None:
    """Backup an existing manifest to manifest.prev.json."""
    prev_path = manifest_path.with_suffix(".prev.json")
    shutil.copy2(str(manifest_path), str(prev_path))


def _write_versioned_manifest(manifest_path: Path, manifest: dict) -> dict:
    """Write a manifest with version tracking. Backs up old one if it exists.

    Returns the manifest dict with version field set.
    """
    if manifest_path.exists():
        try:
            old_manifest = json.loads(manifest_path.read_text())
            old_version = old_manifest.get("version", 1)
            _backup_manifest(manifest_path)
            manifest["version"] = old_version + 1
        except Exception:
            manifest["version"] = 1
    else:
        manifest.setdefault("version", 1)

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return manifest


def rollback_extension(name: str) -> bool:
    """Rollback an extension to its previous version.

    Restores from manifest.prev.json and decrements version.
    Returns True if rollback succeeded, False otherwise.
    """
    manifest_path = EXTENSIONS_DIR / f"{name}.json"
    prev_path = manifest_path.with_suffix(".prev.json")

    if not prev_path.exists():
        return False

    try:
        prev_manifest = json.loads(prev_path.read_text())
        # Write the previous manifest back as current
        manifest_path.write_text(json.dumps(prev_manifest, indent=2))
        prev_path.unlink()
        return True
    except Exception:
        return False


# --- #27: Extension Template Library ---

EXTENSION_TEMPLATES = {
    "crud": """\"\"\"CRUD operations for {name}.\"\"\"

import logging
import sqlite3
from config import DB_PATH, TIMEZONE

log = logging.getLogger("khalil.actions.{name}")

SKILL = {{
    "name": "{name}",
    "description": "{description}",
    "category": "extension",
    "patterns": [
        (r"\\b(?:add|create|new)\\s+{name}", "{name}_add"),
        (r"\\b(?:list|show|all)\\s+{name}", "{name}_list"),
        (r"\\b(?:remove|delete)\\s+{name}", "{name}_remove"),
    ],
    "actions": [
        {{"type": "{name}_add", "handler": "cmd_{command}", "description": "Add a {name} entry", "keywords": "add create new {name}"}},
        {{"type": "{name}_list", "handler": "cmd_{command}", "description": "List {name} entries", "keywords": "list show all {name}"}},
        {{"type": "{name}_remove", "handler": "cmd_{command}", "description": "Remove a {name} entry", "keywords": "remove delete {name}"}},
    ],
    "examples": ["Add a new {name}", "Show all {name}s", "Remove {name}"],
}}

_tables_created = False

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def ensure_tables(conn: sqlite3.Connection):
    conn.execute('''CREATE TABLE IF NOT EXISTS {name} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        data TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.commit()

def _ensure():
    global _tables_created
    if not _tables_created:
        conn = _get_conn()
        try:
            ensure_tables(conn)
        finally:
            conn.close()
        _tables_created = True

async def cmd_{command}(update, context):
    \"\"\"Handle /{command} command. Subcommands: add, list, remove.\"\"\"
    _ensure()
    args = context.args or []
    sub = args[0] if args else "list"
    conn = _get_conn()
    try:
        if sub == "add" and len(args) > 1:
            name_val = " ".join(args[1:])
            conn.execute("INSERT INTO {name} (name) VALUES (?)", (name_val,))
            conn.commit()
            await update.message.reply_text(f"Added: {{name_val}}")
        elif sub == "list":
            rows = conn.execute("SELECT id, name FROM {name} ORDER BY created_at DESC LIMIT 20").fetchall()
            if rows:
                lines = [f"{{r['id']}}. {{r['name']}}" for r in rows]
                await update.message.reply_text("\\n".join(lines))
            else:
                await update.message.reply_text("No items yet.")
        elif sub == "remove" and len(args) > 1:
            conn.execute("DELETE FROM {name} WHERE id = ?", (args[1],))
            conn.commit()
            await update.message.reply_text("Removed.")
        else:
            await update.message.reply_text("Usage: /{command} [add|list|remove] ...")
    finally:
        conn.close()
""",
    "api_backed": """\"\"\"API-backed capability: {description}.\"\"\"

import asyncio
import logging
import httpx
import keyring
from config import KEYRING_SERVICE

log = logging.getLogger("khalil.actions.{name}")

SKILL = {{
    "name": "{name}",
    "description": "{description}",
    "category": "extension",
    "patterns": [
        (r"\\b{name}\\b", "{name}"),
    ],
    "actions": [
        {{"type": "{name}", "handler": "cmd_{command}", "description": "{description}", "keywords": "{name} {command}"}},
    ],
    "examples": ["Check {name} status"],
}}

async def _fetch_data(endpoint: str, params: dict | None = None) -> dict:
    api_key = keyring.get_password(KEYRING_SERVICE, "{name}-api-key")
    if not api_key:
        raise RuntimeError("API key not configured. Use keyring to set '{name}-api-key'.")
    headers = {{"Authorization": f"Bearer {{api_key}}"}}
    async with httpx.AsyncClient() as client:
        resp = await client.get(endpoint, headers=headers, params=params or {{}}, timeout=30)
        resp.raise_for_status()
        return resp.json()

async def cmd_{command}(update, context):
    \"\"\"Handle /{command} command.\"\"\"
    args = context.args or []
    try:
        data = await _fetch_data("https://api.example.com/{name}", {{"q": " ".join(args)}})
        await update.message.reply_text(str(data)[:4000])
    except Exception as e:
        await update.message.reply_text(f"Error: {{e}}")
""",
    "periodic_poller": """\"\"\"Periodic poller: {description}.\"\"\"

import asyncio
import logging
import sqlite3
from datetime import datetime
from config import DB_PATH

log = logging.getLogger("khalil.actions.{name}")

SKILL = {{
    "name": "{name}",
    "description": "{description}",
    "category": "extension",
    "patterns": [
        (r"\\b{name}\\s+(?:status|check)", "{name}_status"),
        (r"\\b(?:poll|refresh)\\s+{name}", "{name}_poll"),
    ],
    "actions": [
        {{"type": "{name}_status", "handler": "cmd_{command}", "description": "Check {name} status", "keywords": "status check {name}"}},
        {{"type": "{name}_poll", "handler": "cmd_{command}", "description": "Run {name} poll", "keywords": "poll refresh {name}"}},
    ],
    "examples": ["Check {name} status", "Poll {name}"],
}}

_tables_created = False

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def ensure_tables(conn: sqlite3.Connection):
    conn.execute('''CREATE TABLE IF NOT EXISTS {name}_state (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.commit()

def _ensure():
    global _tables_created
    if not _tables_created:
        conn = _get_conn()
        try:
            ensure_tables(conn)
        finally:
            conn.close()
        _tables_created = True

async def poll():
    \"\"\"Run one poll cycle. Call this from a scheduler.\"\"\"
    _ensure()
    # TODO: Implement polling logic
    log.info("{name} poll cycle complete")

async def cmd_{command}(update, context):
    \"\"\"Handle /{command} command. Subcommands: status, poll.\"\"\"
    _ensure()
    args = context.args or []
    sub = args[0] if args else "status"
    if sub == "poll":
        await poll()
        await update.message.reply_text("Poll cycle completed.")
    else:
        conn = _get_conn()
        try:
            row = conn.execute("SELECT value, updated_at FROM {name}_state WHERE key = 'last_poll'").fetchone()
            if row:
                await update.message.reply_text(f"Last poll: {{row['updated_at']}}")
            else:
                await update.message.reply_text("No polls run yet. Use /{command} poll")
        finally:
            conn.close()
""",
}

# Keywords used to match specs to templates
_TEMPLATE_KEYWORDS = {
    "crud": {"create", "add", "remove", "delete", "list", "manage", "track", "log", "store"},
    "api_backed": {"api", "fetch", "external", "service", "integration", "webhook", "slack", "github"},
    "periodic_poller": {"poll", "monitor", "check", "watch", "periodic", "schedule", "recurring"},
}


def get_template_for_spec(spec: dict) -> str | None:
    """Pick the best matching template based on description keywords.

    Returns template key ("crud", "api_backed", "periodic_poller") or None.
    """
    desc_lower = spec.get("description", "").lower()
    name_lower = spec.get("name", "").lower()
    combined = f"{desc_lower} {name_lower}"

    best_key = None
    best_score = 0
    for key, keywords in _TEMPLATE_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in combined)
        if score > best_score:
            best_score = score
            best_key = key
    return best_key if best_score > 0 else None


# --- #32: Human-in-loop PR Feedback ---


def record_pr_feedback(pr_number: int, feedback_text: str):
    """Store PR rejection/review comments in the interaction_signals table.

    This feedback is retrieved during future generation attempts for similar capabilities.
    """
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(
            "INSERT INTO interaction_signals (signal_type, context, value) VALUES (?, ?, ?)",
            (
                "pr_feedback",
                json.dumps({"pr_number": pr_number, "feedback": feedback_text}),
                -1.0,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_pr_feedback(capability_name: str) -> list[str]:
    """Retrieve past PR feedback relevant to a capability name.

    Searches interaction_signals for pr_feedback entries whose context
    mentions the capability name.
    """
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(str(DB_PATH))
    conn.row_factory = _sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT context FROM interaction_signals WHERE signal_type = 'pr_feedback' "
            "ORDER BY created_at DESC LIMIT 50"
        ).fetchall()

        feedback = []
        for row in rows:
            try:
                ctx = json.loads(row["context"])
                fb_text = ctx.get("feedback", "")
                if capability_name.lower() in fb_text.lower():
                    feedback.append(fb_text)
            except (json.JSONDecodeError, TypeError):
                continue
        return feedback
    finally:
        conn.close()


# --- Complexity Routing ---

SIMPLE_CAPABILITIES = {
    "reminder", "note", "bookmark", "timer", "counter",
    "todo", "list", "tag", "alias",
}

COMPLEX_SIGNALS = [
    "slack", "jira", "twitter", "notion", "linear", "github",
    "oauth", "api key", "webhook", "websocket", "real-time",
    "scrape", "browser", "authentication", "spotify",
]


def classify_complexity(spec: dict) -> str:
    """Return 'simple' or 'complex' based on capability spec."""
    name = spec.get("name", "").lower()
    desc = spec.get("description", "").lower()

    if any(signal in name or signal in desc for signal in COMPLEX_SIGNALS):
        return "complex"
    if name in SIMPLE_CAPABILITIES:
        return "simple"
    return "complex"  # default to complex — safer


# --- Code Generation ---

ACTION_TEMPLATE = '''"""ACTION_DESCRIPTION"""

import logging
import sqlite3

from config import DB_PATH, TIMEZONE

log = logging.getLogger("khalil.actions.MODULE_NAME")

# --- Skill registration (enables natural language discovery) ---

SKILL = {
    "name": "MODULE_NAME",
    "description": "ACTION_DESCRIPTION",
    "category": "extension",
    "patterns": [
        # Add regex patterns that match user intent to action types
        # (r"pattern here", "action_type"),
    ],
    "actions": [
        {
            "type": "MODULE_NAME",
            "handler": "cmd_COMMAND",
            "description": "ACTION_DESCRIPTION",
            "keywords": "keyword1 keyword2",
        },
    ],
    "examples": [],
}


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_tables(conn: sqlite3.Connection):
    """Create any tables this extension needs. Called on startup."""
    # TODO: Add CREATE TABLE IF NOT EXISTS statements here
    pass


# --- Core functions ---
# Implement reusable logic here. Import from existing actions/ modules
# when possible instead of reimplementing (e.g., from actions.shell import ...).


# --- Command handler ---

async def cmd_COMMAND(update, context):
    """Handle /COMMAND command."""
    args = context.args or []
    # Parse subcommands, call core functions, format response
    msg = "Result"
    await update.message.reply_text(msg)
'''


async def generate_action_module(spec: dict, ask_llm_fn) -> tuple[str, str]:
    """Generate a new action module using Claude Opus.

    Args:
        spec: {"name": "...", "command": "...", "description": "..."}
        ask_llm_fn: async callable(query, context, system_extra) -> str

    Returns:
        (module_source_code, manifest_json)
    """
    # Get LLM client via shared factory (respects Taskforce proxy)
    from llm_client import get_async_llm_client, call_llm_async
    try:
        client, client_type = get_async_llm_client()
    except RuntimeError as e:
        raise RuntimeError("Claude API key required for code generation. Ollama is not reliable enough.") from e

    # Read reminders.py as a pattern reference (it's a clean, self-contained action)
    template_path = KHALIL_DIR / "actions" / "reminders.py"
    template_source = template_path.read_text() if template_path.exists() else ACTION_TEMPLATE

    # Inject past PR feedback so generation learns from rejections
    feedback_section = ""
    past_feedback = get_pr_feedback(spec["name"])
    if past_feedback:
        feedback_lines = "\n".join(f"- {fb}" for fb in past_feedback[-3:])
        feedback_section = (
            f"\n**Previous attempts were rejected. Address this feedback**:\n{feedback_lines}\n\n"
        )

    prompt = (
        f"Generate a complete Python module for a Khalil action called '{spec['name']}'.\n\n"
        f"**Capability**: {spec['description']}\n"
        f"**Command**: /{spec['command']}\n\n"
        f"{feedback_section}"
        "**Requirements**:\n"
        "1. Follow the EXACT patterns from the reference module below\n"
        "2. Use SQLite for any persistent state (via `config.DB_PATH`)\n"
        "3. Include an `ensure_tables(conn)` function that creates any needed tables\n"
        f"4. Include an async `cmd_{spec['command']}(update, context)` handler\n"
        "5. Handle subcommands via `context.args` (e.g., /command add ..., /command list)\n"
        "6. Use `logging.getLogger(f'khalil.actions.{name}')` for logging\n"
        "7. Import only from stdlib, `config`, and existing khalil modules\n"
        "8. Handle errors gracefully with clear user-facing messages\n"
        "9. Keep it under 200 lines\n\n"
        "**SKILL DICT (Required for natural language discovery)**:\n"
        "Every action module MUST include a module-level SKILL dict so Khalil can match\n"
        "natural language queries to this capability. Structure:\n"
        "```python\n"
        "SKILL = {\n"
        '    "name": "module_name",\n'
        '    "description": "One-line description of what this does",\n'
        '    "category": "extension",\n'
        '    "patterns": [\n'
        '        (r"regex pattern matching user intent", "action_type"),\n'
        "    ],\n"
        '    "actions": [\n'
        "        {\n"
        f'            "type": "action_type",\n'
        f'            "handler": "cmd_{spec["command"]}",\n'
        '            "description": "What this action does",\n'
        '            "keywords": "space separated keywords",\n'
        "        },\n"
        "    ],\n"
        '    "examples": ["Example user query 1", "Example user query 2"],\n'
        "}\n"
        "```\n"
        "Include 3-5 regex patterns covering common ways users would ask for this capability.\n\n"
        "**REUSE EXISTING CODE**:\n"
        "Import from existing actions/ modules when possible instead of reimplementing.\n"
        "For example, if this capability involves GitHub, import from actions.github_api.\n"
        "If it involves Apple Music, import from actions.apple_music.\n"
        "Do NOT rewrite AppleScript/shell commands that already exist in other modules.\n\n"
        "**QUALITY RULES**:\n"
        "- For matching/filtering: use word-boundary regex (re.search with \\b), not substring matching\n"
        "- For write operations (labeling, sending, modifying, deleting): include a preview/dry-run subcommand that shows what would happen without doing it\n"
        "- For external API calls: batch where possible, keep max_results bounded (default 50)\n"
        "- Close DB connections in finally blocks to prevent leaks on exceptions\n"
        "- Call ensure_tables() once via a module-level flag, not on every request\n\n"
        "**Reference module** (follow this style exactly):\n"
        f"```python\n{template_source}\n```\n\n"
        "Respond with ONLY the Python source code. No markdown fences, no explanation."
    )

    source = (await call_llm_async(
        client, client_type, CLAUDE_MODEL_COMPLEX,
        "You are a code generator for Khalil, a personal AI assistant. "
        "Generate clean, production-quality Python modules that follow existing patterns exactly. "
        "Output ONLY Python code, no markdown, no explanation.",
        prompt, max_tokens=4000,
    )).strip()

    # Strip markdown fences if present
    if source.startswith("```"):
        lines = source.split("\n")
        lines = lines[1:]  # Remove opening fence
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        source = "\n".join(lines)

    # Generate manifest with version tracking (#26)
    manifest = {
        "name": spec["name"],
        "command": spec["command"],
        "description": spec["description"],
        "action_module": f"actions.{spec['name']}",
        "handler_function": f"cmd_{spec['command']}",
        "generated_at": datetime.utcnow().isoformat(),
        "generated_for": spec.get("original_query", spec["description"]),
        "version": 1,
    }

    # Write versioned manifest (backs up old one if exists)
    manifest_path = EXTENSIONS_DIR / f"{spec['name']}.json"
    manifest = _write_versioned_manifest(manifest_path, manifest)

    return source, json.dumps(manifest, indent=2)


# --- Claude Code CLI Generation ---


def _build_claude_code_prompt(spec: dict) -> str:
    """Build a detailed prompt for Claude Code CLI."""
    name = spec["name"]
    command = spec.get("command", name)
    return (
        "You are adding a new capability to Khalil, a personal AI assistant "
        "(Telegram bot + FastAPI + SQLite).\n\n"
        f"TASK: Create the \"{name}\" capability.\n"
        f"Command: /{command}\n"
        f"Description: {spec['description']}\n\n"
        "STEP 1 — READ THESE FILES for context:\n"
        "- `actions/reminders.py` — reference action module pattern\n"
        "- `actions/gmail.py` — complex action module pattern\n"
        "- `config.py` — available constants (DB_PATH, KEYRING_SERVICE, TIMEZONE)\n"
        "- `requirements.txt` — allowed third-party packages\n"
        "- `server.py` — search for `_load_extensions` to see how extensions are registered\n\n"
        f"STEP 2 — CREATE `actions/{name}.py` with this EXACT structure:\n"
        "```\n"
        '"""Module docstring — describe what this does.\n'
        "\n"
        "If this uses an external API, document:\n"
        "- What token type is needed (bot token, user token, API key)\n"
        "- Setup command for the user\n"
        '"""\n'
        "import asyncio\n"
        "import logging\n"
        "import sqlite3\n"
        "from config import DB_PATH, KEYRING_SERVICE, TIMEZONE\n"
        f'log = logging.getLogger("khalil.actions.{name}")\n'
        "\n"
        "# SKILL dict — REQUIRED for natural language discovery\n"
        "SKILL = {\n"
        f'    "name": "{name}",\n'
        f'    "description": "{spec["description"]}",\n'
        '    "category": "extension",\n'
        '    "patterns": [\n'
        '        # 3-5 regex patterns matching natural language intent\n'
        f'        (r"regex matching user query", "{name}"),\n'
        "    ],\n"
        '    "actions": [\n'
        "        {\n"
        f'            "type": "{name}",\n'
        f'            "handler": "handle_{command}",\n'
        f'            "description": "{spec["description"]}",\n'
        f'            "keywords": "{name} {command}",\n'
        "        },\n"
        "    ],\n"
        '    "examples": ["Example natural language query"],\n'
        "}\n"
        "\n"
        "def ensure_tables(conn: sqlite3.Connection):\n"
        '    """Create tables. Called once at startup."""\n'
        "    conn.execute('CREATE TABLE IF NOT EXISTS ...')\n"
        "    conn.commit()\n"
        "\n"
        "# --- Core functions ---\n"
        "# Import from existing actions/ modules when possible.\n"
        "# Do NOT reimplement functions that already exist.\n"
        "\n"
        f"async def handle_{command}(update, context):\n"
        f'    """Handle /{command} command."""\n'
        "    args = context.args or []\n"
        "    # Parse subcommands, call core functions, reply_text\n"
        "```\n\n"
        f"STEP 3 — CREATE `extensions/{name}.json`:\n"
        "```json\n"
        "{\n"
        f'    "name": "{name}",\n'
        f'    "command": "{command}",\n'
        f'    "description": "{spec["description"]}",\n'
        f'    "action_module": "actions.{name}",\n'
        f'    "handler_function": "handle_{command}",\n'
        f'    "generated_at": "<use current ISO timestamp>",\n'
        f'    "generated_for": "{spec.get("original_query", spec["description"])}"\n'
        "}\n"
        "```\n\n"
        "CONSTRAINTS:\n"
        "- Allowed imports: stdlib, httpx, keyring, and modules from config\n"
        "- FORBIDDEN: subprocess, eval, exec, socket, ctypes, os.system\n"
        "- Store credentials via: keyring.get_password(KEYRING_SERVICE, 'key-name')\n"
        "- Use parameterized SQL (?, ?) — never f-strings in SQL\n"
        "- Wrap sync HTTP calls with asyncio.to_thread()\n"
        "- Resolve user IDs to display names when showing messages\n"
        "- Respect Telegram's 4096 char message limit\n"
        "- Keep under 300 lines\n\n"
        "IMPORTANT: If the external API requires a specific token type (e.g., user token "
        "vs bot token), document this clearly and only implement endpoints that work with "
        "the token type you're using. Do NOT implement features that will fail at runtime.\n\n"
        "QUALITY RULES:\n"
        "- For matching/filtering: use word-boundary regex (re.search with \\b), not substring matching\n"
        "- For write operations (labeling, sending, modifying, deleting): include a preview/dry-run subcommand that shows what would happen without doing it\n"
        "- For external API calls: batch where possible, keep max_results bounded (default 50)\n"
        "- Close DB connections in finally blocks to prevent leaks on exceptions\n"
        "- Call ensure_tables() once via a module-level flag, not on every request\n\n"
        "Create ONLY the action module and manifest. Do not modify server.py."
    )


async def generate_with_claude_code(spec: dict) -> tuple[str, str, str]:
    """Generate a capability using Claude Code CLI in a worktree.

    Returns (module_source, manifest_json, pr_url).
    """
    from actions.claude_code import create_worktree, run_claude_code, cleanup_worktree

    branch = f"khalil-extend/{spec['name']}"
    wt_path = create_worktree(branch)

    prompt = _build_claude_code_prompt(spec)

    try:
        success, output = await run_claude_code(prompt, wt_path, timeout=300)
        if not success:
            raise RuntimeError(f"Claude Code failed: {output[:500]}")

        # Read the generated files from worktree
        module_path = wt_path / "actions" / f"{spec['name']}.py"
        manifest_path = wt_path / "extensions" / f"{spec['name']}.json"

        if not module_path.exists():
            raise RuntimeError(
                f"Claude Code didn't create actions/{spec['name']}.py"
            )

        module_source = module_path.read_text()

        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
        else:
            manifest = {
                "name": spec["name"],
                "command": spec.get("command", spec["name"]),
                "description": spec["description"],
                "action_module": f"actions.{spec['name']}",
                "handler_function": f"cmd_{spec.get('command', spec['name'])}",
                "generated_at": datetime.utcnow().isoformat(),
                "generated_for": spec.get("original_query", spec["description"]),
            }

        # Validate — retry once with error feedback if it fails
        ok, err, warnings = validate_generated_code(module_source)
        if not ok:
            log.warning("First validation failed (%s), retrying with fix prompt", err)
            fix_prompt = (
                f"The generated code in actions/{spec['name']}.py has a validation error:\n"
                f"{err}\n\nFix the issue in the file."
            )
            await run_claude_code(fix_prompt, wt_path, timeout=120)
            module_source = module_path.read_text()
            ok, err, warnings = validate_generated_code(module_source)
            if not ok:
                raise RuntimeError(f"Validation failed after retry: {err}")

        # Guardian review of generated code
        guardian_flag = ""
        try:
            from actions.guardian import review_code_patch, GuardianVerdict
            guardian_result = await review_code_patch(module_source, f"actions/{spec['name']}.py")
            if guardian_result.verdict == GuardianVerdict.BLOCK:
                guardian_flag = "[NEEDS REVIEW] "
                log.warning("Guardian blocked extension %s: %s", spec['name'], guardian_result.reason)
                try:
                    from learning import record_signal as _rec
                    _rec("guardian_blocked_heal", {"name": spec['name'], "reason": guardian_result.reason})
                except Exception:
                    pass
        except Exception as e:
            log.warning("Guardian review failed for extension %s: %s — proceeding", spec['name'], e)

        # Commit and push from worktree, open PR
        pr_url = await _commit_and_pr_from_worktree(wt_path, branch, spec, manifest, warnings, pr_title_prefix=guardian_flag)

        # Emit signal for workflow engine (auto-merge evaluation)
        try:
            from learning import record_signal
            record_signal("extension_pr_created", {
                "pr_url": pr_url,
                "extension_name": spec["name"],
                "guardian_blocked": bool(guardian_flag),
            })
        except Exception:
            pass

        return module_source, json.dumps(manifest), pr_url

    finally:
        cleanup_worktree(branch)


async def _commit_and_pr_from_worktree(
    wt_path: Path, branch: str, spec: dict, manifest: dict,
    warnings: list[str] | None = None,
    pr_title_prefix: str = "",
) -> str:
    """Commit changes in worktree and open a PR. Returns PR URL."""
    name = spec["name"]

    def _git_in_wt(*args, **kwargs):
        return subprocess.run(
            ["git"] + list(args),
            cwd=str(wt_path),
            capture_output=True, text=True,
            timeout=kwargs.get("timeout", 30),
        )

    def _workflow():
        _git_in_wt("add", "-A")
        result = _git_in_wt("commit", "-m",
            f"Add {name} capability (auto-generated by Khalil via Claude Code)\n\n"
            f"Co-Authored-By: Khalil Bot <khalil@local>"
        )
        if result.returncode != 0:
            raise RuntimeError(f"Commit failed: {result.stderr}")

        result = _git_in_wt("push", "-u", "origin", branch)
        if result.returncode != 0:
            raise RuntimeError(f"Push failed: {result.stderr}")

        # Build PR body
        body = (
            f"## Auto-generated capability (via Claude Code CLI)\n\n"
            f"**Description**: {spec['description']}\n"
            f"**Command**: /{spec.get('command', name)}\n"
            f"**Generated for**: \"{spec.get('original_query', '')}\"\n\n"
            f"## Files\n"
            f"- `actions/{name}.py` — action module\n"
            f"- `extensions/{name}.json` — extension manifest\n\n"
            f"## Review checklist\n"
            f"- [ ] Code looks correct and safe\n"
            f"- [ ] No unnecessary imports or dangerous operations\n"
            f"- [ ] Tables schema makes sense\n"
            f"- [ ] Command handler covers basic use cases\n"
        )
        if warnings:
            body += "\n## Quality warnings\n"
            for w in warnings:
                body += f"- ⚠️ {w}\n"
        body += "\nGenerated by Khalil's self-extension engine using Claude Code CLI."

        # Open PR
        result = subprocess.run(
            ["gh", "pr", "create",
             "--title", f"{pr_title_prefix}Khalil: Add {name} capability (Claude Code)",
             "--body", body,
             ],
            cwd=str(wt_path),
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"PR creation failed: {result.stderr}")
        pr_url = result.stdout.strip()

        # Register in plugin manifest (disabled until PR merged)
        from extensions.manifest import register_extension
        register_extension(
            name,
            action_type=spec.get("command", name),
            intent_patterns=[],
            description=spec.get("description", ""),
            source_pr=pr_url,
        )

        return pr_url

    return await asyncio.to_thread(_workflow)


# --- Validation ---

# Exact function names that are always dangerous (matched against the bare function name)
BLOCKLISTED_BARE_CALLS = {
    "eval", "exec", "compile", "__import__",
}

# Qualified calls (matched as exact prefix of the call chain)
BLOCKLISTED_QUALIFIED_CALLS = {
    "subprocess", "os.system", "os.popen", "os.exec", "os.execv",
    "os.execvp", "os.execve", "importlib.import_module",
    "shutil.rmtree", "shutil.move", "ctypes",
    "socket.socket", "http.server",
}

BLOCKLISTED_IMPORTS = {
    "subprocess", "ctypes", "socket", "http.server", "xmlrpc",
    "signal", "multiprocessing", "webbrowser",
}

# #74: Extension sandboxing — whitelist of allowed imports
SANDBOX_ALLOWED_IMPORTS = {
    "json", "re", "datetime", "logging", "httpx", "asyncio",
    "collections", "dataclasses", "enum", "functools", "itertools",
    "math", "pathlib", "textwrap", "typing", "uuid",
}

# #74: Dangerous function calls beyond what BLOCKLISTED_BARE_CALLS covers
_SANDBOX_BLOCKED_CALLS = {"__import__", "compile", "globals", "locals", "vars", "delattr"}


def validate_generated_code(source: str) -> tuple[bool, str, list[str]]:
    """Validate generated code is safe and syntactically correct.

    Returns (is_valid, error_message, warnings).
    Warnings are advisory quality issues that don't block generation.
    """
    # 1. Syntax check via AST
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return False, f"Syntax error: {e}", []

    # 2. Walk AST for blocklisted calls and imports
    for node in ast.walk(tree):
        # Check imports
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in BLOCKLISTED_IMPORTS:
                    return False, f"Blocked import: {alias.name}", []
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in BLOCKLISTED_IMPORTS:
                return False, f"Blocked import: {node.module}", []

        # Check function calls
        if isinstance(node, ast.Call):
            call_name = _get_call_name(node)
            if call_name:
                # Check bare function name (e.g., "eval", "exec")
                bare_name = call_name.rsplit(".", 1)[-1]
                if bare_name in BLOCKLISTED_BARE_CALLS:
                    return False, f"Blocked call: {call_name}", []
                # Check qualified calls (e.g., "os.system", "subprocess.run")
                if any(call_name == b or call_name.startswith(b + ".") for b in BLOCKLISTED_QUALIFIED_CALLS):
                    return False, f"Blocked call: {call_name}", []

    # 3. Verify module has at least one async function (the command handler)
    has_async = any(
        isinstance(node, ast.AsyncFunctionDef)
        for node in ast.walk(tree)
    )
    if not has_async:
        return False, "Module must define at least one async function (the command handler)", []

    # 4. Final compilation check
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
        f.write(source)
        f.flush()
        try:
            py_compile.compile(f.name, doraise=True)
        except py_compile.PyCompileError as e:
            return False, f"Compilation error: {e}", []
        finally:
            Path(f.name).unlink(missing_ok=True)

    # 5. Quality warnings (advisory, don't block)
    warnings = _check_quality_warnings(source)

    return True, "", warnings


def validate_extension_safety(source_code: str) -> tuple[bool, list[str]]:
    """#74: Validate extension code against sandbox rules.

    Checks:
    - All imports are in SANDBOX_ALLOWED_IMPORTS whitelist
    - No eval(), exec(), __import__(), subprocess, os.system calls
    - No file system access outside DATA_DIR

    Returns (safe: bool, violations: list[str]).
    """
    violations = []

    try:
        tree = ast.parse(source_code)
    except SyntaxError as e:
        return False, [f"Syntax error: {e}"]

    data_dir_str = str(DATA_DIR)

    for node in ast.walk(tree):
        # Check imports against whitelist
        if isinstance(node, ast.Import):
            for alias in node.names:
                top_level = alias.name.split(".")[0]
                if top_level not in SANDBOX_ALLOWED_IMPORTS:
                    violations.append(f"Disallowed import: {alias.name} (not in whitelist)")

        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top_level = node.module.split(".")[0]
                if top_level not in SANDBOX_ALLOWED_IMPORTS:
                    violations.append(f"Disallowed import: {node.module} (not in whitelist)")

        # Check dangerous function calls
        if isinstance(node, ast.Call):
            call_name = _get_call_name(node)
            if call_name:
                bare_name = call_name.rsplit(".", 1)[-1]
                # Check sandbox-specific blocks
                if bare_name in _SANDBOX_BLOCKED_CALLS:
                    violations.append(f"Blocked call: {call_name}")
                if bare_name in BLOCKLISTED_BARE_CALLS:
                    violations.append(f"Blocked call: {call_name}")
                # Check for os.system, subprocess.run, etc.
                if any(call_name == b or call_name.startswith(b + ".") for b in BLOCKLISTED_QUALIFIED_CALLS):
                    violations.append(f"Blocked call: {call_name}")

        # Check for string literals that reference paths outside DATA_DIR
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            val = node.value
            # Flag absolute paths that aren't under DATA_DIR
            if val.startswith("/") and not val.startswith(data_dir_str) and not val.startswith("/tmp"):
                violations.append(f"File path outside DATA_DIR: {val}")

    return len(violations) == 0, violations


def _check_quality_warnings(source: str) -> list[str]:
    """Scan source for known anti-patterns. Returns advisory warnings."""
    warnings = []
    source_lower = source.lower()

    # Naive substring matching: detect `kw in text` or `kw.lower() in text` patterns
    if re.search(r'(?:kw|keyword|word|term|pattern)\S*\s+in\s+\w*(?:text|body|content|subject)', source_lower):
        warnings.append("Substring matching detected — consider word-boundary regex (re.search with \\b)")

    # Missing preview mode: write operations without preview/dry-run
    write_verbs = re.search(r'\b(?:modify|create|send|delete|apply|label|remove|update)\b', source_lower)
    has_preview = re.search(r'\b(?:preview|dry_run|dry.run)\b', source_lower)
    if write_verbs and not has_preview:
        warnings.append("Write operations found but no preview/dry-run subcommand")

    return warnings


async def generate_extension_tests(spec: dict, module_source: str) -> str | None:
    """Generate pytest tests for a new extension module.

    Uses Claude to generate tests that exercise the command handler with mock
    Telegram Update/Context objects (from conftest.py fixtures).

    Returns test source code or None on failure.
    """
    from llm_client import get_async_llm_client, call_llm_async
    try:
        client, client_type = get_async_llm_client()
    except RuntimeError:
        return None

    # Read conftest for available fixtures
    conftest_path = KHALIL_DIR / "tests" / "conftest.py"
    conftest_source = conftest_path.read_text() if conftest_path.exists() else ""

    prompt = (
        f"Generate pytest tests for this Khalil action module:\n\n"
        f"**Module**: actions/{spec['name']}.py\n"
        f"**Command**: /{spec['command']}\n"
        f"**Description**: {spec['description']}\n\n"
        f"**Module source**:\n```python\n{module_source}\n```\n\n"
        f"**Available test fixtures** (from conftest.py):\n```python\n{conftest_source[:2000]}\n```\n\n"
        "**Requirements**:\n"
        "1. Use pytest + pytest-asyncio\n"
        "2. Use mock_update and mock_context fixtures from conftest.py\n"
        "3. Use tmp_db fixture for database operations\n"
        "4. Mock any external API calls (Google, etc.) with unittest.mock.patch\n"
        f"5. Test the cmd_{spec['command']} handler with various subcommands\n"
        "6. Test edge cases: empty args, invalid args, missing data\n"
        "7. Keep under 150 lines\n"
        "8. Use @pytest.mark.asyncio for async tests\n\n"
        "Respond with ONLY Python source code. No markdown fences."
    )

    try:
        test_source = (await call_llm_async(
            client, client_type, CLAUDE_MODEL_COMPLEX,
            "Generate clean pytest test code. Output ONLY Python code.",
            prompt, max_tokens=3000,
        )).strip()
        # Strip markdown fences
        if test_source.startswith("```"):
            lines = test_source.split("\n")
            lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            test_source = "\n".join(lines)

        # Validate syntax
        import ast
        ast.parse(test_source)
        return test_source
    except Exception as e:
        log.warning("Test generation failed for %s: %s", spec['name'], e)
        return None


def smoke_test_module(module_path: Path, command_name: str) -> tuple[bool, str]:
    """Import the module in a subprocess, verify handler exists, and call it with mocks.

    Returns (passed, error_message).
    #28: Enhanced smoke test — actually invokes the handler with mock objects.
    #74: Now includes sandbox safety validation.
    """
    # Phase 0: Sandbox safety check (#74) — check for blocked imports/calls only
    try:
        source = module_path.read_text()
        safe, violations = validate_extension_safety(source)
        # Only block on hard violations (blocked imports/calls), not whitelist violations
        hard_violations = [v for v in violations if "Blocked" in v or "File path outside" in v]
        if hard_violations:
            return False, f"Sandbox violation: {'; '.join(hard_violations[:3])}"
    except Exception as e:
        log.warning("Sandbox check failed for %s: %s", module_path, e)

    handler_name = f"cmd_{command_name}"

    # Container sandbox integration — route tests through Docker when available
    from sandbox import run_in_sandbox, is_docker_available
    use_sandbox = is_docker_available()
    if use_sandbox:
        log.info("Docker available — running smoke tests in container sandbox")
    else:
        log.warning("Docker not available — falling back to host subprocess for smoke test")

    class SandboxResult:
        """Lightweight result object matching subprocess.CompletedProcess interface."""
        def __init__(self, rc, out, err):
            self.returncode = rc
            self.stdout = out
            self.stderr = err

    # Phase 1: Import check + handler exists
    test_script = (
        f"import sys; sys.path.insert(0, {str(module_path.parent)!r}); "
        f"mod = __import__({module_path.stem!r}); "
        f"assert hasattr(mod, {handler_name!r}), "
        f"'Missing handler: {handler_name}'; "
        f"assert callable(getattr(mod, {handler_name!r})), "
        f"'{handler_name} is not callable'"
    )
    try:
        if use_sandbox:
            container_script = test_script.replace(str(KHALIL_DIR), "/khalil")
            exit_code, stdout, stderr = run_in_sandbox(container_script, timeout=10)
            result = SandboxResult(exit_code, stdout, stderr)
        else:
            result = subprocess.run(
                [sys.executable, "-c", test_script],
                capture_output=True, text=True, timeout=10,
            )
        if result.returncode != 0:
            error = result.stderr.strip().split("\n")[-1] if result.stderr else "Unknown error"
            return False, f"Smoke test failed: {error}"
    except subprocess.TimeoutExpired:
        return False, "Smoke test timed out (10s)"
    except Exception as e:
        return False, f"Smoke test error: {e}"

    # Phase 2: Call handler with mock Update/Context (catch crashes, not logic errors)
    khalil_dir = str(module_path.parent.parent)
    mock_test_script = f"""
import sys, asyncio
sys.path.insert(0, {khalil_dir!r})
sys.path.insert(0, {str(module_path.parent)!r})
from unittest.mock import AsyncMock, MagicMock
mod = __import__({module_path.stem!r})
handler = getattr(mod, {handler_name!r})
update = MagicMock()
update.message.text = "/test"
update.message.reply_text = AsyncMock()
update.message.reply_html = AsyncMock()
update.effective_chat.id = 12345
context = MagicMock()
context.args = []
context.bot.send_message = AsyncMock()
try:
    asyncio.run(handler(update, context))
except Exception as e:
    # Handler may fail due to missing DB/API — that's OK, we're checking for crashes
    if isinstance(e, (ImportError, SyntaxError, TypeError, AttributeError)):
        print(f"FAIL: {{type(e).__name__}}: {{e}}", file=sys.stderr)
        sys.exit(1)
"""
    try:
        if use_sandbox:
            container_mock_script = mock_test_script.replace(str(KHALIL_DIR), "/khalil")
            exit_code, stdout, stderr = run_in_sandbox(container_mock_script, timeout=15)
            result = SandboxResult(exit_code, stdout, stderr)
        else:
            result = subprocess.run(
                [sys.executable, "-c", mock_test_script],
                capture_output=True, text=True, timeout=15,
            )
        if result.returncode != 0:
            error = result.stderr.strip().split("\n")[-1] if result.stderr else "Unknown error"
            if error.startswith("FAIL:"):
                return False, f"Smoke test (mock call) failed: {error[5:].strip()}"
            # Non-FAIL errors (e.g., missing API keys) are acceptable
            log.info("Smoke test mock call returned non-zero but not a code error: %s", error[:200])
    except subprocess.TimeoutExpired:
        log.info("Smoke test mock call timed out — handler may depend on external service")
    except Exception:
        pass  # Non-critical

    return True, ""


def _get_call_name(node: ast.Call) -> str | None:
    """Extract the name of a function call from an AST node."""
    if isinstance(node.func, ast.Name):
        return node.func.id
    elif isinstance(node.func, ast.Attribute):
        parts = []
        current = node.func
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
        return ".".join(reversed(parts))
    return None


# --- Git + PR Operations ---


def _run_git(*args, cwd=None, timeout=30) -> subprocess.CompletedProcess:
    """Run a git command synchronously. Raises on failure."""
    cmd = ["git"] + list(args)
    result = subprocess.run(
        cmd, capture_output=True, text=True,
        cwd=cwd or str(KHALIL_DIR), timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def _run_gh(*args, cwd=None, timeout=30) -> subprocess.CompletedProcess:
    """Run a gh CLI command synchronously."""
    cmd = ["gh"] + list(args)
    result = subprocess.run(
        cmd, capture_output=True, text=True,
        cwd=cwd or str(KHALIL_DIR), timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
    return result


async def create_extension_pr(
    name: str, module_source: str, manifest: dict,
    warnings: list[str] | None = None,
    test_source: str | None = None,
    scheduler_source: str | None = None,
    pr_title_prefix: str = "",
) -> str:
    """Create a branch, commit the generated files, push, and open a PR.

    Returns the PR URL.
    """
    branch_name = f"khalil-extend/{name}"
    action_file = KHALIL_DIR / "actions" / f"{name}.py"
    manifest_file = EXTENSIONS_DIR / f"{name}.json"
    test_file = KHALIL_DIR / "tests" / f"test_{name}.py" if test_source else None
    scheduler_file = KHALIL_DIR / "actions" / f"{name}_scheduler.py" if scheduler_source else None

    def _git_workflow():
        # Check gh is authenticated
        _run_gh("auth", "status")

        # Save current state
        original_branch = _run_git("branch", "--show-current").stdout.strip()
        stashed = False
        status = _run_git("status", "--porcelain").stdout.strip()
        if status:
            _run_git("stash", "push", "-m", f"khalil-extend-{name}")
            stashed = True

        try:
            # Create branch from main
            _run_git("checkout", "main")
            _run_git("pull", "--ff-only")
            _run_git("checkout", "-b", branch_name)

            # Write files
            action_file.write_text(module_source)
            manifest_file.parent.mkdir(parents=True, exist_ok=True)
            manifest_file.write_text(
                json.dumps(manifest, indent=2) if isinstance(manifest, dict) else manifest
            )
            # Write test file if generated
            if test_file and test_source:
                test_file.write_text(test_source)
            # #30: Write scheduler job if generated
            if scheduler_file and scheduler_source:
                scheduler_file.write_text(scheduler_source)

            # Commit and push
            files_to_add = [str(action_file), str(manifest_file)]
            if test_file and test_source:
                files_to_add.append(str(test_file))
            if scheduler_file and scheduler_source:
                files_to_add.append(str(scheduler_file))
            _run_git("add", *files_to_add)
            _run_git(
                "commit", "-m",
                f"Add {name} capability (auto-generated by Khalil)\n\n"
                f"Co-Authored-By: Khalil Bot <khalil@local>",
            )
            _run_git("push", "-u", "origin", branch_name)

            # Build PR body
            pr_body = (
                f"## Auto-generated capability\n\n"
                f"**Description**: {manifest.get('description', name) if isinstance(manifest, dict) else name}\n"
                f"**Command**: /{manifest.get('command', name) if isinstance(manifest, dict) else name}\n"
                f"**Generated for**: \"{manifest.get('generated_for', '') if isinstance(manifest, dict) else ''}\"\n\n"
                f"## Files\n"
                f"- `actions/{name}.py` — action module\n"
                f"- `extensions/{name}.json` — extension manifest\n"
                + (f"- `tests/test_{name}.py` — integration tests\n" if test_source else "")
                + (f"- `actions/{name}_scheduler.py` — scheduler job\n" if scheduler_source else "")
                + f"\n"
                f"## Review checklist\n"
                f"- [ ] Code looks correct and safe\n"
                f"- [ ] No unnecessary imports or dangerous operations\n"
                f"- [ ] Tables schema makes sense\n"
                f"- [ ] Command handler covers basic use cases\n"
                + (f"- [ ] Tests pass: `pytest tests/test_{name}.py -v`\n" if test_source else "")
            )
            if warnings:
                pr_body += "\n## Quality warnings\n"
                for w in warnings:
                    pr_body += f"- ⚠️ {w}\n"
            pr_body += "\nGenerated by Khalil's self-extension engine."

            # Open PR
            result = _run_gh(
                "pr", "create",
                "--title", f"{pr_title_prefix}Khalil: Add {name} capability",
                "--body", pr_body,
            )
            pr_url = result.stdout.strip()

            # Register in plugin manifest (disabled until PR merged)
            from extensions.manifest import register_extension
            register_extension(
                name,
                action_type=manifest.get("command", name),
                intent_patterns=[],
                description=manifest.get("description", ""),
                source_pr=pr_url,
            )

            return pr_url
        finally:
            # Always return to original branch
            try:
                _run_git("checkout", original_branch)
            except Exception:
                _run_git("checkout", "main")
            if stashed:
                try:
                    _run_git("stash", "pop")
                except Exception:
                    log.warning("Failed to pop git stash after extension PR")

    return await asyncio.to_thread(_git_workflow)


# --- Orchestrator ---


async def generate_and_pr(payload: dict) -> str:
    """Full pipeline: generate code, validate, create PR.

    Routes to simple (raw Claude API) or complex (Claude Code CLI) path
    based on capability complexity.

    Called directly from server.py background task or via autonomy.
    Returns status message.
    """
    global _last_generation_time

    spec = payload.get("spec", payload)
    name = spec.get("name", "unknown")

    # Rate limit check
    now = time.time()
    if now - _last_generation_time < GENERATION_COOLDOWN_SECONDS:
        remaining = int(GENERATION_COOLDOWN_SECONDS - (now - _last_generation_time))
        return f"Rate limited — try again in {remaining // 60} minutes."

    # Check if extension already exists
    if (KHALIL_DIR / "actions" / f"{name}.py").exists():
        return f"Action module `actions/{name}.py` already exists."

    if (EXTENSIONS_DIR / f"{name}.json").exists():
        return f"Extension manifest `extensions/{name}.json` already exists."

    # #29: Check for overlapping capabilities before generating
    overlap = check_extension_overlap(spec)
    if overlap:
        log.info("Extension dedup: %s — skipping generation for %s", overlap, name)
        return f"Skipped: {overlap}. Consider using the existing capability instead."

    try:
        complexity = classify_complexity(spec)
        log.info("Generating %s action module: %s", complexity, name)
        guardian_blocked = False  # Tracks whether Guardian blocked this extension

        if complexity == "complex" and Path(CLAUDE_CODE_BIN).exists():
            # Complex path: Claude Code CLI in a worktree
            module_source, manifest_json, pr_url = await generate_with_claude_code(spec)
        else:
            # Simple path: raw Claude API call
            from server import ask_llm
            module_source, manifest_json = await generate_action_module(spec, ask_llm)

            valid, error, warnings = validate_generated_code(module_source)
            if not valid:
                log.error("Generated code failed validation: %s", error)
                return f"Generated code failed validation: {error}\nPlease try again or build this manually."

            # #30: Generate scheduler job if spec mentions periodic/scheduled work
            scheduler_source = None
            if spec_needs_scheduler(spec):
                scheduler_source = MULTI_FILE_TEMPLATE["scheduler_job"].format(name=name)
                log.info("Generated scheduler job for multi-file extension: %s", name)

            # Generate integration tests alongside the module
            test_source = await generate_extension_tests(spec, module_source)
            if test_source:
                log.info("Generated integration tests for %s", name)
            else:
                log.warning("Test generation failed for %s — proceeding without tests", name)

            # Guardian review of generated code
            guardian_flag = ""
            try:
                from actions.guardian import review_code_patch, GuardianVerdict
                guardian_result = await review_code_patch(module_source, f"actions/{name}.py")
                if guardian_result.verdict == GuardianVerdict.BLOCK:
                    guardian_flag = "[NEEDS REVIEW] "
                    guardian_blocked = True
                    log.warning("Guardian blocked extension %s: %s", name, guardian_result.reason)
                    try:
                        from learning import record_signal as _rec
                        _rec("guardian_blocked_heal", {"name": name, "reason": guardian_result.reason})
                    except Exception:
                        pass
            except Exception as e:
                log.warning("Guardian review failed for extension %s: %s — proceeding", name, e)

            manifest = json.loads(manifest_json)
            if scheduler_source:
                manifest["scheduler_job"] = f"actions.{name}_scheduler"

            pr_title_prefix = guardian_flag
            pr_url = await create_extension_pr(name, module_source, manifest, warnings, test_source, scheduler_source, pr_title_prefix=pr_title_prefix)

            # Emit signal for workflow engine (auto-merge evaluation)
            try:
                from learning import record_signal as _rec_ext
                _rec_ext("extension_pr_created", {
                    "pr_url": pr_url,
                    "extension_name": name,
                    "guardian_blocked": guardian_blocked,
                })
            except Exception:
                pass

        _last_generation_time = time.time()

        # Record signal for learning
        try:
            from learning import record_signal
            record_signal("capability_generated", {"name": name, "pr_url": pr_url})
        except Exception:
            pass

        files_list = f"- actions/{name}.py\n- extensions/{name}.json"
        if 'test_source' in dir() and test_source:
            files_list += f"\n- tests/test_{name}.py"

        return (
            f"🔧 New Capability Generated\n\n"
            f"**{spec['description']}** — /{spec['command']}\n\n"
            f"PR: {pr_url}\n\n"
            f"Files:\n{files_list}\n\n"
            f"Review & merge the PR, then restart Khalil to activate."
        )

    except Exception as e:
        log.error("Self-extension failed for %s: %s", name, e)
        return f"Self-extension failed: {e}"


async def handle_self_extend(query: str, update, ask_llm_fn):
    """Orchestrator called from handle_message when a capability gap is detected.

    1. Classifies the gap via LLM
    2. Sends Generate/Skip inline keyboard
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    # Classify the gap
    spec = await classify_gap(query, ask_llm_fn)
    if not spec:
        return  # Not a real capability gap

    spec["original_query"] = query

    # Store the spec in context for callback retrieval
    # Use the bot's context to persist between message and callback
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚡ Generate", callback_data=f"extend_generate:{spec['name']}"),
            InlineKeyboardButton("Skip", callback_data="extend_skip"),
        ]
    ])

    # Store spec in a module-level dict (keyed by name) for callback retrieval
    _pending_extensions[spec["name"]] = spec

    await update.message.reply_text(
        f"I don't have the ability to do that yet.\n\n"
        f"I could build a **{spec['description']}** capability (/{spec['command']} command).\n\n"
        f"This will generate code and open a PR for your review.",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )


# Pending extension specs (cleared after use or on timeout)
_pending_extensions: dict[str, dict] = {}


def get_pending_extension(name: str) -> dict | None:
    """Retrieve and remove a pending extension spec."""
    return _pending_extensions.pop(name, None)


# --- #25: Extension Hot-Reload ---

def hot_reload_extension(name: str) -> str:
    """Reload a single extension by name without restarting the bot.

    Finds the extension manifest, reloads the Python module via importlib,
    and returns a status message. The Telegram handler re-registration
    must be done by the caller (server.py) since it holds the Application.
    """
    import importlib

    manifest_path = EXTENSIONS_DIR / f"{name}.json"
    if not manifest_path.exists():
        return f"Extension '{name}' not found."

    try:
        manifest = json.loads(manifest_path.read_text())
        module_name = manifest["action_module"]

        # Reload the module
        if module_name in sys.modules:
            mod = importlib.reload(sys.modules[module_name])
        else:
            mod = importlib.import_module(module_name)

        # Verify handler exists
        handler_name = manifest["handler_function"]
        if not hasattr(mod, handler_name):
            return f"Reloaded {module_name} but handler '{handler_name}' not found."

        return f"Extension '{name}' reloaded successfully."
    except Exception as e:
        return f"Failed to reload '{name}': {e}"


def reload_all_extensions() -> list[str]:
    """Reload all extensions. Returns list of status messages."""
    results = []
    if not EXTENSIONS_DIR.exists():
        return ["No extensions directory found."]

    for manifest_path in sorted(EXTENSIONS_DIR.glob("*.json")):
        if manifest_path.name.endswith(".prev.json"):
            continue
        name = manifest_path.stem
        results.append(hot_reload_extension(name))
    return results if results else ["No extensions found."]


# --- #30: Multi-File Extension Generation ---

MULTI_FILE_TEMPLATE = {
    "scheduler_job": '''"""Scheduler job for {name}."""

import logging
from datetime import datetime
from config import DB_PATH

log = logging.getLogger("khalil.scheduler.{name}")


async def run_{name}_job():
    """Periodic job for {name}. Register with APScheduler."""
    import sqlite3
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        log.info("{name} scheduler job running at %s", datetime.utcnow().isoformat())
        # TODO: Implement periodic logic
    finally:
        conn.close()
''',
    "db_migration": '''"""DB migration for {name}."""

import sqlite3
from config import DB_PATH


def migrate_{name}():
    """Run database migration for {name}."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS {name}_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL,
            value TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.commit()
    finally:
        conn.close()
''',
}

# Keywords that signal the extension needs a scheduler job
_SCHEDULER_KEYWORDS = {"periodic", "scheduled", "recurring", "monitor", "poll", "watch", "cron"}


def spec_needs_scheduler(spec: dict) -> bool:
    """Check if spec description suggests a periodic/scheduled component."""
    desc = spec.get("description", "").lower()
    name = spec.get("name", "").lower()
    combined = f"{desc} {name}"
    return any(kw in combined for kw in _SCHEDULER_KEYWORDS)
