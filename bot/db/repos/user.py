from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import User


class UserRepo:
    @staticmethod
    async def upsert(
        session: AsyncSession,
        telegram_id: int,
        username: str | None,
        first_name: str,
        last_name: str | None,
    ) -> User:
        # Use PostgreSQL ON CONFLICT for race-condition safety
        stmt = (
            pg_insert(User)
            .values(
                id=telegram_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
            )
            .on_conflict_do_update(
                index_elements=[User.id],
                set_={
                    "username": username,
                    "first_name": first_name,
                    "last_name": last_name,
                },
            )
        )
        await session.execute(stmt)
        await session.flush()
        # Fetch and return the user
        result = await session.execute(select(User).where(User.id == telegram_id))
        return result.scalar_one()

    @staticmethod
    async def get(session: AsyncSession, telegram_id: int) -> User | None:
        result = await session.execute(select(User).where(User.id == telegram_id))
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_tg_id(session: AsyncSession, tg_id: int) -> User | None:
        """Look up a user by Telegram user id (users.id == tg_id).

        Alias for ``get`` with an explicit name that matches import-path terminology.
        ``users.id`` IS the Telegram user id — there is no separate ``tg_id`` column.
        """
        result = await session.execute(select(User).where(User.id == tg_id))
        return result.scalar_one_or_none()

    @staticmethod
    async def get_members(session: AsyncSession) -> list[User]:
        result = await session.execute(select(User).where(User.is_member.is_(True)))
        return list(result.scalars().all())

    @staticmethod
    async def set_member(
        session: AsyncSession,
        telegram_id: int,
        is_member: bool,
        joined_at=None,
        left_at=None,
    ) -> None:
        values: dict = {"is_member": is_member}
        if joined_at is not None:
            values["joined_at"] = joined_at
        if left_at is not None:
            values["left_at"] = left_at
        await session.execute(update(User).where(User.id == telegram_id).values(**values))
        await session.flush()

    @staticmethod
    async def get_admins(session: AsyncSession) -> list[User]:
        result = await session.execute(select(User).where(User.is_admin.is_(True)))
        return list(result.scalars().all())
