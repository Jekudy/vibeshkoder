from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Application


class ApplicationRepo:
    @staticmethod
    async def create(session: AsyncSession, user_id: int) -> Application:
        app = Application(user_id=user_id, status="filling")
        session.add(app)
        await session.flush()
        return app

    @staticmethod
    async def get(session: AsyncSession, app_id: int) -> Application | None:
        result = await session.execute(
            select(Application).where(Application.id == app_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_active(
        session: AsyncSession, user_id: int
    ) -> Application | None:
        result = await session.execute(
            select(Application)
            .where(
                Application.user_id == user_id,
                Application.status.in_(("filling", "pending", "privacy_block", "vouched")),
            )
            .order_by(Application.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_last_rejected(
        session: AsyncSession, user_id: int
    ) -> Application | None:
        result = await session.execute(
            select(Application)
            .where(
                Application.user_id == user_id,
                Application.status == "rejected",
            )
            .order_by(Application.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def update_status(
        session: AsyncSession, app_id: int, status: str, **extra_fields
    ) -> None:
        values: dict = {"status": status, **extra_fields}
        await session.execute(
            update(Application).where(Application.id == app_id).values(**values)
        )
        await session.flush()

    @staticmethod
    async def get_pending_older_than(
        session: AsyncSession, hours: int
    ) -> list[Application]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        result = await session.execute(
            select(Application).where(
                Application.status == "pending",
                func.coalesce(Application.submitted_at, Application.created_at) < cutoff,
            )
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_pending_created_older_than(
        session: AsyncSession, hours: int
    ) -> list[Application]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        result = await session.execute(
            select(Application).where(
                Application.status == "pending",
                Application.created_at < cutoff,
            )
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_funnel_stats(session: AsyncSession) -> dict:
        result = await session.execute(
            select(Application.status, func.count())
            .group_by(Application.status)
        )
        return dict(result.all())
