"""Sprint planning and work status — reads planning CSV directly."""

import csv
import logging
import os
from pathlib import Path

from config import WORK_DIR

log = logging.getLogger("pharoclaw.actions.work")

_EMPLOYER = os.getenv("PHAROCLAW_EMPLOYER", "employer")
_PLANNING_FILE = os.getenv("PHAROCLAW_PLANNING_CSV", "planning.csv")
PLANNING_CSV = WORK_DIR / _EMPLOYER / _PLANNING_FILE


def _load_epics() -> list[dict]:
    """Load and clean sprint planning CSV rows."""
    if not PLANNING_CSV.exists():
        return []

    epics = []
    with open(PLANNING_CSV, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Skip error rows
            values = list(row.values())
            if any("#NAME?" in str(v) for v in values if v):
                continue

            desc = row.get("Description of Work (Squad Internal)", "").strip()
            if not desc:
                continue

            epics.append({
                "theme": row.get("Theme", "").strip(),
                "description": desc,
                "status": row.get("Status", "").strip(),
                "priority": row.get("Priority", "").strip(),
                "owner": row.get("Planning RM", "").strip() or row.get("Delivery RM", "").strip(),
                "estimate": row.get("Estimate", "").strip(),
                "percent_complete": row.get("% Complete", "").strip(),
                "epic_id": row.get("Epic or Contribution ID", "").strip(),
                "commitment": row.get("Commitment", "").strip(),
            })
    return epics


def get_sprint_summary() -> str:
    """Sprint dashboard: totals by status and priority."""
    epics = _load_epics()
    if not epics:
        return "No sprint planning data found."

    total = len(epics)

    # By status
    status_counts: dict[str, int] = {}
    for e in epics:
        s = e["status"] or "Unknown"
        status_counts[s] = status_counts.get(s, 0) + 1

    # By priority
    priority_counts: dict[str, int] = {}
    for e in epics:
        p = e["priority"] or "Unset"
        priority_counts[p] = priority_counts.get(p, 0) + 1

    lines = [f"📊 Sprint Dashboard — {total} epics\n"]

    lines.append("By status:")
    for s, c in sorted(status_counts.items(), key=lambda x: -x[1]):
        lines.append(f"  {s}: {c}")

    lines.append("\nBy priority:")
    for p, c in sorted(priority_counts.items()):
        lines.append(f"  {p}: {c}")

    return "\n".join(lines)


def get_p0_epics() -> str:
    """Return P0 and ARC P0 epics."""
    epics = _load_epics()
    p0s = [e for e in epics if "P0" in (e["priority"] or "").upper()]
    if not p0s:
        return "No P0 epics found."

    lines = [f"🔴 P0 Epics ({len(p0s)}):\n"]
    for e in p0s:
        owner = f" [{e['owner']}]" if e["owner"] else ""
        lines.append(f"• [{e['priority']}] {e['description'][:100]}{owner} — {e['status']}")
    return "\n".join(lines)


def get_epics_by_theme(theme: str) -> str:
    """Filter epics by theme (case-insensitive partial match)."""
    epics = _load_epics()
    theme_lower = theme.lower()
    matches = [e for e in epics if theme_lower in (e["theme"] or "").lower()]
    if not matches:
        # List available themes
        themes = sorted(set(e["theme"] for e in epics if e["theme"]))
        return f"No epics for theme '{theme}'.\n\nAvailable themes: {', '.join(themes)}"

    lines = [f"📂 {theme} — {len(matches)} epics:\n"]
    for e in matches:
        owner = f" [{e['owner']}]" if e["owner"] else ""
        lines.append(f"• [{e['priority'] or '?'}] {e['description'][:100]}{owner} — {e['status']}")
    return "\n".join(lines)


def get_epics_by_owner(name: str) -> str:
    """Filter epics by owner name (case-insensitive partial match)."""
    epics = _load_epics()
    name_lower = name.lower()
    matches = [e for e in epics if name_lower in (e["owner"] or "").lower()]
    if not matches:
        owners = sorted(set(e["owner"] for e in epics if e["owner"]))
        return f"No epics for owner '{name}'.\n\nKnown owners: {', '.join(owners)}"

    lines = [f"👤 {name} — {len(matches)} epics:\n"]
    for e in matches:
        lines.append(f"• [{e['priority'] or '?'}] {e['description'][:100]} — {e['status']}")
    return "\n".join(lines)


def get_in_progress() -> str:
    """Return all in-progress epics."""
    epics = _load_epics()
    active = [e for e in epics if "in progress" in (e["status"] or "").lower()]
    if not active:
        return "No in-progress epics."

    lines = [f"🔄 In Progress ({len(active)}):\n"]
    for e in active:
        owner = f" [{e['owner']}]" if e["owner"] else ""
        lines.append(f"• [{e['priority'] or '?'}] {e['description'][:100]}{owner}")
    return "\n".join(lines)
