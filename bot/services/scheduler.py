from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from bot.config import settings
from bot.db.engine import async_session
from bot.db.models import IntroRefreshTracking
from bot.db.repos.application import ApplicationRepo
from bot.db.repos.intro import IntroRepo
from bot.db.repos.llm_usage_ledger import LedgerRepo
from bot.db.repos.user import UserRepo
from bot.html_escape import html_escape
from bot.db.repos.butler_action import ButlerActionRepo
from bot.services.extractor import extraction_scheduler_tick
from bot.services.forget_cascade import cascade_worker_tick
from bot.services.graph_projector import default_projector_config, project_incremental
from bot.services.graph_purge_worker import graph_purge_worker_tick
from bot.services.invite_worker import process_invite_outbox
from bot.services.llm_gateway import (
    LiveExtractCandidatesGateway,
    load_gateway_config,
    resolve_provider,
)
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


async def run_extraction_scheduler_tick() -> None:
    """T6-03 wrapper — wires the Phase 6 extraction tick into apscheduler.

    PHASE6_PLAN.md §5.B + T6-03 design §3-§4:

    * Opens a fresh ``async_session()``.
    * Builds ``LiveExtractCandidatesGateway`` locally via env-derived config
      (same pattern as the QA handler at ``bot/handlers/qa.py:332-343``).
    * Calls ``extraction_scheduler_tick`` from ``bot.services.extractor``,
      which short-circuits when the feature flag is OFF (default).
    * Commits on success; logs + ignores any exception so the scheduler
      keeps running (per Phase 5 invite-outbox precedent).

    The flag default is OFF so this job is a strict no-op until an operator
    flips ``memory.extraction.scheduler.enabled`` in the ``feature_flags``
    table. The 15-min interval mirrors ``check_vouch_deadlines``; the
    actual tick runtime is dominated by gateway HTTP latency (~5-15s) when
    the flag is on.
    """
    try:
        async with async_session() as session:
            try:
                cfg = load_gateway_config()
                provider = resolve_provider(cfg.provider)
                gateway = LiveExtractCandidatesGateway(
                    ledger_repo=LedgerRepo(), provider=provider, config=cfg
                )
                await extraction_scheduler_tick(session, gateway=gateway)
                await session.commit()
            except Exception:
                # Rollback the per-tick transaction so the scheduler can
                # retry on the next fire without poisoning the session.
                try:
                    await session.rollback()
                except Exception:
                    logger.exception("extraction_scheduler_tick rollback failed")
                logger.exception("extraction_scheduler_tick crashed")
    except Exception:
        # Catch-all — the wrapper must NEVER let an exception propagate to
        # apscheduler, which would mark the job as failed and stop firing.
        logger.exception("extraction_scheduler_tick session setup failed")


# ─── T7-04: Phase 7 daily-digest scheduler jobs ─────────────────────────────


