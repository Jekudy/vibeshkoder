from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from bot.config import settings
from bot.db.engine import async_session
from bot.db.models import IntroRefreshTracking
from bot.db.repos.application import ApplicationRepo
from bot.db.repos.intro import IntroRepo
from bot.db.repos.user import UserRepo
from bot.html_escape import html_escape
from bot.services.forget_cascade import cascade_worker_tick
from bot.services.invite_worker import process_invite_outbox
from bot.texts import (
    ADMIN_NUDGE_MSG,
    NUDGE_MSG,
    REFRESH_PROMPT,
    REJECTED_MSG,
)

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="UTC")


def format_admin_nudge(name: str, username: str, app_id: int) -> str:
    return ADMIN_NUDGE_MSG.format(
        name=html_escape(name),
        username=html_escape(username),
        app_id=app_id,
    )


async def _log_vouch_deadline_cas_lost(session, app_id: int, branch: str) -> None:
    observed_app = await ApplicationRepo.get(session, app_id)
    logger.info(
        "scheduler.cas_lost",
        extra={
            "app_id": app_id,
            "branch": branch,
            "observed_status": observed_app.status if observed_app else None,
        },
    )


async def check_vouch_deadlines(bot: Bot) -> None:
    """Check pending applications for 48h nudge and 72h auto-reject."""
    async with async_session() as session:
        # 72h auto-reject
        apps_to_reject = await ApplicationRepo.get_pending_older_than(
            session, settings.VOUCH_TIMEOUT_HOURS
        )
        for app in apps_to_reject:
            rejected = await ApplicationRepo.update_status_if(
                session,
                app.id,
                expected_from="pending",
                new_status="rejected",
                rejected_at=datetime.now(timezone.utc),
            )
            if not rejected:
                logger.info(
                    "Skipping auto-reject for app %s — status changed since SELECT",
                    app.id,
                )
                continue

            # Delete questionnaire message from chat
            if app.questionnaire_message_id:
                try:
                    await bot.delete_message(
                        chat_id=settings.COMMUNITY_CHAT_ID,
                        message_id=app.questionnaire_message_id,
                    )
                except Exception:
                    logger.warning(
                        "Failed to delete message %s for rejected app %s",
                        app.questionnaire_message_id,
                        app.id,
                    )
            # DM applicant
            try:
                await bot.send_message(chat_id=app.user_id, text=REJECTED_MSG)
            except Exception:
                logger.warning("Failed to DM user %s about rejection", app.user_id)

        # 48h nudge (only apps not yet nudged and not yet rejected above)
        apps_to_nudge = await ApplicationRepo.get_pending_created_older_than(
            session, settings.NUDGE_TIMEOUT_HOURS
        )
        for app in apps_to_nudge:
            if app.status != "pending":
                continue
            # Nudge newcomer
            if app.nudged_newcomer_at is None:
                try:
                    await bot.send_message(chat_id=app.user_id, text=NUDGE_MSG)
                except Exception:
                    logger.warning("Failed to nudge user %s", app.user_id)
                nudged = await ApplicationRepo.update_status_if(
                    session,
                    app.id,
                    expected_from="pending",
                    new_status="pending",
                    nudged_newcomer_at=datetime.now(timezone.utc),
                )
                if not nudged:
                    await _log_vouch_deadline_cas_lost(session, app.id, "nudge")
                    continue

            # Notify admins
            if app.notified_admin_at is None:
                user = await UserRepo.get(session, app.user_id)
                name = user.first_name if user else "Unknown"
                username = user.username or "no_username" if user else "unknown"
                for admin_id in settings.ADMIN_IDS:
                    try:
                        await bot.send_message(
                            chat_id=admin_id,
                            text=format_admin_nudge(name, username, app.id),
                        )
                    except Exception:
                        logger.warning("Failed to notify admin %s", admin_id)
                notified = await ApplicationRepo.update_status_if(
                    session,
                    app.id,
                    expected_from="pending",
                    new_status="pending",
                    notified_admin_at=datetime.now(timezone.utc),
                )
                if not notified:
                    await _log_vouch_deadline_cas_lost(session, app.id, "notify")
                    continue

        await session.commit()


