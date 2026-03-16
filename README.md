# Khalil — Personal AI Assistant

A self-healing, self-extending personal AI assistant that runs as a Telegram bot on macOS. Built with FastAPI, SQLite (with vector embeddings), and Ollama/Claude for reasoning.

Khalil indexes your emails, Drive files, and personal documents into a local knowledge base, then answers questions, takes actions, and learns from interactions — all through Telegram.

## Quick Start

```bash
# 1. Set up
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure secrets
python3 -c "import keyring; keyring.set_password('khalil-assistant', 'telegram-bot-token', 'YOUR_TOKEN')"
python3 -c "import keyring; keyring.set_password('khalil-assistant', 'anthropic-api-key', 'YOUR_KEY')"

# 3. Start Ollama (for embeddings + local LLM)
ollama serve &
ollama pull nomic-embed-text
ollama pull qwen2.5:14b

# 4. Run
python3 server.py
```

Or run as a macOS daemon:
```bash
cp com.khalil.daemon.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.khalil.daemon.plist
```

## Architecture

```
Telegram ←→ server.py (FastAPI + python-telegram-bot)
                │
                ├── autonomy.py        → Action classification & approval flow
                ├── knowledge/         → Vector search over emails, Drive, docs
                ├── actions/           → 13 action modules (Gmail, Shell, Calendar, ...)
                ├── scheduler/         → 11 scheduled jobs (digests, sync, reflection)
                ├── learning.py        → Self-improvement (signals, insights, preferences)
                ├── healing.py         → Self-healing (detect failures → generate fixes → PR)
                └── mcp_server.py      → MCP protocol server for Claude Code
```

### Key Design Decisions

- **Local-first**: Embeddings and LLM run on Ollama (free). Claude API is optional.
- **Autonomy model**: Three levels control what Khalil can do without asking. Hard guardrails are immutable.
- **Self-correcting**: Shell errors are classified (transient/correctable/permanent) and retried with LLM-generated corrections in real time.
- **Self-healing**: When existing functionality fails repeatedly, Khalil reads its own source, generates a patch via Claude Opus, validates it, opens a PR, and verifies the fix worked.
- **Self-extending**: When Khalil can't do something you ask, it detects the gap via semantic regex matching, generates a new action module, smoke-tests it, and opens a PR.

## Telegram Commands

### Core
| Command | Description |
|---------|-------------|
| `/start` | Initialize and authorize |
| `/search <query>` | Search knowledge base (emails, Drive, timeline) |
| `/brief` | Generate morning brief |
| `/clear` | Clear conversation history |

### Integrations
| Command | Description |
|---------|-------------|
| `/email <query>` | Search, draft, or send emails |
| `/drive <query>` | Search Google Drive |
| `/calendar` | Today's events |
| `/remind <text> <time>` | Set a reminder |
| `/run <command>` | Execute a shell command (safety-classified) |
| `/sync` | Manually sync new emails |
| `/jobs` | Check job scraper for new matches |

### Dashboards
| Command | Description |
|---------|-------------|
| `/finance` | Portfolio summary, RRSP/TFSA alerts, deadlines |
| `/work` | Sprint dashboard & P0 epics |
| `/goals` | Quarterly goals with progress tracking |
| `/project <name>` | Project status (Zia, Tiny Grounds, Bézier, Khalil) |

### System
| Command | Description |
|---------|-------------|
| `/mode` | View/change autonomy level |
| `/approve` / `/deny` | Approve or deny pending actions |
| `/audit` | View action audit log |
| `/learn` | Self-improvement insights & preferences |
| `/health` | System health check |
| `/stats` | Knowledge base statistics |
| `/backup` | Export/import state |
| `/nudge` | Proactive check — what needs attention? |

### Natural Language

Khalil understands natural language for common actions:
- "Open Slack" → executes `open -a 'Slack'`
- "Remind me to review the PR tomorrow at 9am" → creates reminder
- "Send an email to Ahmed about the meeting" → drafts email with approval
- "Check disk space" → executes `df -h`
- "How many Cursor windows are open?" → runs osascript, responds "You have 5 Cursor windows open"
- "What's my battery?" → checks battery level and answers in plain English
- "What apps are running?" → lists running processes

Machine-state queries (window counts, battery, IP, uptime, running processes) are pattern-matched to pre-built shell commands — no LLM needed for command generation. Shell output is then interpreted by the LLM into a natural language answer rather than dumping raw stdout.

## Autonomy Model

Three levels control what Khalil can auto-execute vs. what needs your approval:

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

