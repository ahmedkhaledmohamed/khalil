# Khalil Evolution and Improvement Plan

**Status:** Proposed

**Last updated:** August 20, 2026

**Planning horizon:** Five phases, delivered through independently reviewable pull requests

## Decision

Khalil should pause capability expansion and concentrate on reliable execution of its highest-value workflows. The system already has broad integration coverage, proactive loops, multi-step orchestration, self-healing, and self-extension. Its main constraint is no longer what it can attempt; it is whether an attempted task follows consistent policy, reports failures accurately, and produces a verified outcome.

The next evolution therefore prioritizes:

1. Safe and deterministic code generation.
2. One enforceable execution and approval path.
3. Consistent behavior across channels.
4. Durable graph execution with bounded loops.
5. Outcome-based evaluation and selective product expansion.

This plan supersedes the completed 28-item improvement plan. That work added useful reliability, evaluation, observability, and self-healing mechanisms, but the mechanisms remain distributed across incompatible runtime paths.

## Current-State Assessment

The August 2026 architecture review found five structural constraints.

### Safety is path-dependent

`ExecutionBus` defines autonomy checks, approvals, rate limiting, and audit behavior, but the primary LLM tool loop can invoke handlers directly. Built-in actions and individual handlers also enforce policy inconsistently. A side effect can therefore receive different protection depending on how Khalil selected it.

### Channels do not share one runtime

Telegram uses task state, context assembly, the iterative tool loop, and verification. Generic channels first attempt regex-based skill dispatch and then use a simpler conversational path. The same request can select a different capability or call it with a different contract depending on the channel.

### Capability contracts are implicit

Skill handlers and Telegram command handlers use different signatures, while generation validation checks only whether a named function exists. Generated code can pass validation and CI without having a working entry point.

### Loops and graphs are separate subsystems

The reactive tool loop, proactive agent loop, DAG orchestrator, scheduler, healing loop, and extension loop each manage execution differently. They do not share durable state, budgets, termination reasons, or a common verification model.

### Activity is measured more reliably than outcomes

Khalil records extensive interaction and tool activity, but verified task completion, false-success rate, approval correctness, latency, cost, and recovery quality are incomplete or disconnected. Green checks can therefore coexist with broken advertised behavior.

## Product Objective

Khalil should reliably complete and verify the ten personal workflows that create the most value for Ahmed before expanding its capability surface.

Success means:

- The same request behaves consistently across supported channels.
- Every side effect passes through the same policy and approval process.
- Empty results are distinguishable from authentication, network, and execution failures.
- Multi-step work can resume safely without duplicating side effects.
- Khalil can explain what completed, what failed, and what evidence supports the outcome.
- Self-improvement produces reviewable proposals that pass deterministic behavioral tests.

## Engineering Principles

1. **Keep a modular monolith.** Khalil is a local-first, single-user system; microservices would add operational cost without solving the current problems.
2. **One execution path.** Selection mechanisms may differ, but execution, policy, verification, and audit must not.
3. **Structured state over prose.** Success, failure, evidence, side effects, and retryability should be typed fields rather than inferred from response text.
4. **Compose workflows before generating code.** Co-occurring actions should produce declarative workflow proposals unless a reusable primitive is missing.
5. **Verification is part of execution.** A handler returning without an exception does not prove that the requested outcome occurred.
6. **Self-improvement remains human-reviewed.** Generated changes stay quarantined until Khalil demonstrates consistently safe generation and evaluation.

## Phase 0: Stabilize Generated Changes

**Objective:** Prevent Khalil from producing contaminated or structurally invalid pull requests.

### Deliverables

- Create every generated branch from a fetched, explicit `origin/main` commit.
- Refuse generation when the base repository or worktree state is ambiguous.
- Define an allowlist of expected generated files and reject unrelated diffs.
- Validate that manifest commands are non-empty, unique, and syntactically safe.
- Validate exact handler contracts for skills and channel command adapters.
- Execute every advertised natural-language example through the real dispatcher.
- Require capability-specific tests for generated changes.
- Include actual changed-file and commit counts in generated PR descriptions.
- Keep generated and healed PRs in a human-review queue; do not auto-merge them.

