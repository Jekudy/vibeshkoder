from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from importlib import import_module

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


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
