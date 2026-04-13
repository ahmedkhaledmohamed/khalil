# Khalil — Autonomous AI Personal Agent

A self-improving AI agent that runs on a laptop. Classifies intent, assembles context from a 46K-document knowledge base, executes with phase-aware control, and verifies results. When it fails, it fixes itself. When it can't do something, it builds the capability and opens a PR.

**Architecture**: Intent classification → context assembly → phase-aware execution → verification.
**Models**: Claude Opus (primary) → Sonnet → GPT-5.2 → Gemini 2.5 Pro → Ollama (local fallback).
**Scale**: 59 skills, 200+ actions, 910 tests, 24 behavioral contracts, 4-tier quality system.

## Quick Start

```bash
git clone git@github.com:ahmedkhaledmohamed/khalil.git
cd khalil
make install
```

The interactive installer walks you through 9 phases — all optional except the core (Telegram token + API key):

1. System deps (Homebrew, Python, Ollama)
2. Python environment (venv + 67 packages)
3. Ollama models (embeddings required, local LLM optional)
4. Core secrets (Telegram + Anthropic)
5. **Data sources** (Google Workspace, Slack, Spotify, Notion, Home Assistant — all optional)
6. Database (restore backup, import knowledge, or fresh start)
7. **Knowledge base indexing** (indexes your emails, docs, repos — optional, 10-30 min)
8. LaunchAgent (macOS daemon)
9. Start + health check

Safe to re-run — skips completed phases.

```bash
make status    # Check health
make logs      # Tail logs
make restart   # Restart daemon
make secrets   # Re-configure integrations
make index     # Re-index knowledge base
make test      # Run 910 tests
```

<details>
<summary>Manual setup (without installer)</summary>

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 -c "import keyring; keyring.set_password('khalil-assistant', 'telegram-bot-token', 'YOUR_TOKEN')"
python3 -c "import keyring; keyring.set_password('khalil-assistant', 'anthropic-api-key', 'YOUR_KEY')"
ollama serve & ollama pull nomic-embed-text
python3 server.py
```

</details>

Or use the CLI (no Telegram required):
```bash
python3 cli.py "What meetings do I have today?"
python3 cli.py   # interactive REPL mode
```

## Architecture

```
Channels (Telegram, Slack, Discord, WhatsApp)
       │
       ▼
  server.py (FastAPI + python-telegram-bot)
       │
       ├── intent.py             → Heuristic intent classifier (TASK/QUESTION/CHAT/CONTINUATION)
       ├── task_manager.py       → Persistent task state (active → blocked → completed → failed)
       ├── context.py            → Intent-aware context assembly (skip KB for continuations)
       ├── verification.py       → Centralized post-action verification + hallucination detection
       │
       ├── skills.py             → Self-describing skill registry (59 skills, auto-discovery)
       ├── autonomy.py           → Action classification & approval flow
       ├── orchestrator.py       → Multi-step task decomposition & execution
       ├── model_router.py       → Smart model selection (simple/medium/complex)
       │
       ├── knowledge/            → Vector search over 46K+ documents
       ├── actions/              → 86 action modules (200+ action types)
       ├── channels/             → 4 bidirectional messaging channels
       ├── scheduler/            → 20+ scheduled jobs (digests, sync, enrichment, health checks)
       ├── synthesis/            → Cross-domain awareness (capacity, risk detection)
       ├── state/                → Real-time state providers (calendar, email, device)
       │
       ├── learning.py           → Self-improvement (signals, insights, preferences)
       ├── healing.py            → Self-healing (detect failures → generate patches → PR)
       ├── workflows.py          → Reactive workflow engine (trigger → condition → action)
       ├── agents/coordinator.py → Agent swarm for parallel sub-tasks
       └── mcp_server.py         → MCP protocol server for Claude Code
