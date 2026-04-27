from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


class _AsyncSessionContext:
    def __init__(self, session) -> None:
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        return None


async def _get_pending_ids(
    *,
    user_id: int,
    created_at: datetime,
    submitted_at: datetime | None,
    hours: int = 72,
) -> list[int]:
    models = import_module("bot.db.models")
    application_repo = import_module("bot.db.repos.application")
    app = models.Application(
        user_id=user_id,
        status="pending",
        created_at=created_at,
        submitted_at=submitted_at,
    )
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(models.Base.metadata.create_all)

        async with sessionmaker() as session:
            session.add(app)
            await session.commit()

            result = await application_repo.ApplicationRepo.get_pending_older_than(
                session, hours
            )
            return [pending_app.id for pending_app in result]
    finally:
        await engine.dispose()


async def _auto_reject_after_status_flip_to_vouched() -> tuple[bool, str]:
    models = import_module("bot.db.models")
    application_repo = import_module("bot.db.repos.application")
    now = datetime.now(timezone.utc)
    app = models.Application(
        user_id=2001,
        status="pending",
        created_at=now - timedelta(hours=80),
        submitted_at=now - timedelta(hours=80),
    )
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(models.Base.metadata.create_all)

        async with sessionmaker() as session:
            session.add(app)
            await session.commit()

            apps_to_reject = await application_repo.ApplicationRepo.get_pending_older_than(
                session, 72
            )
            assert [pending_app.id for pending_app in apps_to_reject] == [app.id]

            await session.execute(
                update(models.Application)
                .where(models.Application.id == app.id)
                .values(status="vouched")
            )
            await session.flush()

            rejected = await application_repo.ApplicationRepo.update_status_if(
                session,
                app.id,
                expected_from="pending",
                new_status="rejected",
                rejected_at=datetime.now(timezone.utc),
            )
            await session.refresh(app)
            return rejected, app.status
    finally:
        await engine.dispose()


async def _auto_reject_when_status_still_pending() -> tuple[bool, str, bool]:
    models = import_module("bot.db.models")
    application_repo = import_module("bot.db.repos.application")
    now = datetime.now(timezone.utc)
    app = models.Application(
        user_id=2002,
        status="pending",
        created_at=now - timedelta(hours=80),
        submitted_at=now - timedelta(hours=80),
    )
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(models.Base.metadata.create_all)

        async with sessionmaker() as session:
            session.add(app)
            await session.commit()

            apps_to_reject = await application_repo.ApplicationRepo.get_pending_older_than(
                session, 72
            )
            assert [pending_app.id for pending_app in apps_to_reject] == [app.id]

            rejected = await application_repo.ApplicationRepo.update_status_if(
                session,
                app.id,
                expected_from="pending",
                new_status="rejected",
                rejected_at=datetime.now(timezone.utc),
            )
            await session.refresh(app)
            return rejected, app.status, app.rejected_at is not None
    finally:
        await engine.dispose()


def test_pending_older_than_uses_submitted_at() -> None:
    now = datetime.now(timezone.utc)
    pending_ids = asyncio.run(
        _get_pending_ids(
            user_id=1001,
            created_at=now - timedelta(hours=80),
            submitted_at=now - timedelta(hours=10),
        )
    )

    assert pending_ids == []


def test_auto_reject_skips_when_status_changed_to_vouched() -> None:
    rejected, status = asyncio.run(_auto_reject_after_status_flip_to_vouched())

    assert rejected is False
    assert status == "vouched"


def test_auto_reject_succeeds_when_status_still_pending() -> None:
    rejected, status, has_rejected_at = asyncio.run(_auto_reject_when_status_still_pending())

    assert rejected is True
    assert status == "rejected"
    assert has_rejected_at is True


def test_auto_reject_side_effects_are_skipped_when_cas_loses(app_env, monkeypatch) -> None:
    async def run() -> None:
        scheduler = import_module("bot.services.scheduler")
        session = SimpleNamespace(commit=AsyncMock())
        app = SimpleNamespace(
            id=42,
            user_id=2003,
            status="pending",
            questionnaire_message_id=9001,
        )
        get_pending_older_than = AsyncMock(return_value=[app])
        update_status_if = AsyncMock(return_value=False)
        get_pending_created_older_than = AsyncMock(return_value=[])
        bot = SimpleNamespace(
            delete_message=AsyncMock(),
            send_message=AsyncMock(),
        )

        monkeypatch.setattr(
            scheduler,
            "async_session",
            lambda: _AsyncSessionContext(session),
        )
        monkeypatch.setattr(
            scheduler.ApplicationRepo,
            "get_pending_older_than",
            get_pending_older_than,
        )
        monkeypatch.setattr(
            scheduler.ApplicationRepo,
            "update_status_if",
            update_status_if,
        )
        monkeypatch.setattr(
            scheduler.ApplicationRepo,
            "get_pending_created_older_than",
            get_pending_created_older_than,
        )

        await scheduler.check_vouch_deadlines(bot)

        update_status_if.assert_awaited_once()
        bot.delete_message.assert_not_awaited()
        bot.send_message.assert_not_awaited()
        session.commit.assert_awaited_once()

    asyncio.run(run())


def test_pending_older_than_legacy_null_falls_back_to_created_at() -> None:
    now = datetime.now(timezone.utc)
    pending_ids = asyncio.run(
        _get_pending_ids(
            user_id=1002,
            created_at=now - timedelta(hours=80),
            submitted_at=None,
        )
    )

    assert pending_ids == [1]


def test_pending_within_threshold_not_returned() -> None:
    now = datetime.now(timezone.utc)
    pending_ids = asyncio.run(
        _get_pending_ids(
            user_id=1003,
            created_at=now - timedelta(hours=80),
            submitted_at=now - timedelta(hours=50),
        )
    )

    assert pending_ids == []
