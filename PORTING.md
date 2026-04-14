# Porting Khalil to a New Machine

> **Automated setup**: Run `make install` for an interactive wizard that handles all steps below. The manual guide is preserved as reference.

Step-by-step guide to get Khalil fully operational on a new macOS device. Estimated time: ~30 minutes (excluding OAuth flows).

## Prerequisites

- macOS (Apple Silicon or Intel)
- Homebrew installed
- GitHub CLI authenticated (`gh auth login`)

## 1. Clone Repos

```bash
# Main codebase
git clone git@github.com:ahmedkhaledmohamed/khalil.git ~/Developer/Personal/scripts/khalil

# Knowledge state (portable memories, preferences, summaries)
git clone git@github.com:ahmedkhaledmohamed/khalil-knowledge.git ~/Developer/Personal/khalil-knowledge

# Personal repo (archives, work context — khalil indexes this)
git clone git@github.com:ahmedkhaledmohamed/scripts.git ~/Developer/Personal
```

## 2. Python Environment

```bash
cd ~/Developer/Personal/scripts/khalil
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Requires Python 3.13+. Install via `brew install python@3.13` if needed.

## 3. External Tools

```bash
# Required
brew install ollama gh

# Pull embedding + local LLM models
ollama serve &  # start daemon
ollama pull nomic-embed-text    # 768-dim embeddings (required)
ollama pull qwen3:14b           # local LLM fallback (optional)

# Optional
brew install --cask claude      # Claude Code CLI (for code generation features)
```

## 4. Secrets (Keyring)

Khalil stores secrets in the macOS Keychain under service `khalil-assistant`. Set them via Python:

```python
import keyring
svc = 'khalil-assistant'

# Required
keyring.set_password(svc, 'telegram-bot-token', '<from @BotFather>')
keyring.set_password(svc, 'anthropic-api-key', '<from console.anthropic.com>')

# Google OAuth (if using Gmail/Calendar/Drive features)
# Place credentials.json from GCP Console in ~/Developer/Personal/scripts/
# Then run interactive auth — see step 5

# Optional (enable specific integrations)
keyring.set_password(svc, 'spotify-client-id', '...')
keyring.set_password(svc, 'spotify-client-secret', '...')
keyring.set_password(svc, 'readwise-api-token', '...')
keyring.set_password(svc, 'notion-api-key', '...')
keyring.set_password(svc, 'github-pat', '...')
keyring.set_password(svc, 'replicate-api-token', '...')
```

## 5. Google OAuth Tokens

Download your OAuth client `credentials.json` from [GCP Console](https://console.cloud.google.com/apis/credentials) and place it at `~/Developer/Personal/scripts/credentials.json`.

Each Google service uses a separate token file for least-privilege scoping:

| Token file | Scopes | How to generate |
|------------|--------|-----------------|
| `token.json` | gmail.readonly, drive.readonly | `python3 -c "from actions.gmail import ...; ..."` |
| `token_khalil.json` | gmail.compose | First email send triggers auth |
| `token_calendar.json` | calendar.readonly | First calendar query triggers auth |
| `token_modify.json` | gmail.modify | First label operation triggers auth |
| `token_contacts.json` | contacts.readonly | First contact lookup triggers auth |
| `token_tasks.json` | tasks.readonly | First task query triggers auth |
| `token_drive_write.json` | drive.file | First doc creation triggers auth |
| `token_work.json` | gmail.readonly (work) | Separate auth for work Gmail |
| `token_youtube.json` | youtube.readonly | First YouTube query triggers auth |

Most tokens are generated on first use — Khalil will open a browser for OAuth consent. The `oauth_utils.py` module handles refresh, atomic writes, and corruption recovery.

## 6. Restore the Database

Three options, from most complete to lightest:

### Option A: Full DB from GitHub Release (recommended)

```bash
cd ~/Developer/Personal/scripts/khalil

# Download latest full DB backup
gh release download --repo ahmedkhaledmohamed/khalil-knowledge \
  --pattern "khalil_db_backup.gz" --dir data/

