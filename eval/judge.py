"""Three-tier evaluation engine for PharoClaw's eval pipeline.

Tier 1: Deterministic — exact checks (routing, containment, latency).
Tier 2: Heuristic — structural quality checks without LLM.
Tier 3: LLM Judge — uses local Ollama to score open-ended responses.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from eval.cases import TestCase
from eval.runner import TestResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

@dataclass
class Check:
    name: str           # e.g. "routing", "contains:weather", "latency"
    passed: bool
    detail: str = ""    # extra info on failure


@dataclass
class EvalResult:
    case_id: str
    passed: bool
    checks: list[Check]
    scores: dict | None = None      # only for LLM judge: {relevance, accuracy, completeness, conciseness, overall}
    gap_hint: str | None = None     # pre-classification hint for gap analysis


# ---------------------------------------------------------------------------
# Tier 1: Deterministic
# ---------------------------------------------------------------------------

class DeterministicEval:
    """For cases with eval_strategy='deterministic'."""

    def evaluate(self, case: TestCase, result: TestResult) -> EvalResult:
        checks: list[Check] = []
        gap_hint: str | None = None

        # 1. Routing
        if case.expected_action:
            routing_ok = result.pipeline_path != "error" and result.error is None
            checks.append(Check(
                name="routing",
                passed=routing_ok,
                detail="" if routing_ok else f"pipeline_path={result.pipeline_path}, error={result.error}",
            ))
            if not routing_ok:
                gap_hint = "pattern_gap"

        # 2. Content containment
        response_lower = (result.response or "").lower()
        for s in (case.expected_contains or []):
            found = s.lower() in response_lower
            checks.append(Check(
                name=f"contains:{s}",
                passed=found,
                detail="" if found else f"'{s}' not found in response",
            ))

        # 3. Negative containment
        for s in (case.expected_not_contains or []):
            absent = s.lower() not in response_lower
            checks.append(Check(
                name=f"not_contains:{s}",
                passed=absent,
                detail="" if absent else f"'{s}' unexpectedly found in response",
            ))
            if not absent:
                gap_hint = "safety_leak"

        # 4. No error
        no_err = result.error is None
        checks.append(Check(
            name="no_error",
            passed=no_err,
            detail="" if no_err else str(result.error),
        ))
        if not no_err and gap_hint is None:
            gap_hint = "handler_error"

        # 5. Latency
        if result.latency_s is not None:
            threshold = 3.0 if case.expected_path == "direct_action" else 60.0
            within = result.latency_s < threshold
            checks.append(Check(
                name="latency",
                passed=within,
                detail="" if within else f"{result.latency_s:.2f}s exceeds {threshold}s",
            ))

        # 6. Skill-specific validators (only if we got a response)
        if result.response and result.error is None:
            from eval.validators import validate
            for name, passed, detail in validate(case.expected_action, case.query, result.response):
                checks.append(Check(name=f"validator:{name}", passed=passed, detail=detail))

        passed = all(c.passed for c in checks)
        return EvalResult(
            case_id=case.id,
            passed=passed,
            checks=checks,
            gap_hint=gap_hint if not passed else None,
        )


# ---------------------------------------------------------------------------
# Tier 2: Heuristic
# ---------------------------------------------------------------------------

_GENERIC_PREFIXES = (
    "i can't",
    "i don't have",
    "i'm unable",
)


class HeuristicEval:
    """For cases with eval_strategy='heuristic'."""

    def evaluate(self, case: TestCase, result: TestResult) -> EvalResult:
        checks: list[Check] = []
        response = result.response or ""

        # 1. Non-empty
        non_empty = len(response) > 10
        checks.append(Check(
            name="non_empty",
            passed=non_empty,
            detail="" if non_empty else f"response length {len(response)} <= 10",
        ))

        # 2. Not generic
        lower = response.lower().lstrip()
        is_generic = any(lower.startswith(p) for p in _GENERIC_PREFIXES)
        checks.append(Check(
            name="not_generic",
            passed=not is_generic,
            detail="" if not is_generic else f"response starts with generic refusal",
        ))

        # 3. No thinking leak
        no_leak = "Thinking..." not in response
        checks.append(Check(
            name="no_thinking_leak",
            passed=no_leak,
            detail="" if no_leak else "'Thinking...' leaked into response",
        ))

        # 4. Length appropriate
        complexity = getattr(case, "complexity", "moderate")
        if complexity == "trivial":
            length_ok = len(response) < 500
            detail = f"trivial response too long ({len(response)} >= 500)" if not length_ok else ""
        else:
            length_ok = len(response) > 50
            detail = f"response too short ({len(response)} <= 50)" if not length_ok else ""
        checks.append(Check(name="length_appropriate", passed=length_ok, detail=detail))

        # 5. No error
        no_err = result.error is None
        checks.append(Check(
            name="no_error",
            passed=no_err,
            detail="" if no_err else str(result.error),
        ))

        # 6. Skill-specific validators (only if we got a response)
        if result.response and result.error is None:
            from eval.validators import validate
            action = getattr(case, "expected_action", None)
            for name, passed, detail in validate(action, case.query, result.response):
                checks.append(Check(name=f"validator:{name}", passed=passed, detail=detail))

        passed = all(c.passed for c in checks)
        return EvalResult(case_id=case.id, passed=passed, checks=checks)


# ---------------------------------------------------------------------------
# Tier 3: LLM Judge
# ---------------------------------------------------------------------------

_JUDGE_PROMPT_TEMPLATE = """\
You are an evaluation judge. Rate the assistant response on a 1-5 scale for each criterion.

