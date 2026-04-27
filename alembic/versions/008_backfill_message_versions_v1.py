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
    SELECT id, text, caption, message_kind, date, raw_update_id, is_redacted
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
            content_hash = compute_content_hash(
                text=row.text,
                caption=row.caption,
                message_kind=row.message_kind,
                entities=None,
            )
            inserted = bind.execute(
                _INSERT_VERSION,
                {
                    "chat_message_id": row.id,
                    "text": row.text,
                    "caption": row.caption,
                    "normalized_text": row.text,
                    # Pin to the original message moment so q&a citations remain stable
                    # regardless of when the migration ran (per HANDOFF.md §6 #5 +
                    # issue #31 acceptance "captured_at=date").
                    "captured_at": row.date,
                    "content_hash": content_hash,
                    "raw_update_id": row.raw_update_id,
                    "is_redacted": row.is_redacted,
                },
            )
            version_id = inserted.scalar()
            bind.execute(
                _UPDATE_PARENT,
                {"version_id": version_id, "chat_message_id": row.id},
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
