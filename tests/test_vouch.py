"""Tests for the vouch flow: VouchRepo, ApplicationRepo state transitions.

No pytest-asyncio available — all async code runs via asyncio.run().
Tests exercise the repo layer and the handle_vouch business logic via the
VouchRepo / ApplicationRepo directly. The full aiogram CallbackQuery dispatch
is not tested here (that would need a live Dispatcher + Bot mock setup);
instead, we test the data integrity invariants that handle_vouch depends on.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

def _run(coro):
    return asyncio.run(coro)


class TestVouchRepoCreate:
    """VouchRepo.create persists a VouchLog row correctly."""

    def test_vouch_log_created(self, session_factory_sqlite):
        from bot.db.models import User
        from bot.db.repos.application import ApplicationRepo
        from bot.db.repos.vouch import VouchRepo

        async def _run_test():
            async with session_factory_sqlite() as session:
                applicant = User(id=1001, first_name="Applicant")
                voucher = User(id=2001, first_name="Voucher", is_member=True)
                session.add_all([applicant, voucher])
                await session.flush()

                app = await ApplicationRepo.create(session, 1001)
                await ApplicationRepo.update_status(session, app.id, "pending")

                vouch_log = await VouchRepo.create(
                    session,
                    voucher_id=2001,
                    vouchee_id=1001,
                    application_id=app.id,
                )
                await session.commit()

                assert vouch_log.id is not None
                assert vouch_log.voucher_id == 2001
                assert vouch_log.vouchee_id == 1001
                assert vouch_log.application_id == app.id

        _run(_run_test())

    def test_no_vouch_log_when_not_created(self, session_factory_sqlite):
        """Without calling VouchRepo.create, no VouchLog row exists."""
        from bot.db.models import User, VouchLog
        from bot.db.repos.application import ApplicationRepo
        from sqlalchemy import select

        async def _run_test():
            async with session_factory_sqlite() as session:
                applicant = User(id=1002, first_name="NoVouch")
                session.add(applicant)
                await session.flush()

                await ApplicationRepo.create(session, 1002)
                await session.commit()

                result = await session.execute(select(VouchLog).where(VouchLog.vouchee_id == 1002))
                assert result.scalar_one_or_none() is None

        _run(_run_test())


class TestVouchApplicationStateTransition:
    """Application status changes correctly through the vouch flow."""

    def test_accept_vouch_sets_vouched_status(self, session_factory_sqlite):
        """Accepting a vouch → Application.status='vouched', vouched_by set."""
        from bot.db.models import Application, User
        from bot.db.repos.application import ApplicationRepo
        from bot.db.repos.vouch import VouchRepo
        from sqlalchemy import update

        async def _run_test():
            async with session_factory_sqlite() as session:
                applicant = User(id=1003, first_name="Applicant2")
                voucher = User(id=2002, first_name="Voucher2", is_member=True)
                session.add_all([applicant, voucher])
                await session.flush()

                app = await ApplicationRepo.create(session, 1003)
                await ApplicationRepo.update_status(session, app.id, "pending")

                # Simulate handle_vouch optimistic-lock update
                now = datetime.now(timezone.utc)
                result = await session.execute(
                    update(Application)
                    .where(Application.id == app.id, Application.status == "pending")
                    .values(status="vouched", vouched_by=2002, vouched_at=now)
                    .returning(Application.id)
                )
                updated_id = result.scalar_one_or_none()
                await session.flush()
                assert updated_id is not None

                await VouchRepo.create(session, 2002, 1003, app.id)
                await session.commit()

                refreshed = await ApplicationRepo.get(session, app.id)
                assert refreshed.status == "vouched"
                assert refreshed.vouched_by == 2002

        _run(_run_test())

    def test_deny_does_not_create_vouch_log(self, session_factory_sqlite):
        """When a denial path is taken, no VouchLog row is created."""
        from bot.db.models import User, VouchLog
        from bot.db.repos.application import ApplicationRepo
        from sqlalchemy import select

        async def _run_test():
            async with session_factory_sqlite() as session:
                applicant = User(id=1004, first_name="Denied")
                session.add(applicant)
                await session.flush()

                app = await ApplicationRepo.create(session, 1004)
                await ApplicationRepo.update_status(session, app.id, "pending")
                # Denial path: status → rejected, no VouchLog
                await ApplicationRepo.update_status(
                    session, app.id, "rejected",
                    rejected_at=datetime.now(timezone.utc),
                )
                await session.commit()

                refreshed = await ApplicationRepo.get(session, app.id)
                assert refreshed.status == "rejected"

                result = await session.execute(
                    select(VouchLog).where(VouchLog.vouchee_id == 1004)
                )
                assert result.scalar_one_or_none() is None

        _run(_run_test())

    def test_vouch_timeout_72h_application_rejected(self, session_factory_sqlite):
        """Applications older than 72h are returned by get_pending_older_than."""
        from bot.db.models import Application, User
        from bot.db.repos.application import ApplicationRepo
        from sqlalchemy import update

        async def _run_test():
            async with session_factory_sqlite() as session:
                user = User(id=1005, first_name="OldApplicant")
                session.add(user)
                await session.flush()

                app = await ApplicationRepo.create(session, 1005)
                await ApplicationRepo.update_status(session, app.id, "pending")

                # Back-date created_at to 73 hours ago
                old_ts = datetime.now(timezone.utc) - timedelta(hours=73)
                await session.execute(
                    update(Application)
                    .where(Application.id == app.id)
                    .values(created_at=old_ts)
                )
                await session.commit()

                # Should appear in the 72h cutoff query
                stale = await ApplicationRepo.get_pending_older_than(session, 72)
                assert any(a.id == app.id for a in stale)

        _run(_run_test())

    def test_get_voucher_for_user(self, session_factory_sqlite):
        """VouchRepo.get_voucher_for_user returns the correct VouchLog."""
        from bot.db.models import User
        from bot.db.repos.application import ApplicationRepo
        from bot.db.repos.vouch import VouchRepo

        async def _run_test():
            async with session_factory_sqlite() as session:
                applicant = User(id=1006, first_name="FindableVouchee")
                voucher = User(id=2003, first_name="FindableVoucher", is_member=True)
                session.add_all([applicant, voucher])
                await session.flush()

                app = await ApplicationRepo.create(session, 1006)
                await VouchRepo.create(session, 2003, 1006, app.id)
                await session.commit()

                log = await VouchRepo.get_voucher_for_user(session, 1006)
                assert log is not None
                assert log.voucher_id == 2003

        _run(_run_test())