async def digest_daily_job(bot: Bot) -> None:
    """Daily digest run trigger — fires at ``DIGEST_HOUR_MSK`` (Europe/Moscow).

    Strict no-op when the ``memory.digests.daily.enabled`` feature flag is
    OFF. Window is yesterday 00:00 MSK..today 00:00 MSK (stored as UTC).

    Wraps the synthesis pipeline in try/except so apscheduler never stops
    firing — orchestrator output is persisted to ``digests``/``digest_runs``
    rows regardless of outcome.
    """
    try:
        async with async_session() as session:
            from bot.db.repos.feature_flag import FeatureFlagRepo

            flag_enabled = await FeatureFlagRepo.get(
                session, "memory.digests.daily.enabled"
            )
            if not flag_enabled:
                logger.info("digest_daily_job: flag disabled, skipping")
                return

            # Compute window from MSK midnight boundaries — stored as UTC.
            from zoneinfo import ZoneInfo

            from bot.services.digests import load_digest_config, run_digest

            msk = ZoneInfo("Europe/Moscow")
            now_msk = datetime.now(tz=msk)
            today_msk_midnight = now_msk.replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            yesterday_msk_midnight = today_msk_midnight.replace(
                day=today_msk_midnight.day
            )
            from datetime import timedelta as _td

            yesterday_msk_midnight = today_msk_midnight - _td(days=1)
            window_start = yesterday_msk_midnight.astimezone(timezone.utc)
            window_end = today_msk_midnight.astimezone(timezone.utc)

            digest_config = load_digest_config()
            gateway_config = load_gateway_config()
            try:
                digest = await run_digest(
                    session,
                    type="daily",
                    window_start=window_start,
                    window_end=window_end,
                    ledger_repo=LedgerRepo(),
                    provider=resolve_provider(gateway_config.provider),
                    config=gateway_config,
                    digest_config=digest_config,
                )
                await session.commit()
                logger.info(
                    "digest_daily_job: ws=%s we=%s digest_id=%s status=%s",
                    window_start.isoformat(),
                    window_end.isoformat(),
                    digest.id,
                    digest.status,
                )

                # AC #6: auto-post draft digest to destination channel.
                if digest.status == "draft":
                    try:
                        from bot.services.digest_publisher import publish_digest

                        await publish_digest(
                            session,
                            bot=bot,
                            digest=digest,
                            digest_config=digest_config,
                        )
                        await session.commit()
                    except Exception:
                        try:
                            await session.rollback()
                        except Exception:
                            logger.exception(
                                "digest_daily_job: publish rollback failed"
                            )
                        logger.exception("digest_daily_job: publish_digest failed")

            except Exception:
                try:
                    await session.rollback()
                except Exception:
                    logger.exception("digest_daily_job rollback failed")
                logger.exception("digest_daily_job: run_digest crashed")
    except Exception:
        # Catch-all — see precedent in extraction_scheduler_tick.
        logger.exception("digest_daily_job: session setup failed")


async def digest_stale_posting_reaper_job() -> None:
    """Reap orphan ``digests.status='posting'`` rows from publisher crashes.

    Runs every 5 minutes (NOT gated by feature flag — even after a flag
    flip-OFF, any in-flight ``posting`` / ``approved_for_publish`` rows
    must still be cleaned up).

    Two reaping branches in a single UPDATE (PHASE8_PLAN.md §5.G stale-approved
    reaper extension — kept in this single job rather than a new scheduler
    entry):

    1. ``status='posting'`` rows older than ``posting_started_at`` + 2 min
       (Phase 7 §5.K baseline — publisher crash window between row-lock
       acquisition and ``bot.send_message`` completion).
    2. ``status='approved_for_publish'`` rows older than ``approved_at`` +
       5 min (FHR HIGH-2 / Phase 8 §5.G — orchestrator crash window between
       ``approve_digest`` commit and ``publish_digest`` dispatch). Without
       this branch, the weekly digest is stuck forever and admin
       ``/digest_approve <id>`` retries are blocked by the pre-flight guard.

    The 5-min approved threshold is wider than posting's 2-min because the
    approve-then-publish sequence normally completes in <30s; 5 min handles
    network hiccups + Telegram retries.

    Per row, ``error_text`` is set to ``stale_posting_reaper`` or
    ``stale_approved_reaper`` depending on the source status (the RETURNING
    branch reads it back).
    """
    try:
        from sqlalchemy import text as _text

        async with async_session() as session:
            result = await session.execute(
                _text("""
                    UPDATE digests
                    SET status='failed',
                        error_text=CASE
                          WHEN status='posting' THEN 'stale_posting_reaper'
                          WHEN status='approved_for_publish'
                              THEN 'stale_approved_reaper'
                        END,
                        posting_started_at=NULL,
                        updated_at=now()
                    WHERE
                        (status='posting'
                         AND posting_started_at < now() - interval '2 minutes')
                        OR
                        (status='approved_for_publish'
                         AND approved_at < now() - interval '5 minutes')
                    RETURNING id, error_text
                """)
            )
            rows = result.fetchall()
            if not rows:
                await session.commit()
                return
            for row in rows:
                # ``error_text`` was set by the CASE expression in the UPDATE;
                # use the same value for the audit row so the trail matches.
                await session.execute(
                    _text(
                        "INSERT INTO digest_runs "
                        "(digest_id, status, error_text, started_at, finished_at) "
                        "VALUES (:id, 'failed', :err, now(), now())"
                    ),
                    {"id": row.id, "err": row.error_text},
                )
                logger.warning(
                    "digest_stale_posting_reaper: reaped digest_id=%s "
                    "error_text=%s",
                    row.id,
                    row.error_text,
                )
            await session.commit()
    except Exception:
        # Reaper must NEVER let an exception propagate — apscheduler would
        # stop firing the job. Log + continue.
        logger.exception("digest_stale_posting_reaper crashed")


