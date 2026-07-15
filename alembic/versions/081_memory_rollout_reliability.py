"""Add retry metadata and Q&A delivery idempotency.

Revision ID: 081
Revises: 080
Create Date: 2026-07-14
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "081"
down_revision: Union[str, Sequence[str], None] = "080"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "message_media",
        sa.Column(
            "description_attempts",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "message_media",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "message_media",
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_message_media_pending_due",
        "message_media",
        ["description_status", "next_attempt_at"],
        unique=False,
    )

    op.add_column(
        "qa_traces",
        sa.Column("source_chat_message_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_qa_traces_source_chat_message_id",
        "qa_traces",
        "chat_messages",
        ["source_chat_message_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "uq_qa_traces_source_chat_message_id",
        "qa_traces",
        ["source_chat_message_id"],
        unique=True,
        postgresql_where=sa.text("source_chat_message_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM qa_traces
                WHERE source_chat_message_id IS NOT NULL
            ) THEN
                RAISE EXCEPTION
                    'Cannot downgrade 081: qa_traces contains delivery idempotency data';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM message_media
                WHERE description_attempts <> 0
                   OR next_attempt_at IS NOT NULL
                   OR last_error_code IS NOT NULL
            ) THEN
                RAISE EXCEPTION
                    'Cannot downgrade 081: message_media contains retry metadata';
            END IF;
        END
        $$
        """
    )
    op.drop_index("uq_qa_traces_source_chat_message_id", table_name="qa_traces")
    op.drop_constraint(
        "fk_qa_traces_source_chat_message_id",
        "qa_traces",
        type_="foreignkey",
    )
    op.drop_column("qa_traces", "source_chat_message_id")

    op.drop_index("ix_message_media_pending_due", table_name="message_media")
    op.drop_column("message_media", "last_error_code")
    op.drop_column("message_media", "next_attempt_at")
    op.drop_column("message_media", "description_attempts")
