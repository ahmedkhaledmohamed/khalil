"""Auto-fix cycle — bridge eval gaps to healing.py for automated remediation.

V1 scope: PATTERN_GAP only (adding regex patterns is purely additive and safe).
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from eval.gap_analysis import Gap, GapReport, GapCategory

log = logging.getLogger("khalil.eval.autofix")

KHALIL_DIR = Path(__file__).resolve().parent.parent


@dataclass
class FixAttempt:
    skill: str
    gap_count: int
    queries: list[str]          # failing queries
    fix_type: str               # "add_pattern"
    applied: bool = False
    confidence: float = 0.0
    error: str | None = None
    patch_preview: str = ""     # what was changed


def extract_fixable_gaps(report: GapReport, cases=None) -> dict[str, list[Gap]]:
    """Extract PATTERN_GAP gaps grouped by skill. Only these are auto-fixable in v1."""
    by_skill: dict[str, list[Gap]] = defaultdict(list)
    for gap in report.gaps:
        cat = gap.category.value if hasattr(gap.category, "value") else str(gap.category)
        if cat == "pattern_gap" and gap.affected_skill:
            by_skill[gap.affected_skill].append(gap)
    return dict(by_skill)


async def generate_pattern_fix(skill: str, failing_queries: list[str]) -> FixAttempt:
    """Generate new regex patterns for a skill's SKILL dict to match failing queries.

    Uses healing.py's Claude-based patch generation when available,
    falls back to simple heuristic pattern generation.
    """
    attempt = FixAttempt(
        skill=skill,
        gap_count=len(failing_queries),
        queries=failing_queries,
        fix_type="add_pattern",
    )

    # Find the skill's action module
    skill_file = KHALIL_DIR / "actions" / f"{skill}.py"
    if not skill_file.exists():
        # Try finding by searching actions/
        candidates = list((KHALIL_DIR / "actions").glob("*.py"))
        for c in candidates:
            content = c.read_text()
            if f'"name": "{skill}"' in content or f"'name': '{skill}'" in content:
                skill_file = c
                break

    if not skill_file.exists():
        attempt.error = f"Could not find action module for skill '{skill}'"
        return attempt

    # Try using healing.py's Claude-based generation
    try:
        from healing import generate_healing_patch, validate_patch, score_healing_confidence

        source = skill_file.read_text()

        # Build a specialized prompt for pattern addition
        query_list = "\n".join(f'  - "{q}"' for q in failing_queries[:20])
        diagnosis = {
            "failure_type": "intent_pattern_miss",
            "file": str(skill_file.relative_to(KHALIL_DIR)),
            "function": "SKILL",
            "source": source,
            "signal_context": {
                "failing_queries": failing_queries[:20],
                "skill_name": skill,
            },
            "prompt_override": (
                f"The following user queries should match the SKILL dict patterns in this module "
                f"but currently don't. Add new regex patterns to the 'patterns' list in the SKILL dict "
                f"so these queries will be routed correctly.\n\n"
                f"Failing queries:\n{query_list}\n\n"
                f"Rules:\n"
                f"- Only modify the 'patterns' list in the SKILL dict\n"
                f"- Use \\b word boundaries for precision\n"
                f"- Use re.IGNORECASE where appropriate\n"
                f"- Don't modify any other code\n"
                f"- Keep patterns specific to avoid false positives\n"
            ),
        }

        patched, explanation = await generate_healing_patch(diagnosis)

        if patched:
            valid, issues = validate_patch(source, patched, str(skill_file))
            if valid:
                confidence = score_healing_confidence(
                    skill, len(failing_queries), patched, source
                )
                attempt.confidence = confidence
                attempt.patch_preview = explanation[:200]
                attempt.applied = False  # Don't apply yet — caller decides
                # Store the patch for later application
                attempt._patched_source = patched
                attempt._original_source = source
                attempt._file_path = skill_file
                return attempt
            else:
                attempt.error = f"Patch validation failed: {issues}"
                return attempt
        else:
            attempt.error = "Healing patch generation returned empty"
    except ImportError:
        log.info("healing.py not available, using heuristic pattern generation")
    except Exception as e:
        log.warning("Claude-based fix generation failed: %s", e)
        attempt.error = f"Claude fix failed: {e}"

    # Fallback: heuristic pattern generation
    try:
        _generate_heuristic_patterns(attempt, skill_file, failing_queries)
    except Exception as e:
        attempt.error = f"Heuristic fix failed: {e}"

    return attempt


def _generate_heuristic_patterns(attempt: FixAttempt, skill_file: Path, queries: list[str]):
    """Generate simple regex patterns from failing queries without LLM."""
    # Extract key terms from failing queries
    all_words = set()
    for q in queries:
        words = re.findall(r'\b[a-z]{3,}\b', q.lower())
        all_words.update(words)

    # Remove very common words
    stopwords = {
        "the", "what", "how", "can", "you", "please", "show", "get", "tell",
        "about", "this", "that", "for", "and", "with",
    }
    key_terms = all_words - stopwords

    if not key_terms:
        attempt.error = "No key terms extracted from failing queries"
        return

    # Build a simple pattern from the most common terms
    suggested_patterns = []
    for term in sorted(key_terms)[:5]:
        suggested_patterns.append(f'r"\\b{term}\\b"')

    attempt.confidence = 0.3  # Low confidence for heuristic
    attempt.patch_preview = f"Suggested patterns: {', '.join(suggested_patterns)}"


def apply_fix(attempt: FixAttempt) -> bool:
    """Apply a fix attempt by writing the patched source to disk."""
    if not hasattr(attempt, "_patched_source") or not hasattr(attempt, "_file_path"):
        return False

    try:
        attempt._file_path.write_text(attempt._patched_source)
        attempt.applied = True
        return True
    except Exception as e:
        attempt.error = f"Failed to write patch: {e}"
        return False


def rollback_fix(attempt: FixAttempt) -> bool:
    """Rollback a fix by restoring the original source."""
    if not hasattr(attempt, "_original_source") or not hasattr(attempt, "_file_path"):
        return False

    try:
        attempt._file_path.write_text(attempt._original_source)
        attempt.applied = False
        return True
    except Exception as e:
        log.error("Failed to rollback: %s", e)
        return False


async def run_autofix_cycle(
    report: GapReport,
    cases: list = None,
    confidence_threshold: float = 0.6,
    dry_run: bool = True,
) -> list[FixAttempt]:
    """Run one auto-fix cycle: identify fixable gaps, generate patches, optionally apply.

    Args:
        report: Gap report from eval pipeline
        cases: Optional test cases for context
        confidence_threshold: Min confidence to auto-apply (default 0.6)
        dry_run: If True, generate fixes but don't apply them

    Returns:
        List of FixAttempt objects with results
    """
    fixable = extract_fixable_gaps(report, cases)

    if not fixable:
        print("  No auto-fixable gaps found.", file=sys.stderr)
        return []

    print(
        f"  Found {sum(len(g) for g in fixable.values())} pattern gaps "
        f"across {len(fixable)} skills",
        file=sys.stderr,
    )

    attempts: list[FixAttempt] = []

    # Look up failing queries from the cases list
    case_map = {c.id: c for c in (cases or [])}

    for skill, gaps in fixable.items():
        # Get the actual failing queries
        failing_queries = []
        for gap in gaps:
            case = case_map.get(gap.case_id)
            if case:
                failing_queries.append(case.query)
            else:
                failing_queries.append(gap.detail[:100])

        attempt = await generate_pattern_fix(skill, failing_queries)
        attempts.append(attempt)

        if attempt.error:
            print(f"    {skill}: FAILED — {attempt.error}", file=sys.stderr)
        elif dry_run:
            print(
                f"    {skill}: generated fix (confidence={attempt.confidence:.2f}, dry_run=True)",
                file=sys.stderr,
            )
        elif attempt.confidence >= confidence_threshold:
            if apply_fix(attempt):
                print(
                    f"    {skill}: APPLIED (confidence={attempt.confidence:.2f})",
                    file=sys.stderr,
                )
            else:
                print(f"    {skill}: apply failed — {attempt.error}", file=sys.stderr)
        else:
            print(
                f"    {skill}: skipped (confidence={attempt.confidence:.2f} < {confidence_threshold})",
                file=sys.stderr,
            )

    return attempts
