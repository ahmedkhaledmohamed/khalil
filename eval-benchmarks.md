# Khalil Eval Benchmarks


Evaluation framework for Khalil, a self-improving AI personal assistant. Measures routing accuracy, task completion, tool reliability, multi-turn coherence, and self-healing — benchmarked against published AI agent evaluation standards.

**Repo**: [ahmedkhaledmohamed/khalil](https://github.com/ahmedkhaledmohamed/khalil) | **Eval code**: [`eval/`](https://github.com/ahmedkhaledmohamed/khalil/tree/main/eval)

---

## Industry Benchmarks Referenced

| Benchmark | What We Measure | Khalil Metric |
|-----------|----------------|---------------|
| [GAIA](https://arxiv.org/abs/2311.12983) | Multi-step task scenarios | 16 end-to-end scenarios with tool verification |
| [TheAgentCompany](https://openreview.net/pdf/b533993ef9bc8320779646b1c475e47635dd98c2.pdf) | Task completion rate | Plans started vs completed (target: >50%) |
| [τ-bench](https://langwatch.ai/scenario/testing-guides/tool-calling/) | Tool selection accuracy | Tool-use eval: 48 tests covering schema, params, filters, bypass |
| [ConvBench / Ragas](https://docs.ragas.io/en/stable/) | Multi-turn coherence | Pronoun resolution, context carryover, reference tracking |
| [Microsoft Failure Taxonomy](https://cdn-dynmedia-1.microsoft.com/is/content/microsoftcorp/microsoft/final/en-us/microsoft-brand/documents/Taxonomy-of-Failure-Mode-in-Agentic-AI-Systems-Whitepaper.pdf) | Error classification | 9 gap types: pattern_gap, handler_missing, handler_bad_output, timeout, etc. |
| [METR](https://metr.org/blog/2025-03-19-measuring-ai-ability-to-complete-long-tasks/) | Time horizons | P50/P95 latency, timeout handling |
| [DeepEval](https://deepeval.com/guides/guides-multi-turn-evaluation-metrics) | Multi-turn metrics | Embedding-based semantic similarity scoring |

---

## Eval Framework Architecture

```
eval/
├── cases.py              # Test case generator (~2,500 cases from skill metadata)
├── fixtures/
│   ├── cases.json        # Frozen case set (851 cases, regression anchor)
│   └── golden.yaml       # Hand-curated golden cases (479)
├── runner.py             # Parallel execution via InstrumentedChannel
├── judge.py              # 3-tier evaluation engine
├── gap_analysis.py       # Failure categorization (9 gap types)
├── autofix.py            # Code generation for pattern/handler fixes
├── scenarios.py          # 16 end-to-end multi-turn scenarios
├── scenario_runner.py    # Scenario execution + evaluation
├── metrics.py            # Production DB → industry metrics
├── tool_use_eval.py      # 48 tool selection regression tests
├── validators.py         # Skill-specific response quality checks
├── visualize.py          # Pass rate trend visualization
├── generate_dashboard.py # Interactive HTML dashboard
└── SCORECARD.md          # Canonical results record
```

### Three-Tier Judge

| Tier | Strategy | When Used | Examples |
|------|----------|-----------|---------|
| 1. Deterministic | Exact routing + containment checks | Cases with known expected_action | "set timer 5 min" → verify timer skill routed |
| 2. Heuristic | Structural quality checks (no LLM) | Cases with expected_contains/not_contains | Response includes "Toronto" but not "Traceback" |
| 3. LLM Judge | Ollama-based scoring (5 dimensions) | Open-ended conversational cases | Scores: relevance, accuracy, completeness, conciseness, overall |

### Test Case Generation

Cases generated from three sources:
1. **Skill metadata** — patterns and keywords from the 60+ registered skills produce ~1,500 templated cases
2. **Paraphrases** — synonym substitution generates alternate phrasings (~500 variants)
3. **Golden set** — 479 hand-curated YAML cases with exact expected outputs

Each case specifies: `query`, `expected_path` (direct_action / llm_intent / conversational), `expected_action`, `expected_contains`, `eval_strategy`.

### Gap Analysis Categories

| Gap Type | Meaning | Auto-Fixable |
|----------|---------|--------------|
| `pattern_gap` | Skill exists but pattern didn't match | Yes — autofix generates regex |
| `handler_missing` | No handler for this capability | Partial — self-extension engine |
| `handler_bad_output` | Handler ran but output failed quality checks | No — needs investigation |
| `timeout` | Handler exceeded time limit | Config change |
| `error_cascade` | One failure caused downstream failures | No |
| `hallucination` | Response contains fabricated information | No |
| `context_loss` | Multi-turn context dropped | Architecture fix |
| `llm_quality` | LLM response below heuristic threshold | Model/prompt tuning |
| `edge_case` | Empty input, unicode, injection attempts | Handler hardening |

---

## Scorecard (as of April 9, 2026)

| Metric | Value | Target | Benchmark Source | Status |
|--------|-------|--------|-----------------|--------|
| Eval pass rate (frozen 851 cases) | 83.9% | >85% | Internal | BORDERLINE |
| Tool-use eval | 100% (48/48) | 100% | τ-bench | PASS |
| Tool success rate | 84.4% (205/243) | >90% | τ-bench | BORDERLINE |
| User correction rate | 0.0% (0/19,950) | <10% | Custom | PASS |
| Latency P50 | 0.79s | <2s | METR | PASS |
| Latency P95 | 54.57s | <10s | METR | FAIL |
| Error cascade rate | 0.0% | <5% | Microsoft taxonomy | PASS |
| Self-heal success | 25% (1/4) | >50% | Custom | FAIL |
| Capability gap closure | 51.7% (15/29) | >60% | Custom | BORDERLINE |

### Latest Run (Run #9, 100-case subset): **96.0%** pass rate

---

## Eval Run History

| Run | Date | Cases | Passed | Rate | Delta | Notes |
|-----|------|-------|--------|------|-------|-------|
| 0 | Mar 28 | 50 | 13 | 26% | — | Initial run, all failures were LLM timeouts |
| 1 | Mar 29 | 309 | 116 | 37.5% | +11.5pp | Direct dispatch fix |
| 2 | Mar 29 | 323 | 214 | 66.3% | +28.8pp | Query generator + routing priority fix |
| 3 | Mar 29 | 245 | 186 | 75.9% | +9.6pp | Keyword → llm_intent reclassification |
| 4 | Mar 29 | 245 | 206 | 84.1% | +8.2pp | Latency threshold fix |
| 5 | Mar 29 | 196 | 181 | 92.3% | +8.2pp | Param extraction improvements |
| 6 | Apr 5 | 2,458 | 2,216 | 90.2% | -2.1pp | Full-scope production validation |
| 7 | Apr 8 | 910 | 649 | 71.3% | — | Direct-action only (API key issue) |
| 8 | Apr 8 | 2,774 | 650 | 23.4% | — | Full generated suite, handler_bad_output dominant |
| 9 | Apr 8 | 851 | 714 | 83.9% | -2.2pp | Frozen cases, apples-to-apples baseline |

**Trajectory**: 26% → 92.3% on controlled cases over 10 days. Full-scope production validation stabilizing at ~84%.

---

## End-to-End Scenarios (16)

Multi-turn task scenarios inspired by GAIA. Each defines user turns with expected tool calls, side effects, and verifiable success criteria.

| Category | Scenarios | What's Tested |
|----------|-----------|---------------|
| Email | label_and_archive, search_and_forward | Multi-step email workflows, count verification |
| Calendar | check_and_create | Availability check → event creation |
| Git/PR | create_pr_workflow, pr_number_extraction, repo_context_awareness | Hallucination guards for PR numbers |
| Shell | multi_step | Dependent command chains |
| Multi-turn | pronoun_resolution, context_carryover | "Compare them", "Actually make that 4pm" |
| Recovery | restart_continuity | Post-restart context recall |
| Failure handling | tool_failure_graceful, tool_timeout_handling | Error reporting without tracebacks |
| Safety | shell_dangerous_command | Blocks `rm -rf /`, `DROP TABLE` |
| Memory | memory_store_and_recall | Store fact → recall later |
| Scheduling | reminder_with_followup | Create → modify → verify reminder |

---

## Production Metrics (Mar 15 – Apr 9, 2026)

| Metric | Value |
|--------|-------|
| Total messages processed | 24,560 |
| Memories stored | 327 |
| Conversation summaries | 109 |
| Signals collected | 27,509 |
| Tool calls | 211 |
| Shell commands (83.4% of tool calls) | 176 |

### Signal Distribution

| Signal | Count | % |
|--------|-------|---|
| Model routing | 11,758 | 42.7% |
| Capability usage | 9,289 | 33.8% |
| Response latency | 4,056 | 14.8% |
| LLM failures | 2,916 | 10.6% |
| Conversation success | 246 | 0.89% |
| Action execution failure | 56 | 0.20% |

---

## Self-Improvement Loop

The eval framework feeds into Khalil's autonomous improvement cycle:

```
Eval Run → Gap Analysis → Priority Ranking → Autofix (code gen) → PR → Re-eval
                                                  ↑
                                        Self-extension engine
                                        (detects new capability gaps,
                                         generates handlers, opens PRs)
```

| Component | Status |
|-----------|--------|
| Signal collection (27,509 signals) | Strong |
| Daily micro-reflection | Working |
| Weekly reflection | Working |
| Capability gap detection (29 gaps) | Working |
| Evolution / code gen | Broken — truncated patches, guardian blocks |
| Auto-merge heals | Fixed (PR #199) |
| Preference learning (6 prefs, <0.65 confidence) | Weak |

---

## Running the Eval

```bash
# Full pipeline with frozen cases
python -m eval --cases eval/fixtures/cases.json --parallel

# Tool-use regression tests (48 tests)
python eval/tool_use_eval.py

# End-to-end scenarios (16 scenarios)
python eval/scenario_runner.py

# Production metrics from live DB
python eval/metrics.py

# Generate interactive dashboard
python eval/generate_dashboard.py

# Pass rate trend across runs
python -m eval --trend
```

---

## Open Issues

| # | Issue | Impact | Effort |
|---|-------|--------|--------|
| 1 | LLM retry/fallback for timeouts | P95 latency 54.57s | Medium |
| 2 | Evolution patch truncation | Self-heal generates incomplete code | Medium |
| 3 | Shell path hallucination | 47 failures in 48h | Medium |
| 4 | Handler output quality (2,098 failures) | Dominant failure mode | Large |
| 5 | Golden cases for 32 untested skills | No regression anchor | Large |
| 6 | Edge case handling (empty input) | 100 failures | Small |
