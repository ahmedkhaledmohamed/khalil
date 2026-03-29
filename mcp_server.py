#!/usr/bin/env python3
"""MCP server for Claude Code integration.

Exposes PharoClaw's knowledge base to Claude Code sessions via MCP protocol.
Run as: python3 mcp_server.py (stdio transport, invoked by Claude Code)
"""

import asyncio
import os
import sys

# Add pharoclaw directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

from knowledge.search import hybrid_search, keyword_search, get_stats
from knowledge.context import get_section, get_section_names, get_relevant_context

mcp = FastMCP("pharoclaw", instructions="PharoClaw — Ahmed's personal knowledge base. Search archives, get context sections, and retrieve life timeline events.")


@mcp.tool()
async def search_knowledge(query: str, category: str | None = None) -> str:
    """Search Ahmed's personal knowledge base (emails, Drive, timeline, context).

    Args:
        query: Search query (e.g. "RRSP overcontribution", "Spotify messaging")
        category: Optional filter (e.g. "email:finance", "life:timeline", "personal:context")
    """
    results = await hybrid_search(query, limit=8, category=category)
    if not results:
        return "No results found."

    output = []
    for r in results:
        match_type = r.get("match_type", "unknown")
        output.append(
            f"[{r['category']}] ({match_type}) {r['title']}\n{r['content'][:300]}"
        )
    return f"Found {len(results)} results:\n\n" + "\n\n---\n\n".join(output)


@mcp.tool()
async def get_context(section_name: str) -> str:
    """Get a specific section from Ahmed's CONTEXT.md personal profile.

    Args:
        section_name: Section name or partial match (e.g. "career", "family", "values", "projects")
    """
    section = get_section(section_name)
    if section:
        return section
    names = get_section_names()
    return f"Section '{section_name}' not found. Available sections: {', '.join(names)}"


@mcp.tool()
async def get_timeline(year: str | None = None) -> str:
    """Get life timeline events from Ahmed's email history.

    Args:
        year: Optional year filter (e.g. "2024", "2022")
    """
    query = f"timeline {year}" if year else "life timeline career immigration"
    results = await hybrid_search(query, limit=10, category="life:timeline")
    if not results:
        # Fall back to keyword search
        kw_results = keyword_search(year or "timeline", limit=10, category="life:timeline")
        if not kw_results:
            return "No timeline events found."
        results = kw_results

    output = []
    for r in results:
        output.append(f"{r['title']}\n{r['content'][:200]}")
    return f"Timeline events ({len(results)}):\n\n" + "\n\n---\n\n".join(output)


@mcp.tool()
async def knowledge_stats() -> str:
    """Get statistics about PharoClaw's knowledge base."""
    stats = get_stats()
    lines = [f"Total documents: {stats['total_documents']}", "", "By category:"]
    for cat, count in stats["by_category"].items():
        lines.append(f"  {cat}: {count}")
    return "\n".join(lines)


@mcp.tool()
async def create_reminder(text: str, due_at: str) -> str:
    """Create a PharoClaw reminder that will fire at the specified time.

    Args:
        text: Reminder text (e.g. "Review sprint planning")
        due_at: When to fire — ISO format (e.g. "2026-03-17T09:00:00") or relative (e.g. "in 2 hours", "tomorrow 9am")
    """
    from actions.reminders import create_reminder as _create, _parse_relative_time
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from config import TIMEZONE

    # Try relative time first, then ISO
    dt = _parse_relative_time(due_at)
    if not dt:
        try:
            dt = datetime.fromisoformat(due_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo(TIMEZONE))
        except ValueError:
            return f"Could not parse time: '{due_at}'. Use ISO format or relative (e.g. 'in 2 hours', 'tomorrow 9am')."

    result = _create(text, dt)
    return f"Reminder created: #{result['id']} '{result['text']}' due {result['due_at']}"


@mcp.tool()
async def add_goal(category: str, text: str) -> str:
    """Add a goal to the current quarter's goals.

    Args:
        category: Goal category — one of: career, health, learning, personal
        text: Goal description (e.g. "Ship native messaging MVP")
    """
    from actions.goals import add_goal as _add
    return _add(category, text)


@mcp.tool()
async def complete_goal(category: str, index: int) -> str:
    """Mark a goal as done by category and number.

    Args:
        category: Goal category — one of: career, health, learning, personal
        index: 1-based index of the goal within the category (use search_knowledge or get_goals to find it)
    """
    from actions.goals import complete_goal as _complete
    return _complete(category, index)


