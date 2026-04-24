"""Tests for scheduler jobs: check_vouch_deadlines, check_intro_refresh, sync_google_sheets.

No pytest-asyncio available — all async code runs via asyncio.run().
Scheduler functions open their own DB sessions via bot.db.engine.async_session,
so we monkeypatch that factory to inject an in-memory SQLite session instead
of connecting to the real database.

Each job is tested for: triggered (condition met) and not-triggered (no data).
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.db.models import Base


def _run(coro):
    return asyncio.run(coro)


def _make_session_factory():
    """Return a fresh in-memory SQLite async session factory with schema created."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _create_schema():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create_schema())
    return factory, engine


# ── check_vouch_deadlines ─────────────────────────────────────────────────────


class TestCheckVouchDeadlines:
    def test_triggered_rejects_stale_application(self, app_env):
        """Applications older than VOUCH_TIMEOUT_HOURS are rejected and bot.delete_message called."""
        from bot.db.models import Application, User
        from bot.db.repos.application import ApplicationRepo
        from sqlalchemy import update

        factory, engine = _make_session_factory()
        bot_mock = AsyncMock()
        bot_mock.send_message = AsyncMock()
        bot_mock.delete_message = AsyncMock()

        # Create stale pending application
        async def _setup():
            async with factory() as session:
                user = User(id=7001, first_name="StaleVouchUser")
                session.add(user)
                await session.flush()
                app = await ApplicationRepo.create(session, 7001)
                await ApplicationRepo.update_status(session, app.id, "pending")
                # Back-date to 73h ago
                old_ts = datetime.now(timezone.utc) - timedelta(hours=73)
                await session.execute(
                    update(Application)
                    .where(Application.id == app.id)
                    .values(
                        created_at=old_ts,
                        questionnaire_message_id=555,
                    )
                )
                await session.commit()
                return app.id

        app_id = _run(_setup())

        # Patch the session factory that the scheduler uses
        @asynccontextmanager
        async def _mock_session():
            async with factory() as session:
                yield session

        with patch("bot.services.scheduler.async_session", _mock_session):
            from bot.services.scheduler import check_vouch_deadlines

            _run(check_vouch_deadlines(bot_mock))

        # Verify rejection DM sent
        bot_mock.send_message.assert_called()

        # Verify the application was actually rejected in DB
        async def _check():
            async with factory() as session:
                return await ApplicationRepo.get(session, app_id)

        updated_app = _run(_check())
        assert updated_app.status == "rejected"

        asyncio.run(engine.dispose())

    def test_not_triggered_when_no_stale_applications(self, app_env):
        """With no stale applications, bot.delete_message is never called."""
        factory, engine = _make_session_factory()
        bot_mock = AsyncMock()
        bot_mock.send_message = AsyncMock()
        bot_mock.delete_message = AsyncMock()

        @asynccontextmanager
        async def _mock_session():
            async with factory() as session:
                yield session

        with patch("bot.services.scheduler.async_session", _mock_session):
            from bot.services.scheduler import check_vouch_deadlines

            _run(check_vouch_deadlines(bot_mock))

        bot_mock.delete_message.assert_not_called()

        asyncio.run(engine.dispose())


# ── check_intro_refresh ───────────────────────────────────────────────────────