# ─── T8-05: Phase 8 weekly-digest scheduler jobs ────────────────────────────


async def digest_weekly_job(bot: Bot) -> None:
    """Weekly digest run trigger — fires Mon ``DIGEST_WEEKLY_HOUR_MSK`` MSK.

    Per PHASE8_PLAN.md §5.G. Strict no-op when ``memory.digests.weekly.enabled``
    is OFF. Window is the most recently completed ISO week:
    ``last_monday 00:00 MSK..this_monday 00:00 MSK`` (stored as UTC).

    H8 stagger: registered at 09:15 MSK so the LLM gateway has 15 minutes of
    slack past the daily 09:00 cron — avoids contention on Mondays.

    H4 status-aware match block: ``run_digest`` may return either a fresh
    ``draft`` (happy path → transition to ``awaiting_review`` + admin DM)
    or — via the per-(type,ws,we) idempotency lock — an EXISTING row in any
    status. Each branch handles the discovered terminal state explicitly;
    cron NEVER auto-regenerates rejected runs. The wrapping try/except
    chain ensures apscheduler never sees an exception — every outcome is
    persisted via ``digests`` / ``digest_runs`` rows.

    T8-04 (parallel sprint) owns ``bot.services.digest_review``. If the
    module isn't merged yet the import is guarded and the job logs a
    warning — the draft row is still created, just not advanced.
    """
    try:
        async with async_session() as session:
            from bot.db.repos.feature_flag import FeatureFlagRepo

            flag_enabled = await FeatureFlagRepo.get(
                session, "memory.digests.weekly.enabled"
            )
            if not flag_enabled:
                logger.info("digest_weekly_job: flag disabled, skipping")
                return

            from zoneinfo import ZoneInfo

            from bot.services.digests import load_digest_config, run_digest

            msk = ZoneInfo("Europe/Moscow")
            now_msk = datetime.now(tz=msk)
            today_msk_midnight = now_msk.replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            # ISO: Mon=1, Tue=2, ..., Sun=7. days_since_monday=0 when cron
            # fires on Mon. The "most recent Monday 00:00 MSK" is therefore
            # ``today_msk_midnight - timedelta(days=days_since_monday)``.
            days_since_monday = today_msk_midnight.isoweekday() - 1
            this_monday_msk = today_msk_midnight - timedelta(days=days_since_monday)
            last_monday_msk = this_monday_msk - timedelta(days=7)

            window_start = last_monday_msk.astimezone(timezone.utc)
            window_end = this_monday_msk.astimezone(timezone.utc)

            digest_config = load_digest_config()
            gateway_config = load_gateway_config()
            try:
                digest = await run_digest(
                    session,
                    type="weekly",
                    window_start=window_start,
                    window_end=window_end,
                    ledger_repo=LedgerRepo(),
                    provider=resolve_provider(gateway_config.provider),
                    config=gateway_config,
                    digest_config=digest_config,
                )
                await session.commit()
                logger.info(
                    "digest_weekly_job: ws=%s we=%s digest_id=%s status=%s",
                    window_start.isoformat(),
                    window_end.isoformat(),
                    digest.id,
                    digest.status,
                )

                # H4 status-aware match block — see §5.G.
                if digest.status == "draft":
                    # Happy path: fresh draft → review-gate transition.
                    try:
                        from bot.services.digest_review import (
                            transition_to_awaiting_review,
                        )
                    except ImportError:
                        logger.warning(
                            "digest_weekly_job: bot.services.digest_review not "
                            "merged yet (T8-04 parallel sprint); weekly draft "
                            "id=%s created but cannot transition to "
                            "awaiting_review",
                            digest.id,
                        )
                        return

                    try:
                        await transition_to_awaiting_review(
                            session, digest_id=digest.id
                        )
                        await session.commit()
                        # §5.J: weekly_awaiting_review handoff DM.
                        from bot.services.digest_admin_notify import (
                            notify_admins_digest_failure,
                        )

                        await notify_admins_digest_failure(
                            bot,
                            digest_id=digest.id,
                            status="awaiting_review",
                            error_text="weekly_awaiting_review",
                        )
                    except Exception:
                        try:
                            await session.rollback()
                        except Exception:
                            logger.exception(
                                "digest_weekly_job: review transition "
                                "rollback failed"
                            )
                        logger.exception(
                            "digest_weekly_job: transition_to_awaiting_review failed"
                        )
                elif digest.status in (
                    "awaiting_review",
                    "approved_for_publish",
                    "posting",
                    "posted",
                ):
                    # Idempotency hit: prior cycle already advanced this
                    # window. No-op — admin is already in the loop.
                    logger.info(
                        "digest_weekly_job: existing %s row id=%s, no-op",
                        digest.status,
                        digest.id,
                    )
                elif digest.status in ("rejected_by_admin", "rejected_by_reaper"):
                    # Cron does NOT auto-regenerate. Admin must run
                    # /digest_now weekly --regenerate.
                    logger.info(
                        "digest_weekly_job: window has rejected run id=%s "
                        "status=%s, awaiting admin /digest_now weekly --regenerate",
                        digest.id,
                        digest.status,
                    )
                elif digest.status in ("failed", "cost_exceeded"):
                    # Surface to admin DM.
                    from bot.services.digest_admin_notify import (
                        notify_admins_digest_failure,
                    )

                    await notify_admins_digest_failure(
                        bot,
                        digest_id=digest.id,
                        status=digest.status,
                        error_text=(
                            digest.error_text
                            or "weekly_digest_window_in_error_state"
                        ),
                    )
                    logger.error(
                        "digest_weekly_job: window in error state %s id=%s, "
                        "admin DM dispatched",
                        digest.status,
                        digest.id,
                    )
                elif digest.status in ("skipped", "skipped_no_destination"):
                    logger.info(
                        "digest_weekly_job: window status=%s id=%s, no action",
                        digest.status,
                        digest.id,
                    )
                elif digest.status in ("redacted", "redacted_edit_failed"):
                    logger.info(
                        "digest_weekly_job: window redacted id=%s status=%s",
                        digest.id,
                        digest.status,
                    )
                else:
                    logger.error(
                        "digest_weekly_job: unexpected status %s for digest_id=%s",
                        digest.status,
                        digest.id,
                    )

            except Exception:
                try:
                    await session.rollback()
                except Exception:
                    logger.exception("digest_weekly_job rollback failed")
                logger.exception("digest_weekly_job: run_digest crashed")
    except Exception:
        # Catch-all — apscheduler must never see an exception or it would
        # mark the job as failed and stop firing.
        logger.exception("digest_weekly_job: session setup failed")


