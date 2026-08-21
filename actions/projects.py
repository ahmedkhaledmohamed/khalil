"""Project status tracking — read project info from repo files and knowledge base."""

import logging
import os
from pathlib import Path

from config import KHALIL_DIR

log = logging.getLogger("khalil.actions.projects")

# Public defaults contain only Khalil itself. Add user-specific projects with
# KHALIL_PROJECTS="key|Display name|~/path/to/status.md;...".
KNOWN_PROJECTS = {
    "khalil": {
        "name": "Khalil — Personal AI Assistant",
        "file": KHALIL_DIR / "README.md",
    },
}
for _entry in os.getenv("KHALIL_PROJECTS", "").split(";"):
    _parts = [part.strip() for part in _entry.split("|", 2)]
    if len(_parts) == 3 and all(_parts):
        _key, _name, _path = _parts
        KNOWN_PROJECTS[_key.lower()] = {
            "name": _name,
            "file": Path(_path).expanduser(),
        }

# Optional aliases use KHALIL_PROJECT_ALIASES="alias=project-key;...".
ALIASES = {
    "assistant": "khalil",
}
for _entry in os.getenv("KHALIL_PROJECT_ALIASES", "").split(";"):
    if "=" in _entry:
        _alias, _key = (part.strip().lower() for part in _entry.split("=", 1))
        if _alias and _key in KNOWN_PROJECTS:
            ALIASES[_alias] = _key


def resolve_project(name: str) -> str | None:
    """Resolve a project name or alias to a known project key."""
    name_lower = name.lower().strip()
    if name_lower in KNOWN_PROJECTS:
        return name_lower
    if name_lower in ALIASES:
        return ALIASES[name_lower]
    # Partial match
    for key in KNOWN_PROJECTS:
        if name_lower in key or key in name_lower:
            return key
    return None


def get_project_status(key: str) -> str:
    """Read a project's status from its markdown file."""
    project = KNOWN_PROJECTS.get(key)
    if not project:
        return f"Unknown project: {key}"

    path = project["file"]
    if not path.exists():
        return f"{project['name']}\n\nNo project file found at {path}"

    try:
        content = path.read_text(encoding="utf-8")
        return content[:3500]  # Telegram message limit
    except OSError as e:
        return f"Error reading project file: {e}"


def list_projects() -> str:
    """List all known projects with a one-line status."""
    lines = []
    for key, info in KNOWN_PROJECTS.items():
        path = info["file"]
        if path.exists():
            content = path.read_text(encoding="utf-8")
            # Extract status: look for "## Status" section or inline status markers
            status = "—"
            lines_list = content.split("\n")
            for i, line in enumerate(lines_list):
                if line.strip().lower().startswith("## status") or line.strip().lower() == "status":
                    # Grab next non-empty line as the status
                    for j in range(i + 1, min(i + 4, len(lines_list))):
                        candidate = lines_list[j].strip()
                        if candidate and not candidate.startswith("#"):
                            status = candidate
                            break
                    break
            lines.append(f"• **{info['name']}**\n  {status}")
        else:
            lines.append(f"• **{info['name']}** — no file")

    return "\n\n".join(lines)


def get_open_tasks(key: str) -> list[str]:
    """Extract unchecked tasks from a project file."""
    project = KNOWN_PROJECTS.get(key)
    if not project or not project["file"].exists():
        return []

    content = project["file"].read_text(encoding="utf-8")
    tasks = []
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped.startswith("- [ ]"):
            tasks.append(stripped[6:].strip())
    return tasks


def get_stale_projects() -> list[str]:
    """Return project names that have open tasks (for weekly summary inclusion)."""
    stale = []
    for key, info in KNOWN_PROJECTS.items():
        tasks = get_open_tasks(key)
        if tasks:
            stale.append(f"{info['name']}: {len(tasks)} open tasks")
    return stale
