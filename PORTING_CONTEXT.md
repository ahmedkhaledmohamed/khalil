# Khalil Porting — Quick Reference

> For the full step-by-step guide, see [`PORTING.md`](./PORTING.md) in the khalil repo.

## Backup Architecture

Khalil's state is preserved across three layers:

```
Layer 1: Full DB backup (GitHub Release asset)
├── What: gzipped khalil.db (~150-200 MB compressed)
├── Where: github.com/ahmedkhaledmohamed/khalil-knowledge/releases
├── When: Daily at 3:15 AM
├── Retention: Last 7 backups
└── Restore: gh release download → gunzip → data/khalil.db

Layer 2: Knowledge JSON export (git-committed)
├── What: 8 portable tables as JSON (memories, summaries, preferences, etc.)
├── Where: github.com/ahmedkhaledmohamed/khalil-knowledge (repo files)
├── When: Daily at 3:00 AM
├── Flow: branch → commit → PR → auto-squash-merge
└── Restore: import_knowledge() merges into existing DB

Layer 3: Source repos (re-indexable)
├── What: Work docs, side projects, archives, email
├── Where: ~/Developer/* (various repos)
├── When: Re-indexed on demand via /sync or scheduled jobs
└── Restore: Clone repos → index_all(force=True)
```

## What Lives Where

| Data | Location | Portable? |
|------|----------|-----------|
| Full DB (everything) | GitHub Release on khalil-knowledge | Yes — download + gunzip |
| Memories, preferences | khalil-knowledge repo (JSON files) | Yes — git clone + import |
| Google OAuth tokens | ~/Developer/Personal/scripts/token_*.json | No — re-auth on new machine |
| API keys & secrets | macOS Keychain (khalil-assistant) | No — re-enter via keyring |
| Work documents | ~/Developer/* repos | Yes — git clone + re-index |
| Embeddings | data/khalil.db (documents table) | Via full DB, or regenerated via Ollama |
| Daemon config | ~/Library/LaunchAgents/com.khalil.daemon.plist | Copy + update paths |

## New Machine Checklist

```
[ ] Clone: khalil, khalil-knowledge, Personal repos
[ ] Python 3.13 + venv + pip install -r requirements.txt
[ ] brew install ollama gh
[ ] ollama pull nomic-embed-text
[ ] Set keyring secrets: telegram-bot-token, anthropic-api-key
[ ] Place credentials.json for Google OAuth
[ ] Restore DB (gh release download or import_knowledge)
[ ] Copy + load launchd plist
[ ] Send /start on Telegram to register owner
[ ] Verify: curl localhost:8321/health
```
