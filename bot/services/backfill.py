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
"""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

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
            content_hash = compute_content_hash(
                text=msg.text,
                caption=msg.caption,
                message_kind=msg.message_kind,
                entities=None,
            )
            version = MessageVersion(
                chat_message_id=msg.id,
                version_seq=1,
                text=msg.text,
                caption=msg.caption,
                normalized_text=msg.text,
                entities_json=None,
                edit_date=None,
                # Pin to the original message moment so q&a citations remain stable
                # regardless of when the backfill ran (per HANDOFF.md §6 #5 backfill
                # clause + issue #31 acceptance "captured_at=date").
                captured_at=msg.date,
                content_hash=content_hash,
                raw_update_id=msg.raw_update_id,
                is_redacted=msg.is_redacted,
            )
            session.add(version)
            await session.flush()  # need version.id for the chat_messages update below

            await session.execute(
                update(ChatMessage)
                .where(ChatMessage.id == msg.id)
                .values(current_version_id=version.id)
            )
            total += 1

        if commit_per_batch:
            await session.commit()

        # If we got fewer than batch_size, no more rows to process.
        if len(batch) < batch_size:
            break

    return total
