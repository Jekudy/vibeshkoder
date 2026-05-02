"""backfill_v1_live_post_t1_07

Backfill v1 MessageVersion for chat_messages with current_version_id IS NULL
(post-008 cohort).

Created post-T1-07 deploy of v1 backfill (008). This migration walks the same WHERE
clause as 008 to close the cohort of live messages persisted between 008 deploy and
the Phase 4 hotfix deploy (issue #164). Walks ``chat_messages WHERE
current_version_id IS NULL``, with no date filter — 008 was idempotent on the same
WHERE, so 023 produces the same effect on any uncovered row regardless of ingestion
date.

Idempotency: ``MessageVersionRepo.insert_version`` already idempotent on
``(chat_message_id, content_hash)``; UPDATE current_version_id is a no-op when
already set. Re-running migration 023 produces zero net mutations.

Concurrency safety mirrors 008: per-row advisory lock + FOR UPDATE re-read.

Revision ID: 023
Revises: 022_add_qa_traces
Create Date: 2026-05-02
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from bot.services.content_hash import compute_content_hash

revision: str = "023"
down_revision: Union[str, Sequence[str], None] = "022_add_qa_traces"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_BATCH_SIZE = 1000

_SELECT_LEGACY = sa.text(
    """
    SELECT id, chat_id, message_id, text, caption, message_kind, date, raw_update_id, is_redacted
    FROM chat_messages
    WHERE current_version_id IS NULL
    ORDER BY id
    LIMIT :batch_size
    """
)

_INSERT_VERSION = sa.text(
    """
    INSERT INTO message_versions
        (chat_message_id, version_seq, text, caption, normalized_text,
         entities_json, edit_date, captured_at, content_hash, raw_update_id, is_redacted)
    VALUES
        (:chat_message_id, 1, :text, :caption, :normalized_text,
         NULL, NULL, :captured_at, :content_hash, :raw_update_id, :is_redacted)
    RETURNING id
    """
)

_UPDATE_PARENT = sa.text(
    """
    UPDATE chat_messages
    SET current_version_id = :version_id
    WHERE id = :chat_message_id
    """
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError(
            f"023 backfill requires postgres (got {bind.dialect.name!r}). See T0-02."
        )

    total = 0
    while True:
        rows = list(bind.execute(_SELECT_LEGACY, {"batch_size": _BATCH_SIZE}))
        if not rows:
            break

        for row in rows:
            # 1. Per-row advisory lock — same key format as bot/db/locks.py:21.
            # Serializes against concurrent live handlers (edited_message, chat_messages)
            # for this exact message row.
            lock_key = f"chat_msg:{row.chat_id}:{row.message_id}"
            bind.execute(
                sa.text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": lock_key}
            )

            # 2. Re-read row with FOR UPDATE to pick up any offrecord flip or
            # current_version_id write that landed between the batch SELECT and the
            # lock acquisition above.
            fresh = bind.execute(
                sa.text(
                    "SELECT id, text, caption, message_kind, date, raw_update_id,"
                    " is_redacted, current_version_id"
                    " FROM chat_messages WHERE id = :id FOR UPDATE"
                ),
                {"id": row.id},
            ).one()

            # 3. Skip if a concurrent live handler already wrote v1 for this row.
            if fresh.current_version_id is not None:
                continue

            # 4. Build version from POST-LOCK fresh values. A concurrent
            # normal→offrecord flip nulls text/caption and sets is_redacted=True;
            # using stale pre-lock values would persist an UNREDACTED version row
            # (privacy bypass).
            content_hash = compute_content_hash(
                text=fresh.text,
                caption=fresh.caption,
                message_kind=fresh.message_kind,
                entities=None,
            )
            inserted = bind.execute(
                _INSERT_VERSION,
                {
                    "chat_message_id": fresh.id,
                    "text": fresh.text,
                    "caption": fresh.caption,
                    "normalized_text": fresh.text,
                    # Pin to the original message moment so q&a citations remain stable
                    # regardless of when the migration ran (per HANDOFF.md §6 #5 +
                    # issue #31 acceptance "captured_at=date").
                    "captured_at": fresh.date,
                    "content_hash": content_hash,
                    "raw_update_id": fresh.raw_update_id,
                    "is_redacted": fresh.is_redacted,
                },
            )
            version_id = inserted.scalar()
            bind.execute(
                _UPDATE_PARENT,
                {"version_id": version_id, "chat_message_id": fresh.id},
            )
            total += 1

        if len(rows) < _BATCH_SIZE:
            break

    print(f"[023] backfilled v1 message_versions for {total} chat_messages rows")


def downgrade() -> None:
    """Forward-only data migration. v1 rows are not destroyed on rollback.

    This migration only adds rows and sets current_version_id on rows that had it
    NULL. The downgrade is a no-op rather than a destructive wipe, because:
    - The v1 rows were the missing data, not surplus data.
    - Rolling back the schema (if ever needed) would be a separate ticket.
    """
    pass