@mcp.tool()
async def search_work(query: str, filter: str | None = None) -> str:
    """Search sprint planning data by keyword, theme, owner, or priority.

    Args:
        query: Search term (e.g. "P0", "Push Notifications", "Mesfin")
        filter: Optional filter type — one of: "p0", "progress", "theme", "owner"
    """
    from actions.work import (
        get_sprint_summary, get_p0_epics, get_epics_by_theme,
        get_epics_by_owner, get_in_progress,
    )

    if filter == "p0" or query.upper() == "P0":
        return get_p0_epics()
    elif filter == "progress":
        return get_in_progress()
    elif filter == "theme":
        return get_epics_by_theme(query)
    elif filter == "owner":
        return get_epics_by_owner(query)
    else:
        # Try theme first, fall back to owner, then keyword search
        theme_result = get_epics_by_theme(query)
        if "No epics for theme" not in theme_result:
            return theme_result
        owner_result = get_epics_by_owner(query)
        if "No epics for owner" not in owner_result:
            return owner_result
        # Fall back to knowledge base search
        results = await hybrid_search(query, limit=5, category="work:planning")
        if results:
            return "\n\n".join(f"[{r['category']}] {r['title']}\n{r['content'][:300]}" for r in results)
        return f"No work data found for '{query}'."


@mcp.tool()
async def sprint_summary() -> str:
    """Get the sprint dashboard — totals by status and priority."""
    from actions.work import get_sprint_summary
    return get_sprint_summary()


@mcp.tool()
async def financial_dashboard() -> str:
    """Get financial overview with upcoming deadlines."""
    from actions.finance import get_financial_overview, get_deadlines, format_deadlines_text
    overview = get_financial_overview()
    deadlines = get_deadlines()
    deadline_text = format_deadlines_text(deadlines)
    return f"{overview}\n\n{deadline_text}"


@mcp.tool()
async def run_nudge() -> str:
    """Run proactive checks — surfaces things that need attention.

    Detects: stale goals, stale projects, passed financial deadlines,
    stale portfolio, overdue reminders.
    """
    from scheduler.proactive import run_proactive_checks

    findings = run_proactive_checks()
    if not findings:
        return "All clear — nothing needs attention."
    return "Things that need attention:\n\n" + "\n\n".join(findings)


@mcp.tool()
async def get_morning_brief_data() -> str:
    """Get raw morning brief context without LLM synthesis.

    Returns weather, reminders, calendar, work priorities, goals, and deadlines
    as structured data for use in Claude Code conversations.
    """
    from datetime import date, datetime
    from zoneinfo import ZoneInfo
    from config import TIMEZONE

    today = date.today()
    sections = [f"Date: {today.isoformat()}, {today.strftime('%A')}"]

    # Weather
    try:
        from scheduler.digests import _get_weather
        weather = await _get_weather()
        if weather:
            sections.append(f"Weather: {weather}")
    except Exception:
        pass

    # Reminders
    try:
        from actions.reminders import list_reminders
        reminders = list_reminders()
        today_iso = today.isoformat()
        due_today = [r for r in reminders if r["due_at"][:10] == today_iso]
        upcoming = [r for r in reminders if r["due_at"] > today_iso][:5]
        if due_today:
            sections.append("Due today:\n" + "\n".join(f"  - {r['text']}" for r in due_today))
        if upcoming:
            sections.append("Upcoming reminders:\n" + "\n".join(
                f"  - {r['text']} ({r['due_at'][:16]})" for r in upcoming
            ))
    except Exception:
        pass

    # Calendar
    try:
        from actions.calendar import get_today_events, format_events_text
        events = await get_today_events()
        if events:
            sections.append(f"Calendar ({len(events)} events):\n{format_events_text(events)}")
    except Exception:
        pass

    # Work
    try:
        from actions.work import get_sprint_summary, get_p0_epics
        sections.append(f"Work:\n{get_sprint_summary()}")
        sections.append(f"P0 Epics:\n{get_p0_epics()}")
    except Exception:
        pass

    # Goals
    try:
        from actions.goals import get_goal_summary
        sections.append(f"Goals: {get_goal_summary()}")
    except Exception:
        pass

    # Financial deadlines
    try:
        from actions.finance import get_deadlines
        deadlines = get_deadlines()
        urgent = [d for d in deadlines if -7 <= d["days_away"] <= 14]
        if urgent:
            sections.append("Financial deadlines:\n" + "\n".join(
                f"  - {'PASSED: ' if d['status'] == 'PASSED' else ''}{d['item']} ({d['date']})"
                for d in urgent
            ))
    except Exception:
        pass

    return "\n\n".join(sections)


