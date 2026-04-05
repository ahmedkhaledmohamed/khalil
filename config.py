"""Khalil configuration and settings."""

import os
from pathlib import Path
from enum import Enum

# Khalil repo root
KHALIL_DIR = Path(__file__).parent
DATA_DIR = KHALIL_DIR / "data"
DB_PATH = DATA_DIR / "khalil.db"
EXTENSIONS_DIR = KHALIL_DIR / "extensions"

# External: Personal repo (configurable via env var)
PERSONAL_REPO_PATH = Path(os.environ.get(
    "KHALIL_PERSONAL_REPO",
    str(Path.home() / "Developer" / "Personal"),
))
SCRIPTS_DIR = PERSONAL_REPO_PATH / "scripts"

# Archives (in Personal repo)
ARCHIVES_DIR = PERSONAL_REPO_PATH / "archives" / "google"
GMAIL_DIR = ARCHIVES_DIR / "gmail"
DRIVE_DIR = ARCHIVES_DIR / "drive"
TIMELINE_FILE = ARCHIVES_DIR / "timeline.md"
CONTEXT_FILE = PERSONAL_REPO_PATH / "CONTEXT.md"

# Cursor conversation transcripts
CURSOR_TRANSCRIPTS_DIR = Path.home() / ".cursor" / "projects"
CURSOR_CATALOG_FILE = PERSONAL_REPO_PATH / "archives" / "cursor-conversations.md"

# Content directories (in Personal repo)
WORK_DIR = PERSONAL_REPO_PATH / "work"
CAREER_DIR = PERSONAL_REPO_PATH / "career"
FINANCE_DIR = PERSONAL_REPO_PATH / "finance"
PROJECTS_DIR = PERSONAL_REPO_PATH / "projects"
GOALS_DIR = PERSONAL_REPO_PATH / "goals"

# Google OAuth (in Personal/scripts/, shared with other tools)
CREDENTIALS_FILE = SCRIPTS_DIR / "credentials.json"
TOKEN_FILE = SCRIPTS_DIR / "token.json"  # gmail.readonly + drive.readonly
TOKEN_FILE_COMPOSE = SCRIPTS_DIR / "token_khalil.json"  # gmail.compose for send
TOKEN_FILE_CALENDAR = SCRIPTS_DIR / "token_calendar.json"  # calendar.readonly
TOKEN_FILE_MODIFY = SCRIPTS_DIR / "token_modify.json"  # gmail.modify for label management (#46)
TOKEN_FILE_CONTACTS = SCRIPTS_DIR / "token_contacts.json"  # contacts.readonly for People API (#49)
TOKEN_FILE_TASKS = SCRIPTS_DIR / "token_tasks.json"  # tasks.readonly for Google Tasks (#50)
TOKEN_FILE_DRIVE_WRITE = SCRIPTS_DIR / "token_drive_write.json"  # drive.file for Doc/Sheet creation (#54)
TOKEN_FILE_WORK = SCRIPTS_DIR / "token_work.json"  # gmail.readonly for work account (#55)
TOKEN_FILE_SPOTIFY = SCRIPTS_DIR / "token_spotify.json"  # Spotify OAuth token cache
TOKEN_FILE_YOUTUBE = SCRIPTS_DIR / "token_youtube.json"  # youtube.readonly for YouTube Data API

# App Store Connect (Zia app ID — set after configuring ASC API key)
ZIA_APP_ID = ""

# Embedding config
OLLAMA_URL = "http://localhost:11434"
EMBED_MODEL = "nomic-embed-text"
EMBED_DIM = 768  # nomic-embed-text dimension
EMBED_PROVIDER = "ollama"  # #68: "ollama" (default) — abstraction for future providers

# LLM config — "ollama" (free, local) or "claude" (paid, cloud)
LLM_BACKEND = "claude"  # "ollama" for local, "claude" for Taskforce proxy
OLLAMA_LLM_MODEL = "qwen3:14b"

# Claude API (used when LLM_BACKEND = "claude")
CLAUDE_MODEL = "claude-sonnet-4-20250514"
CLAUDE_MODEL_COMPLEX = "claude-opus-4-20250514"
MAX_CONTEXT_TOKENS = 8000
# Taskforce proxy — set KHALIL_CLAUDE_BASE_URL to override the Anthropic API endpoint
# e.g. "https://hendrix-genai.spotify.net/taskforce/anthropic"
CLAUDE_BASE_URL = os.getenv("KHALIL_CLAUDE_BASE_URL", "https://hendrix-genai.spotify.net/taskforce/anthropic/v1")
# Taskforce uses "apikey" header instead of "x-api-key". Set to override.
CLAUDE_API_KEY_HEADER = os.getenv("KHALIL_CLAUDE_API_KEY_HEADER", "apikey")

