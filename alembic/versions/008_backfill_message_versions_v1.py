"""backfill_message_versions_v1

T1-07: walk legacy ``chat_messages`` rows (``current_version_id IS NULL``) and create a
``message_versions`` v1 row for each. Sets ``chat_messages.current_version_id`` so the
operation is idempotent — re-running this migration after success is a no-op because
the WHERE clause excludes already-backfilled rows.

Implementation note (Codex BLOCKER #1 fix):
``alembic/env.py`` already runs migrations under ``asyncio.run(...)`` and hands a SYNC
connection to ``do_run_migrations`` via ``connection.run_sync(...)``. Inside an
``upgrade()`` body, ``op.get_bind()`` returns that sync connection — wrapped on top of
the async driver. Calling ``asyncio.run(...)`` here would nest event loops and raise
``RuntimeError`` at runtime. So this migration uses the sync connection from
``op.get_bind()`` directly, runs the same hash + INSERT logic via plain SQLAlchemy
Core, and stays inside Alembic's transaction.

Because the work is done in the same transaction Alembic opens for the migration,
``commit_per_batch`` is NOT applicable here. For ~5k–50k legacy rows this is acceptable
on first deployment; if the dataset grows past this, a future ticket will switch to a
data-only migration that opens its own connection outside Alembic.

Concurrency safety (Codex Sprint #80 Finding 2 HIGH):
Per-row protocol makes this migration race-safe even if run during live ingestion:

1. ``pg_advisory_xact_lock(hashtext('chat_msg:{chat_id}:{message_id}'))`` — same key
   format as ``bot/db/locks.py:21``. Serializes against any concurrent transaction
   (live edited_message, chat_messages handlers) for the same message row.
2. Re-read the row with ``FOR UPDATE`` after the lock. Picks up any offrecord flip or
   ``current_version_id`` write that landed between the batch SELECT and lock
   acquisition.
3. If post-lock ``current_version_id IS NOT NULL`` — a concurrent live handler already
   wrote v1 — skip this row (idempotent).
4. Build ``message_versions`` from the POST-LOCK fresh values (text, caption,
   is_redacted). A concurrent normal→offrecord flip nulls text/caption and sets
   is_redacted=True; using stale pre-lock values would persist an UNREDACTED version
   row (privacy bypass).

Revision ID: 008
Revises: 007
Create Date: 2026-04-27
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from bot.services.content_hash import compute_content_hash

revision: str = "008"
down_revision: Union[str, Sequence[str], None] = "007"
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
            f"T1-07 backfill requires postgres (got {bind.dialect.name!r}). See T0-02."
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

    print(f"[T1-07] backfilled v1 message_versions for {total} chat_messages rows")


def downgrade() -> None:
    """Wipe v1 versions created by the backfill and reset current_version_id.

    Targets only ``version_seq=1`` rows whose ``raw_update_id IS NULL`` (heuristic for
    backfilled rows — live ingestion writes raw_update_id once T1-04+T1-14 land). Any
    manually-inserted v1 test rows with NULL ``raw_update_id`` would also be wiped, but
    that's acceptable in the downgrade path since tests use isolated transactions.
    """
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            UPDATE chat_messages
            SET current_version_id = NULL
            WHERE current_version_id IN (
                SELECT id FROM message_versions
                WHERE version_seq = 1 AND raw_update_id IS NULL
            )
            """
        )
    )
    bind.execute(
        sa.text(
            """
            DELETE FROM message_versions
            WHERE version_seq = 1 AND raw_update_id IS NULL
            """
        )
    )