@mcp.tool()
async def healing_status() -> str:
    """Get recent self-healing activity — patches applied, failures detected."""
    import sqlite3
    from config import DB_PATH

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, summary, evidence, recommendation, status, created_at "
        "FROM insights WHERE category = 'self_heal' ORDER BY created_at DESC LIMIT 10"
    ).fetchall()
    conn.close()

    if not rows:
        return "No self-healing activity recorded."

    lines = [f"Recent self-healing activity ({len(rows)} entries):"]
    for r in rows:
        status_icon = {"applied": "OK", "pending": "PENDING", "dismissed": "SKIP"}.get(r["status"], r["status"])
        lines.append(f"  [{status_icon}] #{r['id']}: {r['summary']} ({r['created_at'][:10]})")
    return "\n".join(lines)


@mcp.tool()
async def extension_status() -> str:
    """Get installed extensions and their health — invocations, errors, error rates."""
    import json
    from config import EXTENSIONS_DIR
    from learning import get_extension_health

    extensions = []
    if EXTENSIONS_DIR and EXTENSIONS_DIR.exists():
        for manifest_path in sorted(EXTENSIONS_DIR.glob("*.json")):
            try:
                manifest = json.loads(manifest_path.read_text())
                extensions.append({
                    "name": manifest.get("name", manifest_path.stem),
                    "command": manifest.get("command", ""),
                    "description": manifest.get("description", ""),
                })
            except Exception:
                extensions.append({"name": manifest_path.stem, "error": "invalid manifest"})

    if not extensions:
        lines = ["No extensions installed."]
    else:
        lines = [f"Installed extensions ({len(extensions)}):"]
        for ext in extensions:
            if "error" in ext:
                lines.append(f"  - {ext['name']} (ERROR: {ext['error']})")
            else:
                lines.append(f"  - {ext['name']}: {ext['description']} (/{ext['command']})")

    # Health stats
    health = get_extension_health(days=7)
    if health:
        lines.append("\nHealth (last 7 days):")
        for h in health:
            lines.append(f"  - {h['extension']}: {h['invocations']} calls, {h['error_rate_pct']}% error rate")

    return "\n".join(lines)


@mcp.tool()
async def audit_log(limit: int = 10) -> str:
    """Get recent audit log entries — actions taken, their results, and autonomy levels.

    Args:
        limit: Maximum entries to return (default 10)
    """
    import sqlite3
    from config import DB_PATH

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT action_type, description, result, autonomy_level, timestamp "
        "FROM audit_log ORDER BY timestamp DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()

    if not rows:
        return "No audit log entries."

    lines = [f"Recent audit log ({len(rows)} entries):"]
    for r in rows:
        result_short = (r["result"] or "")[:60]
        lines.append(
            f"  [{r['timestamp'][:16]}] {r['action_type']}: {r['description'][:80]} "
            f"(autonomy={r['autonomy_level']}, result={result_short})"
        )
    return "\n".join(lines)


@mcp.tool()
async def learned_preferences() -> str:
    """Get current learned preferences — behavioral adaptations PharoClaw has made."""
    from learning import list_preferences

    prefs = list_preferences()
    if not prefs:
        return "No learned preferences yet."

    lines = [f"Learned preferences ({len(prefs)}):"]
    for p in prefs:
        lines.append(f"  - {p['key']}: {p['value']} (confidence={p['confidence']}, updated={p['updated_at'][:10]})")
    return "\n".join(lines)


# --- Dev Environment Tools ---

@mcp.tool()
async def dev_environment_status() -> str:
    """Get current development environment — Cursor windows, iTerm sessions, frontmost app.

    Shows what projects are open in Cursor (with CPU/memory), active terminal
    sessions with running processes, and which app is in focus.
    """
    from actions.terminal import (
        get_cursor_status, format_cursor_status,
        get_terminal_status, format_terminal_status,
        get_frontmost_app,
    )

    cursor_status, terminal_status, frontmost = await asyncio.gather(
        get_cursor_status(),
        get_terminal_status(),
        get_frontmost_app(),
    )

    lines = [format_cursor_status(cursor_status)]
    lines.append("")
    lines.append(format_terminal_status(terminal_status))
    if frontmost:
        lines.append(f"\nFrontmost: {frontmost}")

    return "\n".join(lines)


@mcp.tool()
async def cursor_open_file(path: str, line: int | None = None) -> str:
    """Open a file in Cursor IDE, optionally at a specific line.

    Args:
        path: File or folder path to open (absolute or relative)
        line: Optional line number to jump to
    """
    from actions.terminal import cursor_open
    result = await cursor_open(path, line)
    if result["success"]:
        loc = f"{path}:{line}" if line else path
        return f"Opened in Cursor: {loc}"
    return f"Failed to open in Cursor: {result['error']}"


