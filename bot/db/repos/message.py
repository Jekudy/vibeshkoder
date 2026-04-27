"""Repository for ``chat_messages``.

T0-03: ``MessageRepo.save`` is idempotent on ``(chat_id, message_id)`` — repeat saves return
the existing row without raising and without producing a duplicate. The previous
implementation used ``session.add`` + ``flush`` and threw ``IntegrityError`` on duplicates,
forcing the handler to do a broad ``session.rollback()`` that also wiped the upstream
``UserRepo.upsert`` and ``set_member`` calls in the same transaction.

Issue #67: upgraded from ``ON CONFLICT DO NOTHING`` to conditional ``ON CONFLICT DO UPDATE``
so that a re-delivered message (polling restart, retry, late edit) refreshes the
``memory_policy`` / ``is_redacted`` fields when the caller explicitly passes them. Without
this, a duplicate delivery returns the OLD row with the stale policy, causing the
``chat_messages`` handler to attach a fresh ``offrecord_marks`` row to a row whose policy
fields are out of date (duplicate marks + policy desync).

On conflict behaviour (Issue #67):
- Caller passes at least one of ``memory_policy`` / ``is_redacted`` (non-None) →
  ``ON CONFLICT DO UPDATE SET`` only those fields. RETURNING always returns the resulting row.
- Both are None (legacy gatekeeper-era code path / T0-03 callers) →
  falls back to ``ON CONFLICT DO NOTHING`` + a SELECT for the existing row (preserves
  original T0-03 semantics — the None caller cannot overwrite policy fields with NULL).

All other fields (ids, timestamps, text, raw_json) stay immutable on conflict per
content_hash invariant — edits go through message_versions, not re-saves.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import case, select
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
        reply_to_message_id: int | None = None,
        message_thread_id: int | None = None,
        caption: str | None = None,
        message_kind: str | None = None,
        raw_update_id: int | None = None,
        memory_policy: str | None = None,
        is_redacted: bool | None = None,
    ) -> ChatMessage:
        """Idempotent save: returns the resulting row on both insert and conflict paths.

        On a fresh ``(chat_id, message_id)`` → INSERT, then RETURNING.

        On duplicate ``(chat_id, message_id)`` (Issue #67):
        - If ``memory_policy`` or ``is_redacted`` is non-None → ``ON CONFLICT DO UPDATE``
          refreshes only those policy fields. RETURNING always yields the post-update row.
          No separate SELECT needed.
        - If both are None (legacy callers) → ``ON CONFLICT DO NOTHING``; the existing row
          is fetched via a separate SELECT. Preserves original T0-03 contract: a None
          caller cannot clobber policy fields with NULL.

        Idempotency key: ``ix_chat_messages_chat_msg`` unique index on ``(chat_id, message_id)``.

        T1-09/10/11 normalized fields are optional kwargs. If omitted they default to None
        (rows from the gatekeeper-era code path keep their original nullable shape).
        Both operations live in the caller's transaction (no commit, no rollback here).
        Flushes after the write so the row is visible inside the caller's transaction.
        """
        values: dict = {
            "message_id": message_id,
            "chat_id": chat_id,
            "user_id": user_id,
            "text": text,
            "date": date,
            "raw_json": raw_json,
        }
        if reply_to_message_id is not None:
            values["reply_to_message_id"] = reply_to_message_id
        if message_thread_id is not None:
            values["message_thread_id"] = message_thread_id
        if caption is not None:
            values["caption"] = caption
        if message_kind is not None:
            values["message_kind"] = message_kind
        if raw_update_id is not None:
            values["raw_update_id"] = raw_update_id
        if memory_policy is not None:
            values["memory_policy"] = memory_policy
        if is_redacted is not None:
            values["is_redacted"] = is_redacted

        # Determine whether the caller wants policy fields refreshed on conflict.
        wants_policy_update = memory_policy is not None or is_redacted is not None

        if wants_policy_update:
            # Build the SET clause with only the fields that were explicitly passed.
            # Immutable fields (ids, text, date, raw_json) are never updated on conflict.
            #
            # STICKY POLICY (Codex CRITICAL / Sprint #80 fixup):
            # memory_policy and is_redacted are monotonically ratcheted toward more-restrictive
            # values. A stale duplicate original delivery (e.g. Telegram polling glitch re-delivers
            # a message after its #offrecord edit) must NOT downgrade an existing 'offrecord' row
            # back to 'normal'. The CASE expressions below enforce this invariant atomically in SQL:
            #
            #   memory_policy: if the stored value is already 'offrecord', keep 'offrecord';
            #                  otherwise adopt the incoming EXCLUDED.memory_policy.
            #
            #   is_redacted:   once True, stays True — OR semantics: existing OR incoming.
            #                  A caller passing is_redacted=False cannot unflag a redacted row.
            #
            # Build the INSERT statement first so .excluded references are on the same object.
            insrt = pg_insert(ChatMessage).values(**values)
            set_clause: dict = {}
            if memory_policy is not None:
                set_clause["memory_policy"] = case(
                    (ChatMessage.memory_policy == "offrecord", "offrecord"),
                    else_=insrt.excluded.memory_policy,
                )
            if is_redacted is not None:
                set_clause["is_redacted"] = case(
                    (ChatMessage.is_redacted.is_(True), True),
                    else_=insrt.excluded.is_redacted,
                )

            stmt = (
                insrt
                .on_conflict_do_update(
                    index_elements=["chat_id", "message_id"],
                    set_=set_clause,
                )
                .returning(ChatMessage)
            )
            result = await session.execute(stmt)
            row = result.scalar_one()
            await session.flush()
            return row

        # Legacy path: both policy args are None → DO NOTHING, then SELECT.
        stmt_nothing = (
            pg_insert(ChatMessage)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["chat_id", "message_id"])
            .returning(ChatMessage)
        )
        result = await session.execute(stmt_nothing)
        inserted = result.scalar_one_or_none()
        if inserted is not None:
            # Mirror UserRepo.upsert / set_member: flush after a write so the row is
            # visible inside the caller's transaction without committing.
            await session.flush()
            return inserted

        # Conflict path: the row already exists. The follow-up SELECT triggers SQLAlchemy's
        # autoflush, so no explicit flush is required here (and there is nothing to flush —
        # the conflict path made no mutation).
        # #80: use SELECT ... FOR UPDATE to acquire a row-level lock for the duration of
        # the transaction. This prevents a TOCTOU race where a concurrent transaction
        # (e.g. an offrecord flip in edited_message.py) writes memory_policy AFTER this
        # transaction has read 'normal' but before it has committed.
        # Note: with_for_update(key_share=False) in SQLAlchemy emits plain FOR UPDATE
        # (not FOR NO KEY UPDATE). The key_share flag controls the shared variant only;
        # the exclusive variant is always full FOR UPDATE regardless of key_share.
        existing = await session.execute(
            select(ChatMessage)
            .where(
                ChatMessage.chat_id == chat_id,
                ChatMessage.message_id == message_id,
            )
            .with_for_update(key_share=False)
        )
        return existing.scalar_one()

    @staticmethod
    async def find_by_exact_text(session: AsyncSession, text: str) -> ChatMessage | None:
        result = await session.execute(
            select(ChatMessage)
            .where(ChatMessage.text == text)
            .order_by(ChatMessage.date.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()