async def digest_stale_review_reaper_job(bot: Bot) -> None:
    """48h DM + 7d auto-reject for ``awaiting_review`` weekly digests.

    Per PHASE8_PLAN.md §5.G + §13 AC7. Runs every 30 min, NOT flag-gated —
    even after a flag flip-OFF, any rows already in ``awaiting_review`` must
    still be reaped to bound queue size.

    Two passes per tick:

    1. **7d auto-reject pass FIRST.** Guarded UPDATE transitions
       ``awaiting_review`` rows older than 7 days to ``rejected_by_reaper``.
       Audit row inserted with ``error_text='review_deadline_exceeded'``.
       Admin DM fires with ``error_text='review_7d_auto_rejected'``. Doing
       the 7d pass FIRST means an 8d row terminates this tick rather than
       getting a 48h DM (which would then be terminated next tick).

    2. **48h DM pass.** M4 guarded UPDATE: SELECT candidate rows where
       ``awaiting_review_at < now() - 48h`` AND the ``[48h_notified]`` marker
       is absent from ``review_notes``. For each, attempt a guarded UPDATE
       gated on ``status='awaiting_review'`` — rowcount=0 means an admin
       advanced the state between SELECT and UPDATE; skip the DM cleanly.
       Rowcount=1 → DM fires AFTER the marker landed, guaranteeing at-most-
       once notification per row.

    The wrapping try/except so a reaper crash does not disrupt other
    scheduler jobs.
    """
    try:
        from sqlalchemy import text as _text

        async with async_session() as session:
            # Step 1: 7d auto-reject pass. Guarded UPDATE — type + status +
            # age all enforced in WHERE; rowcount drives audit insert and
            # admin DM.
            seven_d_rows = (
                await session.execute(
                    _text(
                        """
                        UPDATE digests
                        SET status='rejected_by_reaper',
                            review_notes=COALESCE(review_notes,'') || '[stale_7d]',
                            updated_at=now()
                        WHERE type='weekly'
                          AND status='awaiting_review'
                          AND awaiting_review_at < now() - interval '7 days'
                        RETURNING id
                        """
                    )
                )
            ).fetchall()
            seven_d_ids = [row.id for row in seven_d_rows]
            for digest_id in seven_d_ids:
                await session.execute(
                    _text(
                        "INSERT INTO digest_runs (digest_id, status, "
                        "error_text, started_at, finished_at) "
                        "VALUES (:id, 'rejected_by_reaper', "
                        "'review_deadline_exceeded', now(), now())"
                    ),
                    {"id": digest_id},
                )
                logger.warning(
                    "digest_stale_review_reaper: rejected digest_id=%s", digest_id
                )
            await session.commit()
            # 7d-pass admin DMs — dispatched after commit so the row is durable
            # before the side-effect.
            if seven_d_ids:
                from bot.services.digest_admin_notify import (
                    notify_admins_digest_failure,
                )

                for digest_id in seven_d_ids:
                    await notify_admins_digest_failure(
                        bot,
                        digest_id=digest_id,
                        status="rejected_by_reaper",
                        error_text="review_7d_auto_rejected",
                    )

            # Step 2: 48h notify pass — DM at most once per row via marker.
            candidate_rows = (
                await session.execute(
                    _text(
                        """
                        SELECT id
                        FROM digests
                        WHERE type='weekly'
                          AND status='awaiting_review'
                          AND awaiting_review_at < now() - interval '48 hours'
                          AND (review_notes IS NULL
                               OR review_notes NOT LIKE '%[48h_notified]%')
                        """
                    )
                )
            ).fetchall()

            notified_ids: list[int] = []
            for row in candidate_rows:
                # M4: guarded UPDATE — admin may have approved / rejected
                # between SELECT and UPDATE. Marker must only land on rows
                # STILL in awaiting_review. rowcount=0 → log + skip DM.
                update_result = await session.execute(
                    _text(
                        "UPDATE digests SET "
                        "review_notes=COALESCE(review_notes,'') "
                        "|| '[48h_notified]', "
                        "updated_at=now() "
                        "WHERE id=:id AND status='awaiting_review' "
                        "RETURNING id"
                    ),
                    {"id": row.id},
                )
                if update_result.rowcount == 0:
                    logger.info(
                        "digest_stale_review_reaper: digest_id=%s no longer "
                        "awaiting_review, skipping 48h DM (state advanced)",
                        row.id,
                    )
                    continue
                notified_ids.append(row.id)
            await session.commit()

            if notified_ids:
                from bot.services.digest_admin_notify import (
                    notify_admins_digest_failure,
                )

                for digest_id in notified_ids:
                    # DM AFTER successful guarded marker — order guarantees
                    # at-most-once notification per row.
                    await notify_admins_digest_failure(
                        bot,
                        digest_id=digest_id,
                        status="awaiting_review",
                        error_text="review_48h_reminder",
                    )
    except Exception:
        # Reaper crash must NEVER propagate — apscheduler would stop firing
        # the job. Log + continue.
        logger.exception("digest_stale_review_reaper crashed")


