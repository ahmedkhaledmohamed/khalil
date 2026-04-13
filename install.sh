#!/usr/bin/env bash
# Khalil Installer — from clone to running in one command.
#
# Usage:
#   bash install.sh              # Full interactive setup
#   bash install.sh --force      # Re-run all phases
#   bash install.sh --secrets-only  # Re-configure secrets
#   bash install.sh --phase 3    # Run specific phase
#   bash install.sh --non-interactive  # No prompts (use env vars)
#
set -euo pipefail

# ── Colors ──
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}✓${NC} $1"; }
warn() { echo -e "  ${YELLOW}!${NC} $1"; }
fail() { echo -e "  ${RED}✗${NC} $1"; }
skip() { echo -e "  ${DIM}-${NC} $1"; }
header() { echo -e "\n${BOLD}[$1/8] $2${NC}"; }

# ── Paths ──
KHALIL_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="${KHALIL_DIR}/.venv"
PYTHON="${VENV}/bin/python3"
SETUP_UTILS="${KHALIL_DIR}/scripts/setup_utils.py"
STATE_FILE="${KHALIL_DIR}/data/.install_state"
PLIST_TEMPLATE="${KHALIL_DIR}/com.khalil.daemon.plist"
PLIST_NAME="com.khalil.daemon.plist"
PLIST_DEST="${HOME}/Library/LaunchAgents/${PLIST_NAME}"
PERSONAL_REPO="${KHALIL_PERSONAL_REPO:-${HOME}/Developer/Personal}"
PORT=8033

# ── CLI Args ──
FORCE=false
SECRETS_ONLY=false
NON_INTERACTIVE=false
PHASE_ONLY=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --force) FORCE=true; shift ;;
        --secrets-only) SECRETS_ONLY=true; shift ;;
        --non-interactive) NON_INTERACTIVE=true; shift ;;
        --phase) PHASE_ONLY="$2"; shift 2 ;;
        --help) echo "Usage: bash install.sh [--force] [--secrets-only] [--non-interactive] [--phase N]"; exit 0 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

# ── State tracking ──
mkdir -p "${KHALIL_DIR}/data"

phase_done() {
    [[ "$FORCE" == "true" ]] && return 1
    [[ -f "$STATE_FILE" ]] && grep -q "^$1=done" "$STATE_FILE" 2>/dev/null
}

mark_done() {
    if [[ -f "$STATE_FILE" ]]; then
        grep -v "^$1=" "$STATE_FILE" > "${STATE_FILE}.tmp" 2>/dev/null || true
        mv "${STATE_FILE}.tmp" "$STATE_FILE"
    fi
    echo "$1=done" >> "$STATE_FILE"
}

should_run() {
    local phase_num="$1"
    if [[ -n "$PHASE_ONLY" ]] && [[ "$PHASE_ONLY" != "$phase_num" ]]; then
        return 1
    fi
    if [[ "$SECRETS_ONLY" == "true" ]] && [[ "$phase_num" != "4" ]] && [[ "$phase_num" != "5" ]]; then
        return 1
    fi
    return 0
}

prompt_yn() {
    local msg="$1" default="${2:-y}"
    if [[ "$NON_INTERACTIVE" == "true" ]]; then
        [[ "$default" == "y" ]] && return 0 || return 1
    fi
    local prompt
    [[ "$default" == "y" ]] && prompt="[Y/n]" || prompt="[y/N]"
    read -rp "  ? $msg $prompt: " answer
    answer="${answer:-$default}"
    [[ "$answer" =~ ^[Yy] ]]
}

prompt_secret() {
    local key="$1" label="$2"
    if $PYTHON "$SETUP_UTILS" check_secret "$key" 2>/dev/null; then
        ok "$label (already configured)"
        return 0
    fi
    # Check env var fallback
    local env_key
    env_key=$(echo "$key" | tr '-' '_' | tr '[:lower:]' '[:upper:]')
    if [[ -n "${!env_key:-}" ]]; then
        $PYTHON "$SETUP_UTILS" set_secret "$key" "${!env_key}"
        ok "$label (from env var $env_key)"
        return 0
    fi
    if [[ "$NON_INTERACTIVE" == "true" ]]; then
        fail "$label — not configured (set $env_key env var)"
        return 1
    fi
    read -rsp "  Enter $label: " value
    echo
    if [[ -z "$value" ]]; then
        skip "$label — skipped"
        return 1
    fi
    if ! $PYTHON "$SETUP_UTILS" validate_secret "$key" "$value" 2>/dev/null; then
        warn "$label — format warning (stored anyway)"
    fi
    $PYTHON "$SETUP_UTILS" set_secret "$key" "$value"
    ok "$label — stored in keychain"
}