# Backup LLM providers via Taskforce (fallback: Claude → OpenAI → Google)
OPENAI_BASE_URL = os.getenv("KHALIL_OPENAI_BASE_URL", "https://hendrix-genai.spotify.net/taskforce/openai/v1")
OPENAI_MODEL = "gpt-5.2"
GOOGLE_BASE_URL = os.getenv("KHALIL_GOOGLE_BASE_URL", "https://hendrix-genai.spotify.net/taskforce/google/v1")
GOOGLE_MODEL = "gemini-2.5-flash"

# Owner identity (for personalized prompts)
OWNER_NAME = os.getenv("KHALIL_OWNER_NAME", "User")

# Timezone
TIMEZONE = os.getenv("KHALIL_TIMEZONE", "UTC")

# Weather (Open-Meteo, free, no API key)
# Defaults to Toronto. Override with KHALIL_WEATHER_LAT / KHALIL_WEATHER_LON.
WEATHER_LAT = float(os.getenv("KHALIL_WEATHER_LAT", "43.6629"))
WEATHER_LON = float(os.getenv("KHALIL_WEATHER_LON", "-79.3957"))

# Web search
SEARCH_PROVIDER = "duckduckgo"  # no API key needed

# Telegram
TELEGRAM_POLL_TIMEOUT = 30

# Keyring service name
KEYRING_SERVICE = "khalil-assistant"
# App Store Connect API keys (stored in keyring, not here):
#   appstore-key-id       — API Key ID from App Store Connect
#   appstore-issuer-id    — Issuer ID from App Store Connect
#   appstore-private-key  — Contents of the .p8 private key file

# Agent loop — continuous sense-think-act background process
AGENT_LOOP_ENABLED = os.getenv("KHALIL_AGENT_LOOP", "true").lower() == "true"
AGENT_LOOP_INTERVAL_S = int(os.getenv("KHALIL_AGENT_LOOP_INTERVAL", "300"))  # 5 min default
AGENT_LOOP_QUIET_HOURS = (23, 7)  # no non-urgent actions between 11pm-7am

# Self-healing
HEALING_FAILURE_THRESHOLD = 3    # failures before triggering self-heal

# Reactive workflow engine
WORKFLOW_ENGINE_ENABLED = True
WORKFLOW_MAX_RUNS_PER_HOUR = 10

# Claude Code CLI (for complex code generation)
CLAUDE_CODE_BIN = "/opt/homebrew/bin/claude"
WORKTREES_DIR = KHALIL_DIR / ".worktrees"

# Container sandbox
SANDBOX_IMAGE = "khalil-sandbox"
SANDBOX_MEM_LIMIT = "256m"
SANDBOX_TIMEOUT = 15

# Agent swarms
SWARM_ENABLED = True
MAX_CONCURRENT_AGENTS = 3

# Voice interaction
VOICE_REPLY_ENABLED = False  # opt-in: reply with voice audio by default
TTS_VOICE = "Samantha"  # macOS say voice

# Apple Reminders sync — push Khalil reminders to Reminders.app
APPLE_REMINDERS_SYNC = True


class AutonomyLevel(Enum):
    SUPERVISED = 1   # Ask before every action
    GUIDED = 2       # Auto for safe, ask for risky
    AUTONOMOUS = 3   # Auto within guardrails


class ActionType(Enum):
    READ = "read"         # Search, summarize, retrieve
    WRITE = "write"       # Send email, create file, modify
    DANGEROUS = "dangerous"  # Money, delete, share externally


# Hard guardrails — NEVER auto-execute regardless of autonomy level
HARD_GUARDRAILS = [
    "send_money",
    "modify_financial_account",
    "delete_data",
    "share_externally",
    "modify_repo_committed_files",
    "family_member_data_action",
    "generate_capability",
    "shell_dangerous",
    "browser_financial_site",
]

# Privacy: sensitive query patterns that should NOT be sent raw to Claude API
SENSITIVE_PATTERNS = [
    r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b",  # Phone numbers
    r"\b\d{3}[-]?\d{2}[-]?\d{4}\b",     # SSN pattern
    r"\b[A-Z]{2}\d{6}\b",                # Passport numbers
    r"\bcredit card\b",
    r"\bpassword\b",
    r"\bSIN\s*\d",                        # Canadian SIN
]