# ─── T10-07: Phase 10 graph scheduler jobs ──────────────────────────────────


async def graph_projection_nightly_job(bot: Bot) -> None:
    """Nightly graph projection at 03:30 MSK.

    Per PHASE10_PLAN.md §5.H. Strict no-op when ``memory.graph.projection.enabled``
    is OFF. On failure: logs exception (no admin DM in this sprint — T10-07 scope).

    Wraps in try/except so apscheduler never stops firing.
    """
    try:
        async with async_session() as session:
            from bot.db.repos.feature_flag import FeatureFlagRepo

            flag_enabled = await FeatureFlagRepo.get(
                session, "memory.graph.projection.enabled"
            )
            if not flag_enabled:
                logger.info("graph_projection_nightly_job: flag disabled, skipping")
                return

            from bot.services.graph_adapter import Neo4jAdapter

            config = default_projector_config(Neo4jAdapter())
            try:
                result = await project_incremental(
                    session,
                    config=config,
                    started_by="scheduler",
                )
                await session.commit()
                logger.info(
                    "graph_projection_nightly_job: run_id=%s status=%s "
                    "sources_processed=%s triples_created=%s cost_usd=%s",
                    result.run_id,
                    result.status,
                    result.sources_processed,
                    result.triples_created,
                    result.cost_usd,
                )
            except Exception:
                try:
                    await session.rollback()
                except Exception:
                    logger.exception("graph_projection_nightly_job: rollback failed")
                logger.exception("graph_projection_nightly_job: incremental projection failed")
    except Exception:
        logger.exception("graph_projection_nightly_job: session setup failed")


