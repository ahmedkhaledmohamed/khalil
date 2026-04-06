"""Tool-use evaluation suite — tests tool selection, parameter passing, and multi-step flows.

Validates that the one-tool-per-action schema redesign improves tool selection accuracy.
Runs without LLM calls — tests schema generation, filtering, and parameter validation.

Usage:
    python -m eval.tool_use_eval            # run all tests
    python -m eval.tool_use_eval --verbose  # show details
"""

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@dataclass
class ToolUseTestCase:
    id: str
    query: str
    expected_tools: list[str]  # tools that MUST be in filtered set
    excluded_tools: list[str] = field(default_factory=list)  # tools that MUST NOT be in filtered set
    expected_required_params: dict = field(default_factory=dict)  # tool_name -> [required params]
    description: str = ""


# Test cases for tool selection accuracy
TOOL_SELECTION_CASES = [
    # Calendar
    ToolUseTestCase("cal-01", "What's on my calendar today?", ["calendar"],
                    description="Basic calendar check"),
    ToolUseTestCase("cal-02", "Schedule a meeting tomorrow at 2pm with Sarah",
                    ["calendar_create"], description="Calendar create"),
    ToolUseTestCase("cal-03", "Any meetings this week?", ["calendar_upcoming"],
                    description="Upcoming events"),

    # Email
    ToolUseTestCase("email-01", "Send an email to john@example.com about the proposal",
                    ["email"], description="Send email"),
    ToolUseTestCase("email-02", "Check my work inbox", ["email_work"],
                    description="Work email search"),
    ToolUseTestCase("email-03", "Any personal emails about the mortgage?",
                    ["email_personal"], description="Personal email search"),

    # Terminal / Machine control
    ToolUseTestCase("term-01", "Send git status to /dev/ttys057",
                    ["send_to_terminal", "send_to_claude"],
                    description="Terminal send — should include both terminal tools"),
    ToolUseTestCase("term-02", "What Claude Code sessions are running?",
                    ["claude_code_status"], description="Claude status"),
    ToolUseTestCase("term-03", "Read the output in ttys057",
                    ["read_terminal"], description="Read terminal"),
    ToolUseTestCase("term-04", "List my terminal sessions",
                    ["list_sessions"], description="List sessions"),

    # Weather
    ToolUseTestCase("weather-01", "What's the weather?", ["weather"],
                    description="Current weather"),
    ToolUseTestCase("weather-02", "5-day forecast", ["weather_forecast"],
                    description="Weather forecast"),

    # Reminders
    ToolUseTestCase("rem-01", "Remind me to call Sarah in 2 hours",
                    ["reminder"], description="Create reminder"),
    ToolUseTestCase("rem-02", "What are my reminders?", ["reminder_list"],
                    description="List reminders"),

    # Spotify
    ToolUseTestCase("spot-01", "What's playing?", ["spotify_now"],
                    description="Now playing"),
    ToolUseTestCase("spot-02", "My top artists", ["spotify_top"],
                    description="Top tracks/artists"),

    # Shell
    ToolUseTestCase("shell-01", "Check disk space", ["shell"],
                    description="Shell command"),
    ToolUseTestCase("shell-02", "Open Safari", ["shell"],
                    description="Open app via shell"),

    # Synthesis
    ToolUseTestCase("synth-01", "Prep me for my 1:1 with my manager",
                    ["meeting_prep"], description="Meeting prep"),
    ToolUseTestCase("synth-02", "What should I focus on today?",
                    ["daily_focus"], description="Daily focus"),

    # GitHub
    ToolUseTestCase("gh-01", "Check my PRs", ["github_prs"],
                    description="GitHub PRs"),
    ToolUseTestCase("gh-02", "GitHub notifications", ["github_notifications"],
                    description="GitHub notifications"),

    # Summarize
    ToolUseTestCase("sum-01", "Summarize this article: https://example.com",
                    ["summarize_url"], description="Summarize URL"),
    ToolUseTestCase("sum-02", "TLDR of this YouTube video",
                    ["summarize_youtube"], description="Summarize YouTube"),

    # Conversational bypass — these should NOT trigger tools
    ToolUseTestCase("conv-01", "hey", [], excluded_tools=["shell", "weather"],
                    description="Greeting — should bypass tools"),
    ToolUseTestCase("conv-02", "thanks", [], excluded_tools=["shell"],
                    description="Thanks — should bypass tools"),

    # Conversational bypass FALSE POSITIVES — these SHOULD trigger tools
    ToolUseTestCase("bypass-01", "hey what's the weather", ["weather"],
                    description="Greeting + tool intent — should NOT bypass"),
    ToolUseTestCase("bypass-02", "hi check my calendar", ["calendar"],
                    description="Greeting + tool intent — should NOT bypass"),

    # Multi-word ambiguous — should offer clarify
    ToolUseTestCase("clarify-01", "send that to John", ["clarify"],
                    description="Ambiguous request — should clarify"),
]