User query: {query}
Expected behavior: {expected}
Assistant response: {response}

Rate each criterion (1=terrible, 5=excellent):
- relevance: Does the response address the query?
- accuracy: Is the information correct?
- completeness: Does it cover all aspects of the query?
- conciseness: Is it appropriately concise without losing information?
- overall: Holistic quality score.

Return ONLY valid JSON with these five keys and integer values. Example:
{{"relevance": 4, "accuracy": 5, "completeness": 3, "conciseness": 4, "overall": 4}}
"""


class LLMJudgeEval:
    """For cases with eval_strategy='llm_judge'. Uses local Ollama via server.ask_llm."""

    async def evaluate(self, case: TestCase, result: TestResult) -> EvalResult:
        from server import ask_llm

        prompt = _JUDGE_PROMPT_TEMPLATE.format(
            query=case.query,
            expected=case.expected_behavior or "No specific expectation provided.",
            response=result.response or "(empty response)",
        )

        try:
            raw = await ask_llm(prompt, "")
            scores = self._parse_scores(raw)
        except Exception as e:
            logger.warning("LLM judge failed: %s", e)
            return EvalResult(
                case_id=case.id,
                passed=False,
                checks=[Check(name="llm_judge", passed=False, detail=f"judge error: {e}")],
                scores=None,
            )

        if scores is None:
            return EvalResult(
                case_id=case.id,
                passed=False,
                checks=[Check(name="llm_judge_parse", passed=False, detail="failed to parse judge JSON")],
                scores=None,
            )

        overall = scores.get("overall", 0)
        passed = overall >= 3
        checks = [
            Check(
                name="llm_judge",
                passed=passed,
                detail=f"overall={overall}" if passed else f"overall={overall} < 3",
            ),
        ]
        return EvalResult(
            case_id=case.id,
            passed=passed,
            checks=checks,
            scores=scores,
        )

    @staticmethod
    def _parse_scores(raw: str) -> dict | None:
        """Extract JSON scores from LLM response, tolerating markdown fences."""
        text = raw.strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            lines = text.splitlines()
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Try to find JSON object in response
            start = text.find("{")
            end = text.rfind("}")
            if start == -1 or end == -1:
                return None
            try:
                data = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None

        required = {"relevance", "accuracy", "completeness", "conciseness", "overall"}
        if not required.issubset(data.keys()):
            return None
        # Coerce to int, clamp 1-5
        return {k: max(1, min(5, int(data[k]))) for k in required}


# ---------------------------------------------------------------------------
# Evaluator registry & main entry point
# ---------------------------------------------------------------------------

_deterministic = DeterministicEval()
_heuristic = HeuristicEval()
_llm_judge = LLMJudgeEval()


async def evaluate(case: TestCase, result: TestResult) -> EvalResult:
    """Route to appropriate evaluator based on case.eval_strategy."""
    strategy = getattr(case, "eval_strategy", "deterministic")

    if strategy == "deterministic":
        return _deterministic.evaluate(case, result)
    elif strategy == "heuristic":
        return _heuristic.evaluate(case, result)
    elif strategy == "llm_judge":
        return await _llm_judge.evaluate(case, result)
    else:
        logger.warning("Unknown eval_strategy '%s', falling back to heuristic", strategy)
        return _heuristic.evaluate(case, result)