```

### Agent Pipeline (April 2026)

Every message goes through a 5-stage pipeline:

```
Message → CLASSIFY INTENT → ASSEMBLE CONTEXT → EXECUTE (phase-aware) → VERIFY → COMPLETE
```

1. **Intent Classification** (`intent.py`): Heuristic classifier (no LLM needed) determines TASK, QUESTION, CHAT, or CONTINUATION. Continuations skip KB search to prevent context drift.

2. **Context Assembly** (`context.py`): Different context per intent type. TASK gets deep retrieval (KB search + full documents). CONTINUATION gets task state + 5 recent messages. CHAT gets conversation history only.

3. **Phase-Aware Execution** (`_PhaseTracker` in `server.py`): For artifact tasks, enforces an escalation ladder:
   - Iterations 0-3: free research
   - Iteration 4: nudge ("call generate_file NOW")
   - Iteration 5: restrict (remove search tools)
   - Iteration 6+: force (tool_choice=generate_file)
   - Exhaustion: programmatic fallback (construct generate_file call directly)

4. **Verification** (`verification.py`): Hallucination detection, file creation verification, task completion tracking.

5. **Task State** (`task_manager.py`): Persistent DB-backed task lifecycle (active → blocked → completed → failed). Tasks survive across messages, auto-reset after 3 failures.

### Execution Resilience

- **Isolated circuit breakers**: foreground (user requests, threshold=5) and background (summarization, threshold=2) are independent — background failures can't kill user requests
- **Summarization gate**: background LLM calls suppressed during active tool-use loop
- **Model cascade**: Claude Opus → Sonnet → GPT-5.2 → Gemini → Ollama, with per-model timeouts
- **Separate connection pool** for generate_file (5-minute calls don't starve 15-second tool calls)

### Key Design Decisions

- **Local-first**: Embeddings and LLM run on Ollama (free, private). Claude API is optional for complex tasks.
- **Autonomy model**: Three levels control what the assistant can do without asking. Hard guardrails are immutable.
- **Self-correcting**: Shell errors are classified (transient/correctable/permanent) and retried with LLM-generated corrections in real time.
- **Self-healing**: When existing functionality fails repeatedly, reads its own source, generates a patch via Claude Opus, validates it, opens a PR, and verifies the fix worked.
- **Self-extending**: When it can't do something you ask, detects the gap, generates a new action module, smoke-tests it in a container, and opens a PR.
- **Multi-channel**: Telegram, Slack, Discord, WhatsApp — same capabilities across all. Common `Channel` protocol.
- **Skill-based dispatch**: Each action module declares its own patterns, keywords, and examples. Registry auto-discovers at startup.

## Channels

| Channel | Transport | Setup |
|---------|-----------|-------|
| Telegram | Long polling | Bot token via BotFather |
| Slack | Socket Mode | Slack app with Socket Mode enabled |
| Discord | Gateway | Discord bot token |
| WhatsApp | Meta Cloud API | Business account + webhook |

All channels implement the same `Channel` protocol — send messages, receive messages, inline keyboards.

## Telegram Commands

### Core
| Command | Description |
|---------|-------------|
| `/start` | Initialize and authorize |
| `/search <query>` | Search knowledge base |
| `/brief` | Generate morning brief |
| `/clear` | Clear conversation history |
| `/mode` | View/change autonomy level |

### Integrations
| Command | Description |
|---------|-------------|
| `/email <query>` | Search, draft, or send emails |
| `/drive <query>` | Search Google Drive |
| `/calendar` | Today's events |
| `/remind <text> <time>` | Set a reminder |
| `/run <command>` | Execute a shell command (safety-classified) |
| `/sync` | Sync new emails to knowledge base |
| `/enrich [topic]` | Manually trigger knowledge enrichment |
| `/jobs` | Check job scraper for new matches |

### Dashboards
| Command | Description |
|---------|-------------|
| `/finance` | Portfolio summary and deadline alerts |
| `/work` | Sprint dashboard and active epics |
| `/goals` | Quarterly goals with progress tracking |
| `/project <name>` | Project status overview |

### System
| Command | Description |
|---------|-------------|
| `/approve` / `/deny` | Approve or deny pending actions |
| `/audit` | View action audit log |
| `/learn` | Self-improvement insights and preferences |
| `/health` | System health check |
| `/stats` | Knowledge base statistics |
| `/backup` | Export/import state |
| `/nudge` | Proactive check — what needs attention? |
| `/extensions` | List auto-generated capabilities |
| `/workflows` | View active reactive workflows |
| `/tasks` | Active task tracking |

### Natural Language

Understands natural language for common actions:
- "Open Slack" → executes `open -a 'Slack'`
- "Remind me to review the PR tomorrow at 9am" → creates reminder
- "Send an email to John about the meeting" → drafts email with approval
- "Check disk space" → executes `df -h`
- "How many windows are open?" → runs osascript, responds naturally
- "What's my battery?" → checks battery level
- "Search YouTube for Python tutorials" → YouTube Data API search
- "What's the weather?" → Open-Meteo API lookup

Machine-state queries (window counts, battery, IP, uptime, running processes) are pattern-matched to pre-built shell commands — no LLM needed for command generation. Shell output is then interpreted by the LLM into a natural language answer.

## Action Modules (37)

### Google Workspace
| Module | Capabilities |
|--------|-------------|
| `gmail.py` | Search, read, draft, send emails |
| `gmail_sync.py` | Incremental email sync to knowledge base |
| `email_categorizer.py` | Auto-categorize incoming emails |
| `drive.py` | Search and read Google Drive files |
| `calendar.py` | Events, meeting intelligence, follow-ups |
| `contacts.py` | Google Contacts lookup |
| `tasks_google.py` | Google Tasks integration |

### Productivity
| Module | Capabilities |
|--------|-------------|
| `reminders.py` | Local reminders with recurrence rules |
| `projects.py` | Project status tracking |
| `goals.py` | Goal tracking with quarterly reviews |
| `work.py` | Sprint dashboard and epic tracking |
| `finance.py` | Financial dashboard and deadline alerts |
| `meetings.py` | Meeting intelligence and commitment tracking |

### External Services
| Module | Capabilities |
|--------|-------------|
| `spotify.py` | Currently playing, recent tracks, top items |
| `youtube.py` | Video search, liked videos, subscriptions |
| `github_api.py` | Notifications, PRs, issues |
| `readwise.py` | Highlights and book annotations |
| `notion.py` | Search pages, create pages, query databases |
| `appstore.py` | App Store Connect ratings, reviews, downloads |
| `digitalocean.py` | Droplet status, health metrics, billing |
| `linkedin.py` | Messages, job search, profile views |
| `weather.py` | Current conditions and forecast (Open-Meteo, no API key) |
| `web.py` | Web search (DuckDuckGo) and page fetching |

### macOS & System
| Module | Capabilities |
|--------|-------------|
| `shell.py` | Shell execution with safety classification and retry |
| `terminal.py` | iTerm2 and Cursor IDE control via osascript |
| `browser.py` | Playwright-based web automation (screenshot, extract) |
| `imessage.py` | Read/search iMessage conversations |
| `apple_reminders.py` | Sync with Apple Reminders |
| `voice.py` | Speech-to-text and text-to-speech |
| `slack_reader.py` | Read Slack messages and threads |

### Meta
| Module | Capabilities |
|--------|-------------|
| `extend.py` | Self-extension (gap detection → code gen → PR) |
| `guardian.py` | Secondary LLM review of risky actions |
| `backup.py` | State export/import |
| `jobs.py` | Job scraper bridge |
| `claude_code.py` | Claude Code CLI integration |

## Autonomy Model

Three levels control what the assistant can auto-execute vs. what needs approval:

| | Supervised | Guided | Autonomous |
|---|---|---|---|
| **READ** (search, summarize) | Auto | Auto | Auto |
| **Safe WRITE** (reminder, draft) | Ask | Auto | Auto |
| **Risky WRITE** (send email, install) | Ask | Ask | Auto |
| **DANGEROUS** (delete, money, share) | Ask | Ask | Ask |

**Hard guardrails** — always require approval regardless of level:
`send_money`, `delete_data`, `share_externally`, `modify_financial_account`, `shell_dangerous`, `generate_capability`

Change level: `/mode` → select level via inline keyboard.

## Scheduled Jobs

| Job | Schedule | Description |
|-----|----------|-------------|
| Morning Brief | 7:00 AM daily | Calendar, emails, weather, goals summary |
| Financial Alert | 9:00 AM on 1st & 15th | Investment alerts and deadlines |
| Career Alert | 10:00 AM daily | Job scraper new matches |
| Proactive Alerts | 12:00 PM Wednesday | Overdue items, attention needed |
| Knowledge Enrichment | 2:00 PM Wed & Sat | Auto-detect knowledge gaps, web search, index |
| Weekly Summary | 6:00 PM Sunday | Week recap with achievements |
| Friday Reflection | 5:00 PM Friday | End-of-week reflection |
| Email Sync | Every 6 hours | Pull and index new emails |
| OAuth Refresh | Every 6 hours | Proactive token refresh |
| Daily Self-Check | 8:00 PM daily | System health + self-healing triggers |
| Micro-Reflection | 11:00 PM daily | Daily signals analysis |
| Weekly Reflection | Sunday | Self-improvement insights |
| Reminder Check | Every 60 seconds | Fire due reminders |
| Meeting Brief | Every 5 minutes | Pre-meeting context prep |
| Meeting Follow-up | Every 5 minutes | Post-meeting action item prompts |
| State Alerts | Every 30 minutes | Calendar/email awareness checks |
| Dev State Poll | Every 60 seconds | IDE and terminal awareness |
| Preference Decay | Sunday 8:30 PM | Decay old learned preferences |
| Quarterly Planning | Quarterly | Goal setting and alignment |

## Self-Correction (Real-Time)

When a shell command fails, classifies the error and responds:

| Error Type | Examples | Behavior |
|------------|----------|----------|
| **Transient** | timeout, resource busy, connection refused | Retry same command after 2s |
| **Correctable** | syntax error, wrong flags, bad AppleScript | Feed error to LLM for corrected command, re-execute |
| **Permanent** | permission denied, command not found | Show error with user-friendly hint |

Safety constraint: corrected commands are re-classified and **cannot escalate** (a READ command can't be corrected into a WRITE command).

## Self-Healing (Async)

When existing functionality fails repeatedly:

1. **Record** — failure signals logged at every failure point: shell, actions, API errors, LLM timeouts, intent failures, user corrections
2. **Detect** — 3+ failures with the same fingerprint in 48h triggers healing. Critical errors (`ImportError`, `SyntaxError`) trigger after 1 occurrence
3. **Diagnose** — extracts the failing function's source code via AST
4. **Patch** — Claude Opus generates a fixed version
5. **Validate** — AST parse + blocklist check + full-file compilation
6. **Verify** — monitors for recurrence after merge. Re-triggers with enriched context if fix failed
7. **PR** — creates branch, commits patch, opens PR, notifies via Telegram

Never auto-applies — always goes through PR review.

## Self-Extension

When the assistant can't handle a request:

1. **Detect** — semantic regex gate matches refusal patterns. Structured `[CAPABILITY_GAP: ...]` tags as fast path
2. **Classify** — LLM confirms it's a real capability gap (not just a knowledge gap)
3. **Generate** — Claude Opus writes a new action module following existing patterns
4. **Validate** — AST syntax check + blocklist (no subprocess, no eval, no socket)
5. **Smoke test** — imports the module in an isolated container, verifies handler exists
6. **PR** — creates branch, commits module + manifest, opens PR
7. **Notify** — Telegram message with Generate/Skip buttons, then PR link

Extensions auto-load on restart from `extensions/*.json` manifests.

## Knowledge Base

SQLite with `sqlite-vec` for vector similarity search:

- **Documents**: Gmail archives, Google Drive exports, Cursor transcripts, personal docs
- **Embeddings**: 768-dim vectors via Ollama `nomic-embed-text` (local, free)
- **Search**: Hybrid — vector similarity (40%) + keyword matching (35%) + freshness decay (25%)
- **Enrichment**: Autonomous gap detection — when queries produce poor results, web-searches for answers and indexes them

```bash
# Full re-index
python3 knowledge/indexer.py

# Incremental (only modified files)
python3 knowledge/indexer.py --incremental
```

## Reactive Workflows

Trigger → Condition → Action chains that run automatically:

```
Trigger types: cron, signal, webhook, threshold
Conditions: field comparisons, all/any combinators
Actions: send_message, run_action, call_webhook
```

Workflows persist in SQLite with run tracking. Rate-limited per workflow. The Workflow Evolver detects patterns in interaction signals and proposes new workflows.

## Multi-Step Orchestration

Compound requests are decomposed into parallel/sequential sub-tasks:

- Heuristic detection of multi-step patterns ("and", "then", "also")
- LLM decomposition into dependency graph
- Parallel execution of independent steps
- Agent swarm coordinator for complex queries (configurable concurrency)

## Quality System

4-tier quality framework (910 tests, 24 behavioral contracts):

```bash
pytest tests/ -v                         # 910 tests across 15 files
python3 eval/tool_use_eval.py            # 55 deterministic tool routing tests
python3 eval/reasoning_eval.py           # 31 strategy/anti-pattern tests
```

### CI Gate (eval.yml)

Every PR is gated on:
- Syntax check (all Python files)
- Critical module imports (intent, verification, task_manager, context, tool_catalog)
- 77 unit + pipeline + behavioral contract tests
- Tool-use eval (55 cases) + reasoning eval (31 cases)
- Golden YAML validation (521 frozen cases, 58 categories)

### Behavioral Contracts

24 formalized invariants in `tests/test_behavioral_contracts.py`:

| Contract | What it enforces |
|----------|-----------------|
| Artifact Research Cap | Nudge at 4 iterations, restrict at 5, force at 6 |
| Intent Classification | build→TASK, greeting→CHAT, ?→QUESTION |
| Circuit Breaker Isolation | Background failures don't trip foreground breaker |
| Verification Layer | Hallucinated tool calls always detected |
| Tool Catalog | generate_file and search_knowledge always in core tools |

### Post-Restart Smoke Test

Every restart verifies the pipeline is wired correctly (no LLM calls):
- Intent classifier returns correct type
- PhaseTracker escalation logic works
- Core tools present in catalog
- Hallucination detector functional
- Circuit breakers properly isolated

### Eval Pipeline

```bash
python3 eval/run.py              # full pipeline (2,938 frozen cases)
python3 eval/metrics.py          # production metrics from live DB
python3 eval/metrics.py --json   # compare against baseline thresholds
```

## Configuration

### Secrets (macOS Keyring)

```bash
# Required
keyring.set_password('khalil-assistant', 'telegram-bot-token', '...')

# Optional
keyring.set_password('khalil-assistant', 'anthropic-api-key', '...')
keyring.set_password('khalil-assistant', 'readwise-api-key', '...')
keyring.set_password('khalil-assistant', 'notion-api-key', '...')
keyring.set_password('khalil-assistant', 'github-token', '...')
keyring.set_password('khalil-assistant', 'digitalocean-api-key', '...')
```

Falls back to environment variables: `TELEGRAM_BOT_TOKEN`, `ANTHROPIC_API_KEY`.

### Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `KHALIL_PERSONAL_REPO` | `~/Developer/Personal` | Path to personal document repo |
| `TELEGRAM_BOT_TOKEN` | — | Fallback for keyring |
| `ANTHROPIC_API_KEY` | — | Fallback for keyring |

### LLM Backend

In `config.py`:
```python
LLM_BACKEND = "ollama"           # free, local — or "claude" for cloud
OLLAMA_LLM_MODEL = "qwen3:14b"
CLAUDE_MODEL = "claude-sonnet-4-20250514"
CLAUDE_MODEL_COMPLEX = "claude-opus-4-20250514"  # used for code generation
```

### Google OAuth

Token files managed centrally via `oauth_utils.py` with atomic writes and corruption resilience:
- `credentials.json` — OAuth app config (download from Google Cloud Console)
- `token.json` — gmail.readonly
- `token_compose.json` — gmail.compose
- `token_calendar.json` — calendar.readonly
- `token_modify.json` — gmail.modify
- `token_contacts.json` — contacts.readonly
- `token_tasks.json` — tasks.readonly
- `token_drive_write.json` — drive.file
- `token_youtube.json` — youtube.readonly
- `token_work.json` — work account gmail

## External Dependencies

| Service | Purpose | Required? |
|---------|---------|-----------|
| Ollama | Local embeddings + LLM | Yes (unless using Claude for everything) |
| Telegram Bot API | Primary chat interface | Yes |
| Google OAuth | Gmail, Drive, Calendar, Contacts, Tasks, YouTube | Yes |
| Anthropic API | Claude LLM (cloud) | No (Ollama works offline) |
| GitHub CLI (`gh`) | Self-healing/extension PRs | For self-heal/extend only |
| Playwright | Browser automation | Optional |
| ffmpeg | Voice transcription | Optional |

## Directory Structure

```
khalil/
├── server.py                 # FastAPI + Telegram bot (main entry point)
├── cli.py                    # Terminal REPL (no Telegram required)
├── config.py                 # Centralized configuration
├── skills.py                 # Self-describing skill registry
├── autonomy.py               # Action classification & approval
├── healing.py                # Self-healing engine
├── learning.py               # Self-improvement & preferences
├── monitoring.py             # System health checks
├── model_router.py           # Smart model selection
├── orchestrator.py           # Multi-step task decomposition
├── workflows.py              # Reactive workflow engine
├── oauth_utils.py            # Centralized OAuth token management
├── mcp_server.py             # MCP server for Claude Code
├── mcp_client.py             # MCP client for external tools
│
├── actions/                  # 37 action modules
│   ├── gmail.py              # Email search/draft/send
│   ├── gmail_sync.py         # Email sync worker
│   ├── drive.py              # Google Drive search
│   ├── calendar.py           # Calendar integration
│   ├── shell.py              # Shell execution (classification, retry, hints)
│   ├── extend.py             # Self-extension engine
│   ├── guardian.py            # Secondary LLM review for risky actions
│   ├── browser.py            # Playwright web automation
│   ├── voice.py              # Speech-to-text / text-to-speech
│   ├── web.py                # Web search (DuckDuckGo) + page fetch
│   ├── spotify.py            # Spotify Web API
│   ├── youtube.py            # YouTube Data API v3
│   ├── github_api.py         # GitHub API
│   ├── readwise.py           # Readwise highlights
│   ├── notion.py             # Notion API
│   ├── weather.py            # Open-Meteo weather
│   ├── imessage.py           # macOS iMessage
│   ├── terminal.py           # iTerm2 + Cursor IDE control
│   └── ...                   # reminders, finance, goals, projects, work, etc.
│
├── channels/                 # Messaging channels
│   ├── telegram.py           # Telegram (primary)
│   ├── slack.py              # Slack (Socket Mode)
│   ├── discord.py            # Discord
│   ├── whatsapp.py           # WhatsApp (Meta Cloud API)
│   ├── registry.py           # Unified channel dispatch
│   └── message_context.py    # Platform-agnostic message context
│
├── knowledge/                # Knowledge base
│   ├── indexer.py            # SQLite + sqlite-vec init & ingestion
│   ├── search.py             # Hybrid vector + keyword search
│   ├── embedder.py           # Ollama embedding client
│   └── context.py            # Personal context extraction
│
├── scheduler/                # Scheduled tasks
│   ├── tasks.py              # Job definitions
│   ├── digests.py            # Brief/alert/summary generation
│   ├── enrichment.py         # Autonomous knowledge enrichment
│   ├── planning.py           # Quarterly planning automation
│   ├── proactive.py          # Proactive attention checks
│   └── state_alerts.py       # Calendar/email state alerts
│
├── synthesis/                # Cross-domain awareness
│   ├── aggregator.py         # Multi-domain status snapshot
│   └── capacity.py           # Overcommitment detection
│
├── state/                    # Real-time state providers
│   ├── calendar_provider.py  # Calendar event awareness
│   ├── email_provider.py     # Unread count, needs-reply detection
│   └── collector.py          # Signal aggregation
│
├── agents/                   # Agent swarm
│   └── coordinator.py        # Parallel sub-agent execution
│
├── webhooks/                 # Inbound event triggers
│   ├── github.py             # GitHub webhook handlers
│   └── registry.py           # Webhook routing
│
├── eval/                     # Evaluation pipeline
│   ├── case_gen.py           # Test case generation (~10K cases)
│   ├── runner.py             # Instrumented test runner
│   ├── judge.py              # Response evaluation
│   ├── gap_analysis.py       # Failure categorization + regression tracking
│   ├── plan_gen.py           # Repair plan generation
│   ├── autofix.py            # Auto-fix cycle
│   └── trace.py              # Structured execution traces
│
├── tests/                    # Unit + integration tests
│   └── test_*.py             # 12 test files
│
├── extensions/               # Auto-generated capabilities
│   └── *.json                # Extension manifests
│
└── data/                     # Runtime (gitignored)
    ├── khalil.db             # SQLite database
    └── *.log                 # Logs
```

## MCP Server

Exposes the knowledge base to Claude Code via the MCP protocol:

- `search_knowledge` — hybrid search across all indexed content
- `get_context` — retrieve relevant personal context sections
- `get_timeline` — query life timeline events
- `get_stats` — knowledge base statistics
- Read-only tools for work, finance, goals, and projects

## Database Schema

SQLite tables (initialized in `knowledge/indexer.py`):

| Table | Purpose |
|-------|---------|
| `documents` | Indexed content (emails, Drive, docs) with metadata |
| `document_embeddings` | Vector embeddings (768-dim, sqlite-vec) |
| `pending_actions` | Action queue for approval flow |
| `settings` | App settings (autonomy level, owner chat ID) |
| `reminders` | Local reminders with recurrence rules |
| `audit_log` | Action audit trail with timestamps |
| `conversations` | Multi-turn context memory |
| `interaction_signals` | Failure/correction signals for self-healing |
| `insights` | Self-improvement insights (pending/applied/dismissed) |
| `learned_preferences` | Behavioral preferences with confidence scores |
| `workflows` | Reactive workflow definitions |
| `workflow_runs` | Workflow execution history |
| `meeting_briefs` | Pre-meeting context prep |
| `meeting_commitments` | Tracked action items from meetings |
