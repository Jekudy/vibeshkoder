from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import VouchLog


class VouchRepo:
    @staticmethod
    async def create(
        session: AsyncSession,
        voucher_id: int,
        vouchee_id: int,
        application_id: int,
    ) -> VouchLog:
        vouch = VouchLog(
            voucher_id=voucher_id,
            vouchee_id=vouchee_id,
            application_id=application_id,
        )
        session.add(vouch)
        await session.flush()
        return vouch

    @staticmethod
    async def get_voucher_for_user(
        session: AsyncSession, user_id: int
    ) -> VouchLog | None:
        result = await session.execute(
            select(VouchLog)
            .where(VouchLog.vouchee_id == user_id)
            .order_by(VouchLog.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()
