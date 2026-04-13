# I Built an AI Agent That Improves Itself. Here's What I Learned in 29 Days.

*668 commits. 273 PRs. 200+ actions. One laptop.*

---

## The idea

I wanted a personal AI assistant — not a chatbot, not a wrapper around an API, but something that actually *does things*. Reads my email. Checks my calendar. Controls my IDE. Generates files. And when it can't do something, it figures out how to build the capability itself.

I called it Khalil.

## Day 1: March 15, 2026

First commit at 7:14 PM. By 8:40 PM — 86 minutes later — Khalil opened its first self-generated PR. It detected that it couldn't read Slack, wrote a Slack integration module, tested it, and submitted the code for review. I merged it.

By midnight, 8 PRs were merged. Five of them were written by Khalil.

That was the moment I knew this wasn't going to be a weekend project.

## The architecture (how it started)

The initial design was simple: a Telegram bot backed by a FastAPI server. You send a message, the LLM picks a tool, the tool runs, you get a response.

```
Message → LLM → Pick tool → Execute → Response
```

It worked for simple things. "What's the weather?" "Set a reminder for 3pm." "How many unread emails do I have?"

But the moment I asked for anything multi-step — "Build me a presentation about my work planning" — the whole thing fell apart.

## The 36-hour failure

I asked Khalil to build an HTML presentation about my team's Fall 2026 planning work. It had all the information in its knowledge base (46,000 indexed documents from my emails, Drive, repos, and notes).

What happened: Khalil searched the knowledge base. Found relevant documents. Then searched again. And again. And again. For *36 hours*. Zero files created.

The LLM kept researching because research is safe. Generating a file is risky — what if it's bad? So it defaulted to what felt productive: gathering more context. Forever.

This was the turning point. I realized the problem wasn't the LLM, the tools, or the knowledge base. **The problem was the architecture.** There was no structure telling the system *when to stop thinking and start acting*.

## The redesign: thinking in phases

I threw out the flat tool-routing loop and redesigned around phases.

### Phase 1: What are you asking?

Before doing anything, classify the intent. "Yes" is not the same as "Build me a presentation." A heuristic classifier (no LLM needed — pure regex and pattern matching) categorizes every message:

- **TASK**: "Build me a presentation" → full pipeline
- **QUESTION**: "What's the weather?" → search + answer
- **CONTINUATION**: "Yes", "Sounds good" → inherit active task
- **CHAT**: "Hello", "Thanks" → conversational only

This alone fixed a bizarre bug where saying "Yes" to continue a task would trigger a knowledge base search on the word "Yes" and return random documents about rate limiting algorithms. The user never discussed rate limiting. The KB just had a document that happened to score well against a single-word query.

### Phase 2: What context do you need?

Different intents need different context. A continuation doesn't need a fresh KB search — it needs the task state from the previous message. A new task needs deep retrieval. Chat needs nothing but conversation history.

```
CONTINUATION → task state + 5 recent messages (no KB search)
QUESTION     → KB search + full conversation history
TASK         → KB search + full documents + live state
CHAT         → conversation history only
```

### Phase 3: Execute with guardrails

This is where the real innovation happened. I added a `PhaseTracker` that monitors tool usage during execution and enforces escalating discipline:

```
Iterations 0-3: Free research. Use whatever tools you want.
Iteration 4:    Nudge. "You have enough context. Call generate_file NOW."
Iteration 5:    Restrict. Search tools physically removed from the tool list.
Iteration 6:    Force. tool_choice locked to generate_file.
Exhaustion:     Programmatic fallback. We build the generate_file call ourselves.
```

The escalation ladder means the LLM gets progressively less freedom to procrastinate. At level 3, it literally cannot search anymore — the only tool available is the one that creates the file. And if it *still* refuses (returns text instead of a tool call), the system bypasses the LLM entirely and constructs the API call from the gathered context.

### Phase 4: Verify and complete

After every action, check that it actually worked. If `generate_file` reports success, verify the file exists on disk and has content. Track task state persistently — tasks survive across messages, auto-reset after 3 failures.

## The infrastructure problem nobody talks about

Even with the right architecture, Khalil kept failing. The phase transition would fire correctly at iteration 4, but the API call to get the LLM's response would time out. Every time.

After deep investigation, I found the real killer: **background tasks were sabotaging user requests.**

Every time Khalil saved a message to the database, it triggered a background summarization job. That job made its own LLM API call. Between tool-use iterations, 2-3 background summarizers would fire, each making API calls through the same client. When they timed out, they tripped a circuit breaker — which then blocked the user's tool-use loop.

The user's request would fail not because anything was wrong with their request, but because a background housekeeping task used up the API's patience.

The fix: **failure domain isolation.**

- Separate circuit breakers for foreground (user requests) and background (summarization)
- Suppress background LLM calls entirely during active tool-use loops
- Separate connection pool for long-running file generation
- Automatic retry for transient errors with escalating timeouts

This is infrastructure work that no demo or tutorial ever shows you. But without it, nothing works reliably.

