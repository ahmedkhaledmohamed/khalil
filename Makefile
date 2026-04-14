# Khalil — Autonomous AI Personal Agent
# Usage: make install    (full setup wizard)
#        make start      (start daemon)
#        make stop       (stop daemon)
#        make status     (daemon status + health)

.PHONY: install uninstall start stop restart status logs secrets health test index

KHALIL_DIR := $(shell pwd)
VENV := $(KHALIL_DIR)/.venv
PYTHON := $(VENV)/bin/python3
PLIST := com.khalil.daemon.plist
PLIST_DEST := $(HOME)/Library/LaunchAgents/$(PLIST)
PORT := 8033

install:
	@bash install.sh

secrets:
	@bash install.sh --secrets-only

start:
	@if lsof -i :$(PORT) -sTCP:LISTEN >/dev/null 2>&1; then \
		echo "Khalil already running on port $(PORT)"; \
	elif [ -f "$(PLIST_DEST)" ]; then \
		launchctl load "$(PLIST_DEST)" 2>/dev/null || true; \
		echo "Khalil started. Check: make health"; \
	else \
		echo "No LaunchAgent installed. Run: make install"; \
	fi

stop:
	@launchctl unload "$(PLIST_DEST)" 2>/dev/null || true
	@echo "Khalil stopped."

restart: stop
	@sleep 2
	@$(MAKE) start

status:
	@echo "=== Daemon ==="
	@launchctl list 2>/dev/null | grep -q com.khalil.daemon \
		&& echo "Status: running" \
		|| echo "Status: not running"
	@echo "\n=== Health ==="
	@curl -sf http://localhost:$(PORT)/health 2>/dev/null \
		| $(PYTHON) -m json.tool 2>/dev/null \
		|| echo "Health endpoint unreachable"

logs:
	@tail -f "$(KHALIL_DIR)/data/khalil.error.log"

health:
	@curl -sf http://localhost:$(PORT)/health | $(PYTHON) -m json.tool

test:
	@$(PYTHON) -m pytest tests/ -v --tb=short

index:
	@echo "Indexing knowledge base (this may take 10-30 minutes)..."
	@$(PYTHON) -c "import sys,asyncio;sys.path.insert(0,'.');from knowledge.indexer import init_db,index_all;init_db();asyncio.run(index_all(force=True))"
	@echo "Done. Document count:"
	@$(PYTHON) scripts/setup_utils.py db_doc_count

uninstall:
	@launchctl unload "$(PLIST_DEST)" 2>/dev/null || true
	@rm -f "$(PLIST_DEST)"
	@echo "LaunchAgent removed. Data preserved in data/."
