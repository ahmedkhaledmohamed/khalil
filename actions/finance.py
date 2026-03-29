"""Financial dashboard — portfolio summary, deadlines, and alerts.

Reads from finance/ markdown files and the knowledge base.
"""

import logging
from datetime import date
from pathlib import Path

from config import FINANCE_DIR

log = logging.getLogger("pharoclaw.actions.finance")


def _read_file(path: Path, max_chars: int = 5000) -> str:
    """Read a file, return empty string if missing."""
    try:
        return path.read_text(encoding="utf-8")[:max_chars]
    except (FileNotFoundError, OSError):
        return ""


def get_portfolio_summary() -> str:
    """Read the latest portfolio snapshot."""
    # Find the most recent portfolio file
    files = sorted(FINANCE_DIR.glob("portfolio-*.md"), reverse=True)
    if not files:
        return "No portfolio data found."
    return _read_file(files[0])


def get_financial_overview() -> str:
    """Read the financial overview."""
    return _read_file(FINANCE_DIR / "overview.md")


def get_rsu_summary() -> str:
    """Read the RSU/WTS tax summary."""
    # Find the most recent wts-summary file
    files = sorted(FINANCE_DIR.glob("wts-summary-*.md"), reverse=True)
    if not files:
        return ""
    return _read_file(files[0])


def get_deadlines() -> list[dict]:
    """Return upcoming financial deadlines based on the calendar year."""
    today = date.today()
    year = today.year

    deadlines = [
        {
            "date": f"{year}-03-01",
            "item": "RRSP contribution deadline (for previous tax year deduction)",
        },
        {
            "date": f"{year}-04-30",
            "item": "CRA personal tax filing deadline",
        },
        {
            "date": f"{year}-06-15",
            "item": "CRA self-employment tax filing deadline (if applicable)",
        },
        {
            "date": f"{year}-01-01",
            "item": f"TFSA contribution room reset — $7,000 new room ({year})",
        },
        {
            "date": f"{year}-01-01",
            "item": f"FHSA contribution room reset — $8,000 new room ({year})",
        },
        {
            "date": f"{year}-12-31",
            "item": f"RESP CESG deadline — contribute $2,500/child for $500 gov match",
        },
    ]

    # Filter to upcoming or recently passed (within 30 days)
    upcoming = []
    for d in deadlines:
        deadline_date = date.fromisoformat(d["date"])
        days_away = (deadline_date - today).days
        if days_away >= -30:  # include recently passed for awareness
            d["days_away"] = days_away
            d["status"] = "PASSED" if days_away < 0 else ("SOON" if days_away <= 30 else "upcoming")
            upcoming.append(d)

    return sorted(upcoming, key=lambda x: x["date"])


def format_deadlines_text(deadlines: list[dict]) -> str:
    """Format deadlines for Telegram display."""
    if not deadlines:
        return "No upcoming financial deadlines."

    lines = []
    for d in deadlines:
        days = d["days_away"]
        if days < 0:
            time_str = f"{abs(days)}d ago"
            icon = "⚠️"
        elif days == 0:
            time_str = "TODAY"
            icon = "🔴"
        elif days <= 30:
            time_str = f"in {days}d"
            icon = "🟡"
        else:
            time_str = f"in {days}d"
            icon = "🟢"
        lines.append(f"{icon} {d['date']} ({time_str}) — {d['item']}")

    return "\n".join(lines)


def format_dashboard_text() -> str:
    """Format a concise financial dashboard for Telegram."""
    parts = []

    # Deadlines
    deadlines = get_deadlines()
    soon = [d for d in deadlines if d["status"] in ("PASSED", "SOON")]
    if soon:
        parts.append("⏰ Key Deadlines:\n" + format_deadlines_text(soon))

    # Overview highlights
    overview = get_financial_overview()
    if overview:
        # Add personal financial alerts here (e.g., overcontribution warnings)
        if "overcontribution" in overview.lower():
            parts.append("⚠️ RRSP Overcontribution detected — check overview for details")

    # Portfolio summary line
    portfolio = get_portfolio_summary()
    if portfolio and "Total Household" in portfolio:
        for line in portfolio.split("\n"):
            if "Total Household" in line:
                parts.append(f"💰 {line.strip().strip('|').strip()}")
                break

    if not parts:
        return "No financial data available. Check finance/ directory."

    return "\n\n".join(parts)