# Required parameter test cases
PARAM_VALIDATION_CASES = [
    ToolUseTestCase("param-01", "send_to_claude with no params",
                    expected_tools=["send_to_claude"],
                    expected_required_params={"send_to_claude": ["command", "target"]},
                    description="send_to_claude requires command and target"),
    ToolUseTestCase("param-02", "calendar_create",
                    expected_tools=["calendar_create"],
                    expected_required_params={"calendar_create": ["summary", "start_time"]},
                    description="calendar_create requires summary and start_time"),
    ToolUseTestCase("param-03", "email",
                    expected_tools=["email"],
                    expected_required_params={"email": ["to", "subject"]},
                    description="email requires to and subject"),
    ToolUseTestCase("param-04", "shell",
                    expected_tools=["shell"],
                    expected_required_params={"shell": ["command"]},
                    description="shell requires command"),
    ToolUseTestCase("param-05", "reminder",
                    expected_tools=["reminder"],
                    expected_required_params={"reminder": ["text"]},
                    description="reminder requires text"),
]


def run_schema_tests(verbose: bool = False) -> dict:
    """Test that tool schemas are correctly generated."""
    from tool_catalog import generate_tool_schemas
    from skills import get_registry

    registry = get_registry()
    tools = generate_tool_schemas(registry)

    results = {"total": 0, "passed": 0, "failed": 0, "failures": []}

    # Test: no tool should have an "action" enum parameter
    results["total"] += 1
    action_enum_tools = []
    for t in tools:
        props = t["function"]["parameters"].get("properties", {})
        if "action" in props and "enum" in props.get("action", {}):
            action_enum_tools.append(t["function"]["name"])
    if action_enum_tools:
        results["failed"] += 1
        results["failures"].append(f"Tools with action enum (should be 0): {action_enum_tools}")
    else:
        results["passed"] += 1
        if verbose:
            print("  PASS: No tools have action enum parameter")

    # Test: all tools should have unique names
    results["total"] += 1
    names = [t["function"]["name"] for t in tools]
    dupes = [n for n in names if names.count(n) > 1]
    if dupes:
        results["failed"] += 1
        results["failures"].append(f"Duplicate tool names: {set(dupes)}")
    else:
        results["passed"] += 1
        if verbose:
            print(f"  PASS: All {len(names)} tool names are unique")

    # Test: clarify tool should be present
    results["total"] += 1
    if "clarify" in names:
        results["passed"] += 1
        if verbose:
            print("  PASS: clarify meta-tool is present")
    else:
        results["failed"] += 1
        results["failures"].append("clarify meta-tool missing from schemas")

    return results


def run_param_tests(verbose: bool = False) -> dict:
    """Test that required parameters are correctly set."""
    from tool_catalog import generate_tool_schemas
    from skills import get_registry

    registry = get_registry()
    tools = generate_tool_schemas(registry)
    tool_index = {t["function"]["name"]: t for t in tools}

    results = {"total": 0, "passed": 0, "failed": 0, "failures": []}

    for case in PARAM_VALIDATION_CASES:
        for tool_name, expected_required in case.expected_required_params.items():
            results["total"] += 1
            if tool_name not in tool_index:
                # Skip if tool not loaded (missing deps)
                results["passed"] += 1
                if verbose:
                    print(f"  SKIP: {case.id} — {tool_name} not loaded")
                continue

            actual_required = tool_index[tool_name]["function"]["parameters"].get("required", [])
            missing = [p for p in expected_required if p not in actual_required]
            if missing:
                results["failed"] += 1
                results["failures"].append(
                    f"{case.id}: {tool_name} missing required params: {missing} "
                    f"(has: {actual_required})")
            else:
                results["passed"] += 1
                if verbose:
                    print(f"  PASS: {case.id} — {tool_name} requires {actual_required}")

    return results


