"""Repository for ``telegram_updates`` (T1-03).

Thin data-access layer. T1-04 (raw update persistence service) wires this into the bot's
update path with the ``#offrecord`` ordering rule (detector + redaction in the same DB
transaction as the raw insert). This ticket only provides the SQL primitives.
"""

from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import TelegramUpdate


class TelegramUpdateRepo:
    @staticmethod
    async def insert(
        session: AsyncSession,
        update_type: str,
        update_id: int | None = None,
        raw_json: dict | None = None,
        raw_hash: str | None = None,
        chat_id: int | None = None,
        message_id: int | None = None,
        ingestion_run_id: int | None = None,
        is_redacted: bool = False,
        redaction_reason: str | None = None,
    ) -> TelegramUpdate:
        """Insert a raw Telegram update row.

        For live updates with a non-null ``update_id`` this method is idempotent: a
        duplicate insert (same ``update_id``) returns the existing row instead of raising.
        The conflict path is keyed by the partial unique index
        ``ix_telegram_updates_update_id`` (created in migration 005), so it fires only when
        ``update_id`` is set — synthetic import rows (NULL ``update_id``) always insert.

        Flushes; does not commit. Caller controls the transaction lifecycle.
        """
        if update_id is not None:
            stmt = (
                pg_insert(TelegramUpdate)
                .values(
                    update_id=update_id,
                    update_type=update_type,
                    raw_json=raw_json,
                    raw_hash=raw_hash,
                    chat_id=chat_id,
                    message_id=message_id,
                    ingestion_run_id=ingestion_run_id,
                    is_redacted=is_redacted,
                    redaction_reason=redaction_reason,
                )
                # Target the PARTIAL unique index `ix_telegram_updates_update_id` —
                # postgres ON CONFLICT requires the conflict target to match the index
                # exactly, including its WHERE predicate. Without ``index_where`` the
                # planner can refuse the upsert with "no unique or exclusion constraint
                # matching the ON CONFLICT specification".
                .on_conflict_do_nothing(
                    index_elements=["update_id"],
                    index_where=text("update_id IS NOT NULL"),
                )
                .returning(TelegramUpdate)
            )
            result = await session.execute(stmt)
            inserted = result.scalar_one_or_none()
            if inserted is not None:
                await session.flush()
                return inserted
            # Conflict path — fetch and return the existing row.
            existing = await session.execute(
                select(TelegramUpdate).where(TelegramUpdate.update_id == update_id)
            )
            return existing.scalar_one()

        # No update_id (synthetic import) — always insert.
        row = TelegramUpdate(
            update_type=update_type,
            raw_json=raw_json,
            raw_hash=raw_hash,
            chat_id=chat_id,
            message_id=message_id,
            ingestion_run_id=ingestion_run_id,
            is_redacted=is_redacted,
            redaction_reason=redaction_reason,
        )
        session.add(row)
        await session.flush()
        return row

    @staticmethod
    async def get_by_update_id(
        session: AsyncSession, update_id: int
    ) -> TelegramUpdate | None:
        result = await session.execute(
            select(TelegramUpdate).where(TelegramUpdate.update_id == update_id)
        )
        return result.scalar_one_or_none()
