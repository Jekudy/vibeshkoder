from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.db.models import Application, Base
from bot.db.repos.application import ApplicationRepo


async def _get_pending_ids(app: Application, hours: int = 72) -> list[int]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with sessionmaker() as session:
            session.add(app)
            await session.commit()

            result = await ApplicationRepo.get_pending_older_than(session, hours)
            return [pending_app.id for pending_app in result]
    finally:
        await engine.dispose()


def test_pending_older_than_uses_submitted_at() -> None:
    now = datetime.now(timezone.utc)
    app = Application(
        user_id=1001,
        status="pending",
        created_at=now - timedelta(hours=80),
        submitted_at=now - timedelta(hours=10),
    )

    pending_ids = asyncio.run(_get_pending_ids(app))

    assert pending_ids == []


def test_pending_older_than_legacy_null_falls_back_to_created_at() -> None:
    now = datetime.now(timezone.utc)
    app = Application(
        user_id=1002,
        status="pending",
        created_at=now - timedelta(hours=80),
        submitted_at=None,
    )

    pending_ids = asyncio.run(_get_pending_ids(app))

    assert pending_ids == [1]


def test_pending_within_threshold_not_returned() -> None:
    now = datetime.now(timezone.utc)
    app = Application(
        user_id=1003,
        status="pending",
        created_at=now - timedelta(hours=80),
        submitted_at=now - timedelta(hours=50),
    )

    pending_ids = asyncio.run(_get_pending_ids(app))

    assert pending_ids == []
