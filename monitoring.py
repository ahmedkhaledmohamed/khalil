"""Health checks and monitoring for Khalil subsystems."""

import asyncio
import logging
import subprocess
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from config import DB_PATH, OLLAMA_URL, TIMEZONE

log = logging.getLogger("khalil.monitoring")


# --- #39: Native macOS notifications ---

def send_macos_notification(title: str, message: str, sound: str = "default") -> bool:
    """Send a native macOS notification via osascript. Returns True on success."""
    # Escape double quotes for AppleScript
    title_esc = title.replace('"', '\\"')
    msg_esc = message.replace('"', '\\"')
    script = f'display notification "{msg_esc}" with title "{title_esc}" sound name "{sound}"'
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return True
        log.warning("macOS notification failed: %s", result.stderr.strip())
        return False
    except Exception as e:
        log.warning("macOS notification error: %s", e)
        return False


def get_focus_mode_status() -> dict:
    """#34: Read macOS Focus/DND status. Returns {active: bool, mode: str | None}."""
    try:
        # Check if Focus/DND is active via defaults
        result = subprocess.run(
            ["defaults", "read", "com.apple.controlcenter", "NSStatusItem Visible FocusModes"],
            capture_output=True, text=True, timeout=5,
        )
        # Also check the assertion store for active focus
        result2 = subprocess.run(
            ["plutil", "-p", "/Users/" + subprocess.run(
                ["whoami"], capture_output=True, text=True, timeout=2
            ).stdout.strip() + "/Library/DoNotDisturb/DB/Assertions.json"],
            capture_output=True, text=True, timeout=5,
        )
        # If assertions file has active entries, DND is on
        is_active = "storeAssertionRecords" in result2.stdout and '"lifetimeType" => 0' not in result2.stdout
        return {"active": is_active, "raw": result2.stdout[:200] if is_active else None}
    except Exception as e:
        return {"active": False, "error": str(e)}


async def check_ollama() -> dict:
    """Check if Ollama is reachable and responsive."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
            resp.raise_for_status()
            models = [m["name"] for m in resp.json().get("models", [])]
            return {"status": "ok", "models": models}
    except httpx.ConnectError:
        return {"status": "down", "error": "Cannot connect to Ollama"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def check_database() -> dict:
    """Check database health and stats."""
    import sqlite3

    try:
        conn = sqlite3.connect(str(DB_PATH))

        # Document count
        doc_count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]

        # Last email sync
        row = conn.execute("SELECT value FROM settings WHERE key = 'last_email_sync'").fetchone()
        last_sync = row[0] if row else "never"

        # Active reminders
        reminder_count = conn.execute(
            "SELECT COUNT(*) FROM reminders WHERE status = 'active'"
        ).fetchone()[0]

        # Pending actions
        pending_count = conn.execute(
            "SELECT COUNT(*) FROM pending_actions WHERE status = 'pending'"
        ).fetchone()[0]

        conn.close()
        return {
            "status": "ok",
            "documents": doc_count,
            "last_email_sync": last_sync,
            "active_reminders": reminder_count,
            "pending_actions": pending_count,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


async def _check_calendar() -> dict:
    """Check if Google Calendar API is reachable."""
    try:
        from state.calendar_provider import get_today_events
        events = await get_today_events()
        return {"status": "ok", "events_today": len(events) if events else 0}
    except Exception as e:
        err = str(e)
        if "API has not been used" in err or "is disabled" in err:
            return {"status": "error", "error": "Calendar API not enabled. Enable at: https://console.cloud.google.com/apis/api/calendar-json.googleapis.com/overview"}
        return {"status": "error", "error": err[:200]}


async def _check_gmail() -> dict:
    """Check if Gmail API is reachable."""
    try:
        from actions.gmail import search_emails
        result = await search_emails("in:inbox", max_results=1)
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "error": str(e)[:200]}


async def _check_spotify() -> dict:
    """Check if Spotify API is reachable."""
    try:
        from actions.spotify import get_recently_played
        await get_recently_played(limit=1)
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "error": str(e)[:200]}


async def _check_claude() -> dict:
    """Check if Claude/Anthropic API is reachable via configured LLM client."""
    try:
        from llm_client import get_llm_client, call_llm_sync
        client, client_type = get_llm_client()
        # Minimal call to verify the connection works
        call_llm_sync(client, client_type, "claude-haiku-4-5-20251001", "", "ping", max_tokens=5)
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "error": str(e)[:200]}


async def _check_github() -> dict:
    """Check if GitHub CLI is authenticated."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", "auth", "status",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode == 0:
            return {"status": "ok"}
        return {"status": "error", "error": stderr.decode().strip()[:200]}
    except FileNotFoundError:
        return {"status": "not_configured", "error": "gh CLI not installed"}
    except Exception as e:
        return {"status": "error", "error": str(e)[:200]}


