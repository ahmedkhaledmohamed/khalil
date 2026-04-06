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
from pathlib import Path

from llm_client import get_llm_client, call_llm_sync

from config import (
    CLAUDE_MODEL_COMPLEX,
    HEALING_FAILURE_THRESHOLD,
    KHALIL_DIR,
)
from learning import _get_conn, record_signal, store_insight, get_insights

log = logging.getLogger("khalil.healing")

# Maps failure fingerprints to relevant source files + functions
FAILURE_CODE_MAP = {
    "intent_detection_failure:shell": [("server.py", "detect_intent"), ("server.py", "_try_direct_shell_intent")],
    "intent_detection_failure:reminder": [("server.py", "detect_intent")],
    "intent_detection_failure:email": [("server.py", "detect_intent")],
    "intent_detection_failure:calendar": [("server.py", "detect_intent")],
    "action_execution_failure:shell": [("actions/shell.py", "execute_shell"), ("actions/shell.py", "classify_command")],
    "action_execution_failure:calendar": [("actions/calendar.py", "get_today_events")],
    "action_execution_failure:email": [("actions/gmail.py", "draft_email"), ("actions/gmail.py", "send_draft")],
    "response_suggests_manual_action": [("server.py", "_try_direct_shell_intent"), ("server.py", "detect_intent")],
    "intent_pattern_miss": [("server.py", "_try_direct_shell_intent")],
    # Fallback mappings for generic/unknown fingerprints
    "capability_gap_detected:unknown": [("server.py", "call_llm_with_tools"), ("tool_catalog.py", "generate_tool_schemas")],
    "user_correction:unknown": [("server.py", "call_llm_with_tools"), ("server.py", "_build_system_prompt")],
}

# Prefix-based fallback: if exact fingerprint not found, try signal_type prefix
_FALLBACK_CODE_MAP = {
    "capability_gap_detected": [("server.py", "call_llm_with_tools"), ("tool_catalog.py", "filter_tools_for_query")],
    "user_correction": [("server.py", "call_llm_with_tools"), ("server.py", "_build_system_prompt")],
    "action_execution_failure": [("server.py", "_execute_tool_call")],
    "intent_detection_failure": [("server.py", "detect_intent")],
}

# Errors that are deterministic — trigger healing after just 1 occurrence
CRITICAL_ERROR_PATTERNS = ["ImportError", "ModuleNotFoundError", "AttributeError", "SyntaxError"]

# Signal types that are inherently deterministic — always threshold 1
DETERMINISTIC_SIGNAL_TYPES = {"response_suggests_manual_action", "capability_gap_detected", "intent_pattern_miss"}


# --- Failure Detection ---