async def graph_purge_worker_job() -> None:
    """Graph purge worker tick — every 5 minutes.

    Per PHASE10_PLAN.md §5.H. Reads feature flag ``memory.graph.write_pending.paused``;
    skips if ON (kill-switch for Neo4j downtime). Calls graph_purge_worker_tick with
    batch_size=20.

    Wraps in try/except so apscheduler never stops firing.
    """
    try:
        async with async_session() as session:
            from bot.services.graph_adapter import Neo4jAdapter

            adapter = Neo4jAdapter()
            try:
                tick_result = await graph_purge_worker_tick(
                    session, adapter=adapter, batch_size=20
                )
                await session.commit()
                logger.info(
                    "graph_purge_worker_job: processed=%s errors=%s skipped_paused=%s",
                    tick_result.get("processed", 0),
                    tick_result.get("errors", 0),
                    tick_result.get("skipped_paused", False),
                )
            except Exception:
                try:
                    await session.rollback()
                except Exception:
                    logger.exception("graph_purge_worker_job: rollback failed")
                logger.exception("graph_purge_worker_job: tick failed")
    except Exception:
        logger.exception("graph_purge_worker_job: session setup failed")


# ─── T12-08: Butler TTL expiry worker ───────────────────────────────────────


async def _query_pending_past_ttl(session: Any, *, now: datetime, limit: int) -> list:
    """Thin helper so tests can patch without reaching into the repo directly."""
    return await ButlerActionRepo.get_pending_past_ttl(session, now=now, limit=limit)


async def _expire_action_inline(session: Any, action_id: int, *, action_repo: Any) -> Any:
    """Free-function equivalent of ButlerService.expire_action for the worker tick.

    Uses only ``action_repo`` — no other ButlerService collaborators needed.
    Avoids constructing ButlerService with ``None`` collaborators (M1) so if
    expire_action ever extends to use other repos, AttributeError is explicit
    rather than silent in the apscheduler tick.

    Logic mirrors ButlerService.expire_action exactly:
    - Row not found → raise ButlerActionError(not_found)
    - Already expired / not pending_confirmation → idempotent no-op
    - Past TTL → update_status('expired', 'ttl_expired'), return refreshed row
    """
    from bot.services.butler import ButlerActionError

    action = await action_repo.get_for_update(session, action_id)
    if action is None:
        raise ButlerActionError(
            f"action_id={action_id} not found",
            error_kind="not_found",
            action_id=action_id,
        )
    if action.status == "expired":
        return action
    if action.status != "pending_confirmation":
        return action

    now = datetime.now(timezone.utc)
    if action.expires_at is None or action.expires_at > now:
        return action

    await action_repo.update_status(
        session,
        action_id,
        status="expired",
        rejection_reason="ttl_expired",
    )
    updated = await action_repo.get(session, action_id)
    return updated or action


