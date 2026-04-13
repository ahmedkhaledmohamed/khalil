"""Behavioral contracts — formalized rules that must hold regardless of implementation.

These are NOT implementation tests. They test INVARIANTS: properties of the system
that must be true no matter how the code is refactored. If a contract breaks,
the system has regressed from a known-good state.

Industry reference: Google ML Model Cards, METR agent benchmarks, SLO/SLA framework.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# =============================================================================
# CONTRACT 1: Artifact Research Cap
# =============================================================================

class TestArtifactResearchCapContract:
    """CONTRACT: Artifact tasks must not loop on research indefinitely.

    The PhaseTracker MUST enforce escalating restrictions when an artifact
    task exceeds the research cap. These thresholds are the contract.
    """

    NUDGE_AT = 4     # consecutive research calls before nudge
    RESTRICT_AT = 5  # before research tools removed
    FORCE_AT = 6     # before tool_choice forced to generate_file

    def _make_tracker(self):
        from server import _PhaseTracker
        return _PhaseTracker(is_artifact=True)

    def _base_tools(self):
        return [
            {"function": {"name": "search_knowledge"}},
            {"function": {"name": "read_full_document"}},
            {"function": {"name": "generate_file"}},
            {"function": {"name": "shell"}},
        ]

    def test_free_research_below_nudge(self):
        """Research below NUDGE_AT MUST proceed without interference."""
        p = self._make_tracker()
        for _ in range(self.NUDGE_AT - 1):
            p.record(["search_knowledge"])
        tc, tools, prompt = p.get_config(self.NUDGE_AT - 1, self._base_tools())
        assert tc == "auto"
        assert prompt is None

    def test_nudge_fires_at_threshold(self):
        """At NUDGE_AT consecutive research calls, MUST inject nudge mentioning generate_file."""
        p = self._make_tracker()
        for _ in range(self.NUDGE_AT):
            p.record(["search_knowledge"])
        tc, tools, prompt = p.get_config(self.NUDGE_AT, self._base_tools())
        assert prompt is not None, f"Nudge must fire after {self.NUDGE_AT} research calls"
        assert "generate_file" in prompt.lower()

    def test_restrict_removes_search_tools(self):
        """At RESTRICT_AT, research tools MUST be removed from available tools."""
        p = self._make_tracker()
        for _ in range(self.RESTRICT_AT):
            p.record(["search_knowledge"])
        tc, tools, prompt = p.get_config(self.RESTRICT_AT, self._base_tools())
        tool_names = {t["function"]["name"] for t in tools}
        assert "search_knowledge" not in tool_names, "search_knowledge must be removed"
        assert "read_full_document" not in tool_names, "read_full_document must be removed"
        assert "generate_file" in tool_names, "generate_file must remain"

    def test_force_requires_generate_file(self):
        """At FORCE_AT, tool_choice MUST force generate_file."""
        p = self._make_tracker()
        for _ in range(self.FORCE_AT):
            p.record(["search_knowledge"])
        tc, tools, prompt = p.get_config(self.FORCE_AT, self._base_tools())
        assert isinstance(tc, dict), "tool_choice must be a dict (forced function)"
        assert tc["function"]["name"] == "generate_file"

    def test_non_artifact_never_restricted(self):
        """Non-artifact tasks MUST NEVER have research restricted."""
        from server import _PhaseTracker
        p = _PhaseTracker(is_artifact=False)
        for _ in range(10):
            p.record(["search_knowledge"])
        tc, tools, prompt = p.get_config(5, self._base_tools())
        assert tc == "auto"
        assert prompt is None
        assert len(tools) == len(self._base_tools())

    def test_action_resets_escalation(self):
        """After any action tool call, restrictions MUST reset."""
        p = self._make_tracker()
        for _ in range(self.NUDGE_AT):
            p.record(["search_knowledge"])
        p.record(["generate_file"])  # action resets
        tc, tools, prompt = p.get_config(self.NUDGE_AT + 1, self._base_tools())
        assert tc == "auto"
        assert prompt is None


# =============================================================================
# CONTRACT 2: Intent Classification Invariants
# =============================================================================

class TestIntentClassificationContract:
    """CONTRACT: Intent classification must follow these invariant rules."""

    def test_continuation_requires_active_task(self):
        """CONTINUATION MUST only be returned when has_active_task=True."""
        from intent import classify_intent, Intent
        result = classify_intent("Yes", has_active_task=False)
        assert result != Intent.CONTINUATION, "CONTINUATION without active task is invalid"

    def test_build_create_always_task(self):
        """'Build/create X' MUST always classify as TASK."""
        from intent import classify_intent, Intent
        for query in [
            "Build me an HTML presentation",
            "Create a Python script",
            "Generate a report about sales",
            "Write a summary document",
            "Make a dashboard page",
        ]:
            result = classify_intent(query, has_active_task=False)
            assert result == Intent.TASK, f"'{query}' must be TASK, got {result}"

    def test_greeting_never_task(self):
        """Pure greetings MUST classify as CHAT, never TASK."""
        from intent import classify_intent, Intent
        for greeting in ["Hello", "Thanks", "Hi", "Good morning"]:
            result = classify_intent(greeting, has_active_task=False)
            assert result != Intent.TASK, f"'{greeting}' must not be TASK"

    def test_question_mark_always_question(self):
        """Messages ending with '?' MUST classify as QUESTION (without active task)."""
        from intent import classify_intent, Intent
        for q in ["What's the weather?", "How many emails?", "Is my calendar free?"]:
            result = classify_intent(q, has_active_task=False)
            assert result == Intent.QUESTION, f"'{q}' must be QUESTION, got {result}"


# =============================================================================
# CONTRACT 3: Circuit Breaker Isolation
# =============================================================================

class TestCircuitBreakerIsolationContract:
    """CONTRACT: Background failures must not affect foreground availability."""

    def test_bg_open_does_not_open_fg(self):
        """Tripping _cb_claude_bg MUST NOT open _cb_claude_fg."""
        from server import _cb_claude_fg, _cb_claude_bg
        # Save state
        fg_failures, fg_opened = _cb_claude_fg._failures, _cb_claude_fg._opened_at
        bg_failures, bg_opened = _cb_claude_bg._failures, _cb_claude_bg._opened_at
        try:
            _cb_claude_fg._failures = 0
            _cb_claude_fg._opened_at = None
            _cb_claude_bg._failures = 0
            _cb_claude_bg._opened_at = None

            # Trip background breaker
            for _ in range(_cb_claude_bg.threshold):
                _cb_claude_bg.record_failure()
            assert _cb_claude_bg.is_open(), "Background CB must be open"
            assert not _cb_claude_fg.is_open(), "Foreground CB must NOT be open"
        finally:
            # Restore state
            _cb_claude_fg._failures = fg_failures
            _cb_claude_fg._opened_at = fg_opened
            _cb_claude_bg._failures = bg_failures
            _cb_claude_bg._opened_at = bg_opened

    def test_fg_threshold_higher_than_bg(self):
        """Foreground CB threshold MUST be >= background threshold."""
        from server import _cb_claude_fg, _cb_claude_bg
        assert _cb_claude_fg.threshold >= _cb_claude_bg.threshold, \
            f"FG threshold ({_cb_claude_fg.threshold}) must be >= BG ({_cb_claude_bg.threshold})"

    def test_breakers_are_separate_instances(self):
        """Foreground and background MUST be different objects."""
        from server import _cb_claude_fg, _cb_claude_bg
        assert _cb_claude_fg is not _cb_claude_bg


# =============================================================================
# CONTRACT 4: Verification Layer
# =============================================================================

class TestVerificationLayerContract:
    """CONTRACT: Hallucinated tool calls must be detected before reaching user."""

    def test_called_tool_pattern_detected(self):
        """[Called tool: X] in LLM output MUST trigger hallucination detection."""
        from verification import detect_hallucinated_tools
        assert detect_hallucinated_tools("[Called tool: search_knowledge]\nresults...")

    def test_mcp_call_pattern_detected(self):
        """[MCP_CALL: X] in LLM output MUST trigger hallucination detection."""
        from verification import detect_hallucinated_tools
        assert detect_hallucinated_tools('[MCP_CALL: the-hub.search | {"query":"test"}]')

    def test_tool_call_pattern_detected(self):
        """[tool_call: X] in LLM output MUST trigger hallucination detection."""
        from verification import detect_hallucinated_tools
        assert detect_hallucinated_tools("[tool_call: generate_file]\n...")

    def test_normal_text_not_flagged(self):
        """Normal conversational text MUST NOT trigger false positives."""
        from verification import detect_hallucinated_tools
        assert not detect_hallucinated_tools("I searched the knowledge base and found relevant results.")
        assert not detect_hallucinated_tools("Here's what I found about FL26 planning.")

    def test_preamble_detected(self):
        """Preamble responses MUST be detected."""
        from verification import is_preamble_response
        assert is_preamble_response("I'll search for that information now")
        assert is_preamble_response("Let me check the knowledge base")

    def test_substantive_response_not_preamble(self):
        """Substantive responses (>500 chars) MUST NOT be flagged as preamble."""
        from verification import is_preamble_response
        assert not is_preamble_response("I'll " + "x" * 500)


# =============================================================================
# CONTRACT 5: Tool Catalog Structural Invariants
# =============================================================================

class TestToolCatalogContract:
    """CONTRACT: Core tools must always be available."""

    def test_generate_file_in_core_tools(self):
        """generate_file MUST be in _CORE_TOOLS."""
        from tool_catalog import _CORE_TOOLS
        assert "generate_file" in _CORE_TOOLS

    def test_search_knowledge_in_core_tools(self):
        """search_knowledge MUST be in _CORE_TOOLS."""
        from tool_catalog import _CORE_TOOLS
        assert "search_knowledge" in _CORE_TOOLS

    def test_read_full_document_in_core_tools(self):
        """read_full_document MUST be in _CORE_TOOLS."""
        from tool_catalog import _CORE_TOOLS
        assert "read_full_document" in _CORE_TOOLS

    def test_shell_in_core_tools(self):
        """shell MUST be in _CORE_TOOLS."""
        from tool_catalog import _CORE_TOOLS
        assert "shell" in _CORE_TOOLS

    def test_research_and_action_tools_classified(self):
        """_RESEARCH_TOOLS and _ACTION_TOOLS must be defined and non-empty."""
        from server import _RESEARCH_TOOLS, _ACTION_TOOLS
        assert len(_RESEARCH_TOOLS) >= 2, "Must have at least 2 research tools"
        assert len(_ACTION_TOOLS) >= 2, "Must have at least 2 action tools"
        assert _RESEARCH_TOOLS.isdisjoint(_ACTION_TOOLS), "Research and action tools must not overlap"