async def run_health_check() -> dict:
    """Run all health checks and return a combined report."""
    import asyncio as _aio

    ollama = await check_ollama()
    db = check_database()

    # Check integrations in parallel (with timeouts)
    async def _safe(name, coro, timeout=15):
        try:
            return name, await _aio.wait_for(coro, timeout=timeout)
        except _aio.TimeoutError:
            return name, {"status": "error", "error": f"timed out ({timeout}s)"}
        except Exception as e:
            return name, {"status": "error", "error": str(e)[:200]}

    integration_checks = await _aio.gather(
        _safe("calendar", _check_calendar()),
        _safe("gmail", _check_gmail()),
        _safe("spotify", _check_spotify()),
        _safe("claude", _check_claude()),
        _safe("github", _check_github()),
    )
    integrations = dict(integration_checks)

    # Check OAuth tokens
    try:
        from oauth_utils import check_all_tokens
        tokens = check_all_tokens()
        unhealthy = [t for t in tokens if t["status"] not in ("healthy", "refreshed")]
        integrations["oauth"] = {
            "status": "ok" if not unhealthy else "degraded",
            "healthy": len(tokens) - len(unhealthy),
            "total": len(tokens),
            "unhealthy": [t["name"] for t in unhealthy],
        }
    except Exception as e:
        integrations["oauth"] = {"status": "error", "error": str(e)[:200]}

    # Determine overall status
    issues = []
    if ollama["status"] != "ok":
        issues.append(f"Ollama: {ollama.get('error', 'unavailable')}")
    if db["status"] != "ok":
        issues.append(f"Database: {db.get('error', 'unavailable')}")
    for name, result in integrations.items():
        if result.get("status") not in ("ok", "not_configured"):
            issues.append(f"{name.capitalize()}: {result.get('error', 'unavailable')}")

    # Check if email sync is stale (> 24 hours)
    if db.get("last_email_sync") and db["last_email_sync"] != "never":
        try:
            last = datetime.fromisoformat(db["last_email_sync"])
            tz = ZoneInfo(TIMEZONE)
            now = datetime.now(tz)
            if last.tzinfo is None:
                last = last.replace(tzinfo=tz)
            hours_ago = (now - last).total_seconds() / 3600
            if hours_ago > 24:
                issues.append(f"Email sync stale ({hours_ago:.0f}h ago)")
        except (ValueError, TypeError):
            pass

    overall = "ok" if not issues else "degraded"

    return {
        "status": overall,
        "issues": issues,
        "ollama": ollama,
        "database": db,
        "integrations": integrations,
    }


async def run_startup_self_test() -> dict:
    """Run comprehensive startup self-test of all subsystems.

    Checks: DB, Ollama, OAuth tokens, GitHub auth, scheduler readiness.
    Returns dict with per-subsystem status and overall pass/fail.
    """
    results = {}

    # 1. Database
    db = check_database()
    results["database"] = db

    # 2. Ollama
    ollama = await check_ollama()
    results["ollama"] = ollama

    # 3. OAuth tokens
    try:
        from oauth_utils import check_all_tokens
        token_results = check_all_tokens()
        unhealthy = [t for t in token_results if t["status"] not in ("healthy", "refreshed")]
        results["oauth"] = {
            "status": "ok" if not unhealthy else "degraded",
            "tokens": token_results,
            "unhealthy_count": len(unhealthy),
        }
    except Exception as e:
        results["oauth"] = {"status": "error", "error": str(e)}

    # 4. GitHub auth (for self-healing PRs)
    import subprocess
    try:
        proc = subprocess.run(
            ["gh", "auth", "status"], capture_output=True, text=True, timeout=10
        )
        results["github"] = {
            "status": "ok" if proc.returncode == 0 else "not_authenticated",
        }
    except FileNotFoundError:
        results["github"] = {"status": "not_installed"}
    except Exception as e:
        results["github"] = {"status": "error", "error": str(e)}

    # Overall
    issues = []
    if results["database"]["status"] != "ok":
        issues.append("Database")
    if results["ollama"]["status"] != "ok":
        issues.append("Ollama")
    if results.get("oauth", {}).get("status") != "ok":
        issues.append("OAuth")
    if results.get("github", {}).get("status") != "ok":
        issues.append("GitHub CLI")

    results["overall"] = "ok" if not issues else "degraded"
    results["issues"] = issues
    return results


