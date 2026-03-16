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
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

from config import (
    KHALIL_DIR, EXTENSIONS_DIR, CLAUDE_MODEL_COMPLEX,
    KEYRING_SERVICE, CLAUDE_CODE_BIN,
)

log = logging.getLogger("khalil.extend")

# Rate limit: max 1 generation per hour
_last_generation_time: float = 0
GENERATION_COOLDOWN_SECONDS = 3600

# --- Stage 1: Phrase-based capability gap detection ---

CAPABILITY_GAP_PHRASES = [
    "i can't do that",
    "i don't have the ability",
    "that capability isn't available",
    "i can't currently",
    "not something i can do yet",
    "i don't have a feature for",
    "i don't have that capability",
    "that's not something i support",
    "i'm not able to",
    "no built-in support for",
]


def detect_capability_gap(response: str) -> bool:
    """Stage 1: cheap phrase match on LLM response."""
    response_lower = response.lower()
    return any(phrase in response_lower for phrase in CAPABILITY_GAP_PHRASES)


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
]


async def classify_gap(query: str, ask_llm_fn) -> dict | None:
    """Stage 2: LLM classifies whether this is a real capability gap.

    Returns spec dict {"name": "...", "command": "...", "description": "..."}
    or None if it's just a knowledge gap or normal conversation.
    """
    # Add extension capabilities to the list
    ext_capabilities = _get_extension_capabilities()
    all_capabilities = EXISTING_CAPABILITIES + ext_capabilities

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

    try:
        # Handle markdown code blocks
        text = response.strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        result = json.loads(text.strip())
    except (json.JSONDecodeError, IndexError):
        log.warning("Gap classification returned invalid JSON: %s", response[:200])
        return None

    if result.get("type") != "capability_gap":
        return None

    name = result.get("name", "").strip()
    command = result.get("command", "").strip()
    description = result.get("description", "").strip()

    if not name or not command or not description:
        return None

    # Sanitize name (only alphanumeric + underscore)
    name = re.sub(r"[^a-z0-9_]", "_", name.lower())
    command = re.sub(r"[^a-z0-9]", "", command.lower())

    return {"name": name, "command": command, "description": description}


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


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_tables(conn: sqlite3.Connection):
    """Create any tables this extension needs. Called on startup."""
    # TODO: Add CREATE TABLE IF NOT EXISTS statements here
    pass


# --- Core functions ---
# TODO: Implement the capability


# --- Telegram command handler ---

async def cmd_COMMAND(update, context):
    """Handle /COMMAND command."""
    args = context.args
    # TODO: Implement command handling
    await update.message.reply_text("COMMAND_DESCRIPTION")