# ══════════════════════════════════════════════════
echo -e "\n${BOLD}=== Khalil Installer ===${NC}\n"

# ── Phase 0: Preflight ──
if should_run 0 && ! phase_done "phase0"; then
    header 0 "Preflight checks"
    if [[ "$(uname)" != "Darwin" ]]; then
        fail "Khalil requires macOS"
        exit 1
    fi
    ok "macOS $(sw_vers -productVersion)"

    if ! xcode-select -p &>/dev/null; then
        fail "Xcode CLI tools not installed. Run: xcode-select --install"
        exit 1
    fi
    ok "Xcode CLI tools"

    if ! command -v brew &>/dev/null; then
        fail "Homebrew not found. Install: /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
        exit 1
    fi
    ok "Homebrew $(brew --version | head -1 | awk '{print $2}')"
    mark_done "phase0"
fi

# ── Phase 1: System dependencies ──
if should_run 1 && ! phase_done "phase1"; then
    header 1 "System dependencies"

    for pkg in python@3.13 ollama gh; do
        if brew list "$pkg" &>/dev/null; then
            ok "$pkg installed"
        else
            echo -e "  ${DIM}Installing $pkg...${NC}"
            brew install "$pkg"
            ok "$pkg installed"
        fi
    done

    # Start Ollama as a service
    if ! pgrep -x ollama &>/dev/null; then
        brew services start ollama &>/dev/null || true
        echo -e "  ${DIM}Waiting for Ollama...${NC}"
        for i in $(seq 1 15); do
            curl -sf http://localhost:11434/api/version &>/dev/null && break
            sleep 2
        done
    fi
    if curl -sf http://localhost:11434/api/version &>/dev/null; then
        ok "Ollama running"
    else
        warn "Ollama not reachable — embeddings will be unavailable"
    fi
    mark_done "phase1"
fi

# ── Phase 2: Python environment ──
if should_run 2 && ! phase_done "phase2"; then
    header 2 "Python environment"

    if [[ ! -d "$VENV" ]]; then
        python3.13 -m venv "$VENV"
        ok "Virtual environment created"
    else
        ok "Virtual environment exists"
    fi

    echo -e "  ${DIM}Installing packages...${NC}"
    "$VENV/bin/pip" install -q -r "${KHALIL_DIR}/requirements.txt" 2>/dev/null
    pkg_count=$("$VENV/bin/pip" list --format=columns 2>/dev/null | tail -n +3 | wc -l | tr -d ' ')
    ok "pip packages installed ($pkg_count packages)"

    if $PYTHON "$SETUP_UTILS" check_imports 2>/dev/null; then
        ok "Critical imports verified"
    else
        warn "Some imports failed — check requirements.txt"
    fi
    mark_done "phase2"
fi

# ── Phase 3: Ollama models ──
if should_run 3 && ! phase_done "phase3"; then
    header 3 "Ollama models"

    if ollama list 2>/dev/null | grep -q "nomic-embed-text"; then
        ok "nomic-embed-text ready"
    else
        echo -e "  ${DIM}Pulling nomic-embed-text (required for embeddings)...${NC}"
        ollama pull nomic-embed-text
        ok "nomic-embed-text pulled"
    fi

    if ollama list 2>/dev/null | grep -q "qwen3:14b"; then
        ok "qwen3:14b ready"
    else
        if prompt_yn "Pull qwen3:14b (~8GB local LLM fallback)?" "n"; then
            ollama pull qwen3:14b
            ok "qwen3:14b pulled"
        else
            skip "qwen3:14b — skipped"
        fi
    fi
    mark_done "phase3"
fi

