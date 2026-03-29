"""Goals system — reads/writes goals markdown files with checkbox tracking."""

import logging
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from config import GOALS_DIR, TIMEZONE

log = logging.getLogger("pharoclaw.actions.goals")

# Current year's goals file
GOALS_FILE = GOALS_DIR / "2026.md"

# Template for a new goals file (based on existing 2025.md structure)
_TEMPLATE = """# 🎯 2026 Goals

## Q1 (Jan-Mar)

### Career
- [ ]

### Health
- [ ]

### Learning
- [ ]

### Personal
- [ ]

## Q2 (Apr-Jun)

### Career
- [ ]

### Health
- [ ]

### Learning
- [ ]

### Personal
- [ ]

## Q3 (Jul-Sep)

### Career
- [ ]

### Health
- [ ]

### Learning
- [ ]

### Personal
- [ ]

## Q4 (Oct-Dec)

### Career
- [ ]

### Health
- [ ]

### Learning
- [ ]

### Personal
- [ ]

---

## Year-End Review

*To be filled at end of year*

### Wins


### Lessons Learned


### Looking Forward to 2027

"""


def _current_quarter() -> str:
    """Return current quarter label like 'Q1'."""
    tz = ZoneInfo(TIMEZONE)
    month = datetime.now(tz).month
    return f"Q{(month - 1) // 3 + 1}"


def _ensure_goals_file() -> Path:
    """Create goals file from template if it doesn't exist."""
    if not GOALS_FILE.exists():
        GOALS_DIR.mkdir(parents=True, exist_ok=True)
        GOALS_FILE.write_text(_TEMPLATE, encoding="utf-8")
        log.info(f"Created goals file: {GOALS_FILE}")
    return GOALS_FILE


def _parse_goals(content: str) -> dict[str, dict[str, list[dict]]]:
    """Parse goals markdown into structured data.

    Returns: {quarter: {category: [{text, done, line_num}, ...]}}
    """
    goals: dict[str, dict[str, list[dict]]] = {}
    current_quarter = None
    current_category = None

    for i, line in enumerate(content.splitlines()):
        # Quarter heading: ## Q1 (Jan-Mar)
        q_match = re.match(r"^## (Q\d)\b", line)
        if q_match:
            current_quarter = q_match.group(1)
            goals[current_quarter] = {}
            current_category = None
            continue

        # Category heading: ### Career
        cat_match = re.match(r"^### (\w+)", line)
        if cat_match and current_quarter:
            current_category = cat_match.group(1).lower()
            if current_category not in goals[current_quarter]:
                goals[current_quarter][current_category] = []
            continue

        # Checkbox item: - [ ] or - [x]
        item_match = re.match(r"^- \[([ xX])\] (.+)", line)
        if item_match and current_quarter and current_category:
            text = item_match.group(2).strip()
            if text:  # Skip empty checkboxes
                goals[current_quarter][current_category].append({
                    "text": text,
                    "done": item_match.group(1).lower() == "x",
                    "line_num": i,
                })

    return goals


def get_current_goals() -> str:
    """Return all goals for the current quarter with completion stats."""
    _ensure_goals_file()
    content = GOALS_FILE.read_text(encoding="utf-8")
    goals = _parse_goals(content)

    quarter = _current_quarter()
    q_goals = goals.get(quarter, {})

    if not q_goals or all(len(items) == 0 for items in q_goals.values()):
        return f"No goals set for {quarter} yet.\n\nUse /goals add <category> <goal> to add one."

    lines = [f"🎯 {quarter} Goals\n"]

    total = 0
    done = 0
    for category, items in q_goals.items():
        if not items:
            continue
        lines.append(f"  {category.capitalize()}:")
        for i, item in enumerate(items, 1):
            check = "✅" if item["done"] else "⬜"
            lines.append(f"    {i}. {check} {item['text']}")
            total += 1
            if item["done"]:
                done += 1

    if total > 0:
        pct = int(done / total * 100)
        lines.insert(1, f"  Progress: {done}/{total} ({pct}%)\n")

    return "\n".join(lines)