async def butler_expire_tick(*, bot: Bot, session: Any) -> int:
    """Inner tick: expire all pending_confirmation actions past their TTL.

    Returns the count of actions expired. Callers own session + commit.

    The session parameter is accepted for injection in tests (callers pass
    the active session so the tick participates in the same transaction).

    Uses ``_expire_action_inline`` (free function, M1) rather than constructing
    a ``ButlerService`` with most collaborators set to None.
    """
    from bot.db.repos.butler_action import BUTLER_EXPIRE_BATCH_SIZE
    from bot.db.repos.butler_action import ButlerActionRepo as _ActionRepo

    now = datetime.now(timezone.utc)
    stale_actions = await _query_pending_past_ttl(session, now=now, limit=BUTLER_EXPIRE_BATCH_SIZE)
    if not stale_actions:
        await session.commit()
        return 0

    expired_count = 0
    for action in stale_actions:
        try:
            updated = await _expire_action_inline(session, action.id, action_repo=_ActionRepo)
            if getattr(updated, "status", None) == "expired":
                expired_count += 1
        except Exception:
            logger.exception(
                "butler_expire_tick: expire_action failed for action_id=%s", action.id
            )

    await session.commit()
    if expired_count:
        logger.info("butler_expire_tick: expired %d actions", expired_count)
    return expired_count


