from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Intro, User


class IntroRepo:
    @staticmethod
    async def upsert(
        session: AsyncSession,
        user_id: int,
        intro_text: str,
        vouched_by_name: str,
    ) -> Intro:
        result = await session.execute(
            select(Intro).where(Intro.user_id == user_id)
        )
        intro = result.scalar_one_or_none()
        if intro is None:
            intro = Intro(
                user_id=user_id,
                intro_text=intro_text,
                vouched_by_name=vouched_by_name,
            )
            session.add(intro)
        else:
            intro.intro_text = intro_text
            intro.vouched_by_name = vouched_by_name
        await session.flush()
        return intro

    @staticmethod
    async def get(session: AsyncSession, user_id: int) -> Intro | None:
        result = await session.execute(
            select(Intro).where(Intro.user_id == user_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_all(session: AsyncSession) -> list[Intro]:
        result = await session.execute(select(Intro))
        return list(result.scalars().all())

    @staticmethod
    async def delete(session: AsyncSession, user_id: int) -> None:
        await session.execute(
            delete(Intro).where(Intro.user_id == user_id)
        )
        await session.flush()

    @staticmethod
    async def get_members_without_intro(
        session: AsyncSession,
    ) -> list[User]:
        subq = select(Intro.user_id).scalar_subquery()
        result = await session.execute(
            select(User).where(
                User.is_member.is_(True),
                User.id.not_in(subq),
            )
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_stale_intros(
        session: AsyncSession, days: int
    ) -> list[Intro]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        result = await session.execute(
            select(Intro).where(Intro.updated_at < cutoff)
        )
        return list(result.scalars().all())