# Decompress
gunzip -c data/khalil_db_backup.gz > data/khalil.db
rm data/khalil_db_backup.gz
```

This restores everything: conversations, memories, documents, embeddings, workflows — the complete state.

### Option B: Import portable knowledge (lighter)

```bash
cd ~/Developer/Personal/scripts/khalil
source .venv/bin/activate

# Initialize empty DB schema
python3 -c "from knowledge.indexer import init_db; init_db()"

# Import memories, summaries, preferences, workflows from khalil-knowledge repo
python3 -c "from actions.backup import import_knowledge; print(import_knowledge())"

# Re-index documents (work repos, archives, side projects)
python3 -c "import asyncio; from knowledge.indexer import index_all; asyncio.run(index_all(force=True))"
```

This gives you learned knowledge but not conversation history. Documents are re-indexed from source.

### Option C: Fresh start

```bash
python3 -c "from knowledge.indexer import init_db; init_db()"
```

Empty DB. Khalil learns from scratch. Documents indexed on first sync.

## 7. Telegram Bot

1. Create a bot via [@BotFather](https://t.me/BotFather) (or reuse existing token)
2. Store token in keyring (step 4)
3. Start Khalil: `python3 server.py`
4. Send `/start` to your bot on Telegram — this registers your chat ID as the owner

## 8. Launchd Daemon (Auto-Start)

```bash
# Copy plist template
cp com.khalil.daemon.plist ~/Library/LaunchAgents/

# Edit paths if your khalil directory differs from ~/Developer/Personal/scripts/khalil
# Key fields to check:
#   ProgramArguments → path to .venv/bin/python3 and server.py
#   WorkingDirectory → khalil repo root
#   EnvironmentVariables → KHALIL_PERSONAL_REPO

# Load the daemon
launchctl load ~/Library/LaunchAgents/com.khalil.daemon.plist

# Verify it's running
launchctl list | grep khalil
tail -f data/khalil.log
```

## 9. Verify

```bash
# Check Khalil is responding
curl -s http://localhost:8321/health | python3 -m json.tool

# Send a test message on Telegram
# Ask: "What's the weather?" or "What do you know about me?"

# Verify knowledge DB
python3 -c "
import sqlite3
conn = sqlite3.connect('data/khalil.db')
for table in ['documents', 'memories', 'conversation_summaries', 'learned_preferences']:
    count = conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
    print(f'  {table}: {count} rows')
"
```

## Backup Schedule (Automatic)

Once running, Khalil handles its own backups:

| Time | Job | What it does |
|------|-----|-------------|
| 3:00 AM | Knowledge export | Exports 8 tables as JSON → opens PR on khalil-knowledge |
| 3:15 AM | Full DB backup | Gzips khalil.db → uploads as GitHub Release asset |

Retention: last 7 full DB backups. Knowledge PRs are squash-merged automatically.

## Directory Map

```
~/Developer/Personal/
├── scripts/khalil/              ← Main codebase
│   ├── server.py                  FastAPI + Telegram entry point
│   ├── config.py                  All configuration
│   ├── data/khalil.db             SQLite database (~200 MB)
│   ├── actions/                   37 skill modules
│   ├── knowledge/                 Indexer, search, embeddings
│   └── .venv/                     Python virtual environment
│
├── khalil-knowledge/            ← Portable state (git-synced)
│   ├── memories.json              Learned memories
│   ├── conversation_summaries.json
│   ├── learned_preferences.json
│   └── ...                        8 JSON files + _meta.json
│
├── scripts/                     ← Shared credentials
│   ├── credentials.json           Google OAuth client
│   └── token_*.json               Per-scope OAuth tokens
│
└── work/, career/, projects/    ← Content khalil indexes
```

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Ollama not reachable | `ollama serve` or `brew services start ollama` |
| Token refresh fails | Delete the stale `token_*.json`, re-auth on next use |
| DB locked | Kill any stale khalil processes: `pkill -f server.py` |
| Daemon won't start | Check `data/khalil.error.log` and plist paths |
| Missing module | `source .venv/bin/activate && pip install -r requirements.txt` |