# ── Phase 4: Required secrets ──
if should_run 4 && ! phase_done "phase4"; then
    header 4 "Required secrets"
    all_ok=true
    prompt_secret "telegram-bot-token" "Telegram Bot Token (from @BotFather)" || all_ok=false
    prompt_secret "anthropic-api-key" "Anthropic API Key (from console.anthropic.com)" || all_ok=false
    if [[ "$all_ok" == "true" ]]; then
        mark_done "phase4"
    else
        warn "Some required secrets not configured — Khalil may not start correctly"
    fi
fi

# ── Phase 5: Optional integrations ──
if should_run 5; then
    header 5 "Optional integrations"

    # Google OAuth
    creds_path="${PERSONAL_REPO}/scripts/credentials.json"
    if [[ -f "$creds_path" ]]; then
        ok "Google OAuth credentials.json found"
    else
        skip "Google OAuth — credentials.json not found at $creds_path"
        if [[ "$NON_INTERACTIVE" != "true" ]]; then
            echo -e "  ${DIM}  Download from GCP Console > APIs & Services > Credentials${NC}"
        fi
    fi

    # Optional secrets — prompt only in interactive mode
    if [[ "$NON_INTERACTIVE" != "true" ]]; then
        echo
        for key_label in \
            "github-pat:GitHub Personal Access Token" \
            "spotify-client-id:Spotify Client ID" \
            "spotify-client-secret:Spotify Client Secret" \
            "notion-api-key:Notion API Key" \
            "readwise-api-token:Readwise API Token" \
            "slack-token:Slack Bot Token (xoxb-...)"; do
            key="${key_label%%:*}"
            label="${key_label#*:}"
            if $PYTHON "$SETUP_UTILS" check_secret "$key" 2>/dev/null; then
                ok "$label (configured)"
            else
                read -rsp "  $label (Enter to skip): " value
                echo
                if [[ -n "$value" ]]; then
                    $PYTHON "$SETUP_UTILS" set_secret "$key" "$value"
                    ok "$label — stored"
                else
                    skip "$label"
                fi
            fi
        done
    fi
    mark_done "phase5"
fi

# ── Phase 6: Database ──
if should_run 6 && ! phase_done "phase6"; then
    header 6 "Database"

    db_path="${KHALIL_DIR}/data/khalil.db"
    if [[ -f "$db_path" ]]; then
        doc_count=$($PYTHON "$SETUP_UTILS" db_doc_count 2>/dev/null || echo 0)
        if [[ "$doc_count" -gt 0 ]]; then
            ok "data/khalil.db exists ($doc_count documents)"
            mark_done "phase6"
        else
            warn "data/khalil.db exists but is empty"
        fi
    fi

    if ! phase_done "phase6"; then
        if [[ "$NON_INTERACTIVE" == "true" ]]; then
            echo -e "  ${DIM}Initializing fresh database...${NC}"
            $PYTHON -c "import sys; sys.path.insert(0,'.'); from knowledge.indexer import init_db; init_db()"
            ok "Fresh database initialized"
        else
            echo -e "  Database setup:"
            echo -e "    ${BOLD}[A]${NC} Restore full DB from GitHub Release (recommended if migrating)"
            echo -e "    ${BOLD}[B]${NC} Import portable knowledge from khalil-knowledge repo"
            echo -e "    ${BOLD}[C]${NC} Fresh start (empty database)"
            read -rp "  Choice [C]: " db_choice
            db_choice="${db_choice:-C}"

            case "${db_choice^^}" in
                A)
                    if ! gh auth status &>/dev/null; then
                        warn "GitHub CLI not authenticated. Run: gh auth login"
                        fail "Cannot restore — falling back to fresh database"
                        $PYTHON -c "import sys; sys.path.insert(0,'.'); from knowledge.indexer import init_db; init_db()"
                    else
                        echo -e "  ${DIM}Downloading backup...${NC}"
                        gh release download --repo ahmedkhaledmohamed/khalil-knowledge \
                            --pattern "khalil_db_backup.gz" --dir "${KHALIL_DIR}/data/" --clobber
                        gunzip -c "${KHALIL_DIR}/data/khalil_db_backup.gz" > "$db_path"
                        rm -f "${KHALIL_DIR}/data/khalil_db_backup.gz"
                        doc_count=$($PYTHON "$SETUP_UTILS" db_doc_count 2>/dev/null || echo 0)
                        ok "Database restored ($doc_count documents)"
                    fi
                    ;;
                B)
                    knowledge_dir="${KHALIL_KNOWLEDGE_EXPORT_DIR:-${PERSONAL_REPO}/khalil-knowledge}"
                    if [[ ! -d "$knowledge_dir" ]]; then
                        warn "khalil-knowledge not found at $knowledge_dir"
                        if prompt_yn "Clone it?" "y"; then
                            git clone git@github.com:ahmedkhaledmohamed/khalil-knowledge.git "$knowledge_dir"
                        fi
                    fi
                    $PYTHON -c "import sys; sys.path.insert(0,'.'); from knowledge.indexer import init_db; init_db()"
                    if [[ -d "$knowledge_dir" ]]; then
                        $PYTHON -c "import sys; sys.path.insert(0,'.'); from actions.backup import import_knowledge; print(import_knowledge())"
                        ok "Knowledge imported"
                    fi
                    ;;
                *)
                    $PYTHON -c "import sys; sys.path.insert(0,'.'); from knowledge.indexer import init_db; init_db()"
                    ok "Fresh database initialized"
                    ;;
            esac
        fi
        mark_done "phase6"
    fi
