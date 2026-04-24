from __future__ import annotations

from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.engine import async_session


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI Depends helper that yields an AsyncSession.

    Commits on successful exit, rolls back on exception, always closes.
    """
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
