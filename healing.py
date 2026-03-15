"""Self-healing engine — detect recurring failures, generate patches, open PRs.

When Khalil's existing functionality fails repeatedly (e.g., LLM returns garbage
instead of JSON for intent detection), this module detects the pattern, uses Claude
Opus to generate a targeted code fix, validates it, and opens a PR for review.

Flow: record_signal → detect_recurring_failures → build_diagnosis →
      generate_healing_patch → validate_patch → create_healing_pr → notify
"""

import ast
import asyncio
import json
import logging
import time
from pathlib import Path

import anthropic

from config import (
    CLAUDE_MODEL_COMPLEX,
    HEALING_COOLDOWN_SECONDS,
    HEALING_FAILURE_THRESHOLD,
    KHALIL_DIR,
)
from learning import _get_conn, store_insight, get_insights

log = logging.getLogger("khalil.healing")

# Rate limiting
_last_heal_time: float = 0

# Maps failure fingerprints to relevant source files + functions
FAILURE_CODE_MAP = {
    "intent_detection_failure:shell": [("server.py", "detect_intent"), ("server.py", "_try_direct_shell_intent")],
    "intent_detection_failure:reminder": [("server.py", "detect_intent")],
    "intent_detection_failure:email": [("server.py", "detect_intent")],
    "intent_detection_failure:calendar": [("server.py", "detect_intent")],
    "action_execution_failure:shell": [("actions/shell.py", "execute_shell"), ("actions/shell.py", "classify_command")],
}


# --- Failure Detection ---

