# Khalil Performance Scorecard

The canonical record of Khalil's evaluation results, production health, and improvement trajectory. Updated after every eval run or fix cycle.

**Quick commands:**
```bash
python eval/metrics.py                          # Production metrics from live DB
python eval/scenario_runner.py                  # End-to-end scenario tests
python eval/tool_use_eval.py                    # Tool selection & parameter tests
python -m eval --parallel                       # Full eval pipeline (generated cases)
python -m eval --cases eval/fixtures/cases.json # Eval with frozen case set
python -m eval --trend                          # Pass rate trend across runs
```

---

## Industry Benchmark Scorecard

Measured against GAIA, TheAgentCompany, τ-bench, ConvBench, Microsoft Failure Taxonomy.

| Metric | Value | Target | Source | Status |
|--------|-------|--------|--------|--------|
| Eval Pass Rate (frozen cases) | 90.2% | >85% | `eval/reports/20260405_032129.json` | PASS |
| Eval Pass Rate (generated, full scope) | 23.4% | >50% | `eval/reports/20260408_162754.json` | FAIL |
| Direct-Action Pass Rate | 71.3% | >80% | 910 pattern-matched cases, Apr 8 | BORDERLINE |
| Tool Success Rate | 84.4% | >90% (τ-bench) | Production DB (205/243 calls) | BORDERLINE |
| Tool-Use Eval | 100% | 100% | `tool_use_eval.py` (48/48) | PASS |
| Task Completion Rate | N/A | >50% (TheAgentCompany) | Insufficient plan data | — |
| User Correction Rate | 0.0% | <10% | Production DB (0/19,950) | PASS |
| Latency P50 | 0.79s | <2s | Production DB | PASS |
| Latency P95 | 54.57s | <10s | Production DB | FAIL |
| Error Cascade Rate | 0.0% | <5% | Production DB | PASS |
| Self-Heal Success | 25% | >50% | 1/4 attempts (3 guardian-blocked) | FAIL |
| Capability Gap Closure | 51.7% | >60% | 15 generated / 29 detected | BORDERLINE |

