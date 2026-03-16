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

# Embedding config
OLLAMA_URL = "http://localhost:11434"
EMBED_MODEL = "nomic-embed-text"
EMBED_DIM = 768  # nomic-embed-text dimension

# LLM config — "ollama" (free, local) or "claude" (paid, cloud)
LLM_BACKEND = "ollama"  # switch to "claude" if you have an API key
OLLAMA_LLM_MODEL = "qwen2.5:14b"

# Claude API (used when LLM_BACKEND = "claude")
CLAUDE_MODEL = "claude-sonnet-4-20250514"
CLAUDE_MODEL_COMPLEX = "claude-opus-4-20250514"
MAX_CONTEXT_TOKENS = 8000

# Timezone
TIMEZONE = "America/Toronto"

# Telegram
TELEGRAM_POLL_TIMEOUT = 30

# Keyring service name
KEYRING_SERVICE = "khalil-assistant"

# Self-healing
HEALING_FAILURE_THRESHOLD = 3    # failures before triggering self-heal
HEALING_COOLDOWN_SECONDS = 3600  # max 1 healing PR per hour

# Claude Code CLI (for complex code generation)
CLAUDE_CODE_BIN = "/opt/homebrew/bin/claude"
WORKTREES_DIR = KHALIL_DIR / ".worktrees"


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
