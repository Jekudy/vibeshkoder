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

_IMPORT_MESSAGE_INDEX_PREDICATE = (
    "update_id IS NULL "
    "AND update_type = 'import_message' "
    "AND chat_id IS NOT NULL "
    "AND message_id IS NOT NULL"
)


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
        ``update_id`` is set. Canonical ``import_message`` callers must use
        :meth:`upsert_import_message`; this generic NULL-``update_id`` path does not
        resolve source-identity conflicts.

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

        # No update_id (generic synthetic row) — direct insert.
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
    async def upsert_import_message(
        session: AsyncSession,
        *,
        raw_json: dict,
        chat_id: int,
        message_id: int,
        ingestion_run_id: int,
    ) -> TelegramUpdate:
        """Atomically create or refresh one canonical synthetic import raw row.

        Conflict updates deliberately preserve the original ``ingestion_run_id``.
        Moving ownership to a later duplicate run could make its rollback delete a
        raw row referenced by a normalized message created by the original run.
        """
        insert_stmt = pg_insert(TelegramUpdate).values(
            update_type="import_message",
            update_id=None,
            raw_json=raw_json,
            raw_hash=None,
            chat_id=chat_id,
            message_id=message_id,
            ingestion_run_id=ingestion_run_id,
            is_redacted=False,
            redaction_reason=None,
        )
        statement = insert_stmt.on_conflict_do_update(
            index_elements=["update_type", "chat_id", "message_id"],
            index_where=text(_IMPORT_MESSAGE_INDEX_PREDICATE),
            set_={
                "raw_json": insert_stmt.excluded.raw_json,
                "raw_hash": None,
                "is_redacted": False,
                "redaction_reason": None,
            },
        ).returning(TelegramUpdate)
        row = (await session.execute(statement)).scalar_one()
        await session.flush()
        return row

    @staticmethod
    async def get_by_update_id(session: AsyncSession, update_id: int) -> TelegramUpdate | None:
        result = await session.execute(
            select(TelegramUpdate).where(TelegramUpdate.update_id == update_id)
        )
        return result.scalar_one_or_none()