def format_startup_report(results: dict) -> str:
    """Format startup self-test results for logging or Telegram."""
    lines = ["🔍 Khalil Startup Self-Test\n"]
    status_icons = {"ok": "✅", "degraded": "⚠️", "error": "❌", "down": "❌"}

    for subsystem in ["database", "ollama", "oauth", "github"]:
        info = results.get(subsystem, {})
        icon = status_icons.get(info.get("status", "error"), "❓")
        label = subsystem.capitalize()
        detail = ""
        if subsystem == "database" and info.get("status") == "ok":
            detail = f" ({info.get('documents', 0)} docs)"
        elif subsystem == "ollama" and info.get("status") == "ok":
            detail = f" ({', '.join(info.get('models', [])[:3])})"
        elif subsystem == "oauth":
            detail = f" ({info.get('unhealthy_count', '?')} unhealthy)" if info.get("status") != "ok" else ""
        lines.append(f"  {icon} {label}{detail}")

    overall_icon = "✅" if results["overall"] == "ok" else "⚠️"
    lines.append(f"\n{overall_icon} Overall: {results['overall'].upper()}")
    if results.get("issues"):
        lines.append(f"Issues: {', '.join(results['issues'])}")
    return "\n".join(lines)


def run_pipeline_smoke_test() -> dict:
    """Verify the agent pipeline is wired correctly after restart.

    No LLM calls, no network — all deterministic checks.
    Catches broken imports, changed function signatures, missing tools.
    """
    results = {}
    issues = []

    # 1. Intent classification
    try:
        from intent import classify_intent, Intent
        result = classify_intent("Build me a presentation", has_active_task=False)
        results["intent"] = {"status": "ok" if result == Intent.TASK else "failed"}
        if result != Intent.TASK:
            issues.append("Intent: 'Build presentation' not classified as TASK")
    except Exception as e:
        results["intent"] = {"status": "error", "error": str(e)[:100]}
        issues.append(f"Intent: {e}")

    # 2. PhaseTracker
    try:
        from server import _PhaseTracker
        p = _PhaseTracker(is_artifact=True)
        tc, _, prompt = p.get_config(0, [{"function": {"name": "generate_file"}}])
        results["phase_tracker"] = {"status": "ok" if tc == "auto" and prompt is None else "failed"}
        if tc != "auto":
            issues.append("PhaseTracker: unexpected tool_choice at iteration 0")
    except Exception as e:
        results["phase_tracker"] = {"status": "error", "error": str(e)[:100]}
        issues.append(f"PhaseTracker: {e}")

    # 3. Tool catalog — generate_file and search_knowledge present
    try:
        from tool_catalog import _CORE_TOOLS
        has_gen = "generate_file" in _CORE_TOOLS
        has_search = "search_knowledge" in _CORE_TOOLS
        results["tool_catalog"] = {
            "status": "ok" if has_gen and has_search else "failed",
            "core_tools": len(_CORE_TOOLS),
        }
        if not has_gen:
            issues.append("ToolCatalog: generate_file missing from core tools")
        if not has_search:
            issues.append("ToolCatalog: search_knowledge missing from core tools")
    except Exception as e:
        results["tool_catalog"] = {"status": "error", "error": str(e)[:100]}
        issues.append(f"ToolCatalog: {e}")

    # 4. Verification layer
    try:
        from verification import detect_hallucinated_tools
        detected = detect_hallucinated_tools("[Called tool: test]\nresults")
        results["verification"] = {"status": "ok" if detected else "failed"}
        if not detected:
            issues.append("Verification: hallucination detection not working")
    except Exception as e:
        results["verification"] = {"status": "error", "error": str(e)[:100]}
        issues.append(f"Verification: {e}")

    # 5. Circuit breaker isolation
    try:
        from server import _cb_claude_fg, _cb_claude_bg
        isolated = _cb_claude_fg is not _cb_claude_bg
        results["circuit_breakers"] = {"status": "ok" if isolated else "failed"}
        if not isolated:
            issues.append("CircuitBreakers: fg and bg are the same instance")
    except Exception as e:
        results["circuit_breakers"] = {"status": "error", "error": str(e)[:100]}
        issues.append(f"CircuitBreakers: {e}")

    results["overall"] = "ok" if not issues else "degraded"
    results["issues"] = issues
    return results


def format_pipeline_smoke_report(results: dict) -> str:
    """Format pipeline smoke test results."""
    status_icons = {"ok": "✅", "failed": "❌", "error": "❌"}
    lines = []
    for subsystem in ["intent", "phase_tracker", "tool_catalog", "verification", "circuit_breakers"]:
        info = results.get(subsystem, {})
        icon = status_icons.get(info.get("status", "error"), "❓")
        lines.append(f"  {icon} {subsystem.replace('_', ' ').title()}")
    overall_icon = "✅" if results["overall"] == "ok" else "❌"
    lines.append(f"\n{overall_icon} Pipeline: {results['overall'].upper()}")
    return "\n".join(lines)


async def generate_self_check_message() -> str | None:
    """Generate a self-check notification if there are issues. Returns None if all clear."""
    report = await run_health_check()

    if report["status"] == "ok":
        return None

    lines = ["⚠️ Khalil Self-Check — Issues Detected\n"]
    for issue in report["issues"]:
        lines.append(f"  • {issue}")

    db = report["database"]
    if db["status"] == "ok":
        lines.append(f"\n📊 DB: {db['documents']} docs, {db['active_reminders']} reminders")

    return "\n".join(lines)