def get_all_goals() -> str:
    """Return goals across all quarters."""
    _ensure_goals_file()
    content = GOALS_FILE.read_text(encoding="utf-8")
    goals = _parse_goals(content)

    if not goals:
        return "No goals found."

    lines = ["🎯 2026 Goals Overview\n"]

    for quarter in sorted(goals.keys()):
        q_goals = goals[quarter]
        total = sum(len(items) for items in q_goals.values())
        done = sum(1 for items in q_goals.values() for item in items if item["done"])
        if total == 0:
            continue
        pct = int(done / total * 100)
        lines.append(f"  {quarter}: {done}/{total} ({pct}%)")
        for category, items in q_goals.items():
            for item in items:
                check = "✅" if item["done"] else "⬜"
                lines.append(f"    {check} [{category}] {item['text']}")

    return "\n".join(lines) if len(lines) > 1 else "No goals set yet."


def add_goal(category: str, text: str) -> str:
    """Add a goal to the current quarter. Returns confirmation message."""
    _ensure_goals_file()
    content = GOALS_FILE.read_text(encoding="utf-8")
    quarter = _current_quarter()
    category_lower = category.lower()

    # Valid categories
    valid = {"career", "health", "learning", "personal"}
    if category_lower not in valid:
        return f"Invalid category '{category}'. Use: {', '.join(sorted(valid))}"

    # Find the insertion point: after the ### Category heading in current quarter
    lines = content.splitlines()
    in_quarter = False
    insert_at = None

    for i, line in enumerate(lines):
        q_match = re.match(r"^## (Q\d)\b", line)
        if q_match:
            in_quarter = q_match.group(1) == quarter
            continue

        if in_quarter:
            cat_match = re.match(r"^### (\w+)", line)
            if cat_match and cat_match.group(1).lower() == category_lower:
                # Found the category — find last checkbox or the empty one
                j = i + 1
                while j < len(lines) and (lines[j].startswith("- [") or lines[j].strip() == ""):
                    if lines[j].strip() == "":
                        break
                    j += 1
                # Replace empty checkbox or insert before blank line
                if j > i + 1 and re.match(r"^- \[[ ]\]\s*$", lines[j - 1]):
                    # Last item is an empty checkbox — replace it
                    lines[j - 1] = f"- [ ] {text}"
                    insert_at = j - 1
                else:
                    # Insert new checkbox
                    lines.insert(j, f"- [ ] {text}")
                    insert_at = j
                break

    if insert_at is None:
        return f"Could not find {quarter} > {category.capitalize()} section in goals file."

    GOALS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info(f"Goal added: [{quarter}/{category_lower}] {text}")
    return f"✅ Added to {quarter} {category.capitalize()}:\n  ⬜ {text}"


def complete_goal(category: str, index: int) -> str:
    """Mark a goal as done by category and 1-based index. Returns confirmation."""
    _ensure_goals_file()
    content = GOALS_FILE.read_text(encoding="utf-8")
    quarter = _current_quarter()
    category_lower = category.lower()

    goals = _parse_goals(content)
    q_goals = goals.get(quarter, {})
    items = q_goals.get(category_lower, [])

    if not items:
        return f"No goals found in {quarter} > {category.capitalize()}."

    if index < 1 or index > len(items):
        return f"Invalid index {index}. {category.capitalize()} has {len(items)} goal(s)."

    item = items[index - 1]
    if item["done"]:
        return f"Goal already completed: {item['text']}"

    # Replace the checkbox on the specific line
    lines = content.splitlines()
    line_num = item["line_num"]
    lines[line_num] = lines[line_num].replace("- [ ]", "- [x]", 1)

    GOALS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info(f"Goal completed: [{quarter}/{category_lower}] {item['text']}")
    return f"✅ Completed: {item['text']}"


def get_goal_summary() -> str:
    """Short summary for use in morning briefs and proactive checks."""
    _ensure_goals_file()
    content = GOALS_FILE.read_text(encoding="utf-8")
    goals = _parse_goals(content)

    quarter = _current_quarter()
    q_goals = goals.get(quarter, {})

    total = sum(len(items) for items in q_goals.values())
    done = sum(1 for items in q_goals.values() for item in items if item["done"])

    if total == 0:
        return f"{quarter}: No goals set"

    pct = int(done / total * 100)
    return f"{quarter}: {done}/{total} goals done ({pct}%)"