async def butler_expire_tick_job(bot: Bot) -> None:
    """APScheduler wrapper for the Butler TTL expiry worker.

    Per PHASE12_PLAN_REFRESH.md §T12-08. Strict no-op when the master flag
    ``memory.butler.enabled`` is OFF. Interval configured via
    ``BUTLER_EXPIRE_TICK_SECONDS`` (default 60s).

    Wraps butler_expire_tick in try/except so apscheduler never stops firing.
    F2 pattern: bot threaded through via scheduler args=[bot] so Telegram
    side-effects (e.g. future keyboard editing) can be added without re-wiring.
    """
    try:
        async with async_session() as session:
            from bot.db.repos.feature_flag import FeatureFlagRepo

            flag_enabled = await FeatureFlagRepo.get(session, "memory.butler.enabled")
            if not flag_enabled:
                logger.debug("butler_expire_tick_job: flag disabled, skipping")
                return

            try:
                expired = await butler_expire_tick(bot=bot, session=session)
                logger.debug("butler_expire_tick_job: expired=%d", expired)
            except Exception:
                try:
                    await session.rollback()
                except Exception:
                    logger.exception("butler_expire_tick_job: rollback failed")
                logger.exception("butler_expire_tick_job: tick crashed")
    except Exception:
        # Catch-all — apscheduler must never see an exception or it would
        # mark the job as failed and stop firing.
        logger.exception("butler_expire_tick_job: session setup failed")


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
        args=[bot],  # F2: thread bot through so Telegram redaction side-effect fires
        id="forget_cascade_worker",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )
    # T6-03: Phase 6 extraction scheduler. Default OFF via flag
    # ``memory.extraction.scheduler.enabled`` (see bot/services/extractor.py).
    # 15-min interval mirrors ``check_vouch_deadlines`` — operator-explicit
    # ``/admin_extract --window`` remains the primary entry point until the
    # flag is enabled. Strict no-op when flag is OFF.
    scheduler.add_job(
        run_extraction_scheduler_tick,
        "interval",
        minutes=15,
        id="extraction_scheduler_tick",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )
    # T7-04: Phase 7 daily digest. Cron-MSK, gated by feature flag
    # ``memory.digests.daily.enabled`` (default OFF). The job body
    # re-checks the flag and is a strict no-op when disabled, so this
    # wiring is safe to land in production with the flag off.
    from zoneinfo import ZoneInfo

    msk = ZoneInfo("Europe/Moscow")
    digest_hour_msk = int(getattr(settings, "DIGEST_HOUR_MSK", 9))
    scheduler.add_job(
        digest_daily_job,
        "cron",
        hour=digest_hour_msk,
        minute=0,
        args=[bot],
        id="digest_daily",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
        timezone=msk,
    )
    # T7-04: Phase 7 stale-posting reaper. ALWAYS runs (not flag-gated) so
    # orphan rows from publisher crashes get reaped even after a flag
    # flip-OFF. See PHASE7_PLAN.md §5.K.
    scheduler.add_job(
        digest_stale_posting_reaper_job,
        "interval",
        minutes=5,
        id="digest_stale_posting_reaper",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )
    # T8-05: Phase 8 weekly digest. Mon 09:15 MSK — H8 stagger past the daily
    # 09:00 cron so the LLM gateway has 15 minutes of slack on Mondays.
    # Gated by feature flag ``memory.digests.weekly.enabled`` (default OFF);
    # job body re-checks the flag and is a strict no-op when disabled.
    digest_weekly_hour_msk = int(getattr(settings, "DIGEST_WEEKLY_HOUR_MSK", 9))
    digest_weekly_minute_msk = int(getattr(settings, "DIGEST_WEEKLY_MINUTE_MSK", 15))
    scheduler.add_job(
        digest_weekly_job,
        "cron",
        day_of_week="mon",
        hour=digest_weekly_hour_msk,
        minute=digest_weekly_minute_msk,
        args=[bot],
        id="digest_weekly",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,  # 1h grace — weekly cadence is forgiving
        timezone=msk,
    )
    # T8-05: Phase 8 stale-review reaper. Every 30 min, no flag gate —
    # rows already in awaiting_review must still be bounded even after a
    # flag flip-OFF. Two passes per tick: 7d auto-reject + 48h DM.
    scheduler.add_job(
        digest_stale_review_reaper_job,
        "interval",
        minutes=30,
        args=[bot],
        id="digest_stale_review_reaper",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    # T10-07: Phase 10 graph projection nightly job. Cron 03:30 MSK = 00:30 UTC.
    # Gated by feature flag ``memory.graph.projection.enabled`` (default OFF);
    # job body re-checks the flag and is a strict no-op when disabled.
    # Separate from digest crons (09:00/09:15 MSK) — no LLM gateway pressure overlap.
    graph_projection_hour_msk = int(getattr(settings, "GRAPH_PROJECTION_HOUR_MSK", 3))
    graph_projection_minute_msk = int(getattr(settings, "GRAPH_PROJECTION_MINUTE_MSK", 30))
    scheduler.add_job(
        graph_projection_nightly_job,
        "cron",
        hour=graph_projection_hour_msk,
        minute=graph_projection_minute_msk,
        args=[bot],
        id="graph_projection_nightly",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=1800,  # 30-min grace — projection can run long
        timezone=msk,
    )
    # T10-07: Phase 10 graph purge worker. Every 5 minutes.
    # Not flag-gated at scheduler level — the worker itself reads
    # ``memory.graph.write_pending.paused`` kill-switch each tick.
    scheduler.add_job(
        graph_purge_worker_job,
        "interval",
        minutes=5,
        id="graph_purge_worker",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )
    # T12-08: Butler TTL expiry worker. Default 60s interval; gated by
    # ``memory.butler.enabled`` feature flag (default OFF) in the job body.
    # F2 pattern: bot threaded through args=[bot] per T7 FHR fix df4bb71.
    butler_expire_tick_seconds = int(
        os.environ.get("BUTLER_EXPIRE_TICK_SECONDS", "60")
    )
    scheduler.add_job(
        butler_expire_tick_job,
        "interval",
        seconds=butler_expire_tick_seconds,
        args=[bot],
        id="butler_expire_tick",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )
    scheduler.start()
    logger.info("Scheduler started")


def stop_scheduler() -> None:
    """Shut down the scheduler."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
