from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import MemberTagCooldown


class MemberTagRepo:
    @staticmethod
    async def get(session: AsyncSession, chat_id: int, user_id: int) -> MemberTagCooldown | None:
        result = await session.execute(
            select(MemberTagCooldown).where(
                MemberTagCooldown.chat_id == chat_id,
                MemberTagCooldown.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def set(
        session: AsyncSession,
        chat_id: int,
        user_id: int,
        tag: str | None,
        changed_at: datetime,
    ) -> MemberTagCooldown:
        row = await MemberTagRepo.get(session, chat_id, user_id)
        if row is None:
            row = MemberTagCooldown(
                chat_id=chat_id,
                user_id=user_id,
                last_tag=tag,
                changed_at=changed_at,
            )
            session.add(row)
        else:
            row.last_tag = tag
            row.changed_at = changed_at

        await session.flush()
        return row