@mcp.tool()
async def list_terminal_sessions() -> str:
    """List active iTerm2 terminal sessions with their running processes.

    Shows each session's TTY, running command, PID, and elapsed time.
    """
    from actions.terminal import get_terminal_status, format_terminal_status
    status = await get_terminal_status()
    return format_terminal_status(status)


@mcp.tool()
async def run_in_terminal(command: str, session: str = "current") -> str:
    """Send a command to an iTerm2 terminal session.

    The command is sanitized before injection (no chaining operators, no subshells).
    This action is logged to the audit trail.

    Args:
        command: Shell command to execute in the terminal
        session: TTY path (e.g. "/dev/ttys003") or "current" for active session
    """
    from actions.terminal import send_to_iterm

    result = await send_to_iterm(command, session)
    if result["success"]:
        # Log to audit
        try:
            import sqlite3
            from config import DB_PATH
            from datetime import datetime
            from zoneinfo import ZoneInfo
            from config import TIMEZONE
            conn = sqlite3.connect(str(DB_PATH))
            conn.execute(
                "INSERT INTO audit_log (action_type, description, result, autonomy_level, timestamp) VALUES (?, ?, ?, ?, ?)",
                ("terminal_exec_mcp", f"Sent to terminal: {command}", "ok", "mcp",
                 datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S")),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass
        return f"Command sent to terminal ({session}): {command}"
    return f"Failed: {result['error']}"


@mcp.tool()
async def cursor_diff_files(file1: str, file2: str) -> str:
    """Open a diff view in Cursor comparing two files.

    Args:
        file1: Path to first file
        file2: Path to second file
    """
    from actions.terminal import cursor_diff
    result = await cursor_diff(file1, file2)
    if result["success"]:
        return f"Diff opened in Cursor: {file1} vs {file2}"
    return f"Failed: {result['error']}"


@mcp.tool()
async def cursor_terminal_status() -> str:
    """List all terminals in Cursor's integrated terminal panel.

    Requires the pharoclaw-terminal-bridge extension to be installed in Cursor.
    Returns terminal names, PIDs, active status, and workspace info.
    """
    from actions.terminal import get_cursor_terminal_status, format_cursor_terminal_status
    status = await get_cursor_terminal_status()
    return format_cursor_terminal_status(status)


@mcp.tool()
async def cursor_terminal_send(command: str, target: str = "0") -> str:
    """Send a command to a terminal in Cursor's integrated terminal.

    Requires the pharoclaw-terminal-bridge extension.

    Args:
        command: The command to send (e.g., "npm test", "python server.py")
        target: Terminal name or index (default "0" for first terminal)
    """
    from actions.terminal import bridge_send_command
    result = await bridge_send_command(target, command)
    if result.get("error"):
        return f"Failed: {result['error']}"
    return f"Sent to Cursor terminal [{result.get('terminal', target)}]: {command}"


@mcp.tool()
async def cursor_terminal_create(name: str = "PharoClaw", command: str = None, cwd: str = None) -> str:
    """Create a new terminal in Cursor's integrated terminal panel.

    Requires the pharoclaw-terminal-bridge extension.

    Args:
        name: Display name for the terminal
        command: Optional command to run after creation
        cwd: Optional working directory for the terminal
    """
    from actions.terminal import bridge_create_terminal
    result = await bridge_create_terminal(name=name, cwd=cwd, command=command)
    if result.get("error"):
        return f"Failed: {result['error']}"
    msg = f"Created Cursor terminal: {result.get('name', name)}"
    if command:
        msg += f"\nRunning: {command}"
    return msg


@mcp.tool()
async def cursor_terminal_output(target: str, lines: int = 50) -> str:
    """Read recent output from a Cursor terminal.

    Requires the pharoclaw-terminal-bridge extension with output capture enabled.

    Args:
        target: Terminal name to read output from
        lines: Number of recent lines to return (default 50)
    """
    from actions.terminal import bridge_get_output
    result = await bridge_get_output(target, lines)
    if result.get("error"):
        return f"Failed: {result['error']}"
    output = result.get("output", [])
    if not output:
        return f"No output captured for terminal '{target}'. Note: output capture requires the proposed VS Code API."
    return f"Terminal '{target}' output ({len(output)} lines):\n" + "\n".join(output)


if __name__ == "__main__":
    mcp.run(transport="stdio")
