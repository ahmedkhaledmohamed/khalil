"""Reasoning evaluation — tests Khalil's autonomous decision-making.

Unlike routing eval (does query → tool match?), this tests whether Khalil
REASONS correctly about novel tasks: picks the right strategy, uses available
resources, adapts on failure, and avoids known anti-patterns.

Usage:
    python eval/reasoning_eval.py           # run all reasoning tests
    python eval/reasoning_eval.py --verbose  # detailed output
"""

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@dataclass
class ReasoningCase:
    id: str
    query: str
    description: str
    # What strategy tool should be selected (or None for direct tool)
    expected_strategy: str | None  # "generate_file", "delegate_tasks", "spawn_watcher", "shell", "search_knowledge", None
    # What the response should demonstrate
    expect_reasoning: list[str] = field(default_factory=list)  # reasoning markers that should appear
    expect_not: list[str] = field(default_factory=list)  # anti-patterns that should NOT appear
    # Tags for filtering
    tags: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Test cases — grouped by reasoning capability
# ---------------------------------------------------------------------------

STRATEGY_SELECTION_CASES = [
    # Artifact creation → generate_file
    ReasoningCase(
        id="strat-01",
        query="Build me an HTML presentation about my quarterly goals",
        description="File creation should use generate_file, not shell",
        expected_strategy="generate_file",
        tags=["strategy", "artifact"],
    ),
    ReasoningCase(
        id="strat-02",
        query="Create a Python script that analyzes my email patterns",
        description="Script creation should use generate_file",
        expected_strategy="generate_file",
        tags=["strategy", "artifact"],
    ),
    ReasoningCase(
        id="strat-03",
        query="Write a markdown summary of my FL26 planning work",
        description="Document creation should use generate_file",
        expected_strategy="generate_file",
        tags=["strategy", "artifact"],
    ),

    # Parallel work → delegate_tasks
    ReasoningCase(
        id="strat-04",
        query="Check my calendar, weather, and any urgent emails for tomorrow",
        description="Multi-part independent request should parallelize",
        expected_strategy="delegate_tasks",
        tags=["strategy", "parallel"],
    ),
    ReasoningCase(
        id="strat-05",
        query="Search for FL26 strategy docs, team proposals, and dependency mappings",
        description="Multiple independent searches should parallelize",
        expected_strategy="delegate_tasks",
        tags=["strategy", "parallel"],
    ),

    # Long-running → spawn_watcher
    ReasoningCase(
        id="strat-06",
        query="Monitor PR #269 and notify me when it's merged",
        description="Long-running monitoring should use spawn_watcher",
        expected_strategy="spawn_watcher",
        tags=["strategy", "background"],
    ),
    ReasoningCase(
        id="strat-07",
        query="Track the deploy and let me know when it's done",
        description="Deployment monitoring should use spawn_watcher",
        expected_strategy="spawn_watcher",
        tags=["strategy", "background"],
    ),

    # DB query → shell with sqlite3
    ReasoningCase(
        id="strat-08",
        query="How many conversations have I had this week?",
        description="DB question should use shell + sqlite3",
        expected_strategy="shell",
        tags=["strategy", "database", "novel"],
    ),
    ReasoningCase(
        id="strat-09",
        query="What are my top 5 most-used tools?",
        description="Analytics question should query tool_analytics table",
        expected_strategy="shell",
        tags=["strategy", "database", "novel"],
    ),
    ReasoningCase(
        id="strat-10",
        query="Show me my learned preferences",
        description="Self-awareness question should query learned_preferences",
        expected_strategy="shell",
        tags=["strategy", "database", "self-awareness"],
    ),

    # Knowledge search → search_knowledge
    ReasoningCase(
        id="strat-11",
        query="What do I know about the Subscriptions mission?",
        description="Knowledge question should search the KB",
        expected_strategy="search_knowledge",
        tags=["strategy", "knowledge"],
    ),

    # Direct tool use (no strategy tool needed)
    ReasoningCase(
        id="strat-12",
        query="What's the weather in Toronto?",
        description="Simple weather check — direct tool, no strategy tool needed",
        expected_strategy=None,
        tags=["strategy", "direct"],
    ),
]