| Job | Schedule | What it does |
|-----|----------|-------------|
| Morning Brief | 7:00 AM daily | Calendar, emails, goals summary |
| Financial Alert | 9:00 AM on 1st & 15th | RRSP/TFSA alerts, tax deadlines |
| Career Alert | 10:00 AM daily | Job scraper new matches |
| Proactive Alerts | 12:00 PM Wednesday | Overdue items, attention needed |
| Weekly Summary | 6:00 PM Sunday | Week recap with achievements |
| Friday Reflection | 5:00 PM Friday | End-of-week reflection |
| Email Sync | Every 6 hours | Pull and index new emails |
| Daily Self-Check | 8:00 PM daily | System health monitoring |
| Weekly Reflection | 5:00 PM Sunday | Self-improvement insights |
| Micro-Reflection | 11:00 PM daily | Daily signals + self-healing check |
| Reminder Check | Every 60 seconds | Fire due reminders |

## Self-Correction (Real-Time)

When a shell command fails, Khalil classifies the error and responds accordingly:

| Error type | Examples | Behavior |
|------------|----------|----------|
| **Transient** | timeout, resource busy, connection refused | Retry same command after 2s |
| **Correctable** | syntax error, wrong flags, bad AppleScript | Feed error to LLM for a corrected command, then re-execute |
| **Permanent** | permission denied, command not found | Show error with user-friendly hint |

