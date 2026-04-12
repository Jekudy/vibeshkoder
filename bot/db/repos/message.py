from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import ChatMessage


class MessageRepo:
    @staticmethod
    async def save(
        session: AsyncSession,
        message_id: int,
        chat_id: int,
        user_id: int,
        text: str | None,
        date: datetime,
        raw_json: dict | None = None,
    ) -> ChatMessage:
        msg = ChatMessage(
            message_id=message_id,
            chat_id=chat_id,
            user_id=user_id,
            text=text,
            date=date,
            raw_json=raw_json,
        )
        session.add(msg)
        await session.flush()
        return msg

    @staticmethod
    async def find_by_exact_text(
        session: AsyncSession, text: str
    ) -> ChatMessage | None:
        result = await session.execute(
            select(ChatMessage)
            .where(ChatMessage.text == text)
            .order_by(ChatMessage.date.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()