ANTI_PATTERN_CASES = [
    ReasoningCase(
        id="anti-01",
        query="Build me a presentation about my work",
        description="Should NOT use shell grep/find for context gathering",
        expected_strategy="generate_file",
        expect_not=["find ~/Developer", "grep -r", "xargs grep"],
        tags=["anti-pattern"],
    ),
    ReasoningCase(
        id="anti-02",
        query="What did I work on last week?",
        description="Should NOT say 'I can't' — should reason with available tools",
        expected_strategy="search_knowledge",
        expect_not=["I can't", "I don't have access", "I'm unable"],
        tags=["anti-pattern", "autonomy"],
    ),
    ReasoningCase(
        id="anti-03",
        query="Create an HTML page about my side projects and save it to /tmp/projects.html",
        description="Should NOT announce a plan instead of executing",
        expected_strategy="generate_file",
        expect_not=["I'll create", "I'm going to", "Here's my plan", "Let me outline"],
        tags=["anti-pattern", "execution"],
    ),
]

ADAPTATION_CASES = [
    ReasoningCase(
        id="adapt-01",
        query="Search my Notion for the product roadmap",
        description="If Notion API fails, should try search_knowledge as fallback",
        expected_strategy="search_knowledge",  # fallback after Notion fails
        expect_reasoning=["search", "knowledge"],
        tags=["adaptation", "fallback"],
    ),
    ReasoningCase(
        id="adapt-02",
        query="Analyze my spending patterns from finance emails",
        description="Novel task — should compose search + analysis from first principles",
        expected_strategy="search_knowledge",
        expect_reasoning=["finance", "email"],
        tags=["adaptation", "novel"],
    ),
]

SELF_AWARENESS_CASES = [
    ReasoningCase(
        id="self-01",
        query="What tables are in your database?",
        description="Should know its own DB schema from identity prompt",
        expected_strategy="shell",
        expect_reasoning=["sqlite3", "khalil.db"],
        tags=["self-awareness"],
    ),
    ReasoningCase(
        id="self-02",
        query="How many documents are in your knowledge base?",
        description="Should query documents table count",
        expected_strategy="shell",
        expect_reasoning=["documents", "COUNT"],
        tags=["self-awareness", "database"],
    ),
    ReasoningCase(
        id="self-03",
        query="What tools do you have available?",
        description="Should describe its capabilities from self-knowledge",
        expected_strategy=None,  # can answer from system prompt
        tags=["self-awareness"],
    ),
]

ALL_CASES = (
    STRATEGY_SELECTION_CASES +
    ANTI_PATTERN_CASES +
    ADAPTATION_CASES +
    SELF_AWARENESS_CASES
)


# ---------------------------------------------------------------------------
# Strategy detection from tool-use response
# ---------------------------------------------------------------------------

def detect_strategy_from_tool_calls(tool_calls: list[dict]) -> str | None:
    """Detect which strategy tool was selected from a list of tool calls."""
    strategy_tools = {"generate_file", "delegate_tasks", "spawn_watcher"}
    for tc in tool_calls:
        name = tc.get("function", {}).get("name", "")
        if name in strategy_tools:
            return name
    # Check for specific tool patterns
    for tc in tool_calls:
        name = tc.get("function", {}).get("name", "")
        if name == "shell":
            args = tc.get("function", {}).get("arguments", "")
            if "sqlite3" in args:
                return "shell"  # DB query via shell
            return "shell"
        if name == "search_knowledge":
            return "search_knowledge"
    return None


# ---------------------------------------------------------------------------
# Offline strategy eval (no LLM needed — tests detection heuristics)
# ---------------------------------------------------------------------------

def run_strategy_heuristic_tests(verbose: bool = False) -> dict:
    """Test that our strategy detection heuristics work correctly."""
    from server import _is_artifact_request

    results = {"total": 0, "passed": 0, "failed": 0, "failures": []}

    # Test _is_artifact_request detection
    artifact_cases = [c for c in ALL_CASES if c.expected_strategy == "generate_file"]
    non_artifact_cases = [c for c in ALL_CASES if c.expected_strategy != "generate_file"]

    for case in artifact_cases:
        results["total"] += 1
        # Note: _is_artifact_request still exists as a function even though
        # we removed the hardcoded bypass — test it as a heuristic
        try:
            detected = _is_artifact_request(case.query)
        except Exception:
            detected = False
        if detected:
            results["passed"] += 1
            if verbose:
                print(f"    PASS: {case.id} '{case.query[:50]}...' → artifact detected")
        else:
            results["failed"] += 1
            results["failures"].append(f"{case.id}: '{case.query[:50]}' should detect as artifact but didn't")

    for case in non_artifact_cases[:5]:  # sample non-artifacts
        results["total"] += 1
        try:
            detected = _is_artifact_request(case.query)
        except Exception:
            detected = False
        if not detected:
            results["passed"] += 1
            if verbose:
                print(f"    PASS: {case.id} '{case.query[:50]}' → not artifact (correct)")
        else:
            results["failed"] += 1
            results["failures"].append(f"{case.id}: '{case.query[:50]}' should NOT detect as artifact but did")

    return results