Safety constraint: corrected commands are re-classified and **cannot escalate** (e.g. a READ command can't be corrected into a WRITE command).

Known permanent errors include user-friendly hints — e.g. macOS accessibility permission errors explain exactly which System Settings pane to open.

## Self-Healing (Async)

When Khalil's existing functionality fails repeatedly:

1. **Record** — failure signals logged at every failure point: shell execution, action execution, calendar/email API errors, LLM timeouts, extension runtime failures, intent detection failures, and user corrections
2. **Detect** — 3+ failures with the same fingerprint in 48 hours triggers healing. Critical errors (`ImportError`, `SyntaxError`, `AttributeError`, `ModuleNotFoundError`) trigger after just 1 occurrence
3. **Diagnose** — extracts the failing function's source code via AST. Falls back to traceback parsing for dynamic code mapping when the static map doesn't cover the failure
4. **Patch** — Claude Opus generates a fixed version of the function
5. **Validate** — AST parse + blocklist check + full-file compilation
6. **Verify** — after a heal is merged, monitors for recurrence. If the same failure reappears, marks the heal as `failed_heal` and re-triggers with enriched context (previous patch + new error signals)
7. **PR** — creates branch, commits patch, opens PR, notifies via Telegram

Never auto-applies — always goes through PR review. Dedup and verification prevent runaway loops.

## Self-Extension

When Khalil can't handle a request:

1. **Detect** — semantic regex gate matches refusal patterns (e.g. "I can't", "not able to", "beyond my capabilities", "check manually"). Broader than exact phrase matching — catches novel LLM refusal phrasings. Structured `[CAPABILITY_GAP: ...]` tags are also detected as a fast path
2. **Classify** — LLM confirms it's a real capability gap (not just a knowledge gap)
3. **Generate** — Claude Opus writes a new action module following existing patterns
4. **Validate** — AST syntax check + blocklist (no subprocess, no eval, no socket)
5. **Smoke test** — imports the generated module in a subprocess and verifies the handler function exists. Catches wrong imports, missing dependencies, and misnamed handlers before the PR is opened
6. **PR** — creates `khalil-extend/<name>` branch, commits module + manifest, opens PR
7. **Notify** — Telegram message with Generate/Skip buttons, then PR link

Extensions auto-load on restart from `extensions/*.json` manifests.

## Eval Suite

292 tests across 10 test files covering safety-critical paths:

```bash
pytest tests/ -v
```

| Test file | Coverage |
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

## Knowledge Base

SQLite with `sqlite-vec` for vector similarity search:

- **Documents**: Gmail archives, Google Drive exports, timeline, CONTEXT.md, goal/project/finance files
- **Embeddings**: 768-dim vectors via Ollama `nomic-embed-text` (local, free)
- **Search**: Hybrid — vector similarity + keyword matching, results ranked and truncated for context window

Indexed content lives in the Personal repo (`archives/`, `work/`, `career/`, etc.). Khalil reads from it via the `KHALIL_PERSONAL_REPO` env var.

## Configuration

### Secrets (macOS Keyring)

```bash
# Required
keyring.set_password('khalil-assistant', 'telegram-bot-token', '...')

# Optional (only if LLM_BACKEND = "claude")
keyring.set_password('khalil-assistant', 'anthropic-api-key', '...')
```

Falls back to environment variables: `TELEGRAM_BOT_TOKEN`, `ANTHROPIC_API_KEY`.

### Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `KHALIL_PERSONAL_REPO` | `~/Developer/Personal` | Path to Personal repo (archives, docs) |
| `TELEGRAM_BOT_TOKEN` | — | Fallback for keyring |
| `ANTHROPIC_API_KEY` | — | Fallback for keyring |

### LLM Backend

In `config.py`:
```python
LLM_BACKEND = "ollama"          # free, local — or "claude" for cloud
OLLAMA_LLM_MODEL = "qwen2.5:14b"
CLAUDE_MODEL = "claude-sonnet-4-20250514"
CLAUDE_MODEL_COMPLEX = "claude-opus-4-20250514"  # used for code generation
```

### Google OAuth

Reuses existing tokens from `Personal/scripts/`:
- `credentials.json` — OAuth app config
- `token.json` — gmail.readonly + drive.readonly
- `token_khalil.json` — gmail.compose (for sending)
- `token_calendar.json` — calendar.readonly

## External Dependencies

| Service | Purpose | Required? |
|---------|---------|-----------|
| Ollama | Local embeddings + LLM | Yes (unless using Claude for everything) |
| Telegram Bot API | Chat interface | Yes |
| Google OAuth | Gmail, Drive, Calendar | Yes |
| Anthropic API | Claude LLM (cloud) | No (Ollama works offline) |
| GitHub CLI (`gh`) | Self-healing/extension PRs | For self-heal/extend only |

## Directory Structure

```
khalil/
├── server.py                 # FastAPI + Telegram bot
├── config.py                 # Centralized configuration
├── autonomy.py               # Action classification & approval
├── healing.py                # Self-healing engine
├── learning.py               # Self-improvement & preferences
├── monitoring.py             # System health checks
├── mcp_server.py             # MCP server for Claude Code
├── requirements.txt
├── com.khalil.daemon.plist   # macOS LaunchAgent
├── setup.sh
│
├── actions/                  # Action modules
│   ├── gmail.py              # Email search/draft/send
│   ├── gmail_sync.py         # Email sync worker
│   ├── drive.py              # Google Drive search
│   ├── calendar.py           # Calendar integration
│   ├── reminders.py          # Local reminders
│   ├── finance.py            # Financial dashboard
│   ├── goals.py              # Goal tracking
│   ├── projects.py           # Project status
│   ├── work.py               # Sprint dashboard
│   ├── jobs.py               # Job scraper bridge
│   ├── shell.py              # Shell execution (classification, retry, error hints)
│   ├── extend.py             # Self-extension (gap detection, smoke test, PR)
│   └── backup.py             # Backup/restore
│
├── knowledge/                # Knowledge base
│   ├── indexer.py            # SQLite + sqlite-vec init & ingestion
│   ├── search.py             # Hybrid vector + keyword search
│   ├── embedder.py           # Ollama embedding client
│   └── context.py            # CONTEXT.md extraction
│
├── scheduler/                # Scheduled tasks
│   ├── tasks.py              # Job definitions
│   ├── digests.py            # Brief/alert/summary generation
│   └── proactive.py          # Proactive attention checks
│
├── tests/                    # Eval suite (292 tests)
│   └── test_*.py             # 10 test files
│
├── extensions/               # Auto-generated capabilities
│   └── *.json                # Extension manifests
│
└── data/                     # Runtime (gitignored)
    ├── khalil.db             # SQLite database
    └── *.log                 # Logs
```

## MCP Server

Khalil exposes its knowledge base to Claude Code via the MCP protocol. Available tools:

- `search_knowledge` — hybrid search across all indexed content
- `get_context` — retrieve relevant CONTEXT.md sections
- `get_timeline` — query life timeline events
- `get_stats` — knowledge base statistics
- Read-only tools for work, finance, goals, and projects

## Database Schema

SQLite tables (initialized in `knowledge/indexer.py`):

| Table | Purpose |
|-------|---------|
| `documents` | Indexed content (emails, Drive, docs) with metadata |
| `documents_vec` | Vector embeddings (768-dim, sqlite-vec) |
| `pending_actions` | Action queue for approval flow |
| `settings` | App settings (autonomy level, owner chat ID) |
| `reminders` | Local reminders with recurrence rules |
| `audit_log` | Action audit trail with timestamps |
| `conversations` | Multi-turn context memory |
| `interaction_signals` | Failure/correction signals for self-healing |
| `insights` | Self-improvement insights (pending/applied/dismissed) |
| `learned_preferences` | Behavioral preferences with confidence scores |
