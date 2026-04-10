# Khalil Improvement Plan v2: 28 Items Across 7 Themes

Khalil is at 83.9% frozen eval pass rate (target >85%), with P95 latency at 54.57s (target <10s), self-healing at 25% success, and 32 untested skills. The dominant failure mode is `handler_bad_output` (2,098 failures). The 100-improvement roadmap (executed Mar 16) covered foundational work; this plan targets the next level — reliability, intelligence, and self-sustaining improvement — grounded in GAIA, tau-bench, METR, Microsoft Failure Taxonomy, and NIST AI RMF standards.

---

## Theme 1: RELIABILITY — Fix What Is Broken

### 1. Handler Output Normalization Layer ✅ (PR #222)
Introduce `HandlerResponse` dataclass (`.text`, `.success`, `.metadata`) and a wrapper that normalizes every handler return. Raw text gets wrapped, exceptions get caught, None produces "empty result." Directly addresses the #1 failure mode (2,098 `handler_bad_output` failures).
- **Effort:** M | **Impact:** HIGH | **Benchmark:** Microsoft Failure Taxonomy
- **Files:** `skills.py`, `server.py` (handle_action_intent), `eval/gap_analysis.py`
- **Unlocks:** 7, 12, 22

### 2. Graceful Empty/Whitespace Input Handling ✅ (PR #218)
`handle_message_generic` silently returns on empty input (line ~5423). Add explicit friendly response for empty, whitespace, and 1-2 char inputs. Eliminates 100 eval failures.
- **Effort:** S | **Impact:** MEDIUM | **Benchmark:** ConvBench
- **Files:** `server.py` (handle_message_generic)

### 3. Fix 8 Broken Skills (Timeout Cluster) ✅ (PR #223)
`claude_code_status`, `terminal_exec`, `tmux_*` (5), `voice_*` (2), `workflow_*` (3) all at 0%. Add availability guards that return clear "unavailable" messages when prerequisites aren't met (no tmux session, no ffmpeg, no Claude Code binary). Turns timeouts into fast, informative failures. Eliminates 145 failures.
- **Effort:** M | **Impact:** HIGH | **Benchmark:** tau-bench, GAIA
- **Files:** `actions/tmux_control.py`, `actions/terminal.py`, `actions/claude_code.py`, `actions/voice.py`, `actions/workflows.py`
- **Unlocks:** 8, 18

### 4. LLM Timeout Retry with Fast Fallback ✅ (PR #224)
P95 = 54.57s. Reduce per-model timeout to 8s first attempt, implement 15s total "latency budget" across fallback chain, return cached/degraded response if budget expires. Add streaming partial responses so user sees output within 2s.
- **Effort:** M | **Impact:** HIGH | **Benchmark:** METR, TheAgentCompany
- **Files:** `server.py` (ask_llm, _fallback_to_claude, LLM_TIMEOUT), `resilience.py`
- **Unlocks:** 7, 17

### 5. Shell Path Hallucination Guard ✅ (PR #219)
47 failures in 48h from hallucinated file paths. Add pre-execution path validation: regex-detect paths in commands, verify via `os.path.exists()`, suggest corrections via fuzzy match against cached home directory tree.
- **Effort:** S | **Impact:** MEDIUM | **Benchmark:** GAIA (factual grounding), NIST AI RMF
- **Files:** `actions/shell.py` (execute_shell, classify_command)

### 6. Conversational Mode Quality Floor ✅ (PR #221)
0% pass rate on 100 conversational cases. The system prompt is tuned for action dispatch, not dialogue. Add a dedicated conversational system prompt variant when no skill pattern matches, optimized for quality conversation.
- **Effort:** S | **Impact:** MEDIUM | **Benchmark:** ConvBench, DeepEval
- **Files:** `server.py` (handle_message_generic, _build_system_prompt)
- **Unlocks:** 14

---

## Theme 2: SELF-HEALING — Make the Loop Self-Sustaining

### 7. Guardian Calibration — Reduce False Block Rate ✅ (PR #225)
Guardian blocks 75% of generated patches. The CODE_REVIEW_PROMPT is overly broad. Fix: (a) whitelist safe patterns for Khalil's own codebase (subprocess to osascript/tmux/brew is expected); (b) require structured JSON output; (c) add confidence score — only BLOCK when >0.8; (d) track guardian false-positive rate.
- **Effort:** M | **Impact:** HIGH | **Benchmark:** Anthropic Constitutional AI, NIST AI RMF
- **Files:** `actions/guardian.py` (CODE_REVIEW_PROMPT, _parse_verdict, review_code_patch)
- **Unlocks:** 8, 9

