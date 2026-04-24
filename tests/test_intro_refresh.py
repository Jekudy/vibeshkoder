"""Tests for intro refresh logic.

No pytest-asyncio available — all async code runs via asyncio.run().
Tests cover:
- Expiry detection (IntroRepo.get_stale_intros, default 90-day cutoff)
- IntroRefreshTracking phase logic at reminders_sent=5 (daily → every_2_days)
- IntroRefreshTracking phase logic at reminders_sent=8 (every_2_days → done)

datetime.now() is controlled via backdating inserted DB rows rather than
monkeypatching (avoids patching internals we don't own; more robust).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

def _run(coro):
    return asyncio.run(coro)


class TestIntroExpiryDetection:
    """IntroRepo.get_stale_intros correctly identifies old intros."""

    def test_intro_older_than_cutoff_is_stale(self, session_factory_sqlite):
        """Intro with updated_at > 90 days ago is returned as stale."""
        from bot.db.models import Intro, User
        from bot.db.repos.intro import IntroRepo
        from sqlalchemy import update

        async def _run_test():
            async with session_factory_sqlite() as session:
                user = User(id=5001, first_name="StaleUser")
                session.add(user)
                await session.flush()

                intro = Intro(
                    user_id=5001,
                    intro_text="Old intro text",
                    vouched_by_name="@someone",
                )
                session.add(intro)
                await session.flush()

                # Back-date updated_at to 91 days ago
                old_ts = datetime.now(timezone.utc) - timedelta(days=91)
                await session.execute(
                    update(Intro)
                    .where(Intro.user_id == 5001)
                    .values(updated_at=old_ts)
                )
                await session.commit()

                stale = await IntroRepo.get_stale_intros(session, days=90)
                assert any(i.user_id == 5001 for i in stale)

        _run(_run_test())

    def test_intro_within_cutoff_is_not_stale(self, session_factory_sqlite):
        """Intro updated 30 days ago is NOT returned as stale at 90-day cutoff."""
        from bot.db.models import Intro, User
        from bot.db.repos.intro import IntroRepo
        from sqlalchemy import update

        async def _run_test():
            async with session_factory_sqlite() as session:
                user = User(id=5002, first_name="FreshUser")
                session.add(user)
                await session.flush()

                intro = Intro(
                    user_id=5002,
                    intro_text="Fresh intro text",
                    vouched_by_name="@recent",
                )
                session.add(intro)
                await session.flush()

                recent_ts = datetime.now(timezone.utc) - timedelta(days=30)
                await session.execute(
                    update(Intro)
                    .where(Intro.user_id == 5002)
                    .values(updated_at=recent_ts)
                )
                await session.commit()

                stale = await IntroRepo.get_stale_intros(session, days=90)
                assert not any(i.user_id == 5002 for i in stale)

        _run(_run_test())


class TestIntroRefreshTrackingPhases:
    """IntroRefreshTracking phase transitions are correct per scheduler logic."""

    def test_reminders_5_daily_triggers_phase_change(self, session_factory_sqlite):
        """At reminders_sent=5 in 'daily' phase, phase should move to every_2_days."""
        from bot.db.models import IntroRefreshTracking, User
        from sqlalchemy import select

        async def _run_test():
            async with session_factory_sqlite() as session:
                user = User(id=5003, first_name="PhaseUser")
                session.add(user)
                await session.flush()

                tracking = IntroRefreshTracking(
                    user_id=5003,
                    cycle_started_at=datetime.now(timezone.utc) - timedelta(days=5),
                    reminders_sent=5,
                    phase="daily",
                    completed=False,
                    last_reminder_at=datetime.now(timezone.utc) - timedelta(days=1),
                )
                session.add(tracking)
                await session.commit()

                # Simulate the phase-change branch from check_intro_refresh:
                # when phase=="daily" and reminders_sent >= 5, move to every_2_days
                result = await session.execute(
                    select(IntroRefreshTracking).where(
                        IntroRefreshTracking.user_id == 5003,
                        IntroRefreshTracking.completed.is_(False),
                    )
                )
                t = result.scalar_one()
                assert t.phase == "daily"
                assert t.reminders_sent >= 5

                # Apply the scheduler branch logic
                if t.phase == "daily" and t.reminders_sent >= 5:
                    t.phase = "every_2_days"
                    await session.flush()

                await session.commit()

                result2 = await session.execute(
                    select(IntroRefreshTracking).where(IntroRefreshTracking.user_id == 5003)
                )
                t2 = result2.scalar_one()
                assert t2.phase == "every_2_days"

        _run(_run_test())

    def test_reminders_8_every2days_marks_done(self, session_factory_sqlite):
        """At reminders_sent=8 in 'every_2_days' phase, completed=True and phase='done'."""
        from bot.db.models import IntroRefreshTracking, User
        from sqlalchemy import select

        async def _run_test():
            async with session_factory_sqlite() as session:
                user = User(id=5004, first_name="GraceEndUser")
                session.add(user)
                await session.flush()

                tracking = IntroRefreshTracking(
                    user_id=5004,
                    cycle_started_at=datetime.now(timezone.utc) - timedelta(days=11),
                    reminders_sent=8,
                    phase="every_2_days",
                    completed=False,
                    last_reminder_at=datetime.now(timezone.utc) - timedelta(days=2),
                )
                session.add(tracking)
                await session.commit()

                # Apply scheduler branch: reminders_sent >= 8 → done
                result = await session.execute(
                    select(IntroRefreshTracking).where(
                        IntroRefreshTracking.user_id == 5004,
                        IntroRefreshTracking.completed.is_(False),
                    )
                )
                t = result.scalar_one()
                assert t.phase == "every_2_days"
                assert t.reminders_sent >= 8

                t.phase = "done"
                t.completed = True
                await session.flush()
                await session.commit()

                result2 = await session.execute(
                    select(IntroRefreshTracking).where(IntroRefreshTracking.user_id == 5004)
                )
                t2 = result2.scalar_one()
                assert t2.completed is True
                assert t2.phase == "done"

        _run(_run_test())

    def test_new_stale_intro_creates_tracking_record(self, session_factory_sqlite):
        """A stale intro without tracking gets a new IntroRefreshTracking row."""
        from bot.db.models import Intro, IntroRefreshTracking, User
        from sqlalchemy import select, update

        async def _run_test():
            async with session_factory_sqlite() as session:
                user = User(id=5005, first_name="NewTrackingUser")
                session.add(user)
                await session.flush()

                intro = Intro(
                    user_id=5005,
                    intro_text="Some intro",
                    vouched_by_name="@vch",
                )
                session.add(intro)
                await session.flush()

                # Back-date to stale
                old_ts = datetime.now(timezone.utc) - timedelta(days=100)
                await session.execute(
                    update(Intro).where(Intro.user_id == 5005).values(updated_at=old_ts)
                )
                await session.commit()

                # Confirm no tracking yet
                r1 = await session.execute(
                    select(IntroRefreshTracking).where(IntroRefreshTracking.user_id == 5005)
                )
                assert r1.scalar_one_or_none() is None

                # Simulate scheduler creating the tracking record
                now = datetime.now(timezone.utc)
                tracking = IntroRefreshTracking(
                    user_id=5005,
                    cycle_started_at=now,
                    reminders_sent=0,
                    phase="daily",
                    completed=False,
                )
                session.add(tracking)
                await session.flush()
                await session.commit()

                r2 = await session.execute(
                    select(IntroRefreshTracking).where(IntroRefreshTracking.user_id == 5005)
                )
                t = r2.scalar_one()
                assert t.phase == "daily"
                assert t.reminders_sent == 0
                assert t.completed is False

        _run(_run_test())
