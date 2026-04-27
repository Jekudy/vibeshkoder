"""add_message_versions

T1-06: provenance + edit history for chat_messages. Each version pins a content state
(``text``, ``caption``, ``entities_json``, ``message_kind`` via the parent row) at a
specific moment. New incoming messages create v1; edits append v(n+1) only when the
``content_hash`` changes (T1-14 wiring).

Also closes T1-05's forward-reference: ``chat_messages.current_version_id`` becomes a
real FK to ``message_versions.id`` (ON DELETE SET NULL — keeping the message row even if
the latest version is hard-deleted, e.g. by a forget cascade in Phase 3).

T1-07 will backfill existing chat_messages with a v1 row.

Revision ID: 007
Revises: 006
Create Date: 2026-04-27
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "007"
down_revision: Union[str, Sequence[str], None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "message_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("chat_message_id", sa.Integer(), nullable=False),
        sa.Column("version_seq", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column("normalized_text", sa.Text(), nullable=True),
        sa.Column("entities_json", sa.JSON(), nullable=True),
        sa.Column("edit_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("content_hash", sa.String(length=128), nullable=False),
        sa.Column("raw_update_id", sa.Integer(), nullable=True),
        sa.Column(
            "is_redacted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["chat_message_id"],
            ["chat_messages.id"],
            name="fk_message_versions_chat_message_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["raw_update_id"],
            ["telegram_updates.id"],
            name="fk_message_versions_raw_update_id",
            ondelete="SET NULL",
        ),
        # Logical key: (chat_message_id, version_seq) is unique. Two writers cannot
        # produce v=2 for the same chat_message concurrently — we rely on this when
        # T1-14 computes max(version_seq)+1 inside a transaction.
        sa.UniqueConstraint(
            "chat_message_id",
            "version_seq",
            name="uq_message_versions_chat_message_seq",
        ),
    )
    op.create_index(
        "ix_message_versions_content_hash", "message_versions", ["content_hash"]
    )
    op.create_index(
        "ix_message_versions_captured_at", "message_versions", ["captured_at"]
    )
    # Helper index for "all versions of one message" lookups.
    op.create_index(
        "ix_message_versions_chat_message_id",
        "message_versions",
        ["chat_message_id"],
    )

    # Close T1-05's forward-ref: chat_messages.current_version_id → message_versions.id.
    # ON DELETE SET NULL so a forget-cascade that wipes versions does not orphan the
    # message row (the message becomes "no current version" rather than disappearing).
    op.create_foreign_key(
        "fk_chat_messages_current_version_id",
        "chat_messages",
        "message_versions",
        ["current_version_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_chat_messages_current_version_id",
        "chat_messages",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_message_versions_chat_message_id", table_name="message_versions"
    )
    op.drop_index(
        "ix_message_versions_captured_at", table_name="message_versions"
    )
    op.drop_index(
        "ix_message_versions_content_hash", table_name="message_versions"
    )
    op.drop_table("message_versions")
