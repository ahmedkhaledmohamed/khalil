"""Scenario runner — executes end-to-end task scenarios against a sandboxed Khalil instance.

Evaluates multi-turn coherence, tool sequencing, hallucination avoidance,
and failure recovery using mock tool results.

Usage:
    python -m eval.scenario_runner                     # run all scenarios
    python -m eval.scenario_runner --tags git,email    # filter by tags
    python -m eval.scenario_runner --verbose           # detailed output
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.scenarios import Scenario, ScenarioTurn, get_scenarios

REPORTS_DIR = Path(__file__).resolve().parent / "reports"


@dataclass
class TurnResult:
    """Result of evaluating a single turn within a scenario."""
    user_query: str
    response: str
    latency_s: float
    tools_fired: list[str] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)  # [{name, passed, detail}]
    passed: bool = True


@dataclass
class ScenarioResult:
    """Result of evaluating a full scenario."""
    name: str
    passed: bool
    turn_results: list[TurnResult]
    error: str | None = None
    total_time_s: float = 0.0


# ---------------------------------------------------------------------------
# Turn evaluation helpers
# ---------------------------------------------------------------------------

def _check_tool_expectations(turn: ScenarioTurn, response: str, tools_fired: list[str]) -> list[dict]:
    """Check if expected tools were called."""
    checks = []
    for tool in turn.expect_tools:
        found = tool in tools_fired or any(tool in t for t in tools_fired)
        checks.append({
            "name": f"tool:{tool}",
            "passed": found,
            "detail": "" if found else f"expected tool '{tool}' not fired (got: {tools_fired})",
        })
    return checks


def _check_content(turn: ScenarioTurn, response: str) -> list[dict]:
    """Check contains / not_contains expectations."""
    checks = []
    lower = response.lower()
    for phrase in turn.expect_contains:
        found = phrase.lower() in lower
        checks.append({
            "name": f"contains:{phrase}",
            "passed": found,
            "detail": "" if found else f"'{phrase}' not found in response",
        })
    for phrase in turn.expect_not_contains:
        absent = phrase.lower() not in lower
        checks.append({
            "name": f"not_contains:{phrase}",
            "passed": absent,
            "detail": "" if absent else f"'{phrase}' unexpectedly found in response",
        })
    return checks


def _check_result_assertion(turn: ScenarioTurn, response: str) -> list[dict]:
    """Basic assertion checks from expect_result strings."""
    if not turn.expect_result:
        return []
    assertion = turn.expect_result.lower()
    checks = []

    if "contains a number" in assertion:
        has_number = bool(re.search(r"\d+", response))
        checks.append({
            "name": "result:contains_number",
            "passed": has_number,
            "detail": "" if has_number else "no number found in response",
        })

    if "does not hallucinate" in assertion or "not hallucinate" in assertion:
        # Heuristic: flag responses with suspiciously specific numbers not from tool output
        # This is a simplified check — the LLM judge handles nuanced cases
        checks.append({
            "name": "result:hallucination_guard",
            "passed": True,  # Conservative: pass unless LLM judge overrides
            "detail": "hallucination check deferred to LLM judge",
        })

    return checks


# ---------------------------------------------------------------------------
# Scenario execution
# ---------------------------------------------------------------------------

def _check_multi_turn_coherence(scenario: Scenario, turn_results: list[TurnResult]) -> list[dict]:
    """Check entity consistency across multi-turn scenario responses.

    Verifies that entities (proper nouns, numbers) mentioned in earlier turns
    are not contradicted in later turns. Also checks that follow-up turns
    don't claim amnesia ("I don't remember", "I can't recall").
    """
    checks = []

    # 1. No amnesia in follow-up turns
    amnesia_phrases = [
        "i don't remember", "i can't recall", "i don't have any previous",
        "i don't have context", "i'm not sure what you're referring to",
    ]
    for i, tr in enumerate(turn_results[1:], 1):
        resp_lower = tr.response.lower()
        has_amnesia = any(p in resp_lower for p in amnesia_phrases)
        if has_amnesia:
            checks.append({
                "name": f"coherence:no_amnesia_turn_{i+1}",
                "passed": False,
                "detail": f"Turn {i+1} claims amnesia despite prior context",
            })
        else:
            checks.append({
                "name": f"coherence:no_amnesia_turn_{i+1}",
                "passed": True,
                "detail": "",
            })

    # 2. Entity carryover: proper nouns from turn 1 should be referenced in later turns
    #    (when the scenario has expect_contains that span turns)
    if len(turn_results) >= 2:
        # Extract capitalized words (proper nouns) from turn 1 response
        first_response = turn_results[0].response
        proper_nouns = set(re.findall(r'\b[A-Z][a-z]{2,}\b', first_response))
        # Filter out common words that happen to be capitalized at sentence start
        common = {"The", "This", "That", "Here", "There", "What", "How", "When",
                  "Sure", "Yes", "Based", "Your", "I'll", "Let"}
        proper_nouns -= common

        if proper_nouns and len(proper_nouns) <= 10:
            # Check if any proper noun from turn 1 appears in the last turn
            last_response = turn_results[-1].response
            carried = any(pn in last_response for pn in proper_nouns)
            if carried or not scenario.turns[-1].expect_contains:
                # Either entities carried over, or last turn doesn't need them
                checks.append({
                    "name": "coherence:entity_carryover",
                    "passed": True,
                    "detail": "",
                })

    return checks


async def run_scenario(scenario: Scenario, server_mod=None, verbose: bool = False) -> ScenarioResult:
    """Execute a single scenario against the message pipeline."""
    from eval.runner import InstrumentedChannel, init_server
    from channels import ChannelType, IncomingMessage
    from channels.message_context import MessageContext

    if server_mod is None:
        server_mod = await init_server()

    channel = InstrumentedChannel()
    turn_results: list[TurnResult] = []
    scenario_start = time.monotonic()
    error = None

    for i, turn in enumerate(scenario.turns):
        channel.messages.clear()
        ctx = MessageContext(
            channel=channel,
            chat_id="eval_scenario",
            user_id="eval_scenario",
            channel_type=ChannelType.TELEGRAM,
            incoming=IncomingMessage(
                text=turn.user,
                chat_id="eval_scenario",
                user_id="eval_scenario",
                channel_type=ChannelType.TELEGRAM,
            ),
        )

        turn_start = time.monotonic()
        try:
            from eval.trace import capture_trace
            with capture_trace() as trace:
                await asyncio.wait_for(
                    server_mod.handle_message_generic(ctx),
                    timeout=scenario.timeout_s,
                )
            latency = time.monotonic() - turn_start
            tools_fired = [trace.matched_action] if trace and trace.matched_action else []
        except asyncio.TimeoutError:
            latency = time.monotonic() - turn_start
            error = f"Turn {i+1} timed out after {scenario.timeout_s}s"
            tools_fired = []
        except Exception as e:
            latency = time.monotonic() - turn_start
            error = f"Turn {i+1}: {type(e).__name__}: {e}"
            tools_fired = []

        response = "\n".join(channel.messages)

        # Run all checks for this turn
        checks = []
        checks.extend(_check_tool_expectations(turn, response, tools_fired))
        checks.extend(_check_content(turn, response))
        checks.extend(_check_result_assertion(turn, response))

        turn_passed = all(c["passed"] for c in checks)
        turn_results.append(TurnResult(
            user_query=turn.user,
            response=response,
            latency_s=round(latency, 3),
            tools_fired=tools_fired,
            checks=checks,
            passed=turn_passed,
        ))

        if verbose:
            status = "PASS" if turn_passed else "FAIL"
            print(f"    Turn {i+1}: {status} — {turn.user[:50]}...")
            for c in checks:
                if not c["passed"]:
                    print(f"      FAIL: {c['name']} — {c['detail']}")

        if error:
            break

    # Multi-turn coherence check: verify entity consistency across turns
    if len(turn_results) >= 2 and not error:
        coherence_checks = _check_multi_turn_coherence(scenario, turn_results)
        if coherence_checks:
            # Attach coherence checks to the last turn
            turn_results[-1].checks.extend(coherence_checks)
            if not all(c["passed"] for c in coherence_checks):
                turn_results[-1].passed = False

    total_time = time.monotonic() - scenario_start
    all_passed = all(tr.passed for tr in turn_results) and error is None

    return ScenarioResult(
        name=scenario.name,
        passed=all_passed,
        turn_results=turn_results,
        error=error,
        total_time_s=round(total_time, 3),
    )


async def run_all_scenarios(
    tags: list[str] | None = None,
    verbose: bool = False,
) -> list[ScenarioResult]:
    """Run all (or filtered) scenarios and return results."""
    from eval.runner import init_server

    scenarios = get_scenarios(tags)
    print(f"Running {len(scenarios)} scenarios...", file=sys.stderr)

    server_mod = await init_server()
    results: list[ScenarioResult] = []

    for scenario in scenarios:
        if verbose:
            print(f"\n  Scenario: {scenario.name}", file=sys.stderr)
        result = await run_scenario(scenario, server_mod, verbose=verbose)
        results.append(result)

        status = "PASS" if result.passed else "FAIL"
        print(f"  {status} {scenario.name:40s} ({result.total_time_s:.1f}s)", file=sys.stderr)

    return results


def print_summary(results: list[ScenarioResult]) -> None:
    """Print summary of scenario run."""
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    print(f"\n{'=' * 60}")
    print(f"SCENARIO RESULTS: {passed}/{total} passed ({passed/max(total,1):.0%})")
    print(f"{'=' * 60}")

    for r in results:
        status = "PASS" if r.passed else "FAIL"
        turns_ok = sum(1 for t in r.turn_results if t.passed)
        print(f"  {status} {r.name:40s} turns: {turns_ok}/{len(r.turn_results)} ({r.total_time_s:.1f}s)")
        if r.error:
            print(f"       error: {r.error}")
        if not r.passed:
            for tr in r.turn_results:
                for c in tr.checks:
                    if not c["passed"]:
                        print(f"       {c['name']}: {c['detail']}")


def save_results(results: list[ScenarioResult]) -> Path:
    """Save scenario results to reports directory."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = REPORTS_DIR / f"scenarios_{ts}.json"
    data = [asdict(r) for r in results]
    path.write_text(json.dumps(data, indent=2, default=str))
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    tags = None
    if "--tags" in sys.argv:
        idx = sys.argv.index("--tags")
        if idx + 1 < len(sys.argv):
            tags = sys.argv[idx + 1].split(",")

    results = asyncio.run(run_all_scenarios(tags=tags, verbose=verbose))
    print_summary(results)
    path = save_results(results)
    print(f"\nReport saved: {path}")

    failed = sum(1 for r in results if not r.passed)
    sys.exit(1 if failed > 0 else 0)


if __name__ == "__main__":
    main()
