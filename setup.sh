#!/bin/bash
# Khalil — Legacy Setup Script
# Preserved for backward compatibility. For the full interactive installer, use:
#   make install
# or:
#   bash install.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Khalil Setup ==="

# 1. Python venv
echo ""
echo "1. Setting up Python virtual environment..."
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
    echo "   Created .venv"
else
    echo "   .venv already exists"
fi
source .venv/bin/activate

# 2. Install dependencies
echo ""
echo "2. Installing Python dependencies..."
pip install -q --upgrade pip
pip install -q -r requirements.txt
echo "   Done"

# 3. Check Ollama
echo ""
echo "3. Checking Ollama..."
if command -v ollama &> /dev/null; then
    echo "   Ollama found"
    if ollama list 2>/dev/null | grep -q "nomic-embed-text"; then
        echo "   nomic-embed-text model ready"
    else
        echo "   Pulling nomic-embed-text model..."
        ollama pull nomic-embed-text
    fi
else
    echo "   ⚠️  Ollama not found. Install it:"
    echo "      brew install ollama"
    echo "      ollama serve &"
    echo "      ollama pull nomic-embed-text"
fi

# 4. Create data directory
echo ""
echo "4. Creating data directory..."
mkdir -p data

# 5. Credentials check
echo ""
echo "5. Checking credentials..."
echo ""
echo "   Set your secrets (run these in Terminal):"
echo ""
echo "   # Telegram bot token (get from @BotFather)"
echo "   python3 -c \"import keyring; keyring.set_password('khalil-assistant', 'telegram-bot-token', 'YOUR_TOKEN')\""
echo ""
echo "   # Anthropic API key"
echo "   python3 -c \"import keyring; keyring.set_password('khalil-assistant', 'anthropic-api-key', 'YOUR_KEY')\""
echo ""
echo "   Or set environment variables: TELEGRAM_BOT_TOKEN, ANTHROPIC_API_KEY"

# 6. Index knowledge base
echo ""
echo "6. To index your knowledge base:"
echo "   source .venv/bin/activate"
echo "   python3 -m knowledge.indexer"

# 7. Run server
echo ""
echo "7. To start Khalil:"
echo "   source .venv/bin/activate"
echo "   python3 server.py"

# 8. launchd (always-on) — generate plist from template with real paths
KHALIL_DIR="$(cd "$(dirname "$0")" && pwd)"
PERSONAL_REPO="$(dirname "$(dirname "$KHALIL_DIR")")"
sed -e "s|__KHALIL_DIR__|${KHALIL_DIR}|g" \
    -e "s|__PERSONAL_REPO__|${PERSONAL_REPO}|g" \
    com.khalil.daemon.plist > /tmp/com.khalil.daemon.plist
echo ""
echo "8. To install as always-on daemon:"
echo "   cp /tmp/com.khalil.daemon.plist ~/Library/LaunchAgents/"
echo "   launchctl load ~/Library/LaunchAgents/com.khalil.daemon.plist"

echo ""
echo "=== Setup complete ==="
