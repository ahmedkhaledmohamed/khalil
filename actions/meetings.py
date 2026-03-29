"""Meeting Intelligence — pre-meeting briefs, post-meeting follow-ups, commitment tracking."""

import logging
import re
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from config import DB_PATH, TIMEZONE

log = logging.getLogger("pharoclaw.actions.meetings")

# Title patterns that indicate recurring standups to skip
_STANDUP_PATTERNS = re.compile(
    r"\b(standup|stand-up|daily sync|daily check-in|daily scrum|daily huddle)\b",
    re.IGNORECASE,
)


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_tables(conn: sqlite3.Connection | None = None):
    """Create the commitments table if it doesn't exist."""
    c = conn or _get_conn()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS commitments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_title TEXT NOT NULL,
            person TEXT NOT NULL,
            commitment TEXT NOT NULL,
            due_date TEXT,
            status TEXT DEFAULT 'open',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_commitments_status ON commitments(status);

        CREATE TABLE IF NOT EXISTS meeting_followup_state (
            meeting_key TEXT PRIMARY KEY,
            prompted_at TIMESTAMP,
            responded INTEGER DEFAULT 0
        );
    """)
    if conn is None:
        c.commit()
        c.close()


# --- Task 11.1: Meeting context builder ---


async def build_meeting_context(event: dict) -> str:
    """Build a pre-meeting brief with attendee context and relevant notes.

    Args:
        event: dict with {title, start, end, attendees}

    Returns:
        Formatted meeting brief string.
    """
    from knowledge.search import hybrid_search

    title = event.get("title", "(no title)")
    start = event.get("start", "")
    end = event.get("end", "")
    attendees = event.get("attendees", [])

    sections = []
    sections.append(f"Meeting: {title}")
    sections.append(f"Time: {start} - {end}")

    # Attendee context
    if attendees:
        sections.append(f"\nAttendees ({len(attendees)}):")
        for email in attendees:
            name = email.split("@")[0].replace(".", " ").title()
            sections.append(f"  - {name} ({email})")

            # Search knowledge base for recent interactions with this person
            try:
                results = await hybrid_search(email, limit=3)
                if not results:
                    results = await hybrid_search(name, limit=3)
                if results:
                    snippets = []
                    for r in results[:2]:
                        snippet = (r.get("content") or "")[:150].strip()
                        if snippet:
                            snippets.append(f"    > {r.get('title', '')[:50]}: {snippet}")
                    if snippets:
                        sections.append("    Recent context:")
                        sections.extend(snippets)
            except Exception as e:
                log.debug("Failed to get context for %s: %s", email, e)

    # Topic context — search for the meeting title/topic
    try:
        topic_results = await hybrid_search(title, limit=4)
        if topic_results:
            sections.append("\nRelevant notes:")
            for r in topic_results[:3]:
                snippet = (r.get("content") or "")[:200].strip()
                sections.append(f"  - [{r.get('category', '')}] {r.get('title', '')[:60]}")
                if snippet:
                    sections.append(f"    {snippet}")
    except Exception as e:
        log.debug("Failed to get topic context for '%s': %s", title, e)

    # Suggested talking points
    sections.append("\nSuggested talking points:")
    sections.append(f"  - Review agenda/purpose of '{title}'")
    if attendees:
        sections.append(f"  - Follow up on any open items with attendees")

    # Check for open commitments related to attendees
    open_commitments = _get_commitments_for_attendees(attendees)
    if open_commitments:
        sections.append("\nOpen commitments with attendees:")
        for c in open_commitments[:5]:
            status_note = ""
            if c["due_date"]:
                try:
                    due = datetime.strptime(c["due_date"], "%Y-%m-%d")
                    if due.date() < datetime.now(ZoneInfo(TIMEZONE)).date():
                        status_note = " (OVERDUE)"
                except ValueError:
                    pass
            sections.append(
                f"  - {c['person']}: {c['commitment']}"
                f"{' (due ' + c['due_date'] + ')' if c['due_date'] else ''}{status_note}"
            )

    return "\n".join(sections)


def _get_commitments_for_attendees(attendees: list[str]) -> list[dict]:
    """Get open commitments involving any of the attendees."""
    if not attendees:
        return []
    try:
        conn = _get_conn()
        # Match by email or name extracted from email
        placeholders = ",".join("?" * len(attendees))
        names = [a.split("@")[0].replace(".", " ").lower() for a in attendees]
        all_terms = attendees + names

        conditions = " OR ".join(["LOWER(person) LIKE ?" for _ in all_terms])
        params = [f"%{t.lower()}%" for t in all_terms]

        rows = conn.execute(
            f"SELECT meeting_title, person, commitment, due_date, status "
            f"FROM commitments WHERE status = 'open' AND ({conditions}) "
            f"ORDER BY due_date",
            params,
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        log.debug("Failed to get commitments for attendees: %s", e)
        return []


# --- Task 11.2: Helper for standup detection ---


def is_standup_meeting(title: str) -> bool:
    """Check if a meeting title matches recurring standup patterns."""
    return bool(_STANDUP_PATTERNS.search(title))


def should_send_meeting_brief(event: dict) -> bool:
    """Determine if a meeting warrants a pre-meeting brief.

    Criteria: 3+ attendees OR flagged important, AND not a recurring standup.
    """
    title = event.get("title", "")
    attendees = event.get("attendees", [])

    # Skip standups
    if is_standup_meeting(title):
        return False

    # 3+ attendees
    if len(attendees) >= 3:
        return True

    # Check for importance markers in title
    importance_markers = ("important", "review", "planning", "strategy", "1:1", "exec")
    if any(marker in title.lower() for marker in importance_markers):
        return True

    return False


# --- Task 11.3: Post-meeting follow-up capture ---


def get_recently_ended_meetings() -> list[dict]:
    """Find meetings that ended in the last 10 minutes.

    Used by the scheduler to trigger post-meeting prompts.
    """
    from state.calendar_provider import _fetch_today_events_sync

    try:
        events = _fetch_today_events_sync()
    except Exception as e:
        log.debug("Failed to fetch events for follow-up check: %s", e)
        return []

    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    recently_ended = []

    for event in events:
        if event.get("all_day"):
            continue
        try:
            end_dt = datetime.fromisoformat(event["end"].replace("Z", "+00:00")).astimezone(tz)
        except (ValueError, TypeError):
            continue

        # Ended within last 10 minutes
        minutes_since_end = (now - end_dt).total_seconds() / 60
        if 0 <= minutes_since_end <= 10:
            recently_ended.append(event)

    return recently_ended


def should_prompt_followup(meeting_key: str) -> bool:
    """Check if we should prompt for this meeting (haven't already prompted)."""
    try:
        conn = _get_conn()
        row = conn.execute(
            "SELECT prompted_at FROM meeting_followup_state WHERE meeting_key = ?",
            (meeting_key,),
        ).fetchone()
        conn.close()
        return row is None
    except Exception:
        return True


def record_followup_prompt(meeting_key: str):
    """Record that we prompted for a meeting follow-up."""
    try:
        conn = _get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO meeting_followup_state (meeting_key, prompted_at) VALUES (?, ?)",
            (meeting_key, datetime.now(ZoneInfo(TIMEZONE)).isoformat()),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.debug("Failed to record follow-up prompt: %s", e)


def make_meeting_key(event: dict) -> str:
    """Create a unique key for a meeting (date + title)."""
    title = event.get("title", "")
    start = event.get("start", "")[:10]  # date portion
    return f"{start}|{title}"


def parse_action_items(text: str) -> list[dict]:
    """Parse action items from user response text.

    Handles formats like:
    - "Ahmed: finish the design doc by Friday"
    - "- Review PR by 2026-03-25"
    - "John to send the report"
    """
    items = []
    lines = text.strip().split("\n")

    for line in lines:
        line = line.strip().lstrip("-*").strip()
        if not line:
            continue

        person = "me"
        commitment = line
        due_date = None

        # Try to extract "Person: task" or "Person to task"
        colon_match = re.match(r"^([A-Za-z\s]+?):\s+(.+)", line)
        to_match = re.match(r"^([A-Za-z\s]+?)\s+to\s+(.+)", line)

        if colon_match:
            person = colon_match.group(1).strip()
            commitment = colon_match.group(2).strip()
        elif to_match:
            person = to_match.group(1).strip()
            commitment = to_match.group(2).strip()

        # Try to extract due date
        date_match = re.search(r"by\s+(\d{4}-\d{2}-\d{2})", commitment)
        if date_match:
            due_date = date_match.group(1)
        else:
            # Natural dates: "by Friday", "by tomorrow", "by next week"
            day_match = re.search(
                r"by\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow)",
                commitment,
                re.IGNORECASE,
            )
            if day_match:
                due_date = _resolve_natural_date(day_match.group(1))

        items.append({
            "person": person,
            "commitment": commitment,
            "due_date": due_date,
        })

    return items


def _resolve_natural_date(day_str: str) -> str | None:
    """Convert a day name to the next occurrence as YYYY-MM-DD."""
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)

    if day_str.lower() == "tomorrow":
        return (now + timedelta(days=1)).strftime("%Y-%m-%d")

    day_names = {
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
        "friday": 4, "saturday": 5, "sunday": 6,
    }
    target = day_names.get(day_str.lower())
    if target is None:
        return None

    days_ahead = target - now.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return (now + timedelta(days=days_ahead)).strftime("%Y-%m-%d")


