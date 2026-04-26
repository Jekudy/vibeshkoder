"""add_telegram_updates

T1-03: raw source-of-truth archive of every Telegram update the bot receives. The table
is filled by the live ingestion service in T1-04 and by the import tool in T2-01 / T2-03;
this ticket only ships the schema + a thin repo.

Idempotency: ``update_id`` is unique within Telegram per bot. The partial unique index
``ix_telegram_updates_update_id`` covers ``update_id WHERE update_id IS NOT NULL`` so:
- live updates from polling always have ``update_id`` and the conflict path catches
  duplicates from retries / network replays
- synthetic updates from import (no Telegram update_id) are stored without conflict; the
  importer enforces its own idempotency via ``raw_hash`` + ingestion_run_id

Revision ID: 005
Revises: 004
Create Date: 2026-04-26
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "005"
down_revision: Union[str, Sequence[str], None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "telegram_updates",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("update_id", sa.BigInteger(), nullable=True),
        sa.Column("update_type", sa.String(length=64), nullable=False),
        sa.Column("raw_json", sa.JSON(), nullable=True),
        sa.Column("raw_hash", sa.String(length=128), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("ingestion_run_id", sa.Integer(), nullable=True),
        sa.Column(
            "is_redacted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("redaction_reason", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["ingestion_run_id"],
            ["ingestion_runs.id"],
            name="fk_telegram_updates_ingestion_run_id",
        ),
    )
    # Partial unique index: enforce uniqueness only when update_id is set (live updates).
    # Synthetic import updates leave update_id NULL and rely on raw_hash for dedup.
    op.create_index(
        "ix_telegram_updates_update_id",
        "telegram_updates",
        ["update_id"],
        unique=True,
        postgresql_where=sa.text("update_id IS NOT NULL"),
    )
    op.create_index(
        "ix_telegram_updates_update_type_received_at",
        "telegram_updates",
        ["update_type", "received_at"],
    )
    op.create_index(
        "ix_telegram_updates_chat_id_message_id",
        "telegram_updates",
        ["chat_id", "message_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_telegram_updates_chat_id_message_id", table_name="telegram_updates"
    )
    op.drop_index(
        "ix_telegram_updates_update_type_received_at", table_name="telegram_updates"
    )
    op.drop_index("ix_telegram_updates_update_id", table_name="telegram_updates")
    op.drop_table("telegram_updates")
