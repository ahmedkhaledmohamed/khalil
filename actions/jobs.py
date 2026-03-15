"""Job scraper integration — bridges the standalone job-scraper into Khalil.

Imports core functions from scripts/job-scraper/scraper.py.
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

from config import PERSONAL_REPO_PATH

log = logging.getLogger("khalil.actions.jobs")

# Add job-scraper to path so we can import it
JOB_SCRAPER_DIR = PERSONAL_REPO_PATH / "scripts" / "job-scraper"
SEEN_JOBS_FILE = JOB_SCRAPER_DIR / "seen_jobs.json"


def _get_scraper():
    """Lazy import of scraper module."""
    if str(JOB_SCRAPER_DIR) not in sys.path:
        sys.path.insert(0, str(JOB_SCRAPER_DIR))
    import scraper
    return scraper


def _run_scraper_sync() -> list[dict]:
    """Run the job scraper synchronously. Returns list of new job dicts."""
    scraper = _get_scraper()

    seen_ids = scraper.load_seen_jobs(str(SEEN_JOBS_FILE))

    all_jobs = []
    # Use the API-based scrapers (Greenhouse/Lever) which are most reliable
    for fn in [scraper.scrape_greenhouse_boards, scraper.scrape_lever_boards]:
        try:
            all_jobs.extend(fn())
        except Exception as e:
            log.warning("Scraper %s failed: %s", fn.__name__, e)

    # Also try web scrapers but don't fail on them
    for fn in [scraper.scrape_indeed, scraper.scrape_linkedin, scraper.scrape_ycombinator]:
        try:
            all_jobs.extend(fn())
        except Exception as e:
            log.debug("Web scraper %s failed: %s", fn.__name__, e)

    # Filter and deduplicate
    relevant = [j for j in all_jobs if scraper.is_relevant_job(j)]
    new_jobs = scraper.deduplicate_jobs(relevant, seen_ids)

    # Update seen jobs
    for job in new_jobs:
        seen_ids.add(job.id)
    scraper.save_seen_jobs(str(SEEN_JOBS_FILE), seen_ids)

    # Convert to dicts
    from dataclasses import asdict
    return [asdict(j) for j in new_jobs]


async def fetch_new_jobs() -> list[dict]:
    """Fetch new job matches asynchronously."""
    return await asyncio.to_thread(_run_scraper_sync)


async def get_recent_jobs(limit: int = 10) -> list[dict]:
    """Get recently seen jobs from the seen_jobs file."""
    scraper = _get_scraper()

    if not SEEN_JOBS_FILE.exists():
        return []

    # Run a fresh scrape to get current matches
    return await fetch_new_jobs()


def format_jobs_text(jobs: list[dict], limit: int = 10) -> str:
    """Format job list for Telegram display."""
    if not jobs:
        return "No new job matches found."

    text = f"💼 {len(jobs)} new job match(es):\n\n"
    for j in jobs[:limit]:
        text += f"**{j['title']}**\n"
        text += f"  {j['company']} — {j['location']}\n"
        text += f"  via {j['source']}\n\n"
    return text