def detect_recurring_failures() -> list[dict]:
    """Query recent failure signals and return triggers for recurring patterns.

    Returns list of {fingerprint, signal_type, failure_count, sample_signals}.
    """
    conn = _get_conn()
    from datetime import datetime, timedelta
    # Configurable signal window — default 7 days, overridable via settings table
    window_hours = 168  # 7 days
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = 'healing_signal_window_hours'").fetchone()
        if row:
            window_hours = max(1, int(row[0]))
    except Exception:
        pass
    cutoff = (datetime.utcnow() - timedelta(hours=window_hours)).strftime("%Y-%m-%d %H:%M:%S")

    failure_types = (
        "intent_detection_failure", "action_execution_failure", "user_correction",
        "extension_runtime_failure", "response_suggests_manual_action",
        "capability_gap_detected",
    )
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
        hint = ctx.get("action_hint") or ctx.get("action") or ctx.get("suggested_cmd", "unknown")
        fingerprint = f"{r['signal_type']}:{hint}"
        groups.setdefault(fingerprint, []).append({
            "signal_type": r["signal_type"],
            "context": ctx,
            "created_at": r["created_at"],
        })

    triggers = []
    for fingerprint, signals in groups.items():
        # Deterministic signals and critical errors trigger after just 1 occurrence
        signal_type = signals[0]["signal_type"]
        is_deterministic = signal_type in DETERMINISTIC_SIGNAL_TYPES
        has_critical = any(
            any(pat in str(s["context"].get("error", "")) for pat in CRITICAL_ERROR_PATTERNS)
            for s in signals
        )
        threshold = 1 if (is_deterministic or has_critical) else HEALING_FAILURE_THRESHOLD
        if len(signals) < threshold:
            continue

        # Dedup: skip if we already created a self_heal insight for this fingerprint recently
        # BUT allow re-triggering if the previous heal failed
        recent_heals = conn.execute(
            "SELECT id, summary FROM insights WHERE category = 'self_heal' AND evidence LIKE ? AND created_at > ?",
            (f"%{fingerprint}%", (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")),
        ).fetchall()
        if recent_heals and not any("failed_heal" in (h["summary"] or "") for h in recent_heals):
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


def check_heal_outcomes() -> list[dict]:
    """Check if previous heals actually fixed the problem.

    Returns list of fingerprints where healing failed (signals recurred after heal).
    Marks those insights as 'failed_heal' so they can be re-triggered.
    """
    conn = _get_conn()
    from datetime import datetime, timedelta

    # Find recent self_heal insights
    recent_heals = conn.execute(
        "SELECT id, evidence, summary, created_at FROM insights "
        "WHERE category = 'self_heal' AND created_at > ? AND summary NOT LIKE '%failed_heal%'",
        ((datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S"),),
    ).fetchall()

    failed = []
    for heal in recent_heals:
        # Extract fingerprint from evidence
        evidence = heal["evidence"] or ""
        # Evidence contains the fingerprint — extract it
        import re as _re
        fp_match = _re.search(r"([\w_]+:[\w_]+)", evidence)
        if not fp_match:
            continue
        fingerprint = fp_match.group(1)

        # Check if new failures with same fingerprint appeared AFTER the heal
        recurrences = conn.execute(
            "SELECT COUNT(*) as cnt FROM interaction_signals "
            "WHERE signal_type || ':' || json_extract(context, '$.action') = ? "
            "AND created_at > ?",
            (fingerprint, heal["created_at"]),
        ).fetchone()

        # Also try with action_hint for intent failures
        recurrences2 = conn.execute(
            "SELECT COUNT(*) as cnt FROM interaction_signals "
            "WHERE signal_type || ':' || json_extract(context, '$.action_hint') = ? "
            "AND created_at > ?",
            (fingerprint, heal["created_at"]),
        ).fetchone()

        total = (recurrences["cnt"] if recurrences else 0) + (recurrences2["cnt"] if recurrences2 else 0)
        if total > 0:
            # Mark as failed heal
            conn.execute(
                "UPDATE insights SET summary = ? WHERE id = ?",
                (f"[failed_heal] {heal['summary'] or ''}", heal["id"]),
            )
            conn.commit()
            failed.append({"fingerprint": fingerprint, "insight_id": heal["id"], "recurrences": total})
            log.warning("Heal for %s failed — %d recurrences after fix (insight #%d)",
                        fingerprint, total, heal["id"])

    return failed


# --- #14: Healing Rollback Detector ---

def detect_healing_regressions(hours: int = 24) -> list[dict]:
    """Detect heals that caused regressions and recommend reverts.

    Finds heals merged in the last N hours (insights with category='self_heal'
    and status='applied'), checks if NEW failure signals appeared after the heal
    timestamp, and creates a revert recommendation insight if regressions found.

    Returns list of {heal_insight_id, fingerprint, new_failures, revert_recommended}.
    """
    conn = _get_conn()
    from datetime import datetime, timedelta
    import re as _re

    cutoff = (datetime.utcnow() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")

    # Find applied heals in the window
    applied_heals = conn.execute(
        "SELECT id, evidence, summary, created_at, resolved_at FROM insights "
        "WHERE category = 'self_heal' AND status = 'applied' AND resolved_at > ?",
        (cutoff,),
    ).fetchall()

    regressions = []
    for heal in applied_heals:
        evidence = heal["evidence"] or ""
        fp_match = _re.search(r"([\w_]+:[\w_]+)", evidence)
        if not fp_match:
            continue
        fingerprint = fp_match.group(1)

        # Check for NEW failure signals after the heal was applied
        heal_ts = heal["resolved_at"] or heal["created_at"]
        new_failures_action = conn.execute(
            "SELECT COUNT(*) as cnt FROM interaction_signals "
            "WHERE signal_type || ':' || json_extract(context, '$.action') = ? "
            "AND created_at > ?",
            (fingerprint, heal_ts),
        ).fetchone()
        new_failures_hint = conn.execute(
            "SELECT COUNT(*) as cnt FROM interaction_signals "
            "WHERE signal_type || ':' || json_extract(context, '$.action_hint') = ? "
            "AND created_at > ?",
            (fingerprint, heal_ts),
        ).fetchone()

        total = (new_failures_action["cnt"] if new_failures_action else 0) + \
                (new_failures_hint["cnt"] if new_failures_hint else 0)

        if total > 0:
            revert_recommended = total >= 2  # Recommend revert if 2+ new failures
            regressions.append({
                "heal_insight_id": heal["id"],
                "fingerprint": fingerprint,
                "new_failures": total,
                "revert_recommended": revert_recommended,
            })

            # Create a revert recommendation insight if warranted
            if revert_recommended:
                store_insight(
                    "self_heal_revert",
                    f"Revert recommended: heal for {fingerprint} caused {total} new failures",
                    f"heal_insight_id={heal['id']}, fingerprint={fingerprint}",
                    f"Consider reverting the heal PR. {total} new failures detected after applying fix.",
                )
                log.warning("Regression detected for heal #%d (%s): %d new failures, revert recommended",
                            heal["id"], fingerprint, total)

    return regressions


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


def _extract_targets_from_traceback(signals: list[dict]) -> list[tuple[str, str]] | None:
    """Extract (file, function) targets from traceback strings in signal context."""
    import re as _re
    for signal in signals:
        error = signal.get("context", {}).get("error", "")
        # Match Python traceback frames: File "path/to/file.py", line N, in func_name
        frames = _re.findall(r'File "([^"]+)", line \d+, in (\w+)', error)
        if frames:
            # Use the last frame that's inside the khalil directory
            khalil_frames = [
                (f, func) for f, func in frames
                if "khalil" in f and func not in ("wrapper", "<module>")
            ]
            if khalil_frames:
                filepath, func = khalil_frames[-1]
                # Convert absolute path to relative
                rel = filepath.split("khalil/")[-1] if "khalil/" in filepath else filepath
                return [(rel, func)]
    return None


def build_diagnosis(trigger: dict) -> dict | None:
    """Assemble diagnosis context for Claude Opus."""
    fingerprint = trigger["fingerprint"]
    signals = trigger["sample_signals"]

    # Find relevant source files (3-tier lookup)
    code_targets = FAILURE_CODE_MAP.get(fingerprint)

    if not code_targets:
        # Tier 2: prefix-based fallback (e.g., "capability_gap_detected" for any hint)
        signal_type = fingerprint.split(":")[0]
        code_targets = _FALLBACK_CODE_MAP.get(signal_type)

    if not code_targets:
        # Tier 3: partial match on signal type in FAILURE_CODE_MAP
        for key, targets in FAILURE_CODE_MAP.items():
            if key.startswith(signal_type):
                code_targets = targets
                break

    if not code_targets:
        # Tier 4: extract file/function from traceback in signal context
        code_targets = _extract_targets_from_traceback(signals)
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

    # #13: Flag multi-function when root cause likely spans multiple functions
    multi_function = len(source_context) >= 2

    return {
        "fingerprint": fingerprint,
        "summary": summary,
        "failure_count": trigger["failure_count"],
        "sample_queries": sample_queries,
        "signals": signals,
        "source_context": source_context,
        "primary_target": primary_target,
        "multi_function": multi_function,
    }


# --- Patch Generation ---

async def generate_healing_patch(diagnosis: dict) -> tuple[str, str] | None:
    """Use Claude Opus to generate a fixed function. Returns (patched_source, explanation) or None.

    When diagnosis['multi_function'] is True, generates fixes for all related functions
    separated by ---FUNCTION--- markers. The returned patched_source contains all fixes.
    """
    multi_function = diagnosis.get("multi_function", False)
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

    # Specialized prompt for intent pattern misses — ask for regex additions
    if diagnosis["fingerprint"].startswith("intent_pattern_miss"):
        matched_action = None
        for s in diagnosis["signals"]:
            ctx = s.get("context", {})
            if isinstance(ctx, str):
                import json as _json
                try:
                    ctx = _json.loads(ctx)
                except Exception:
                    ctx = {}
            matched_action = ctx.get("matched_action")
            if matched_action:
                break

        prompt = f"""You are adding regex patterns to Khalil's intent detection function.

## Problem
These queries should route to action "{matched_action}" but the existing regex patterns don't match them:
{sample_failures}

## Current Function (in {primary['file']})
```python
{primary_source['source']}
```

## Requirements
1. Add NEW regex pattern(s) to handle the failing queries
2. Insert them near the existing patterns for "{matched_action}" action
3. Keep patterns simple — use \\b word boundaries and common word order variations
4. Do NOT remove or change existing patterns
5. Do NOT change the function signature
6. Do NOT add new imports

Respond in this format:
EXPLANATION: <one sentence>
IMPORTS: none
```python
<complete function with new patterns added>
```"""

        try:
            client, client_type = get_llm_client()
            text = call_llm_sync(
                client, client_type, CLAUDE_MODEL_COMPLEX,
                "You are a Python expert adding regex patterns to an intent detection function. Output ONLY the format requested.",
                prompt, max_tokens=4000,
            ).strip()
        except Exception as e:
            log.error("LLM API failed for pattern miss healing: %s", e)
            return None

        explanation = ""
        if text.startswith("EXPLANATION:"):
            lines = text.split("\n", 1)
            explanation = lines[0].replace("EXPLANATION:", "").strip()
            text = lines[1] if len(lines) > 1 else ""

        code_match = re.search(r"```python\s*\n(.+?)```", text, re.DOTALL)
        if not code_match:
            log.warning("No code block found in healing response for pattern miss")
            return None

        return code_match.group(1).strip(), explanation

    if multi_function:
        all_funcs = "\n\n".join(
            f"### Function `{s['function']}` in {s['file']}\n```python\n{s['source']}\n```"
            for s in diagnosis["source_context"]
        )
        prompt = f"""You are fixing a bug in Khalil, a Python Telegram bot assistant.

## Problem
{diagnosis['summary']}

## Failure Examples
{sample_failures}

The root cause spans multiple related functions. Fix ALL of them.

## Functions to Fix
{all_funcs}

## Fix Requirements
1. Generate a MINIMAL fix for EACH function — change as few lines as possible
2. The fix must handle the failure cases without breaking the normal path
3. Prefer adding direct pattern-matching fallbacks over changing LLM prompts
4. Do NOT change function signatures
5. Do NOT add new imports unless absolutely necessary (list them separately if needed)

Respond in this format:
EXPLANATION: <one sentence explaining the fix>
IMPORTS: <any new imports needed, one per line, or "none">

Output each fixed function separated by a line containing only ---FUNCTION---:
```python
<complete fixed function 1>
```
---FUNCTION---
```python
<complete fixed function 2>
```"""
    else:
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
        client, client_type = get_llm_client()
        text = call_llm_sync(
            client, client_type, CLAUDE_MODEL_COMPLEX,
            "You are a Python expert fixing bugs in an existing codebase. Output ONLY the format requested.",
            prompt, max_tokens=4000 if multi_function else 2000,
        ).strip()
    except Exception as e:
        log.error("LLM API failed for healing patch: %s", e)
        return None

    # Parse response
    explanation = ""
    if text.startswith("EXPLANATION:"):
        lines = text.split("\n")
        explanation = lines[0].replace("EXPLANATION:", "").strip()

    # Extract new imports if any
    new_imports = ""
    if "IMPORTS:" in text:
        imports_line = [l for l in text.split("\n") if l.startswith("IMPORTS:")][0]
        imports_text = imports_line.replace("IMPORTS:", "").strip()
        if imports_text.lower() != "none":
            new_imports = imports_text

    if multi_function:
        # #13: Parse multi-function response
        patches = parse_multi_function_patch(text)
        if not patches:
            log.error("No code blocks found in multi-function healing response")
            return None
        code = "\n---FUNCTION---\n".join(patches)
    else:
        # Extract single code block
        if "```python" in text:
            code = text.split("```python")[1].split("```")[0].strip()
        elif "```" in text:
            code = text.split("```")[1].split("```")[0].strip()
        else:
            log.error("No code block found in healing response")
            return None

    if new_imports:
        code = new_imports + "\n\n" + code

    return code, explanation


def parse_multi_function_patch(text: str) -> list[str]:
    """#13: Split a multi-function healing response into individual function patches.

    Expects code blocks separated by ---FUNCTION--- markers.
    Returns list of function source strings, or empty list on parse failure.
    """
    # Split on the marker
    sections = text.split("---FUNCTION---")
    patches = []
    for section in sections:
        # Extract code block from each section
        if "```python" in section:
            code = section.split("```python")[1].split("```")[0].strip()
            if code:
                patches.append(code)
        elif "```" in section:
            code = section.split("```")[1].split("```")[0].strip()
            if code:
                patches.append(code)
    return patches


# --- #19: Healing Confidence Scoring ---

def score_healing_confidence(diagnosis: dict, patched_source: str) -> float:
    """Score a healing patch's confidence based on similarity to past successful heals.

    Returns a score between 0.0 and 1.0. Higher = more confident.
    Factors:
    - Past heals for same fingerprint pattern: +0.3
    - Patch size (smaller = more confident): +0.2 to +0.3
    - Signal count (more signals = clearer pattern): +0.1 to +0.3
    """
    conn = _get_conn()
    score = 0.1  # Base confidence

    fingerprint = diagnosis.get("fingerprint", "")
    failure_count = diagnosis.get("failure_count", 0)

    # Factor 1: Past successful heals for similar patterns
    try:
        past_heals = conn.execute(
            "SELECT COUNT(*) FROM insights WHERE category = 'self_heal' "
            "AND summary NOT LIKE '%failed_heal%' AND evidence LIKE ?",
            (f"%{fingerprint.split(':')[0]}%",),
        ).fetchone()[0]
        if past_heals > 0:
            score += min(0.3, past_heals * 0.1)
    except Exception:
        pass

    # Factor 2: Patch size — smaller patches are more likely correct
    lines = patched_source.strip().count("\n") + 1
    if lines <= 10:
        score += 0.3
    elif lines <= 30:
        score += 0.2
    else:
        score += 0.1

    # Factor 3: Signal count — more evidence = clearer pattern
    if failure_count >= 5:
        score += 0.3
    elif failure_count >= 3:
        score += 0.2
    else:
        score += 0.1

    return min(1.0, round(score, 2))


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
            title_prefix = "[NEEDS REVIEW] " if diagnosis.get("_guardian_blocked") else ""
            result = _run_gh(
                "pr", "create",
                "--title", f"{title_prefix}Khalil self-heal: {diagnosis['summary'][:60]}",
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

async def run_self_healing(triggers: list[dict], channel, chat_id: int):
    """Main orchestrator — diagnose, patch, validate, PR, notify.

    Args:
        triggers: list of failure trigger dicts from detect_recurring_failures()
        channel: Channel instance (channels.Channel protocol) for sending notifications
        chat_id: owner chat ID to send notifications to
    """
    for trigger in triggers:
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
            await channel.send_message(
                chat_id,
                f"🔧 Detected recurring failure: {diagnosis['summary']}\n\n"
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
            await channel.send_message(
                chat_id,
                f"🔧 Detected recurring failure: {diagnosis['summary']}\n\n"
                f"Generated a fix but it failed validation: {error}\n"
                f"Manual investigation needed.",
            )
            store_insight("self_heal", diagnosis["summary"], fingerprint, f"Validation failed: {error}")
            continue

        # 3.5. Guardian review of the healing patch
        guardian_blocked = False
        try:
            from actions.guardian import review_code_patch, GuardianVerdict
            guardian_result = await review_code_patch(patched_func, primary["file"])
            if guardian_result.verdict == GuardianVerdict.BLOCK:
                guardian_blocked = True
                log.warning("Guardian blocked healing patch for %s: %s", fingerprint, guardian_result.reason)
                try:
                    from learning import record_signal
                    record_signal("guardian_blocked_heal", {
                        "fingerprint": fingerprint, "reason": guardian_result.reason,
                    })
                except Exception:
                    pass
        except Exception as e:
            log.warning("Guardian review failed for healing patch: %s — proceeding", e)

        # 4. Create PR (prefix title with [NEEDS REVIEW] if guardian blocked)
        patched_content = substitute_function_in_file(target_path, patched_func)
        if guardian_blocked:
            diagnosis = {**diagnosis, "_guardian_blocked": True}
        try:
            pr_url = await create_healing_pr(primary["file"], patched_content, diagnosis)
        except Exception as e:
            log.error("Failed to create healing PR: %s", e)
            await channel.send_message(
                chat_id,
                f"🔧 Detected recurring failure: {diagnosis['summary']}\n\n"
                f"Fix generated and validated, but PR creation failed: {e}",
            )
            continue

        # 5. Emit signal for workflow engine (auto-merge evaluation)
        confidence = score_healing_confidence(diagnosis, patched_func)
        lines_changed = patched_func.strip().count("\n") + 1
        record_signal("self_heal_pr_created", {
            "pr_url": pr_url,
            "confidence": confidence,
            "lines_changed": lines_changed,
            "guardian_blocked": guardian_blocked,
            "fingerprint": fingerprint,
        })

        # 5.5. Auto-merge high-confidence, small, non-blocked patches
        auto_merged = False
        if (
            not guardian_blocked
            and confidence >= 0.7
            and lines_changed <= 15
        ):
            try:
                import subprocess as _sp
                merge_result = _sp.run(
                    ["gh", "pr", "merge", pr_url, "--squash", "--auto"],
                    capture_output=True, text=True, timeout=30,
                )
                if merge_result.returncode == 0:
                    auto_merged = True
                    log.info("Auto-merged healing PR: %s (confidence=%.2f)", pr_url, confidence)
                else:
                    log.warning("Auto-merge failed for %s: %s", pr_url, merge_result.stderr[:200])
            except Exception as e:
                log.warning("Auto-merge attempt failed: %s", e)

        # 6. Notify
        merge_note = " (auto-merged)" if auto_merged else "\n\nReview and merge to apply the fix."
        await channel.send_message(
            chat_id,
            f"🔧 Self-Healing: {diagnosis['summary']}\n\n"
            f"{explanation}\n\n"
            f"PR: {pr_url}{merge_note}",
        )

        # 7. Record insight to prevent re-triggering
        store_insight(
            "self_heal",
            diagnosis["summary"],
            fingerprint,
            f"Fix generated and PR opened: {pr_url}",
        )

        log.info("Self-healing PR created: %s", pr_url)
