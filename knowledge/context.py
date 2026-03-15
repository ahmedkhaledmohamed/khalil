"""CONTEXT.md section retriever — structured access to personal context."""

import re
from pathlib import Path

from config import CONTEXT_FILE


_cached_sections: dict[str, str] | None = None


def _parse_sections() -> dict[str, str]:
    """Parse CONTEXT.md into named sections."""
    global _cached_sections
    if _cached_sections is not None:
        return _cached_sections

    if not CONTEXT_FILE.exists():
        return {}

    content = CONTEXT_FILE.read_text(encoding="utf-8")
    sections = {}
    current_key = "preamble"
    current_lines = []

    for line in content.split("\n"):
        header_match = re.match(r"^(#{1,2})\s+(.+)", line)
        if header_match:
            if current_lines:
                sections[current_key.lower()] = "\n".join(current_lines).strip()
            current_key = header_match.group(2).strip()
            current_lines = [line]
        else:
            current_lines.append(line)

    if current_lines:
        sections[current_key.lower()] = "\n".join(current_lines).strip()

    _cached_sections = sections
    return sections


def get_section(name: str) -> str | None:
    """Get a specific section by name (case-insensitive partial match)."""
    sections = _parse_sections()
    name_lower = name.lower()
    for key, value in sections.items():
        if name_lower in key:
            return value
    return None


def get_section_names() -> list[str]:
    """List all available section names."""
    return list(_parse_sections().keys())


def get_relevant_context(query: str, max_chars: int = 3000) -> str:
    """Get context sections relevant to a query."""
    sections = _parse_sections()
    query_lower = query.lower()

    # Keywords → section mapping heuristics
    relevance_keywords = {
        "career": ["career", "work", "spotify", "experience", "role"],
        "family": ["family", "wife", "kids", "heba", "ella", "leo"],
        "finance": ["finance", "investment", "rrsp", "tfsa", "tax", "rsu"],
        "projects": ["project", "zia", "bezier", "tiny grounds", "side"],
        "education": ["education", "university", "degree", "guc"],
        "immigration": ["immigration", "visa", "pr", "citizenship", "canada"],
        "values": ["values", "principles", "believe", "philosophy"],
    }

    matched_sections = []
    for section_keyword, query_keywords in relevance_keywords.items():
        if any(kw in query_lower for kw in query_keywords):
            for key, value in sections.items():
                if section_keyword in key and value not in matched_sections:
                    matched_sections.append(value)

    # If no specific match, return the preamble/summary
    if not matched_sections:
        for key in ["preamble", "summary", "overview", "identity"]:
            if key in sections:
                matched_sections.append(sections[key])
                break

    result = "\n\n---\n\n".join(matched_sections)
    return result[:max_chars]
