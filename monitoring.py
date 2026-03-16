"""Health checks and monitoring for Khalil subsystems."""

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from config import DB_PATH, OLLAMA_URL, TIMEZONE

log = logging.getLogger("khalil.monitoring")


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


async def run_health_check() -> dict:
    """Run all health checks and return a combined report."""
    ollama = await check_ollama()
    db = check_database()

    # Determine overall status
    issues = []
    if ollama["status"] != "ok":
        issues.append(f"Ollama: {ollama.get('error', 'unavailable')}")
    if db["status"] != "ok":
        issues.append(f"Database: {db.get('error', 'unavailable')}")

    # Check if email sync is stale (> 24 hours)
    if db.get("last_email_sync") and db["last_email_sync"] != "never":
        try:
            last = datetime.fromisoformat(db["last_email_sync"])
            tz = ZoneInfo(TIMEZONE)
            now = datetime.now(tz)
            # Make last tz-aware if needed
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