### 8. Self-Healing Patch Truncation Fix ✅ (PR #220)
Healing generates patches with `max_tokens=1500`, causing mid-line truncation that guardian correctly blocks. Raise to 4000, add truncation detector (check for balanced brackets/parens), re-request continuation if truncated, validate with `ast.parse()` before guardian review.
- **Effort:** S | **Impact:** HIGH | **Benchmark:** Microsoft Failure Taxonomy
- **Files:** `healing.py` (generate_healing_patch, validate_patch), `llm_client.py`
- **Unlocks:** 9

### 9. Closed-Loop Heal Verification
Currently fire-and-forget. Implement: (a) store PR number in evolution_candidates; (b) check merge status via `gh pr view`; (c) re-run relevant eval cases post-merge; (d) auto-create "failed_heal" follow-up if eval still fails; (e) record improvement delta on success.
- **Effort:** L | **Impact:** HIGH | **Benchmark:** METR, TheAgentCompany
- **Files:** `evolution.py` (_check_evolution_outcomes), `healing.py` (check_heal_outcomes), `eval/runner.py`
- **Unlocks:** 17, 20

---

## Theme 3: OBSERVABILITY — Measure What Matters

### 10. Per-Tool Accuracy Breakdown ✅ (PR #226)
Single aggregate tool_success_rate (84.4%) hides which tools are broken. Extend `metrics.py` to compute per-tool: success_rate, avg_latency, call_count from conversations table. Enables targeted investment.
- **Effort:** S | **Impact:** MEDIUM | **Benchmark:** tau-bench
- **Files:** `eval/metrics.py` (compute_metrics, MetricsSnapshot)
- **Unlocks:** 1, 17

### 11. Hallucination Detection Metric ✅ (PR #233)
No metric for factual accuracy. Implement lightweight grounding check: extract entities/numbers from response, verify they appear in retrieved context. Compute grounding_ratio = entities_grounded / entities_total. Log as signal. Pure string matching, no LLM needed.
- **Effort:** M | **Impact:** MEDIUM | **Benchmark:** GAIA (factuality), Ragas (faithfulness)
- **Files:** `evolution.py` (post_interaction_check), `eval/metrics.py`, `eval/validators.py`
- **Unlocks:** 14

### 12. Cost-Per-Task Tracking ✅ (PR #231)
No cost visibility. Capture `usage.prompt_tokens` and `usage.completion_tokens` from API responses, multiply by provider pricing, record alongside latency signal. Add `cost_per_task_p50/p95` to MetricsSnapshot. Enables cost-aware routing.
- **Effort:** S | **Impact:** LOW | **Benchmark:** TheAgentCompany
- **Files:** `server.py` (ask_llm, call_llm_with_tools), `eval/metrics.py`, `model_router.py`
- **Unlocks:** 16

### 13. Recovery Time (MTTR) Metric ✅ (PR #232)
No metric for failure-to-resolution time. Add timestamps: failure detected -> heal PR created -> PR merged -> verified. MTTR = first to last. Store in evolution_candidates table. Target: <24h critical, <7d non-critical.
- **Effort:** S | **Impact:** MEDIUM | **Benchmark:** METR
- **Files:** `evolution.py` (EvolutionCandidate), `eval/metrics.py`
- **Unlocks:** 9

### 14. Multi-Turn Coherence Scoring ✅ (PR #234)
16 scenarios exist but lack a coherence metric. Add `MultiTurnCoherenceEval`: after all turns, check entity consistency across turns (does "Forward that email" resolve correctly?). Add `multi_turn_coherence_score` to MetricsSnapshot.
- **Effort:** M | **Impact:** MEDIUM | **Benchmark:** ConvBench, Ragas, DeepEval
- **Files:** `eval/judge.py`, `eval/scenario_runner.py`, `eval/scenarios.py`
- **Unlocks:** 20

---

## Theme 4: INTELLIGENCE — Make Khalil Smarter

### 15. Golden Case Coverage for 32 Untested Skills ✅ (PR #227)
32 skills have zero golden cases. Generate 3-5 per skill: 1 happy-path per action type + 1 edge case. Target: 100-160 new golden cases in `fixtures/golden.yaml`. The single biggest eval quality investment.
- **Effort:** M | **Impact:** HIGH | **Benchmark:** tau-bench, GAIA
- **Files:** `eval/fixtures/golden.yaml`, `eval/cases.py`, `eval/case_gen.py`
- **Unlocks:** 1, 7, 9