### Acceptance Gate

- A deliberately malformed handler, empty command, or unrelated file change is rejected before a PR is created.
- A generator launched from a non-main worktree still produces a branch containing only its intended changes.
- CI exercises the same entry point used by a real user request.

## Phase 1: Enforce Typed Execution Contracts

**Objective:** Make policy, approval, execution, verification, and audit unavoidable for every action.

### Deliverables

- Introduce typed contracts:
  - `ActionRequest`
  - `ActionResult`
  - `ExecutionContext`
  - `ApprovalDecision`
  - `VerificationResult`
- Represent success, data, errors, side effects, retryability, and evidence explicitly.
- Route LLM tool calls, built-ins, generated capabilities, workflows, scheduled work, and proactive work through `ExecutionBus`.
- Remove direct handler invocation from runtime paths.
- Centralize autonomy classification, approval creation, rate limiting, depth limits, and audit recording.
- Return operational failures as failures rather than successful empty results.
- Add repository interfaces and migrations for execution state instead of opening SQLite connections throughout action modules.

### Acceptance Gate

- No write, send, delete, purchase, or application-control action can bypass the execution bus.
- Policy tests produce the same decision regardless of channel or selection method.
- Authentication failure, network failure, valid empty result, user rejection, and successful completion are observably distinct.

## Phase 2: Create One Channel-Neutral Runtime

**Objective:** Make Telegram, CLI, Slack, Discord, WhatsApp, and API requests share the same behavior.

### Deliverables

- Introduce `AgentRuntime.handle(MessageContext)` as the single request entry point.
- Reduce channel implementations to authentication, message normalization, and response rendering adapters.
- Consolidate heuristic intent classification, LLM detection, regex matching, shell shortcuts, and tool selection behind an explicit routing interface and precedence model.
- Separate reusable capability functions from Telegram-specific command wrappers.
- Move global runtime dependencies into an explicit application container that can be constructed in tests.
- Use one context-assembly and task-state path for all channels.

### Acceptance Gate

- A shared request fixture selects the same action and returns the same structured result across channels.
- Every capability implements one validated interface.
- Channel adapters contain no capability or policy logic.

## Phase 3: Consolidate Graph and Loop Execution

**Objective:** Use one durable execution model for user tasks, workflows, schedules, proactive actions, and recovery.

### Deliverables

- Persist an execution graph with node state, dependencies, inputs, outputs, evidence, and timestamps.
- Define node states such as `pending`, `ready`, `running`, `waiting_for_approval`, `succeeded`, `failed`, `compensated`, and `cancelled`.
- Add retry policies, timeouts, idempotency keys, and compensation behavior for side-effecting nodes.
- Add checkpoints so interrupted work resumes from the last verified node.
- Introduce a loop controller with explicit limits for iterations, wall time, tokens, and cost.
- Require each loop to declare a progress predicate and termination reason.
- Have schedulers, proactive agents, healing, and user requests submit graph runs instead of invoking separate execution paths.
- Record source, urgency, autonomy level, and user visibility as graph metadata.

### Acceptance Gate

- A process restart can resume a multi-step task without repeating completed side effects.
- Retrying a protected action cannot duplicate its externally visible result.
- Every loop ends with a recorded success, failure, timeout, budget exhaustion, cancellation, or approval wait.
- Graph state is sufficient to explain the final response without reconstructing events from logs.

## Phase 4: Measure Verified Outcomes

**Objective:** Make reliability and product value visible before resuming broad capability development.

### Deliverables

- Establish a full-suite CI baseline and classify existing failures as product defects, environment-dependent tests, or obsolete expectations.
- Run generated capability contract tests and advertised examples in CI.
- Derive metrics from typed execution and verification events.
- Build a replay suite from real failures and corrections.
- Define golden end-to-end cases for the ten priority workflows.
- Track failures by capability, channel, selection path, and execution phase.

### Primary Metrics