'''


async def generate_action_module(spec: dict, ask_llm_fn) -> tuple[str, str]:
    """Generate a new action module using Claude Opus.

    Args:
        spec: {"name": "...", "command": "...", "description": "..."}
        ask_llm_fn: async callable(query, context, system_extra) -> str

    Returns:
        (module_source_code, manifest_json)
    """
    # Check Claude API is available
    try:
        import keyring
        api_key = keyring.get_password(KEYRING_SERVICE, "anthropic-api-key")
    except Exception:
        api_key = None

    if not api_key:
        import os
        api_key = os.environ.get("ANTHROPIC_API_KEY")

    if not api_key:
        raise RuntimeError("Claude API key required for code generation. Ollama is not reliable enough.")

    # Read reminders.py as a pattern reference (it's a clean, self-contained action)
    template_path = KHALIL_DIR / "actions" / "reminders.py"
    template_source = template_path.read_text() if template_path.exists() else ACTION_TEMPLATE

    prompt = (
        f"Generate a complete Python module for a Khalil action called '{spec['name']}'.\n\n"
        f"**Capability**: {spec['description']}\n"
        f"**Command**: /{spec['command']}\n\n"
        "**Requirements**:\n"
        "1. Follow the EXACT patterns from the reference module below\n"
        "2. Use SQLite for any persistent state (via `config.DB_PATH`)\n"
        "3. Include an `ensure_tables(conn)` function that creates any needed tables\n"
        f"4. Include an async `cmd_{spec['command']}(update, context)` function as the Telegram handler\n"
        "5. Handle subcommands via `context.args` (e.g., /command add ..., /command list)\n"
        "6. Use `logging.getLogger(f'khalil.actions.{name}')` for logging\n"
        "7. Import only from stdlib, `config`, and existing khalil modules\n"
        "8. Include helpful reply_text messages\n"
        "9. Handle errors gracefully\n"
        "10. Keep it under 200 lines\n\n"
        "**Reference module** (follow this style exactly):\n"
        f"```python\n{template_source}\n```\n\n"
        "Respond with ONLY the Python source code. No markdown fences, no explanation."
    )

    import anthropic
    client = anthropic.AsyncAnthropic(api_key=api_key)

    response = await client.messages.create(
        model=CLAUDE_MODEL_COMPLEX,
        max_tokens=4000,
        system="You are a code generator for Khalil, a personal AI assistant. "
               "Generate clean, production-quality Python modules that follow existing patterns exactly. "
               "Output ONLY Python code, no markdown, no explanation.",
        messages=[{"role": "user", "content": prompt}],
    )

    source = response.content[0].text.strip()

    # Strip markdown fences if present
    if source.startswith("```"):
        lines = source.split("\n")
        lines = lines[1:]  # Remove opening fence
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        source = "\n".join(lines)

    # Generate manifest
    manifest = {
        "name": spec["name"],
        "command": spec["command"],
        "description": spec["description"],
        "action_module": f"actions.{spec['name']}",
        "handler_function": f"cmd_{spec['command']}",
        "generated_at": datetime.utcnow().isoformat(),
        "generated_for": spec.get("original_query", spec["description"]),
    }

    return source, json.dumps(manifest, indent=2)


# --- Claude Code CLI Generation ---


def _build_claude_code_prompt(spec: dict) -> str:
    """Build a detailed prompt for Claude Code CLI."""
    return (
        "You are adding a new capability to Khalil, a personal AI assistant "
        "(Telegram bot + FastAPI + SQLite).\n\n"
        f"TASK: Create the \"{spec['name']}\" capability.\n"
        f"Command: /{spec.get('command', spec['name'])}\n"
        f"Description: {spec['description']}\n\n"
        "REQUIREMENTS:\n"
        "1. Create `actions/{name}.py` following the exact pattern of existing action modules\n"
        "2. Create `extensions/{name}.json` manifest with keys: name, command, description, "
        "action_module, handler_function, generated_at, generated_for\n"
        "3. Read `actions/reminders.py` and `actions/gmail.py` as reference patterns\n"
        "4. Read `config.py` for available constants and paths\n"
        "5. Read `server.py` lines 1-50 and the _load_extensions function to understand "
        "how extensions are registered\n\n"
        "CONSTRAINTS:\n"
        "- Use only stdlib + packages already in requirements.txt\n"
        "- No subprocess, eval, exec, socket, ctypes\n"
        "- Use async functions for Telegram handlers\n"
        "- Use SQLite via config.DB_PATH for any state\n"
        "- Include ensure_tables(conn) if you need DB tables\n"
        "- Handle errors gracefully with logging\n"
        "- Keep under 300 lines\n\n"
        "Create ONLY the action module and manifest. Do not modify server.py."
    ).format(name=spec["name"])


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

        # Validate
        ok, err = validate_generated_code(module_source)
        if not ok:
            raise RuntimeError(f"Validation failed: {err}")

        # Commit and push from worktree, open PR
        pr_url = await _commit_and_pr_from_worktree(wt_path, branch, spec, manifest)
        return module_source, json.dumps(manifest), pr_url

    finally:
        cleanup_worktree(branch)


async def _commit_and_pr_from_worktree(
    wt_path: Path, branch: str, spec: dict, manifest: dict,
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

        # Open PR
        result = subprocess.run(
            ["gh", "pr", "create",
             "--title", f"Khalil: Add {name} capability (Claude Code)",
             "--body",
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
             f"- [ ] Command handler covers basic use cases\n\n"
             f"Generated by Khalil's self-extension engine using Claude Code CLI.",
             ],
            cwd=str(wt_path),
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"PR creation failed: {result.stderr}")
        return result.stdout.strip()

    return await asyncio.to_thread(_workflow)


# --- Validation ---

BLOCKLISTED_CALLS = {
    "subprocess", "os.system", "os.popen", "os.exec", "os.execv",
    "eval", "exec", "compile", "__import__", "importlib",
    "shutil.rmtree", "shutil.move", "ctypes",
    "socket.socket", "http.server",
}

BLOCKLISTED_IMPORTS = {
    "subprocess", "ctypes", "socket", "http.server", "xmlrpc",
}


def validate_generated_code(source: str) -> tuple[bool, str]:
    """Validate generated code is safe and syntactically correct.

    Returns (is_valid, error_message).
    """
    # 1. Syntax check via AST
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return False, f"Syntax error: {e}"

    # 2. Walk AST for blocklisted calls and imports
    for node in ast.walk(tree):
        # Check imports
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in BLOCKLISTED_IMPORTS:
                    return False, f"Blocked import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in BLOCKLISTED_IMPORTS:
                return False, f"Blocked import: {node.module}"

        # Check function calls
        if isinstance(node, ast.Call):
            call_name = _get_call_name(node)
            if call_name and any(b in call_name for b in BLOCKLISTED_CALLS):
                return False, f"Blocked call: {call_name}"

    # 3. Verify module has at least one async function (the command handler)
    has_async = any(
        isinstance(node, ast.AsyncFunctionDef)
        for node in ast.walk(tree)
    )
    if not has_async:
        return False, "Module must define at least one async function (the command handler)"

    # 4. Final compilation check
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
        f.write(source)
        f.flush()
        try:
            py_compile.compile(f.name, doraise=True)
        except py_compile.PyCompileError as e:
            return False, f"Compilation error: {e}"
        finally:
            Path(f.name).unlink(missing_ok=True)

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


async def create_extension_pr(name: str, module_source: str, manifest: dict) -> str:
    """Create a branch, commit the generated files, push, and open a PR.

    Returns the PR URL.
    """
    branch_name = f"khalil-extend/{name}"
    action_file = KHALIL_DIR / "actions" / f"{name}.py"
    manifest_file = EXTENSIONS_DIR / f"{name}.json"

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

            # Commit and push
            _run_git("add", str(action_file), str(manifest_file))
            _run_git(
                "commit", "-m",
                f"Add {name} capability (auto-generated by Khalil)\n\n"
                f"Co-Authored-By: Khalil Bot <khalil@local>",
            )
            _run_git("push", "-u", "origin", branch_name)

            # Open PR
            result = _run_gh(
                "pr", "create",
                "--title", f"Khalil: Add {name} capability",
                "--body",
                f"## Auto-generated capability\n\n"
                f"**Description**: {manifest.get('description', name) if isinstance(manifest, dict) else name}\n"
                f"**Command**: /{manifest.get('command', name) if isinstance(manifest, dict) else name}\n"
                f"**Generated for**: \"{manifest.get('generated_for', '') if isinstance(manifest, dict) else ''}\"\n\n"
                f"## Files\n"
                f"- `actions/{name}.py` — action module\n"
                f"- `extensions/{name}.json` — extension manifest\n\n"
                f"## Review checklist\n"
                f"- [ ] Code looks correct and safe\n"
                f"- [ ] No unnecessary imports or dangerous operations\n"
                f"- [ ] Tables schema makes sense\n"
                f"- [ ] Command handler covers basic use cases\n\n"
                f"Generated by Khalil's self-extension engine.",
            )
            pr_url = result.stdout.strip()

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

    try:
        complexity = classify_complexity(spec)
        log.info("Generating %s action module: %s", complexity, name)

        if complexity == "complex" and Path(CLAUDE_CODE_BIN).exists():
            # Complex path: Claude Code CLI in a worktree
            module_source, manifest_json, pr_url = await generate_with_claude_code(spec)
        else:
            # Simple path: raw Claude API call
            from server import ask_llm
            module_source, manifest_json = await generate_action_module(spec, ask_llm)

            valid, error = validate_generated_code(module_source)
            if not valid:
                log.error("Generated code failed validation: %s", error)
                return f"Generated code failed validation: {error}\nPlease try again or build this manually."

            manifest = json.loads(manifest_json)
            pr_url = await create_extension_pr(name, module_source, manifest)

        _last_generation_time = time.time()

        # Record signal for learning
        try:
            from learning import record_signal
            record_signal("capability_generated", {"name": name, "pr_url": pr_url})
        except Exception:
            pass

        return (
            f"🔧 New Capability Generated\n\n"
            f"**{spec['description']}** — /{spec['command']}\n\n"
            f"PR: {pr_url}\n\n"
            f"Files:\n"
            f"- actions/{name}.py\n"
            f"- extensions/{name}.json\n\n"
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