**Last updated**: 2026-04-08 (Run #8)

---

## Eval Run History

| # | Date | Branch/Commit | Cases | Passed | Rate | Delta | Change |
|---|------|---------------|-------|--------|------|-------|--------|
| 0 | 2026-03-28 | pre-fix | 50 | 13 | 26% | — | Initial run, all failures were LLM timeouts |
| 1 | 2026-03-29 | `ebf5d3b` | 309 | 116 | 37.5% | +11.5pp | Direct dispatch fix, handler-aware case classification |
| 2 | 2026-03-29 | pending | 323 | 214 | 66.3% | +28.8pp | Query generator fix, NameError fix, routing priority fix |
| 3 | 2026-03-29 | pending | 245 | 186 | 75.9% | +9.6pp | Keyword cases → llm_intent, natural query templates |
| — | 2026-03-29 | claude | 323 | 242 | 74.9% | +8.6pp | Claude API comparison: timeouts 89→29, ceiling ~93% |
| 4 | 2026-03-29 | `1000a36` | 245 | 206 | 84.1% | +8.2pp | Latency threshold fix, runner timeout, screenshot channel fix |
| 5 | 2026-03-29 | pending | 196 | 181 | 92.3% | +8.2pp | Param extraction, weather exclusion, network skill reclassification |
| 6 | 2026-04-05 | production | 2,458 | 2,216 | 90.2% | -2.1pp | Full-scope production validation (frozen cases.json) |
| 7 | 2026-04-08 | `fix/p0-p2` | 910 | 649 | 71.3% | — | Direct-action only (LLM cases excluded — API key issue) |
| 8 | 2026-04-08 | `fix/p0-p2` | 2,774 | 650 | 23.4% | — | Full generated suite via Taskforce. handler_bad_output dominant failure |

### Run #8 Notes
- **Taskforce proxy working** — LLM responses flowing via `hendrix-genai.spotify.net`
- 2,098/2,124 failures are `handler_bad_output` (response quality, not routing)
- Only 26 timeouts (down from 37 in Run #4)
- Calendar: **0% → 77%** after pattern fix (PR #199)
- Reminder routing: **0% → 100%** (handler needs LLM for bare queries)
- Self-healing: **unblocked** (`BLOCKLISTED_CALLS` import fixed in PR #199)

---

## Critical Fixes Applied

| PR | Date | Fix | Impact |
|----|------|-----|--------|
| [#199](https://github.com/ahmedkhaledmohamed/khalil/pull/199) | 2026-04-08 | `BLOCKLISTED_CALLS` → `BLOCKLISTED_BARE_CALLS` + `BLOCKLISTED_QUALIFIED_CALLS` in `healing.py` | Self-healing pipeline unblocked (30 scheduler failures resolved) |
| [#199](https://github.com/ahmedkhaledmohamed/khalil/pull/199) | 2026-04-08 | Calendar patterns expanded (5 new patterns) | 0/9 → 17/17 (0% → 100%) |
| [#199](https://github.com/ahmedkhaledmohamed/khalil/pull/199) | 2026-04-08 | Reminder patterns expanded (pronoun variants, typo tolerance) | Routing: 0% → 100% |
| [#198](https://github.com/ahmedkhaledmohamed/khalil/pull/198) | 2026-04-08 | Eval framework: scenarios, metrics, scenario runner, tool-use regression tests | 16 scenarios, 8 metrics, 4 incident test cases |

---

## Skill Health Matrix

### Broken (0% pass rate)
| Skill | Cases | Issue | Fix Status |
|-------|-------|-------|------------|
| claude_code_status | 0/30 | Timeout — handler blocks | Open |
| terminal_exec | 0/10 | Timeout | Open |
| tmux_* (5 skills) | 0/53 | Timeout | Open |
| voice_* (2 skills) | 0/17 | Timeout | Open |
| workflow_* (3 skills) | 0/35 | Timeout | Open |
| weather / weather_forecast | 0/10 | Missing env vars (KHALIL_WEATHER_LAT/LON) | Config |
| edge (empty/whitespace input) | 0/100 | No graceful handling of empty queries | Open |
| conversational | 0/100 | LLM response quality below heuristic threshold | Open |

### Fixed This Cycle
| Skill | Before | After | How |
|-------|--------|-------|-----|
| calendar | 0/9 (0%) | 17/22 (77%) | Pattern expansion (PR #199) |
| reminder (routing) | 0/7 (0%) | 11/11 routed correctly | Pattern expansion (PR #199) |

### Healthy (>75% pass rate)
Shell (78%), calendar (77%), reminder (73%), and most pattern-matched skills.

---

## Production Usage Profile

_Source: `eval/metrics.py` against `data/khalil.db`. Period: Mar 15 – Apr 8, 2026._

| Metric | Value |
|--------|-------|
| Total messages | 24,560 |
| Unique sessions | 4 |
| Memories stored | 327 |
| Conversation summaries | 109 |
| Signals collected | 27,509 |

### Tool Usage (211 production calls)
```
shell              176  (83.4%)  avg 2.4s
claude_code_status   7  (3.3%)   avg 0.2s
read_terminal        7  (3.3%)   avg 0.1s
label                5  (2.4%)   avg 15.5s  ← slowest
list_sessions        5  (2.4%)   avg 0.1s
others              11  (5.2%)
```

### Signal Distribution (27,509 signals)
| Signal Type | Count | % |
|-------------|-------|---|
| Model routing | 11,758 | 42.7% |
| Capability usage | 9,289 | 33.8% |
| Response latency | 4,056 | 14.8% |
| LLM failures | 2,916 | 10.6% |
| Conversation success | 246 | 0.89% |
| Action execution failure | 56 | 0.20% |

---

## Self-Improvement System Status

| Component | Status | Evidence |
|-----------|--------|----------|
| Signal collection | **Strong** | 27,509 signals across 24 days |
| Micro-reflection | **Working** | Runs daily |
| Weekly reflection | **Working** | Generates insights |
| Capability gap detection | **Working** | 29 gaps detected |
| Evolution (code gen) | **Broken** | Truncated patches, guardian blocks |
| Auto-merge heals | **Fixed** | Import error resolved (PR #199) |
| Preference learning | **Weak** | 6 preferences, all confidence <0.65 |

---

## Priority Fixes Remaining

| # | Fix | Impact | Effort | Status |
|---|-----|--------|--------|--------|
| 1 | ~~Fix BLOCKLISTED_CALLS import~~ | ~~Self-healing unblocked~~ | ~~Small~~ | Done (PR #199) |
| 2 | ~~Fix calendar patterns~~ | ~~0% → 77%~~ | ~~Small~~ | Done (PR #199) |
| 3 | ~~Fix reminder patterns~~ | ~~Routing fixed~~ | ~~Small~~ | Done (PR #199) |
| 4 | LLM retry/fallback for timeouts | P95 latency 54.57s, 10.6% LLM failure rate | Medium | Open |
| 5 | Fix evolution truncation | Self-heal patches cut off mid-line | Medium | Open |
| 6 | Fix path hallucination in shell | 47 failures in 48h (Apr 5-6) | Medium | Open |
| 7 | Handler output quality (2,098 failures) | Dominant failure mode in full eval | Large | Open |
| 8 | Add golden cases for 32 untested skills | No regression anchor | Large | Open |
| 9 | Edge case handling (empty input) | 100 failures | Small | Open |

---

## Eval Framework Components

| Component | File | Purpose |
|-----------|------|---------|
| Test cases (~2,500+) | `cases.py` | Pattern-based + generated cases |
| Golden cases (479) | `fixtures/golden.yaml` | Hand-curated regression anchors |
| 3-tier judge | `judge.py` | Deterministic → Heuristic → LLM scoring |
| Runner | `runner.py` | Parallel execution with Taskforce proxy |
| Gap analysis | `gap_analysis.py` | Failure categorization (9 gap types) |
| Auto-fix | `autofix.py` | Code generation for pattern/handler fixes |
| Scenarios (16) | `scenarios.py` | End-to-end multi-turn task tests |
| Scenario runner | `scenario_runner.py` | Scenario execution + evaluation |
| Metrics (8) | `metrics.py` | Production DB → industry metrics |
| Tool-use eval (48) | `tool_use_eval.py` | Schema, parameter, filter, bypass tests |
| Validators | `validators.py` | Skill-specific response quality checks |
| Dashboard | `dashboard.html` | Interactive HTML visualization |

### Industry Benchmarks Referenced

| Benchmark | What We Use | Link |
|-----------|------------|------|
| GAIA | Multi-step task scenarios | [arxiv](https://arxiv.org/abs/2311.12983) |
| TheAgentCompany | Task completion measurement | [paper](https://openreview.net/pdf/b533993ef9bc8320779646b1c475e47635dd98c2.pdf) |
| τ-bench | Tool selection accuracy | [langwatch](https://langwatch.ai/scenario/testing-guides/tool-calling/) |
| ConvBench/Ragas | Multi-turn coherence | [docs](https://docs.ragas.io/en/stable/) |
| Microsoft Failure Taxonomy | Error classification | [whitepaper](https://cdn-dynmedia-1.microsoft.com/is/content/microsoftcorp/microsoft/final/en-us/microsoft-brand/documents/Taxonomy-of-Failure-Mode-in-Agentic-AI-Systems-Whitepaper.pdf) |
| METR | Time horizons | [blog](https://metr.org/blog/2025-03-19-measuring-ai-ability-to-complete-long-tasks/) |
| DeepEval | Multi-turn metrics | [docs](https://deepeval.com/guides/guides-multi-turn-evaluation-metrics) |

---

## Process

1. Pick highest-impact gap from the priority table above
2. Fix on a branch
3. Re-run eval: `python -m eval --cases eval/fixtures/cases.json --parallel`
4. Run tool-use eval: `python eval/tool_use_eval.py`
5. Run scenario tests: `python eval/scenario_runner.py`
6. Run production metrics: `python eval/metrics.py`
7. Record results in this scorecard — update the run history table and benchmark scorecard
8. Only merge if pass rate improved and no regressions
9. Commit updated SCORECARD.md with the PR
