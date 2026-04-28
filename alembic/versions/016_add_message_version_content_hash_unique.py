"""add_message_version_content_hash_unique

T2-81: adds a DB-level UNIQUE constraint on ``(chat_message_id, content_hash)`` in
``message_versions``.  This backs the idempotency guarantee that was previously enforced
only in application code via ``MessageVersionRepo.insert_version``'s ``get_by_hash``
check, creating a TOCTOU window under concurrent identical edits.

Before creating the constraint, duplicate ``(chat_message_id, content_hash)`` pairs are
removed: all but the row with the lowest ``id`` per group are deleted.  Note — the FK
``chat_messages.current_version_id`` is ``ON DELETE SET NULL``, so if a deleted version
row was referenced there the parent chat_messages row gets ``current_version_id = NULL``.
This is acceptable for legacy duplicate cleanup; the live ingestion path will re-bind
``current_version_id`` on the next edit event.

Revision ID: 016
Revises: 015
Create Date: 2026-04-28
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Step 1: remove duplicate (chat_message_id, content_hash) pairs so the UNIQUE
    # constraint can be created cleanly.  Keep only the lowest-id row per group.
    op.execute(
        """
        DELETE FROM message_versions
        WHERE id IN (
            SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY chat_message_id, content_hash
                           ORDER BY id
                       ) AS rn
                FROM message_versions
            ) t
            WHERE rn > 1
        )
        """
    )

    # Step 2: add the unique constraint.
    op.create_unique_constraint(
        "uq_message_versions_chat_message_content_hash",
        "message_versions",
        ["chat_message_id", "content_hash"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_message_versions_chat_message_content_hash",
        "message_versions",
        type_="unique",
    )
