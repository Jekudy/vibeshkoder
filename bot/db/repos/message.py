"""Repository for ``chat_messages``.

T0-03: ``MessageRepo.save`` is idempotent on ``(chat_id, message_id)`` — repeat saves return
the existing row without raising and without producing a duplicate. The previous
implementation used ``session.add`` + ``flush`` and threw ``IntegrityError`` on duplicates,
forcing the handler to do a broad ``session.rollback()`` that also wiped the upstream
``UserRepo.upsert`` and ``set_member`` calls in the same transaction.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
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
        """Idempotent save: returns the existing row on duplicate ``(chat_id, message_id)``.

        Implementation: postgres ``INSERT ... ON CONFLICT DO NOTHING RETURNING id``.
        On conflict the RETURNING row is empty; we then ``SELECT`` the existing row.
        Both operations live in the caller's transaction (no commit, no rollback here).

        Idempotency key: the unique index ``ix_chat_messages_chat_msg`` on
        ``(chat_id, message_id)`` (see ``bot/db/models.py`` ``ChatMessage.__table_args__``).
        """
        stmt = (
            pg_insert(ChatMessage)
            .values(
                message_id=message_id,
                chat_id=chat_id,
                user_id=user_id,
                text=text,
                date=date,
                raw_json=raw_json,
            )
            .on_conflict_do_nothing(index_elements=["chat_id", "message_id"])
            .returning(ChatMessage)
        )
        result = await session.execute(stmt)
        inserted = result.scalar_one_or_none()
        if inserted is not None:
            # Mirror UserRepo.upsert / set_member: flush after a write so the row is
            # visible inside the caller's transaction without committing.
            await session.flush()
            return inserted

        # Conflict path: the row already exists. The follow-up SELECT triggers SQLAlchemy's
        # autoflush, so no explicit flush is required here (and there is nothing to flush —
        # the conflict path made no mutation).
        existing = await session.execute(
            select(ChatMessage).where(
                ChatMessage.chat_id == chat_id,
                ChatMessage.message_id == message_id,
            )
        )
        return existing.scalar_one()

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
