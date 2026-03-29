# PharoClaw — Self-Healing Personal AI Assistant

A personal AI assistant that runs as a Telegram bot on macOS. Indexes your emails, Drive files, and documents into a local knowledge base, then answers questions, takes actions, and learns from interactions. When it fails, it fixes itself. When it can't do something, it builds the capability.

Built with FastAPI, SQLite (with vector embeddings via sqlite-vec), and Ollama/Claude for reasoning.

## Quick Start

```bash
# 1. Set up
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure secrets
python3 -c "import keyring; keyring.set_password('pharoclaw', 'telegram-bot-token', 'YOUR_TOKEN')"
python3 -c "import keyring; keyring.set_password('pharoclaw', 'anthropic-api-key', 'YOUR_KEY')"

# 3. Start Ollama (for embeddings + local LLM)
ollama serve &
ollama pull nomic-embed-text
ollama pull qwen3:14b

# 4. Run
python3 server.py
```

Or run as a macOS daemon:
```bash
cp com.pharoclaw.daemon.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.pharoclaw.daemon.plist
```

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
       ├── skills.py             → Self-describing skill registry (auto-discovery)
       ├── autonomy.py           → Action classification & approval flow
       ├── orchestrator.py       → Multi-step task decomposition & execution
       ├── model_router.py       → Smart model selection (simple/medium/complex)
       │
       ├── knowledge/            → Vector search over emails, Drive, docs
       ├── actions/              → 37 action modules (Gmail, Shell, Calendar, Web, ...)
       ├── channels/             → 4 bidirectional messaging channels
       ├── scheduler/            → 15+ scheduled jobs (digests, sync, enrichment)
       ├── synthesis/            → Cross-domain awareness (capacity, risk detection)
       ├── state/                → Real-time state providers (calendar, email)
       │
       ├── learning.py           → Self-improvement (signals, insights, preferences)
       ├── healing.py            → Self-healing (detect failures → generate patches → PR)
       ├── workflows.py          → Reactive workflow engine (trigger → condition → action)
       ├── agents/coordinator.py → Agent swarm for parallel sub-tasks
       └── mcp_server.py         → MCP protocol server for Claude Code
```

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

## Eval Suite

12 test files covering safety-critical paths:

```bash
pytest tests/ -v
```

| Test File | Coverage |
|-----------|----------|
| `test_shell.py` | Shell command classification (safe/risky/blocked) |
| `test_autonomy.py` | Autonomy levels, approval flow, hard guardrails |
| `test_validation.py` | Code validation blocklists (imports, calls, structure) |
| `test_gap_detection.py` | Capability gap detection (phrases, tags, edge cases) |
| `test_intent.py` | Intent detection patterns (window count, battery, system queries) |
| `test_complexity.py` | Simple vs. complex query classification |
| `test_retry.py` | Error classification, escalation safety |
| `test_signal_coverage.py` | Extension failure triggers, critical error threshold |
| `test_healing.py` | Heal verification loop (recurrence detection) |
| `test_extension_quality.py` | Semantic gate patterns, smoke test validation |
| `test_improvements.py` | Integration tests across features |
| `test_terminal.py` | Terminal and IDE control |

### Eval Pipeline

Separate evaluation framework for intent detection and action quality at scale:

```bash
python3 eval/run.py              # full pipeline
python3 eval/case_gen.py         # generate test cases (~10K)
python3 eval/gap_analysis.py     # analyze failures
python3 eval/autofix.py          # auto-fix cycle
```

## Configuration

### Secrets (macOS Keyring)

```bash
# Required
keyring.set_password('pharoclaw', 'telegram-bot-token', '...')

# Optional
keyring.set_password('pharoclaw', 'anthropic-api-key', '...')
keyring.set_password('pharoclaw', 'readwise-api-key', '...')
keyring.set_password('pharoclaw', 'notion-api-key', '...')
keyring.set_password('pharoclaw', 'github-token', '...')
keyring.set_password('pharoclaw', 'digitalocean-api-key', '...')
```

Falls back to environment variables: `TELEGRAM_BOT_TOKEN`, `ANTHROPIC_API_KEY`.

### Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `PHAROCLAW_REPO` | `~/Developer/Personal` | Path to personal document repo |
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
pharoclaw/
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
    ├── pharoclaw.db             # SQLite database
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
