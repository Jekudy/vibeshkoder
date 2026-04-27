"""Repository for ``message_versions`` (T1-06).

Thin data-access layer. T1-14 (edited_message handler) and T1-07 (v1 backfill) wire this
into the live ingestion path. The repo enforces idempotency on
``(chat_message_id, content_hash)``: if a version with the same hash already exists for
the same message, ``insert_version`` returns it instead of creating a duplicate.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import MessageVersion


class MessageVersionRepo:
    @staticmethod
    async def get_by_hash(
        session: AsyncSession,
        chat_message_id: int,
        content_hash: str,
    ) -> MessageVersion | None:
        """Return the version row matching ``(chat_message_id, content_hash)``, if any."""
        result = await session.execute(
            select(MessageVersion).where(
                MessageVersion.chat_message_id == chat_message_id,
                MessageVersion.content_hash == content_hash,
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_max_version_seq(
        session: AsyncSession, chat_message_id: int
    ) -> int:
        """Return the largest ``version_seq`` for the given message, or 0 if none."""
        result = await session.execute(
            select(func.max(MessageVersion.version_seq)).where(
                MessageVersion.chat_message_id == chat_message_id
            )
        )
        max_seq = result.scalar_one()
        return int(max_seq) if max_seq is not None else 0

    @staticmethod
    async def insert_version(
        session: AsyncSession,
        chat_message_id: int,
        content_hash: str,
        text: str | None = None,
        caption: str | None = None,
        normalized_text: str | None = None,
        entities_json: dict | None = None,
        edit_date: datetime | None = None,
        raw_update_id: int | None = None,
        is_redacted: bool = False,
    ) -> MessageVersion:
        """Idempotent version insert.

        Returns the existing version row if ``(chat_message_id, content_hash)`` already
        exists; otherwise creates a new row at ``version_seq = max + 1``. Flushes; does
        not commit.

        The check-then-create pattern is safe in this codebase because the bot is
        deployed single-instance (one polling consumer per environment, see HANDOFF.md
        §0). If we ever go multi-instance, the unique constraint
        ``uq_message_versions_chat_message_seq`` would catch concurrent v(n+1) creation
        with an ``IntegrityError``, which the caller can recover from by retrying the
        ``get_max_version_seq`` lookup.
        """
        existing = await MessageVersionRepo.get_by_hash(
            session, chat_message_id, content_hash
        )
        if existing is not None:
            return existing

        next_seq = (await MessageVersionRepo.get_max_version_seq(session, chat_message_id)) + 1

        row = MessageVersion(
            chat_message_id=chat_message_id,
            version_seq=next_seq,
            text=text,
            caption=caption,
            normalized_text=normalized_text,
            entities_json=entities_json,
            edit_date=edit_date,
            content_hash=content_hash,
            raw_update_id=raw_update_id,
            is_redacted=is_redacted,
        )
        session.add(row)
        await session.flush()
        return row