### 16. Cost-Aware Model Routing
Model router maps all tiers to Opus via Taskforce ("free"). Route FAST queries (greetings, lookups) to Haiku/Sonnet. Keep privacy routing to Ollama. Add A/B testing: record which model produced better eval scores per query type.
- **Effort:** M | **Impact:** MEDIUM | **Benchmark:** TheAgentCompany
- **Files:** `model_router.py`, `config.py`, `server.py` (ask_llm)
- **Unlocks:** 4

### 17. LLM Response Variance Mitigation ✅ (PR #230)
111 regressions in Run #9 from model variance. Fix: (a) `temperature=0.0` for all tool-use calls; (b) expand `_TOOL_DESCRIPTIONS` to all 50+ tools with explicit examples; (c) pin system prompt formatting order; (d) response normalization before eval comparison.
- **Effort:** M | **Impact:** HIGH | **Benchmark:** tau-bench
- **Files:** `tool_catalog.py`, `server.py` (call_llm_with_tools), `eval/validators.py`
- **Unlocks:** 1, 15

### 18. Preference Learning Amplification
27,509 signals but only 6 preferences (all <0.65 confidence). Too passive — requires `/feedback` command. Add implicit preference detection from conversation patterns, increase confidence via repeated signals, expose `/preferences` command for transparency and correction.
- **Effort:** L | **Impact:** MEDIUM | **Benchmark:** METR, Constitutional AI
- **Files:** `learning.py` (set_preference, get_preference), `server.py` (style_hint injection)
- **Unlocks:** 20

---

## Theme 5: SAFETY AND TRUST

### 19. Audit Trail with Provenance Chain ✅ (PR #235)
`data/audit_trail.jsonl` exists but is dead. Tool calls saved to conversations table lack provenance (which signal triggered it, which model, guardian verdict). Write structured JSONL per tool execution: timestamp, query, tool, args, guardian verdict, model, latency, result summary, autonomy level. Add `/audit [last N]` command.
- **Effort:** M | **Impact:** MEDIUM | **Benchmark:** NIST AI RMF (transparency, accountability)
- **Files:** `server.py` (_execute_tool_call), `data/audit_trail.jsonl`, `autonomy.py`
- **Unlocks:** 21

### 20. Sensitive Data Flow Map
`contains_sensitive_data()` exists but no map of where PII/financial/health data flows. Add classification tags to state providers, ensure tagged data never reaches cloud LLMs when `_force_local` is true, add redaction in audit trail, add `/privacy` command showing data routing.
- **Effort:** L | **Impact:** MEDIUM | **Benchmark:** NIST AI RMF, EU AI Act
- **Files:** `config.py` (SENSITIVE_PATTERNS), `server.py` (contains_sensitive_data), `state/email_provider.py`, `state/calendar_provider.py`

### 21. Autonomy Level Promotion with Decay
3-tier autonomy is static. Build trust over time: after 50 consecutive successes, auto-promote tool to next tier (with notification). After any correction/failure, demote immediately. Add trust score per tool via `/autonomy`.
- **Effort:** M | **Impact:** LOW | **Benchmark:** NIST AI RMF (proportionality)
- **Files:** `autonomy.py` (ACTION_RULES, AutonomyController)
- **Unlocks:** 7

---

## Theme 6: EVAL INFRASTRUCTURE

### 22. Eval Stability Layer (Deterministic Seeding) ✅ (PR #228)
111 phantom regressions from LLM variance. Fix: `temperature=0.0` for eval calls, semantic equivalence mode in heuristic evaluator, retry failed cases once, store raw LLM response for root-cause analysis.
- **Effort:** M | **Impact:** HIGH | **Benchmark:** DeepEval
- **Files:** `eval/runner.py` (run_case), `eval/judge.py` (HeuristicEval, DeterministicEval)
- **Unlocks:** 15, 9

### 23. Continuous Eval (CI Gate) ✅ (PR #229)
Eval runs are manual. Add GitHub Actions workflow: run frozen case eval on every PR, compare against previous pass rate, block merge if >1pp drop, post summary as PR comment.
- **Effort:** M | **Impact:** HIGH | **Benchmark:** TheAgentCompany
- **Files:** `eval/__main__.py`, `eval/gap_analysis.py` (diff_reports), new: `.github/workflows/eval.yml`
- **Unlocks:** 7, 9

