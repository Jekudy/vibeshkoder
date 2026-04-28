"""Repository for ``message_versions`` (T1-06).

Thin data-access layer. T1-14 (edited_message handler) and T1-07 (v1 backfill) wire this
into the live ingestion path. The repo enforces idempotency on
``(chat_message_id, content_hash)``: if a version with the same hash already exists for
the same message, ``insert_version`` returns it instead of creating a duplicate.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
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
    async def get_max_version_seq(session: AsyncSession, chat_message_id: int) -> int:
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
        imported_final: bool = False,
    ) -> MessageVersion:
        """Idempotent version insert.

        Returns the existing version row if ``(chat_message_id, content_hash)`` already
        exists; otherwise creates a new row at ``version_seq = max + 1``. Flushes; does
        not commit.

        ``imported_final`` (T2-03 / #103, see #106): set to True only by the import apply
        path. Live ingestion leaves it False. The column is denormalised provenance — the
        FK chain ``raw_update_id → telegram_updates.ingestion_run_id → run_type`` remains
        the audit trail. See ``docs/memory-system/import-edit-history.md``.

        Concurrency safety: the INSERT is wrapped in a savepoint (``session.begin_nested``).
        If two concurrent callers both pass the ``get_by_hash`` check (TOCTOU window) and
        race to insert the same ``(chat_message_id, content_hash)``, one of them will hit
        the ``uq_message_versions_chat_message_content_hash`` unique constraint and receive
        an ``IntegrityError``. The savepoint rolls back only the failed sub-transaction,
        leaving the outer session intact. The losing caller then reselects the winner's row
        via ``get_by_hash`` and returns it. Any other ``IntegrityError`` (FK violation, NOT
        NULL, etc.) is re-raised so the caller sees the real problem.
        """
        existing = await MessageVersionRepo.get_by_hash(session, chat_message_id, content_hash)
        if existing is not None:
            return existing

        try:
            async with session.begin_nested():
                next_seq = (
                    await MessageVersionRepo.get_max_version_seq(session, chat_message_id)
                ) + 1

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
                    imported_final=imported_final,
                )
                session.add(row)
                await session.flush()
        except IntegrityError:
            # Concurrent insert won the race on uq_message_versions_chat_message_content_hash.
            # Reselect and return their row.
            existing = await MessageVersionRepo.get_by_hash(session, chat_message_id, content_hash)
            if existing is not None:
                return existing
            raise  # unrelated IntegrityError (FK, NOT NULL, etc.) — let caller see it

        return row