async def check_intro_refresh(bot: Bot) -> None:
    """Daily job: remind members with stale intros to refresh."""
    async with async_session() as session:
        stale_intros = await IntroRepo.get_stale_intros(session, settings.INTRO_REFRESH_DAYS)
        now = datetime.now(timezone.utc)

        for intro in stale_intros:
            # Check if tracking record exists
            result = await session.execute(
                select(IntroRefreshTracking).where(
                    IntroRefreshTracking.user_id == intro.user_id,
                    IntroRefreshTracking.completed.is_(False),
                )
            )
            tracking = result.scalar_one_or_none()

            if tracking is None:
                # Start new cycle
                tracking = IntroRefreshTracking(
                    user_id=intro.user_id,
                    cycle_started_at=now,
                    reminders_sent=0,
                    phase="daily",
                    completed=False,
                )
                session.add(tracking)
                await session.flush()

            if tracking.completed:
                continue

            # Determine if we should send a reminder today
            should_send = False

            if tracking.phase == "daily":
                if tracking.reminders_sent < 5:
                    if (
                        tracking.last_reminder_at is None
                        or (now - tracking.last_reminder_at).days >= 1
                    ):
                        should_send = True
                else:
                    # Move to every_2_days
                    tracking.phase = "every_2_days"
                    await session.flush()

            if tracking.phase == "every_2_days":
                if tracking.reminders_sent < 8:  # 5 daily + 3 every_2_days
                    if (
                        tracking.last_reminder_at is None
                        or (now - tracking.last_reminder_at).days >= 2
                    ):
                        should_send = True
                else:
                    tracking.phase = "done"
                    tracking.completed = True
                    await session.flush()
                    continue

            if should_send:
                try:
                    await bot.send_message(chat_id=intro.user_id, text=REFRESH_PROMPT)
                    tracking.reminders_sent += 1
                    tracking.last_reminder_at = now
                    await session.flush()
                except Exception:
                    logger.warning(
                        "Failed to send refresh reminder to user %s",
                        intro.user_id,
                    )

        await session.commit()


async def sync_google_sheets() -> None:
    """Sync intros with Google Sheets (full bi-directional sync)."""
    try:
        from bot.services.sheets import full_sync

        await full_sync()
    except ImportError:
        logger.debug("gspread not installed — skipping Google Sheets sync")
    except Exception:
        logger.exception("Google Sheets sync failed")


def start_scheduler(bot: Bot) -> None:
    """Configure and start the scheduler."""
    scheduler.add_job(
        process_invite_outbox,
        "interval",
        seconds=30,
        args=[bot],
        id="process_invite_outbox",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )
    scheduler.add_job(
        check_vouch_deadlines,
        "interval",
        minutes=15,
        args=[bot],
        id="check_vouch_deadlines",
        replace_existing=True,
    )
    scheduler.add_job(
        check_intro_refresh,
        "cron",
        hour=10,
        minute=0,
        args=[bot],
        id="check_intro_refresh",
        replace_existing=True,
    )
    scheduler.add_job(
        sync_google_sheets,
        "interval",
        minutes=5,
        id="sync_google_sheets",
        replace_existing=True,
    )
    # T3-04 (#96): forget cascade worker. Default 30s interval matches the
    # invite outbox precedent (lowest-latency persistent queue we operate).
    # Gated by feature flag ``memory.forget.cascade_worker.enabled`` (default
    # OFF) — the tick reads the flag every fire and is a strict no-op when
    # disabled, so this wiring is safe to land in production with the flag off.
    scheduler.add_job(
        cascade_worker_tick,
        "interval",
        seconds=30,
        id="forget_cascade_worker",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )
    scheduler.start()
    logger.info("Scheduler started")


def stop_scheduler() -> None:
    """Shut down the scheduler."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