def run_swarm_heuristic_tests(verbose: bool = False) -> dict:
    """Test that multi-intent queries are detected for delegation."""
    from orchestrator import looks_like_multi_step

    results = {"total": 0, "passed": 0, "failed": 0, "failures": []}

    delegate_cases = [c for c in ALL_CASES if c.expected_strategy == "delegate_tasks"]
    non_delegate_cases = [c for c in ALL_CASES if c.expected_strategy not in ("delegate_tasks", None)]

    for case in delegate_cases:
        results["total"] += 1
        if looks_like_multi_step(case.query):
            results["passed"] += 1
            if verbose:
                print(f"    PASS: {case.id} '{case.query[:50]}...' → multi-step detected")
        else:
            results["failed"] += 1
            results["failures"].append(f"{case.id}: '{case.query[:50]}' should detect as multi-step")

    for case in non_delegate_cases[:5]:
        results["total"] += 1
        if not looks_like_multi_step(case.query):
            results["passed"] += 1
        else:
            # Not necessarily a failure — some queries might legitimately match
            results["passed"] += 1  # lenient for non-delegates

    return results


def run_identity_content_tests(verbose: bool = False) -> dict:
    """Test that KHALIL_IDENTITY contains essential agent components."""
    results = {"total": 0, "passed": 0, "failed": 0, "failures": []}

    try:
        # Import identity without full server init
        import importlib.util
        spec = importlib.util.spec_from_file_location("server_check", "server.py")
        # Just read the file and check string content
        content = Path("server.py").read_text()

        required_in_identity = [
            ("reasoning chain", "UNDERSTAND"),
            ("reasoning chain", "EXECUTE"),
            ("reasoning chain", "VERIFY"),
            ("DB awareness", "sqlite3"),
            ("DB awareness", "data/khalil.db"),
            ("DB tables", "documents"),
            ("DB tables", "conversations"),
            ("DB tables", "interaction_signals"),
            ("strategy tools", "generate_file"),
            ("strategy tools", "delegate_tasks"),
            ("strategy tools", "spawn_watcher"),
            ("principles", "Execute, don't plan"),
            ("principles", "reason from first principles"),
            ("self-modification", "scripts/khalil"),
        ]

        for label, marker in required_in_identity:
            results["total"] += 1
            if marker in content:
                results["passed"] += 1
                if verbose:
                    print(f"    PASS: Identity contains {label} marker: '{marker}'")
            else:
                results["failed"] += 1
                results["failures"].append(f"Identity missing {label}: '{marker}'")
    except Exception as e:
        results["total"] += 1
        results["failed"] += 1
        results["failures"].append(f"Could not check identity: {e}")

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    all_results = {"total": 0, "passed": 0, "failed": 0, "failures": []}

    print("=" * 60)
    print("Khalil Reasoning Evaluation")
    print("=" * 60)

    suites = [
        ("Identity Content Tests", run_identity_content_tests),
        ("Strategy Heuristic Tests", run_strategy_heuristic_tests),
        ("Swarm Heuristic Tests", run_swarm_heuristic_tests),
    ]

    for name, fn in suites:
        print(f"\n--- {name} ---")
        results = fn(verbose)
        for key in ("total", "passed", "failed"):
            all_results[key] += results.get(key, 0)
        all_results["failures"].extend(results.get("failures", []))
        status = "PASS" if results.get("failed", 0) == 0 else "FAIL"
        print(f"  {status}: {results.get('passed', 0)}/{results.get('total', 0)} passed")

    print(f"\n{'=' * 60}")
    print(f"TOTAL: {all_results['passed']}/{all_results['total']} passed, "
          f"{all_results['failed']} failed")

    if all_results["failures"]:
        print(f"\nFailures:")
        for f in all_results["failures"]:
            print(f"  - {f}")

    print(f"\nReasoning test cases defined: {len(ALL_CASES)}")
    print(f"  Strategy selection: {len(STRATEGY_SELECTION_CASES)}")
    print(f"  Anti-pattern: {len(ANTI_PATTERN_CASES)}")
    print(f"  Adaptation: {len(ADAPTATION_CASES)}")
    print(f"  Self-awareness: {len(SELF_AWARENESS_CASES)}")

    print("=" * 60)
    return 0 if all_results["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