## The moment it worked

After the architecture redesign, the execution hardening, and the phase-aware execution — I asked again:

> "Build me an HTML presentation about Fall 2026 messaging platform planning."

Khalil called `generate_file` on **iteration 0**. No research loop. No preamble. Straight to action.

Two minutes later: a 704-line, 25,764-character HTML presentation. Opus model, first attempt.

It had taken 5 failed attempts over 2 days to get here. But the system didn't just work once — it worked *structurally*. The architecture prevents the failure modes, not just patches them.

## Building the safety net

Working software is great. Software that *stays* working is better.

I built a 4-tier quality system:

**Tier 1: CI gates.** Every PR runs 77 tests including 24 behavioral contracts — formalized invariants like "the research cap must be 4 iterations" and "background circuit breaker failures must not trip the foreground breaker." If someone changes a threshold, CI catches it.

**Tier 2: Post-restart smoke test.** Every time Khalil restarts, it verifies the pipeline is wired correctly. Intent classifier works? PhaseTracker loads? Core tools present? Hallucination detector functional? All checked in under 1 second, no LLM calls.

**Tier 3: Metric baselines.** Frozen thresholds for tool success rate (>80%), latency P95 (<60s), and error cascade rate (<10%). Daily comparison against baseline.

**Tier 4: Behavioral contracts.** 24 tests that encode invariants, not implementation details. "Artifact tasks MUST hit the research cap at 4 consecutive searches." You can refactor the PhaseTracker however you want — the contract still holds.

## What Khalil can do today

**59 skills, 200+ actions** across:
- Google Workspace (Gmail, Calendar, Drive, Tasks, Contacts)
- Apple ecosystem (Health, Music, Notes, Reminders, HomeKit)
- Developer tools (GitHub, terminal, Cursor IDE, Claude Code)
- Communication (Telegram, Slack, Discord, WhatsApp)
- Productivity (Pomodoro, expense tracking, fasting tracker, fitness sync)
- Knowledge (Anki, Obsidian, Bear Notes, Readwise, web search)
- Home (HomeKit, 1Password, macOS system control)

**Self-improvement loop:**
- Detects capability gaps from conversation signals
- Generates new action modules autonomously
- Self-healing patches for recurring failures
- Opens PRs against its own codebase
- Evolution engine runs 4x daily

**Model cascade:**
Claude Opus (primary) → Sonnet (fast) → GPT-5.2 (backup) → Gemini 2.5 Pro → Ollama qwen3:14b (local fallback)

All running on a MacBook, managed by launchd, with a 150MB SQLite database holding 46,000 documents.

## The numbers

| Metric | Value |
|--------|-------|
| Development period | 29 days (Mar 15 - Apr 13, 2026) |
| Total commits | 668 |
| PRs merged | 273 |
| Action modules | 86 files, 200+ action types |
| Skills registered | 59 |
| Tests | 910 |
| Behavioral contracts | 24 |
| Knowledge base | 46,000 documents |
| Eval cases (frozen) | 2,938 |
| Self-generated capabilities | 30+ |
| Highest velocity day | 63 commits (March 16) |

## What I actually learned

**1. Architecture > patches.** I spent 3 days adding patches to a flat tool-routing loop. Each patch introduced new edge cases. The architectural redesign (intent classification + phase-aware execution) solved 7 failure modes at once.

**2. Background tasks are invisible killers.** The hardest bug to find was background summarization tripping a circuit breaker that killed user requests. The fix (failure domain isolation) is infrastructure work that's unglamorous but essential.

**3. LLMs need structure, not freedom.** Given unlimited tool iterations, the LLM will research forever. The escalation ladder (nudge → restrict → force → programmatic) is the key insight: progressively remove the LLM's ability to procrastinate.

**4. Behavioral contracts > unit tests.** Unit tests verify implementation. Contracts verify invariants. When you refactor, unit tests break (by design). Contracts survive — they test *what must be true*, not *how it's implemented*.

**5. Self-improvement is real, but fragile.** Khalil generated 30+ capabilities autonomously. But the evolution engine needs guardrails — CI gates, eval suites, sandbox execution. Without them, self-improvement becomes self-destruction.

**6. The demo is the easy part.** Getting "Build me a presentation" to work once is a weekend project. Getting it to work reliably — across API outages, timeout cascades, LLM behavioral quirks, background task interference — took 2 weeks of architectural work.

## What's next

Khalil runs 24/7 on my laptop as a launchd daemon. It handles my daily briefings, email triage, meeting prep, and file generation. The evolution engine proposes improvements every 6 hours.

The codebase is open. The architecture is documented. The landing page is live.

If you're building an AI agent and it works in demos but fails in production — the answer is probably not a better prompt or a bigger model. It's probably architecture.

---

*Khalil is open source at [github.com/ahmedkhaledmohamed/khalil](https://github.com/ahmedkhaledmohamed/khalil). Built by [Ahmed Khaled Mohamed](https://ahmedkhaledmohamed.github.io/me/).*