fi

# ── Phase 7: LaunchAgent ──
if should_run 7 && ! phase_done "phase7"; then
    header 7 "LaunchAgent"

    if [[ -f "$PLIST_TEMPLATE" ]]; then
        generated="${KHALIL_DIR}/data/${PLIST_NAME}.generated"
        sed -e "s|__KHALIL_DIR__|${KHALIL_DIR}|g" \
            -e "s|__PERSONAL_REPO__|${PERSONAL_REPO}|g" \
            "$PLIST_TEMPLATE" > "$generated"
        ok "Plist generated"

        if [[ -f "$PLIST_DEST" ]]; then
            if diff -q "$generated" "$PLIST_DEST" &>/dev/null; then
                ok "LaunchAgent up to date"
            else
                launchctl unload "$PLIST_DEST" 2>/dev/null || true
                cp "$generated" "$PLIST_DEST"
                ok "LaunchAgent updated"
            fi
        else
            cp "$generated" "$PLIST_DEST"
            ok "LaunchAgent installed at ~/Library/LaunchAgents/"
        fi
        mark_done "phase7"
    else
        warn "Plist template not found — skipping LaunchAgent setup"
    fi
fi

# ── Phase 8: Start and verify ──
if should_run 8; then
    header 8 "Start & verify"

    # Check port
    if lsof -i ":${PORT}" -sTCP:LISTEN &>/dev/null; then
        pid=$(lsof -ti ":${PORT}" -sTCP:LISTEN 2>/dev/null | head -1)
        ok "Khalil already running (PID $pid)"
    else
        if [[ -f "$PLIST_DEST" ]]; then
            launchctl unload "$PLIST_DEST" 2>/dev/null || true
            launchctl load "$PLIST_DEST"
            echo -e "  ${DIM}Waiting for server...${NC}"
            for i in $(seq 1 15); do
                curl -sf "http://localhost:${PORT}/health" &>/dev/null && break
                sleep 2
            done
        else
            warn "No LaunchAgent — start manually: ${PYTHON} server.py"
        fi
    fi

    if curl -sf "http://localhost:${PORT}/health" &>/dev/null; then
        ok "Health check passed"
    else
        fail "Health check failed"
        if [[ -f "${KHALIL_DIR}/data/khalil.error.log" ]]; then
            echo -e "\n  ${DIM}Last 10 lines of error log:${NC}"
            tail -10 "${KHALIL_DIR}/data/khalil.error.log" | sed 's/^/  /'
        fi
    fi
fi

# ── Summary ──
echo -e "\n${BOLD}=== Setup Complete ===${NC}\n"
echo -e "  Telegram: Send ${BOLD}/start${NC} to your bot to register as owner"
echo -e "  Health:   ${DIM}make health${NC}"
echo -e "  Logs:     ${DIM}make logs${NC}"
echo -e "  CLI:      ${DIM}${PYTHON} cli.py${NC}"
echo -e "  Stop:     ${DIM}make stop${NC}"
echo -e "  Status:   ${DIM}make status${NC}"
echo
