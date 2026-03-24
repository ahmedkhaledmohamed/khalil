"""Guardian — secondary LLM review of tool calls before execution."""

import logging
from dataclasses import dataclass
from enum import Enum

import anthropic

from config import ActionType, KEYRING_SERVICE

log = logging.getLogger("khalil.guardian")

# Fast model for guardian reviews — configurable but defaults to haiku for speed/cost
GUARDIAN_MODEL = "claude-haiku-4-5-20251001"

TOOL_REVIEW_PROMPT = """\
You are a security guardian reviewing a tool call before it executes on macOS.

Evaluate this action:
- Type: {action_type}
- Command: {command}
- Context: {context}

Rate the risk across these dimensions:
1. Destructive potential (deletes files, drops tables, kills processes)
2. Data loss risk (overwrites without backup, truncates)
3. Network exposure (sends data externally, opens ports)
4. Privilege escalation (sudo, chmod, chown)

Respond with exactly one line in this format:
VERDICT: ALLOW|BLOCK|NEEDS_CONFIRMATION
REASON: <one sentence>
"""

CODE_REVIEW_PROMPT = """\
You are a security guardian reviewing a code patch before it's applied.

Target file: {target_file}
Diff:
```
{diff}
```

Check for:
1. Destructive operations (file deletion, data wiping)
2. Security issues (eval, exec, shell injection, credential exposure)
3. Import of dangerous modules (subprocess, ctypes, socket, os.system)
4. Network calls to unknown endpoints

Respond with exactly one line in this format:
VERDICT: ALLOW|BLOCK|NEEDS_CONFIRMATION
REASON: <one sentence>
"""


class GuardianVerdict(Enum):
    ALLOW = "allow"
    BLOCK = "block"
    NEEDS_CONFIRMATION = "needs_confirmation"


@dataclass
class GuardianResult:
    verdict: GuardianVerdict
    reason: str


def _parse_verdict(text: str) -> GuardianResult:
    """Parse the guardian LLM response into a GuardianResult."""
    verdict = GuardianVerdict.NEEDS_CONFIRMATION  # default to cautious
    reason = "Could not parse guardian response"

    for line in text.strip().splitlines():
        line = line.strip()
        if line.startswith("VERDICT:"):
            raw = line.split(":", 1)[1].strip().upper()
            if raw == "ALLOW":
                verdict = GuardianVerdict.ALLOW
            elif raw == "BLOCK":
                verdict = GuardianVerdict.BLOCK
            elif raw == "NEEDS_CONFIRMATION":
                verdict = GuardianVerdict.NEEDS_CONFIRMATION
        elif line.startswith("REASON:"):
            reason = line.split(":", 1)[1].strip()

    return GuardianResult(verdict=verdict, reason=reason)


def _is_safe_action(action_type: str) -> bool:
    """Check if an action is SAFE-tier and should bypass guardian review."""
    from autonomy import ACTION_RULES
    tier = ACTION_RULES.get(action_type, ActionType.WRITE)
    return tier == ActionType.READ


async def review_tool_call(action_type: str, command: str, context: dict) -> GuardianResult:
    """Review a tool call before execution using a fast Claude call.

    BYPASSED for SAFE-tier actions (to avoid latency on reads).
    Returns ALLOW for safe operations, BLOCK for dangerous ones,
    NEEDS_CONFIRMATION for risky ones.
    """
    # Bypass guardian for safe (READ) actions
    if _is_safe_action(action_type):
        return GuardianResult(verdict=GuardianVerdict.ALLOW, reason="Safe-tier action, guardian bypassed")

    prompt = TOOL_REVIEW_PROMPT.format(
        action_type=action_type,
        command=command,
        context=str(context)[:500],
    )

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=GUARDIAN_MODEL,
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text
        result = _parse_verdict(text)
        log.info("Guardian review: %s %s — %s (%s)", action_type, command[:60], result.verdict.value, result.reason)
        return result
    except Exception as e:
        log.error("Guardian review failed: %s — defaulting to NEEDS_CONFIRMATION", e)
        return GuardianResult(
            verdict=GuardianVerdict.NEEDS_CONFIRMATION,
            reason=f"Guardian review failed ({e}), requiring confirmation as fallback",
        )


async def review_code_patch(diff: str, target_file: str) -> GuardianResult:
    """Review a generated code patch before it's applied.

    Checks for destructive operations, security issues, and dangerous imports.
    """
    prompt = CODE_REVIEW_PROMPT.format(
        target_file=target_file,
        diff=diff[:3000],  # cap diff size for fast review
    )

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=GUARDIAN_MODEL,
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text
        result = _parse_verdict(text)
        log.info("Guardian code review: %s — %s (%s)", target_file, result.verdict.value, result.reason)
        return result
    except Exception as e:
        log.error("Guardian code review failed: %s — defaulting to NEEDS_CONFIRMATION", e)
        return GuardianResult(
            verdict=GuardianVerdict.NEEDS_CONFIRMATION,
            reason=f"Guardian code review failed ({e}), requiring confirmation as fallback",
        )
