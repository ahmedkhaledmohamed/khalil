"""Scheduled task definitions for Khalil.

All send_* functions accept a `channel` (channels.Channel protocol) and `chat_id`
instead of a platform-specific bot object, keeping scheduling decoupled from Telegram.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from channels import Channel

log = logging.getLogger("khalil.scheduler")


def _record_digest_sent(digest_type: str):
    """Record that a digest was sent (for engagement tracking)."""
    try:
        from learning import record_signal
        record_signal("digest_sent", {"type": digest_type})
    except Exception:
        pass


def _record_scheduler_failure(task_name: str, error: str):
    """#22: Record scheduler task failures for self-healing detection."""
    try:
        from learning import record_signal
        record_signal("scheduler_task_failure", {
            "task": task_name,
            "error": str(error)[:500],
        })
    except Exception:
        pass


async def sync_emails():
    """Pull new emails, embed, and index into knowledge base."""
    from actions.gmail_sync import sync_new_emails

    try:
        result = await sync_new_emails()
        log.info("Email sync complete: %s", result)
    except Exception as e:
        log.error("Email sync failed: %s", e)
        _record_scheduler_failure("email_sync", e)


async def send_morning_brief(channel: "Channel", chat_id: int, ask_claude_fn):
    """Generate and send morning brief."""
    from scheduler.digests import generate_morning_brief

    # M9: Smart timing — delay if Ahmed isn't typically active at this hour
    try:
        from scheduler.proactive import is_good_time_for_alert
        if not is_good_time_for_alert("morning_brief"):
            log.info("Morning brief deferred: not a good time based on learned patterns")
            return
    except Exception:
        pass  # Fall through to default behavior

    try:
        brief = await generate_morning_brief(ask_claude_fn)
        await channel.send_message(chat_id, brief)
        _record_digest_sent("morning_brief")
        log.info("Morning brief sent successfully")
    except Exception as e:
        log.error(f"Failed to send morning brief: {e}")
        _record_scheduler_failure("morning_brief", e)


async def send_financial_alert(channel: "Channel", chat_id: int, ask_claude_fn):
    """Generate and send financial alert if anything is time-sensitive."""
    from scheduler.digests import generate_financial_alert

    try:
        alert = await generate_financial_alert(ask_claude_fn)
        if alert:
            await channel.send_message(chat_id, alert)
            _record_digest_sent("financial_alert")
            log.info("Financial alert sent")
        else:
            log.info("Financial alert check: nothing urgent")
    except Exception as e:
        log.error(f"Failed to generate financial alert: {e}")
        _record_scheduler_failure("financial_alert", e)


async def send_career_alert(channel: "Channel", chat_id: int):
    """Run job scraper and send results if there are new matches."""
    from scheduler.digests import generate_career_alert

    try:
        alert = await generate_career_alert()
        if alert:
            await channel.send_message(chat_id, alert)
            _record_digest_sent("career_alert")
            log.info("Career alert sent")
        else:
            log.info("Career alert: nothing new")
    except Exception as e:
        log.error(f"Failed to generate career alert: {e}")
        _record_scheduler_failure("career_alert", e)


async def send_weekly_summary(channel: "Channel", chat_id: int, ask_claude_fn):
    """Generate and send weekly summary."""
    from scheduler.digests import generate_weekly_summary

    try:
        summary = await generate_weekly_summary(ask_claude_fn)
        await channel.send_message(chat_id, summary)
        _record_digest_sent("weekly_summary")
        log.info("Weekly summary sent")
    except Exception as e:
        log.error(f"Failed to send weekly summary: {e}")
        _record_scheduler_failure("weekly_summary", e)


async def send_friday_reflection(channel: "Channel", chat_id: int, ask_claude_fn):
    """Generate and send Friday end-of-week reflection."""
    from scheduler.digests import generate_friday_reflection

    try:
        reflection = await generate_friday_reflection(ask_claude_fn)
        await channel.send_message(chat_id, reflection)
        _record_digest_sent("friday_reflection")
        log.info("Friday reflection sent")
    except Exception as e:
        log.error(f"Failed to send Friday reflection: {e}")
        _record_scheduler_failure("friday_reflection", e)


async def run_reflection(channel: "Channel", chat_id: int, ask_claude_fn):
    """Run weekly reflection and notify about non-auto-applied insights."""
    from learning import run_weekly_reflection

    try:
        insights = await run_weekly_reflection(ask_claude_fn)
        if not insights:
            log.info("Weekly reflection: no insights generated")
            return

        # Notify about pending insights that need user review
        pending = [i for i in insights if i.get("auto_apply") is False or i.get("category") == "autonomy"]
        if pending:
            text = f"🧠 Weekly Reflection — {len(insights)} insights\n\n"
            for i in pending:
                text += f"#{i['id']} [{i['category']}]\n  {i['summary']}\n  → /learn apply {i['id']} | /learn dismiss {i['id']}\n\n"
            await channel.send_message(chat_id, text)

        log.info("Weekly reflection complete: %d insights", len(insights))
    except Exception as e:
        log.error(f"Weekly reflection failed: {e}")
        _record_scheduler_failure("weekly_reflection", e)


async def run_micro_reflection(ask_claude_fn, channel: "Channel | None" = None, chat_id: int = None, bot=None):
    """Run daily micro-reflection + self-healing check."""
    from learning import run_daily_micro_reflection, detect_recurring_failures

    # Backward compat: accept bot kwarg, prefer channel
    _channel = channel

    try:
        insights = await run_daily_micro_reflection(ask_claude_fn)
        log.info("Daily micro-reflection: %d insights", len(insights))
    except Exception as e:
        log.error(f"Daily micro-reflection failed: {e}")

    # Check for recurring failures that need self-healing
    try:
        triggers = detect_recurring_failures()
        if triggers and _channel and chat_id:
            from healing import run_self_healing
            await run_self_healing(triggers, _channel, chat_id)
    except Exception as e:
        log.error(f"Self-healing check failed: {e}")
        _record_scheduler_failure("self_healing_check", e)


async def poll_dev_state(channel: "Channel", chat_id: int):
    """Poll dev environment state and notify on changes."""
    try:
        from actions.terminal import poll_and_diff, format_state_changes
        changes = await poll_and_diff()
        if changes:
            message = format_state_changes(changes)
            await channel.send_message(chat_id, message)
            log.info("Dev state changes detected: %d", len(changes))
    except Exception as e:
        log.debug("Dev state poll failed: %s", e)
        _record_scheduler_failure("dev_state_poll", e)