def detect_recurring_failures() -> list[dict]:
    """Query recent failure signals and return triggers for recurring patterns.

    Returns list of {fingerprint, signal_type, failure_count, sample_signals}.
    """
    conn = _get_conn()
    from datetime import datetime, timedelta
    cutoff = (datetime.utcnow() - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")

    failure_types = ("intent_detection_failure", "action_execution_failure", "user_correction")
    placeholders = ",".join("?" for _ in failure_types)
    rows = conn.execute(
        f"SELECT signal_type, context, created_at FROM interaction_signals "
        f"WHERE signal_type IN ({placeholders}) AND created_at > ? ORDER BY created_at",
        (*failure_types, cutoff),
    ).fetchall()

    if not rows:
        return []

    # Group by fingerprint
    groups: dict[str, list[dict]] = {}
    for r in rows:
        ctx = json.loads(r["context"]) if r["context"] else {}
        # Build fingerprint from signal type + action hint or action type
        hint = ctx.get("action_hint") or ctx.get("action") or "unknown"
        fingerprint = f"{r['signal_type']}:{hint}"
        groups.setdefault(fingerprint, []).append({
            "signal_type": r["signal_type"],
            "context": ctx,
            "created_at": r["created_at"],
        })

    triggers = []
    for fingerprint, signals in groups.items():
        if len(signals) < HEALING_FAILURE_THRESHOLD:
            continue

        # Dedup: skip if we already created a self_heal insight for this fingerprint recently
        recent_heals = conn.execute(
            "SELECT id FROM insights WHERE category = 'self_heal' AND evidence LIKE ? AND created_at > ?",
            (f"%{fingerprint}%", (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")),
        ).fetchall()
        if recent_heals:
            log.debug("Skipping %s — already healed recently (insight #%d)", fingerprint, recent_heals[0]["id"])
            continue

        triggers.append({
            "fingerprint": fingerprint,
            "signal_type": signals[0]["signal_type"],
            "failure_count": len(signals),
            "sample_signals": signals[:5],
        })

    if triggers:
        log.info("Detected %d recurring failure pattern(s): %s",
                 len(triggers), [t["fingerprint"] for t in triggers])
    return triggers


# --- Diagnosis ---

def extract_function_source(file_path: Path, function_name: str) -> tuple[str, int, int] | None:
    """Extract a function's source code using AST. Returns (source, start_line, end_line) or None."""
    try:
        source = file_path.read_text()
        tree = ast.parse(source)
    except (OSError, SyntaxError) as e:
        log.error("Failed to parse %s: %s", file_path, e)
        return None

    lines = source.splitlines(keepends=True)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            start = node.lineno - 1  # 0-indexed
            end = node.end_lineno  # already 1-indexed, so this is exclusive
            func_source = "".join(lines[start:end])
            return func_source, start, end

    log.warning("Function %s not found in %s", function_name, file_path)
    return None


def build_diagnosis(trigger: dict) -> dict | None:
    """Assemble diagnosis context for Claude Opus."""
    fingerprint = trigger["fingerprint"]
    signals = trigger["sample_signals"]

    # Find relevant source files
    code_targets = FAILURE_CODE_MAP.get(fingerprint)
    if not code_targets:
        # Try partial match on signal type
        signal_type = fingerprint.split(":")[0]
        for key, targets in FAILURE_CODE_MAP.items():
            if key.startswith(signal_type):
                code_targets = targets
                break

    if not code_targets:
        log.warning("No code mapping for fingerprint: %s", fingerprint)
        return None

    # Extract function source code
    source_context = []
    primary_target = None
    for rel_path, func_name in code_targets:
        file_path = KHALIL_DIR / rel_path
        result = extract_function_source(file_path, func_name)
        if result:
            func_source, start, end = result
            source_context.append({
                "file": rel_path,
                "function": func_name,
                "source": func_source,
                "start_line": start,
                "end_line": end,
            })
            if primary_target is None:
                primary_target = {"file": rel_path, "function": func_name}

    if not source_context:
        log.warning("Could not extract any source for %s", fingerprint)
        return None

    # Build failure summary
    sample_queries = [s["context"].get("query", "N/A") for s in signals[:3]]
    summary = f"{trigger['signal_type'].replace('_', ' ')} ({trigger['failure_count']}x in 48h)"

    return {
        "fingerprint": fingerprint,
        "summary": summary,
        "failure_count": trigger["failure_count"],
        "sample_queries": sample_queries,
        "signals": signals,
        "source_context": source_context,
        "primary_target": primary_target,
    }


# --- Patch Generation ---

async def generate_healing_patch(diagnosis: dict) -> tuple[str, str] | None:
    """Use Claude Opus to generate a fixed function. Returns (patched_source, explanation) or None."""
    primary = diagnosis["primary_target"]
    primary_source = next(
        s for s in diagnosis["source_context"] if s["function"] == primary["function"]
    )

    # Build context from all related functions
    related_code = ""
    for s in diagnosis["source_context"]:
        if s["function"] != primary["function"]:
            related_code += f"\n\n# Related function in {s['file']}:\n{s['source']}"

    sample_failures = "\n".join(
        f"- Query: \"{q}\"" for q in diagnosis["sample_queries"]
    )

    prompt = f"""You are fixing a bug in Khalil, a Python Telegram bot assistant.

## Problem
{diagnosis['summary']}

## Failure Examples
{sample_failures}

These inputs triggered the function but it failed to produce the expected result, causing the bot to fall through to a generic conversational response instead of taking action.

## Current Function (in {primary['file']})
```python
{primary_source['source']}
```
{f"## Related Code{related_code}" if related_code else ""}

## Fix Requirements
1. Generate a MINIMAL fix — change as few lines as possible
2. The fix must handle the failure cases without breaking the normal path
3. Prefer adding direct pattern-matching fallbacks over changing LLM prompts
4. Do NOT change the function signature
5. Do NOT add new imports unless absolutely necessary (list them separately if needed)

Respond in this format:
EXPLANATION: <one sentence explaining the fix>
IMPORTS: <any new imports needed, one per line, or "none">
```python
<complete fixed function source code>
```"""

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=CLAUDE_MODEL_COMPLEX,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
            system="You are a Python expert fixing bugs in an existing codebase. Output ONLY the format requested.",
        )
        text = response.content[0].text.strip()
    except Exception as e:
        log.error("Claude API failed for healing patch: %s", e)
        return None

    # Parse response
    explanation = ""
    if text.startswith("EXPLANATION:"):
        lines = text.split("\n")
        explanation = lines[0].replace("EXPLANATION:", "").strip()

    # Extract code block
    if "```python" in text:
        code = text.split("```python")[1].split("```")[0].strip()
    elif "```" in text:
        code = text.split("```")[1].split("```")[0].strip()
    else:
        log.error("No code block found in healing response")
        return None

    # Extract new imports if any
    new_imports = ""
    if "IMPORTS:" in text:
        imports_line = [l for l in text.split("\n") if l.startswith("IMPORTS:")][0]
        imports_text = imports_line.replace("IMPORTS:", "").strip()
        if imports_text.lower() != "none":
            new_imports = imports_text

    if new_imports:
        code = new_imports + "\n\n" + code

    return code, explanation


# --- Patch Validation ---

def validate_patch(original_source: str, patched_source: str, target_file: Path) -> tuple[bool, str]:
    """Validate a generated patch is safe and correct."""
    from actions.extend import BLOCKLISTED_CALLS, BLOCKLISTED_IMPORTS

    # 1. AST parse
    try:
        tree = ast.parse(patched_source)
    except SyntaxError as e:
        return False, f"Syntax error in patch: {e}"

    # 2. Blocklist check
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in BLOCKLISTED_IMPORTS:
                    return False, f"Blocked import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in BLOCKLISTED_IMPORTS:
                return False, f"Blocked import: {node.module}"
        if isinstance(node, ast.Call):
            from actions.extend import _get_call_name
            call_name = _get_call_name(node)
            if call_name and any(b in call_name for b in BLOCKLISTED_CALLS):
                return False, f"Blocked call: {call_name}"

    # 3. Size guard — patched function shouldn't be >2x original
    orig_lines = len(original_source.splitlines())
    patch_lines = len(patched_source.splitlines())
    if patch_lines > orig_lines * 2 + 10:
        return False, f"Patch too large ({patch_lines} lines vs {orig_lines} original)"

    # 4. Full-file compilation check
    full_patched = substitute_function_in_file(target_file, patched_source)
    if full_patched is None:
        return False, "Failed to substitute function in full file"
    try:
        compile(full_patched, str(target_file), "exec")
    except SyntaxError as e:
        return False, f"Full file compilation failed: {e}"

    return True, ""


def substitute_function_in_file(file_path: Path, new_func_source: str) -> str | None:
    """Replace a function in a file with new source. Returns full patched file or None."""
    try:
        original = file_path.read_text()
        tree = ast.parse(original)
    except (OSError, SyntaxError):
        return None

    # Find the function name in the new source
    try:
        new_tree = ast.parse(new_func_source)
    except SyntaxError:
        return None

    func_name = None
    for node in ast.walk(new_tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_name = node.name
            break

    if not func_name:
        return None

    # Find the function in the original file
    lines = original.splitlines(keepends=True)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            start = node.lineno - 1
            end = node.end_lineno
            # Replace lines
            patched_lines = lines[:start] + [new_func_source + "\n"] + lines[end:]
            return "".join(patched_lines)

    return None


# --- PR Creation ---

async def create_healing_pr(target_file: str, patched_content: str, diagnosis: dict) -> str:
    """Create a branch, commit the patched file, push, and open a PR. Returns PR URL."""
    from actions.extend import _run_git, _run_gh

    fingerprint = diagnosis["fingerprint"].replace(":", "-")
    branch_name = f"khalil-heal/{fingerprint}"

    def _git_workflow():
        _run_gh("auth", "status")
        original_branch = _run_git("branch", "--show-current").stdout.strip()
        stashed = False
        status = _run_git("status", "--porcelain").stdout.strip()
        if status:
            _run_git("stash", "push", "-m", f"khalil-heal-{fingerprint}")
            stashed = True
        try:
            _run_git("checkout", "main")
            _run_git("pull", "--ff-only")
            _run_git("checkout", "-b", branch_name)

            # Write patched file
            file_path = KHALIL_DIR / target_file
            file_path.write_text(patched_content)

            _run_git("add", str(file_path))
            _run_git("commit", "-m",
                      f"Fix {diagnosis['summary'][:60]} (auto-healed by Khalil)\n\n"
                      f"Fingerprint: {diagnosis['fingerprint']}\n"
                      f"Failures: {diagnosis['failure_count']}x in 48h\n\n"
                      f"Co-Authored-By: Khalil Bot <khalil@local>")
            _run_git("push", "-u", "origin", branch_name)

            body = _build_pr_body(diagnosis)
            result = _run_gh(
                "pr", "create",
                "--title", f"Khalil self-heal: {diagnosis['summary'][:60]}",
                "--body", body,
            )
            return result.stdout.strip()
        finally:
            try:
                _run_git("checkout", original_branch)
            except Exception:
                _run_git("checkout", "main")
            if stashed:
                try:
                    _run_git("stash", "pop")
                except Exception:
                    pass

    return await asyncio.to_thread(_git_workflow)


def _build_pr_body(diagnosis: dict) -> str:
    """Build the PR description."""
    samples = "\n".join(f"- `{q}`" for q in diagnosis["sample_queries"][:5])
    files = ", ".join(s["file"] for s in diagnosis["source_context"])
    return (
        f"## Self-Healing Fix\n\n"
        f"**Fingerprint:** `{diagnosis['fingerprint']}`\n"
        f"**Failures:** {diagnosis['failure_count']}x in the last 48 hours\n\n"
        f"### Sample Failing Inputs\n{samples}\n\n"
        f"### Files Modified\n{files}\n\n"
        f"### Review Checklist\n"
        f"- [ ] Fix addresses the root cause\n"
        f"- [ ] No regressions in normal flow\n"
        f"- [ ] No dangerous imports or calls\n\n"
        f"---\n"
        f"*Auto-generated by Khalil's self-healing engine*"
    )


# --- Orchestrator ---

async def run_self_healing(triggers: list[dict], bot, chat_id: int):
    """Main orchestrator — diagnose, patch, validate, PR, notify."""
    global _last_heal_time

    if time.time() - _last_heal_time < HEALING_COOLDOWN_SECONDS:
        log.info("Self-healing on cooldown, skipping")
        return

    for trigger in triggers[:1]:  # Process at most 1 per run
        fingerprint = trigger["fingerprint"]
        log.info("Self-healing: processing %s", fingerprint)

        # 1. Diagnose
        diagnosis = build_diagnosis(trigger)
        if not diagnosis:
            log.warning("Could not build diagnosis for %s", fingerprint)
            continue

        # 2. Generate patch
        result = await generate_healing_patch(diagnosis)
        if not result:
            await bot.send_message(
                chat_id=chat_id,
                text=f"🔧 Detected recurring failure: {diagnosis['summary']}\n\n"
                     f"Could not generate a fix automatically. Manual investigation needed.",
            )
            store_insight("self_heal", diagnosis["summary"], fingerprint, "Fix generation failed")
            continue

        patched_func, explanation = result

        # 3. Validate
        primary = diagnosis["primary_target"]
        target_path = KHALIL_DIR / primary["file"]
        original_source = next(
            s["source"] for s in diagnosis["source_context"]
            if s["function"] == primary["function"]
        )

        valid, error = validate_patch(original_source, patched_func, target_path)
        if not valid:
            log.error("Patch validation failed for %s: %s", fingerprint, error)
            await bot.send_message(
                chat_id=chat_id,
                text=f"🔧 Detected recurring failure: {diagnosis['summary']}\n\n"
                     f"Generated a fix but it failed validation: {error}\n"
                     f"Manual investigation needed.",
            )
            store_insight("self_heal", diagnosis["summary"], fingerprint, f"Validation failed: {error}")
            continue

        # 4. Create PR
        patched_content = substitute_function_in_file(target_path, patched_func)
        try:
            pr_url = await create_healing_pr(primary["file"], patched_content, diagnosis)
        except Exception as e:
            log.error("Failed to create healing PR: %s", e)
            await bot.send_message(
                chat_id=chat_id,
                text=f"🔧 Detected recurring failure: {diagnosis['summary']}\n\n"
                     f"Fix generated and validated, but PR creation failed: {e}",
            )
            continue

        _last_heal_time = time.time()

        # 5. Notify
        await bot.send_message(
            chat_id=chat_id,
            text=f"🔧 Self-Healing: {diagnosis['summary']}\n\n"
                 f"{explanation}\n\n"
                 f"PR: {pr_url}\n\n"
                 f"Review and merge to apply the fix.",
        )

        # 6. Record insight to prevent re-triggering
        store_insight(
            "self_heal",
            diagnosis["summary"],
            fingerprint,
            f"Fix generated and PR opened: {pr_url}",
        )

        log.info("Self-healing PR created: %s", pr_url)