| Metric | Definition | Initial target |
|---|---|---|
| Verified completion rate | Tasks whose requested outcome is confirmed by a verifier | At least 90% for the top ten workflows |
| False-success rate | Responses claiming success without supporting evidence | Below 1% |
| Approval correctness | Protected actions receiving the expected approval decision | 100% in policy fixtures |
| Recovery success rate | Failed tasks completed through bounded retry or recovery | Establish baseline, then improve per workflow |
| Duplicate side-effect rate | Repeated externally visible effects caused by retry or resume | 0 for protected actions |
| P50/P95 task duration | End-to-end wall time by workflow | Set targets after trustworthy instrumentation |
| P50/P95 task cost | Model and external-service cost by workflow | Set targets after trustworthy instrumentation |

### Acceptance Gate

- CI cannot pass a capability whose advertised entry point fails.
- A weekly scorecard reports verified outcomes rather than only calls and handler returns.
- The top ten workflows have reproducible end-to-end fixtures and explicit verification rules.

## Phase 5: Resume Selective Capability Development

**Objective:** Expand only where outcome data demonstrates product value or a missing reusable primitive.

### Deliverables

- Rank workflows using frequency, user value, failure cost, and current reliability.
- Improve the ten selected workflows to the Phase 4 completion target.
- Treat repeated action co-occurrence as a workflow-composition proposal.
- Generate new Python capability code only when an underlying primitive is absent.
- Review the capability catalog periodically and remove or consolidate unused combination modules.

### Acceptance Gate

- Each new capability proposal names the unmet primitive, target workflow, verifier, and expected usage.
- Existing primitives cannot express the requested behavior through graph composition.
- Adding the capability does not reduce completion or safety metrics for priority workflows.

## Proposed Pull Request Sequence

Each item should be a separately reviewable PR with a behavioral proof and rollback path.

1. **Generated branch isolation** — explicit base SHA, clean worktree validation, and changed-file allowlist.
2. **Generated capability contract validation** — manifest, signatures, examples, and required tests.
3. **Typed action results** — introduce contracts and adapters without changing routing behavior.
4. **Mandatory execution bus** — migrate direct tool and built-in execution behind policy.
5. **Unified runtime shell** — establish `AgentRuntime` and migrate one channel at a time.
6. **Durable graph state** — persistence, node lifecycle, checkpointing, and idempotency.
7. **Bounded loop controller** — budgets, progress predicates, and termination reasons.
8. **Outcome telemetry** — execution events, verification metrics, and weekly scorecard.
9. **Full CI baseline** — classify existing failures and expand behavioral coverage.
10. **Top-ten workflow program** — select workflows and improve them sequentially.

Phases should not be implemented as one large refactor. Compatibility adapters should keep Khalil usable while execution paths move behind the new boundaries.

## Deferred Capability Concepts

The April 2026 generated PR review closed five PRs without merging them. Two concepts remain useful after the platform work:

### Browser and Readwise workflow from PR #295

Revisit as graph composition using:

- A browser-tab enumeration primitive.
- A Readwise search primitive with explicit authentication, network, and rate-limit failures.
- Bounded concurrency and query deduplication.
- A verifier that distinguishes zero matches from incomplete search coverage.

### Shared browser session from PR #299

Revisit as a browser primitive that opens a page once and derives title, text, metadata, and screenshot from the same page state. The browser session should be passed through typed execution context and closed deterministically.

The combinations in PRs #296 and #298 should remain declarative workflows unless usage evidence demonstrates a missing primitive. PR #297 should be replaced by input-normalization and replay-test work rather than retained as a capability.

## Non-Goals

- Splitting Khalil into microservices.
- Replacing SQLite solely for scale.
- Adding more channels before runtime convergence.
- Increasing autonomy levels before approval enforcement is universal.
- Auto-merging generated or healed code.
- Optimizing latency or model cost before outcome instrumentation is trustworthy.

## Next Decision

Begin with Phase 0 and approve PR 1 only after its tests demonstrate that Khalil cannot recreate the branch contamination and handler-contract failures identified in PRs #295 through #299.
