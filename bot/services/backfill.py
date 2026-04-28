"""Backfill helpers (T1-07 v1 message_versions).

The backfill walks ``chat_messages`` rows where ``current_version_id IS NULL`` (legacy
rows from before T1-06), computes a content hash, inserts a ``message_versions`` row
with ``version_seq=1`` and ``captured_at=msg.date`` (so q&a citations point at the
original message moment, not at the migration moment), and sets
``chat_messages.current_version_id``. The query's WHERE clause makes the operation
idempotent — re-running after success is a no-op because every targeted row will have
``current_version_id`` set.

Chunked: 1000 rows per batch by default. The function flushes after each batch but
does NOT commit — the caller chooses the transaction boundary. The alembic migration
commits per batch via the ``commit_per_batch=True`` flag to keep prod transactions
bounded; tests use a single outer transaction with rollback for isolation and pass
``commit_per_batch=False`` (default).

Concurrency safety (Codex Sprint #80 Finding 2 HIGH):
The backfill is now race-free against the live edited_message / chat_messages handlers
by construction. Per-row protocol:

1. Acquire ``pg_advisory_xact_lock`` keyed by ``chat_msg:{chat_id}:{message_id}`` (same
   key the live handlers use in ``bot/handlers/chat_messages.py`` and
   ``bot/handlers/edited_message.py``). Serializes against any concurrent transaction
   for the same message.
2. Re-read the row with ``SELECT ... WHERE id=:id FOR UPDATE`` — picks up any
   offrecord-flip, ``current_version_id`` write, or content edit that landed between
   the batch SELECT and the lock acquisition.
3. If the post-lock row has ``current_version_id IS NOT NULL`` — a concurrent live
   handler already wrote v1 — skip the row.
4. Otherwise insert v1 using the POST-LOCK fresh ``text``, ``caption``,
   ``message_kind``, ``is_redacted`` values. This is critical: a concurrent
   normal→offrecord flip via ``_apply_offrecord_flip`` nulls ``text``/``caption`` and
   sets ``is_redacted=True`` on the parent row — backfill MUST honor that, otherwise
   it persists a stale UNREDACTED version row carrying the original text (privacy
   bypass).

Therefore this function is now safe to run concurrently with live ingestion. The
alembic migration that wraps it can run during a normal deploy window without
requiring the bot polling loop to be paused first.
"""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.locks import advisory_lock_chat_message
from bot.db.models import ChatMessage, MessageVersion
from bot.services.content_hash import compute_content_hash

DEFAULT_BATCH_SIZE = 1000


async def backfill_v1_message_versions(
    session: AsyncSession,
    batch_size: int = DEFAULT_BATCH_SIZE,
    commit_per_batch: bool = False,
) -> int:
    """Backfill v1 ``message_versions`` for legacy ``chat_messages`` rows.

    Returns the total number of rows backfilled in this call.

    Flushes after each batch. Commits per batch if ``commit_per_batch=True`` (the
    alembic data migration enables this so a 50k-row backfill does NOT hold one
    transaction for the whole run). Tests pass the default ``False`` so the outer-tx
    rollback fixture can keep isolation.

    Concurrency: race-free with live ingestion. See module docstring for the per-row
    advisory-lock + FOR-UPDATE re-read protocol.
    """
    total = 0
    while True:
        result = await session.execute(
            select(ChatMessage)
            .where(ChatMessage.current_version_id.is_(None))
            .order_by(ChatMessage.id)
            .limit(batch_size)
        )
        batch = result.scalars().all()
        if not batch:
            break

        for msg in batch:
            # 1. Acquire per-row advisory lock — same key as live handlers
            # (``chat_msg:{chat_id}:{message_id}``). Serializes against concurrent
            # offrecord flips and content edits.
            await advisory_lock_chat_message(session, msg.chat_id, msg.message_id)

            # 2. Re-read the row with FOR UPDATE so we pick up any change a concurrent
            # live handler made between the batch SELECT and the lock acquisition.
            fresh_result = await session.execute(
                select(ChatMessage)
                .where(ChatMessage.id == msg.id)
                .with_for_update(key_share=False)
            )
            fresh = fresh_result.scalar_one()

            # 3. Skip if a concurrent live handler already wrote v1 for this row.
            if fresh.current_version_id is not None:
                continue

            # 4. Use POST-LOCK fresh row state for hash + version. A concurrent
            # normal→offrecord flip nulled text/caption and set is_redacted=True;
            # backfill must honor that to avoid persisting a stale unredacted version.
            content_hash = compute_content_hash(
                text=fresh.text,
                caption=fresh.caption,
                message_kind=fresh.message_kind,
                entities=None,
            )
            version = MessageVersion(
                chat_message_id=fresh.id,
                version_seq=1,
                text=fresh.text,
                caption=fresh.caption,
                normalized_text=fresh.text,
                entities_json=None,
                edit_date=None,
                # Pin to the original message moment so q&a citations remain stable
                # regardless of when the backfill ran (per HANDOFF.md §6 #5 backfill
                # clause + issue #31 acceptance "captured_at=date").
                captured_at=fresh.date,
                content_hash=content_hash,
                raw_update_id=fresh.raw_update_id,
                is_redacted=fresh.is_redacted,
            )
            session.add(version)
            await session.flush()  # need version.id for the chat_messages update below

            await session.execute(
                update(ChatMessage)
                .where(ChatMessage.id == fresh.id)
                .values(current_version_id=version.id)
            )
            total += 1

        if commit_per_batch:
            await session.commit()

        # If we got fewer than batch_size, no more rows to process.
        if len(batch) < batch_size:
            break

    return total