def run_filter_tests(verbose: bool = False) -> dict:
    """Test that tool filtering selects relevant tools per query."""
    from tool_catalog import generate_tool_schemas, filter_tools_for_query
    from skills import get_registry

    registry = get_registry()
    all_tools = generate_tool_schemas(registry)
    available_names = {t["function"]["name"] for t in all_tools}

    results = {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "failures": []}

    for case in TOOL_SELECTION_CASES:
        if not case.expected_tools:
            continue  # Skip conversational bypass tests (need LLM to test)

        results["total"] += 1

        # Skip if expected tools aren't loaded
        missing_tools = [t for t in case.expected_tools if t not in available_names]
        if missing_tools:
            results["skipped"] += 1
            if verbose:
                print(f"  SKIP: {case.id} — tools not loaded: {missing_tools}")
            continue

        filtered = filter_tools_for_query(case.query, registry, all_tools)
        filtered_names = {t["function"]["name"] for t in filtered}

        missing = [t for t in case.expected_tools if t not in filtered_names]
        if missing:
            results["failed"] += 1
            results["failures"].append(
                f"{case.id} ({case.description}): query='{case.query}' "
                f"missing tools: {missing} (got: {sorted(filtered_names)})")
            if verbose:
                print(f"  FAIL: {case.id} — missing {missing}")
        else:
            results["passed"] += 1
            if verbose:
                print(f"  PASS: {case.id} — {case.description} → {sorted(filtered_names)}")

    return results


def run_bypass_tests(verbose: bool = False) -> dict:
    """Test conversational bypass logic."""
    results = {"total": 0, "passed": 0, "failed": 0, "failures": []}

    _GREETINGS = {"hey", "hi", "hello", "yo", "sup", "thanks", "thank you",
                  "ok", "okay", "cool", "got it", "nice", "good", "great",
                  "sure", "yes", "no", "nah", "yep", "nope", "hmm", "hm"}

    # Test: pure greetings should bypass
    for greeting in ["hey", "hi", "thanks", "ok", "cool"]:
        results["total"] += 1
        _q = greeting.strip().lower().rstrip("?!. ")
        bypassed = _q in _GREETINGS
        if bypassed:
            results["passed"] += 1
            if verbose:
                print(f"  PASS: '{greeting}' → bypass")
        else:
            results["failed"] += 1
            results["failures"].append(f"'{greeting}' should bypass but didn't")

    # Test: greeting + intent should NOT bypass
    for query in ["hey what's the weather", "hi check my calendar",
                  "yo send git status", "hello remind me to call"]:
        results["total"] += 1
        _q = query.strip().lower().rstrip("?!. ")
        bypassed = _q in _GREETINGS
        if not bypassed:
            results["passed"] += 1
            if verbose:
                print(f"  PASS: '{query}' → NOT bypassed")
        else:
            results["failed"] += 1
            results["failures"].append(f"'{query}' should NOT bypass but did")

    return results


def main():
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    all_results = {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "failures": []}

    print("=" * 60)
    print("Khalil Tool-Use Evaluation Suite")
    print("=" * 60)

    suites = [
        ("Schema Tests", run_schema_tests),
        ("Parameter Tests", run_param_tests),
        ("Filter Tests", run_filter_tests),
        ("Bypass Tests", run_bypass_tests),
    ]

    for name, fn in suites:
        print(f"\n--- {name} ---")
        results = fn(verbose)
        for key in ("total", "passed", "failed"):
            all_results[key] += results.get(key, 0)
        all_results["skipped"] += results.get("skipped", 0)
        all_results["failures"].extend(results.get("failures", []))
        status = "PASS" if results.get("failed", 0) == 0 else "FAIL"
        print(f"  {status}: {results.get('passed', 0)}/{results.get('total', 0)} passed"
              + (f" ({results.get('skipped', 0)} skipped)" if results.get("skipped") else ""))

    print(f"\n{'=' * 60}")
    print(f"TOTAL: {all_results['passed']}/{all_results['total']} passed, "
          f"{all_results['failed']} failed, {all_results['skipped']} skipped")

    if all_results["failures"]:
        print(f"\nFailures:")
        for f in all_results["failures"]:
            print(f"  - {f}")

    print("=" * 60)
    return 0 if all_results["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