### 24. Scenario Coverage Expansion (GAIA-style)
16 scenarios vs GAIA's 466. Expand to 50+: long-horizon multi-tool chains, cross-session context, ambiguous intent requiring clarification, error recovery mid-task, multi-channel flows.
- **Effort:** L | **Impact:** MEDIUM | **Benchmark:** GAIA, TheAgentCompany
- **Files:** `eval/scenarios.py`, `eval/scenario_runner.py`
- **Unlocks:** 14

---

## Theme 7: STRATEGIC CAPABILITIES — World-Class Differentiation

### 25. Proactive Intelligence Layer
Agent loop is reactive (state changes only). Add: (a) daily anticipation pass at 7am (unusual meetings -> research attendees, severe weather, high-priority unread); (b) pre-meeting prep 15min before events (context, email threads with attendees); (c) weekly pattern analyzer (learn rhythms, surface relevant info).
- **Effort:** XL | **Impact:** HIGH | **Benchmark:** METR, TheAgentCompany
- **Files:** `agent_loop.py`, `scheduler/proactive.py`, `scheduler/planning.py`, `state/collector.py`
- **Unlocks:** 18

### 26. Long-Horizon Task Execution (>24h)
Orchestrator only handles synchronous single-turn tasks. Add: persistent task queue in DB, "task watcher" in agent loop polling every 5min, completion conditions (regex on state change, time elapsed, external event), notification + optional follow-up action on completion.
- **Effort:** XL | **Impact:** HIGH | **Benchmark:** METR (96h tasks), TheAgentCompany
- **Files:** `orchestrator.py`, `agent_loop.py`, new: `scheduler/tasks.py`
- **Unlocks:** 25

### 27. Cross-Session Memory with Forgetting
27,509 signals never pruned. Implement: time-decay on memory relevance, conflict resolution (newer wins), memory consolidation (merge related signals into summary), `/forget` command, garbage collection of processed signals >30d.
- **Effort:** L | **Impact:** MEDIUM | **Benchmark:** METR (long-term maintenance)
- **Files:** `learning.py`, `knowledge/search.py`, `memory/session_continuity.py`
- **Unlocks:** 18

### 28. Implicit User Satisfaction Signal
`/feedback` has 0 uses. Implement: (a) "task completion confirmation" — next message is correction or new topic; (b) session quality score = 1 - (negative_signals / total_turns); (c) engagement trend (declining usage = negative); (d) weekly "Satisfaction Index" in digest.
- **Effort:** M | **Impact:** MEDIUM | **Benchmark:** ConvBench, UX research
- **Files:** `learning.py`, `evolution.py` (post_interaction_check), `eval/metrics.py`, `scheduler/digests.py`
- **Unlocks:** 18

---

## Execution Order

### Phase 1 — Quick Wins (Week 1-2)
2 -> 5 -> 8 -> 6 (all S effort, eliminate 250+ failures, unblock healing)

### Phase 2 — Core Reliability (Week 3-4)
1 -> 3 -> 4 -> 7 (M effort, address top failure modes, fix P95 latency)

### Phase 3 — Eval & Observability (Week 5-6)
10 -> 15 -> 22 -> 23 -> 17 (anchor regressions, stabilize eval, CI gate)

### Phase 4 — Intelligence & Safety (Week 7-10)
12 -> 13 -> 11 -> 14 -> 19 -> 9 -> 16 (metrics, safety, closed-loop healing)

### Phase 5 — Strategic Bets (Week 11-16)
18 -> 28 -> 21 -> 20 -> 24 -> 27 -> 25 -> 26 (differentiation, long-horizon)

---

## Projected Impact

| Metric | Current | After Phase 1-2 | After Phase 3-4 | After Phase 5 |
|--------|---------|-----------------|-----------------|---------------|
| Frozen eval pass rate | 83.9% | ~90% | ~93% | ~95% |
| P95 latency | 54.57s | <15s | <10s | <8s |
| Self-heal success | 25% | 60% | 80% | 85% |
| Tool success rate | 84.4% | 88% | 92% | 95% |
| Capability gap closure | 51.7% | 55% | 70% | 80% |
| Hallucination rate | unmeasured | unmeasured | <5% | <3% |
| Multi-turn coherence | unmeasured | unmeasured | measured | >90% |

## Verification

After each phase:
```bash
python -m eval --cases eval/fixtures/cases.json --parallel  # Frozen eval
python eval/tool_use_eval.py                                 # Tool regression
python eval/scenario_runner.py                               # E2E scenarios
python eval/metrics.py                                       # Production metrics
```

Compare against SCORECARD.md baselines. Only merge if pass rate improved and no regressions >1pp.