# --- Task 11.4: Commitment tracker ---


def add_commitment(
    meeting_title: str,
    person: str,
    commitment: str,
    due_date: str | None = None,
) -> dict:
    """Store a commitment from a meeting."""
    conn = _get_conn()
    cursor = conn.execute(
        "INSERT INTO commitments (meeting_title, person, commitment, due_date, status) "
        "VALUES (?, ?, ?, ?, 'open')",
        (meeting_title, person, commitment, due_date),
    )
    conn.commit()
    cid = cursor.lastrowid
    conn.close()

    log.info("Commitment #%d added: %s -> %s", cid, person, commitment[:60])
    return {
        "id": cid,
        "meeting_title": meeting_title,
        "person": person,
        "commitment": commitment,
        "due_date": due_date,
        "status": "open",
    }


def list_commitments(status: str = "open") -> list[dict]:
    """List commitments by status."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, meeting_title, person, commitment, due_date, status, created_at "
        "FROM commitments WHERE status = ? ORDER BY due_date, created_at",
        (status,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def complete_commitment(commitment_id: int) -> bool:
    """Mark a commitment as done."""
    conn = _get_conn()
    result = conn.execute(
        "UPDATE commitments SET status = 'done' WHERE id = ? AND status = 'open'",
        (commitment_id,),
    )
    conn.commit()
    conn.close()
    return result.rowcount > 0


def get_overdue_commitments(days_past: int = 2) -> list[dict]:
    """Get commitments that are past due by N+ days."""
    tz = ZoneInfo(TIMEZONE)
    cutoff = (datetime.now(tz) - timedelta(days=days_past)).strftime("%Y-%m-%d")
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, meeting_title, person, commitment, due_date, status, created_at "
        "FROM commitments WHERE status = 'open' AND due_date IS NOT NULL AND due_date < ? "
        "ORDER BY due_date",
        (cutoff,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def format_commitments(commitments: list[dict]) -> str:
    """Format commitments for display."""
    if not commitments:
        return "No open commitments."

    lines = []
    for c in commitments:
        due = f" (due {c['due_date']})" if c.get("due_date") else ""
        overdue = ""
        if c.get("due_date"):
            try:
                due_dt = datetime.strptime(c["due_date"], "%Y-%m-%d")
                if due_dt.date() < datetime.now(ZoneInfo(TIMEZONE)).date():
                    days_late = (datetime.now(ZoneInfo(TIMEZONE)).date() - due_dt.date()).days
                    overdue = f" OVERDUE {days_late}d"
            except ValueError:
                pass
        lines.append(
            f"  #{c['id']} [{c['person']}] {c['commitment'][:80]}{due}{overdue}\n"
            f"      from: {c['meeting_title'][:50]}"
        )

    return "\n".join(lines)