class TestCheckIntroRefresh:
    def test_triggered_sends_refresh_reminder(self, app_env):
        """A stale intro gets a refresh prompt sent via bot.send_message."""
        from bot.db.models import Intro, User
        from sqlalchemy import update

        factory, engine = _make_session_factory()
        bot_mock = AsyncMock()
        bot_mock.send_message = AsyncMock()

        async def _setup():
            async with factory() as session:
                user = User(id=8001, first_name="StaleIntroUser")
                session.add(user)
                await session.flush()

                intro = Intro(
                    user_id=8001,
                    intro_text="Stale intro",
                    vouched_by_name="@someone",
                )
                session.add(intro)
                await session.flush()

                # Back-date to 91 days ago so it's stale
                old_ts = datetime.now(timezone.utc) - timedelta(days=91)
                await session.execute(
                    update(Intro)
                    .where(Intro.user_id == 8001)
                    .values(updated_at=old_ts)
                )
                await session.commit()

        _run(_setup())

        @asynccontextmanager
        async def _mock_session():
            async with factory() as session:
                yield session

        with patch("bot.services.scheduler.async_session", _mock_session):
            from bot.services.scheduler import check_intro_refresh

            _run(check_intro_refresh(bot_mock))

        # send_message should have been called for the stale user
        bot_mock.send_message.assert_called()
        call_kwargs = [c.kwargs for c in bot_mock.send_message.call_args_list]
        user_ids_notified = [kw.get("chat_id") for kw in call_kwargs]
        assert 8001 in user_ids_notified

        asyncio.run(engine.dispose())

    def test_not_triggered_when_no_stale_intros(self, app_env):
        """With no stale intros, bot.send_message is never called."""
        factory, engine = _make_session_factory()
        bot_mock = AsyncMock()
        bot_mock.send_message = AsyncMock()

        @asynccontextmanager
        async def _mock_session():
            async with factory() as session:
                yield session

        with patch("bot.services.scheduler.async_session", _mock_session):
            from bot.services.scheduler import check_intro_refresh

            _run(check_intro_refresh(bot_mock))

        bot_mock.send_message.assert_not_called()

        asyncio.run(engine.dispose())


# ── sync_google_sheets ────────────────────────────────────────────────────────


class TestSyncGoogleSheets:
    def test_success_calls_full_sync(self, app_env):
        """sync_google_sheets calls bot.services.sheets.full_sync when available."""
        full_sync_mock = AsyncMock()

        with patch("bot.services.sheets.full_sync", full_sync_mock):
            # Also patch import so the scheduler finds it
            import sys
            import types

            fake_sheets = types.ModuleType("bot.services.sheets")
            fake_sheets.full_sync = full_sync_mock
            original = sys.modules.get("bot.services.sheets")
            sys.modules["bot.services.sheets"] = fake_sheets
            try:
                from bot.services.scheduler import sync_google_sheets

                _run(sync_google_sheets())
                full_sync_mock.assert_called_once()
            finally:
                if original is not None:
                    sys.modules["bot.services.sheets"] = original
                else:
                    sys.modules.pop("bot.services.sheets", None)

    def test_failure_does_not_propagate_exception(self, app_env):
        """sync_google_sheets swallows exceptions from full_sync gracefully."""
        import sys
        import types

        async def _failing_full_sync():
            raise RuntimeError("sheets down")

        fake_sheets = types.ModuleType("bot.services.sheets")
        fake_sheets.full_sync = _failing_full_sync
        original = sys.modules.get("bot.services.sheets")
        sys.modules["bot.services.sheets"] = fake_sheets
        try:
            from bot.services import scheduler as sched_mod

            # Reload to pick up the patched module
            import importlib

            importlib.reload(sched_mod)

            # Should not raise
            _run(sched_mod.sync_google_sheets())
        finally:
            if original is not None:
                sys.modules["bot.services.sheets"] = original
            else:
                sys.modules.pop("bot.services.sheets", None)
            importlib.reload(sched_mod)

    def test_sync_skipped_when_sheets_not_configured(self, app_env, monkeypatch):
        """When GOOGLE_SHEETS_CREDS_FILE is empty, sync_google_sheets is a no-op."""
        monkeypatch.setenv("GOOGLE_SHEETS_CREDS_FILE", "")
        monkeypatch.setenv("GOOGLE_SHEET_ID", "")

        # full_sync returns early if not configured; we verify no error is raised
        from bot.services.scheduler import sync_google_sheets

        # Should complete without exception
        _run(sync_google_sheets())
