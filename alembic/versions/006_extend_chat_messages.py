"""extend_chat_messages

T1-05: extend the existing ``chat_messages`` table with normalized fields the memory
system needs (reply / thread / caption / message_kind / memory_policy / visibility /
content_hash / updated_at / FK to telegram_updates / future FK to message_versions).

CRITICAL safety rule: every new column is NULLABLE or has a SERVER DEFAULT. Existing
``chat_messages`` rows persisted by the gatekeeper bot must survive untouched. The new
columns are populated by the normalization service in later tickets (T1-09 reply,
T1-10 thread, T1-11 caption/kind, T1-12 policy detector, T1-06/07 message_versions).

``current_version_id`` is created here as a plain integer column (no FK yet) because
``message_versions`` does not exist until T1-06. T1-06 will add the FK constraint and
backfill v1 versions; this migration leaves it as a forward-compatible placeholder.

Revision ID: 006
Revises: 005
Create Date: 2026-04-27
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "006"
down_revision: Union[str, Sequence[str], None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Additive columns. Order matches HANDOFF.md §6 migration #4.
    op.add_column(
        "chat_messages",
        sa.Column("raw_update_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "chat_messages",
        sa.Column("reply_to_message_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "chat_messages",
        sa.Column("message_thread_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "chat_messages",
        sa.Column("caption", sa.Text(), nullable=True),
    )
    op.add_column(
        "chat_messages",
        sa.Column("message_kind", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "chat_messages",
        sa.Column("current_version_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "chat_messages",
        sa.Column(
            "memory_policy",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'normal'"),
        ),
    )
    op.add_column(
        "chat_messages",
        sa.Column(
            "visibility",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'member'"),
        ),
    )
    op.add_column(
        "chat_messages",
        sa.Column(
            "is_redacted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "chat_messages",
        sa.Column("content_hash", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "chat_messages",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    # FK to telegram_updates.id (T1-03). Named explicitly so create_all and the migration
    # produce identical schemas.
    op.create_foreign_key(
        "fk_chat_messages_raw_update_id",
        "chat_messages",
        "telegram_updates",
        ["raw_update_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # CHECK constraints for memory_policy + visibility — typed allowed sets.
    op.create_check_constraint(
        "ck_chat_messages_memory_policy",
        "chat_messages",
        "memory_policy IN ('normal','nomem','offrecord','forgotten')",
    )
    op.create_check_constraint(
        "ck_chat_messages_visibility",
        "chat_messages",
        "visibility IN ('private','member','internal','public')",
    )

    # Indexes for retrieval paths the memory system will exercise.
    op.create_index(
        "ix_chat_messages_chat_id_date", "chat_messages", ["chat_id", "date"]
    )
    op.create_index(
        "ix_chat_messages_reply_to_message_id",
        "chat_messages",
        ["reply_to_message_id"],
    )
    op.create_index(
        "ix_chat_messages_message_thread_id",
        "chat_messages",
        ["message_thread_id"],
    )
    op.create_index(
        "ix_chat_messages_memory_policy", "chat_messages", ["memory_policy"]
    )
    op.create_index(
        "ix_chat_messages_content_hash", "chat_messages", ["content_hash"]
    )

    # No data backfill needed — server_defaults populate memory_policy='normal' and
    # visibility='member' for existing rows automatically. content_hash stays NULL for
    # legacy chat_messages rows; computation strategy is owned by T1-08 and applied to
    # message_versions v1 rows by T1-07 (legacy chat_messages.content_hash remains NULL
    # forever — only the version table carries the hash going forward).


def downgrade() -> None:
    op.drop_index("ix_chat_messages_content_hash", table_name="chat_messages")
    op.drop_index("ix_chat_messages_memory_policy", table_name="chat_messages")
    op.drop_index("ix_chat_messages_message_thread_id", table_name="chat_messages")
    op.drop_index("ix_chat_messages_reply_to_message_id", table_name="chat_messages")
    op.drop_index("ix_chat_messages_chat_id_date", table_name="chat_messages")

    op.drop_constraint(
        "ck_chat_messages_visibility", "chat_messages", type_="check"
    )
    op.drop_constraint(
        "ck_chat_messages_memory_policy", "chat_messages", type_="check"
    )
    op.drop_constraint(
        "fk_chat_messages_raw_update_id", "chat_messages", type_="foreignkey"
    )

    op.drop_column("chat_messages", "updated_at")
    op.drop_column("chat_messages", "content_hash")
    op.drop_column("chat_messages", "is_redacted")
    op.drop_column("chat_messages", "visibility")
    op.drop_column("chat_messages", "memory_policy")
    op.drop_column("chat_messages", "current_version_id")
    op.drop_column("chat_messages", "message_kind")
    op.drop_column("chat_messages", "caption")
    op.drop_column("chat_messages", "message_thread_id")
    op.drop_column("chat_messages", "reply_to_message_id")
    op.drop_column("chat_messages", "raw_update_id")
