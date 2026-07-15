"""Make extraction spend and live cursor progress durable and exactly-once.

Revision ID: 085
Revises: 084
Create Date: 2026-07-14
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "085"
down_revision: Union[str, Sequence[str], None] = "084"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "extraction_runs",
        sa.Column("semantic_key", sa.CHAR(length=64), nullable=True),
    )
    op.add_column(
        "extraction_runs",
        sa.Column("source_snapshot_hash", sa.CHAR(length=64), nullable=True),
    )
    op.add_column(
        "extraction_runs",
        sa.Column("prompt_template_version", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "extraction_runs",
        sa.Column("provider", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "extraction_runs",
        sa.Column("model", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "extraction_runs",
        sa.Column("selection_mode", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "extraction_runs",
        sa.Column("cursor_start_message_version_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "extraction_runs",
        sa.Column("cursor_end_message_version_id", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "ck_extraction_runs_semantic_identity_complete",
        "extraction_runs",
        "(semantic_key IS NULL AND source_snapshot_hash IS NULL "
        "AND prompt_template_version IS NULL AND provider IS NULL "
        "AND model IS NULL AND selection_mode IS NULL) OR "
        "(semantic_key IS NOT NULL AND source_snapshot_hash IS NOT NULL "
        "AND prompt_template_version IS NOT NULL AND provider IS NOT NULL "
        "AND model IS NOT NULL AND selection_mode IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_extraction_runs_semantic_hashes",
        "extraction_runs",
        "(semantic_key IS NULL OR semantic_key ~ '^[0-9a-f]{64}$') AND "
        "(source_snapshot_hash IS NULL OR source_snapshot_hash ~ '^[0-9a-f]{64}$')",
    )
    op.create_check_constraint(
        "ck_extraction_runs_selection_cursor",
        "extraction_runs",
        "(selection_mode IS NULL "
        "AND cursor_start_message_version_id IS NULL "
        "AND cursor_end_message_version_id IS NULL) OR "
        "(selection_mode = 'event_time' "
        "AND cursor_start_message_version_id IS NULL "
        "AND cursor_end_message_version_id IS NULL) OR "
        "(selection_mode = 'version_cursor' "
        "AND cursor_start_message_version_id IS NOT NULL "
        "AND cursor_end_message_version_id IS NOT NULL "
        "AND cursor_start_message_version_id >= 0 "
        "AND cursor_end_message_version_id >= cursor_start_message_version_id)",
    )
    op.create_index(
        "uq_extraction_runs_semantic_key",
        "extraction_runs",
        ["semantic_key"],
        unique=True,
        postgresql_where=sa.text("semantic_key IS NOT NULL"),
    )
    op.create_index(
        "ix_extraction_runs_unresolved_cursor",
        "extraction_runs",
        ["source_chat_id", "cursor_start_message_version_id"],
        unique=False,
        postgresql_where=sa.text(
            "selection_mode = 'version_cursor' AND run_status IN ('running','failed')"
        ),
    )

    op.create_table(
        "extraction_cursors",
        sa.Column("source_chat_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "last_message_version_id",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "last_message_version_id >= 0",
            name="ck_extraction_cursors_last_message_version_id_nonnegative",
        ),
        sa.PrimaryKeyConstraint("source_chat_id"),
    )

    op.add_column(
        "extraction_candidates",
        sa.Column("payload_schema_version", sa.String(length=32), nullable=True),
    )
    op.create_check_constraint(
        "ck_extraction_candidates_payload_schema_version",
        "extraction_candidates",
        "payload_schema_version IS NULL OR payload_schema_version = 'karpathy-wiki-v1'",
    )
    op.create_index(
        "ix_extraction_candidates_pending_legacy",
        "extraction_candidates",
        ["created_at", "id"],
        unique=False,
        postgresql_where=sa.text("status = 'pending' AND payload_schema_version IS NULL"),
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM extraction_cursors) THEN
                RAISE EXCEPTION
                    'Cannot downgrade 085: extraction_cursors contains rollout data';
            END IF;
            IF EXISTS (
                SELECT 1 FROM extraction_runs WHERE semantic_key IS NOT NULL
            ) THEN
                RAISE EXCEPTION
                    'Cannot downgrade 085: extraction_runs contains semantic identity data';
            END IF;
            IF EXISTS (
                SELECT 1 FROM extraction_candidates
                WHERE payload_schema_version IS NOT NULL
            ) THEN
                RAISE EXCEPTION
                    'Cannot downgrade 085: extraction_candidates contains versioned payloads';
            END IF;
        END
        $$
        """
    )

    op.drop_index(
        "ix_extraction_candidates_pending_legacy",
        table_name="extraction_candidates",
    )
    op.drop_constraint(
        "ck_extraction_candidates_payload_schema_version",
        "extraction_candidates",
        type_="check",
    )
    op.drop_column("extraction_candidates", "payload_schema_version")

    op.drop_table("extraction_cursors")

    op.drop_index("ix_extraction_runs_unresolved_cursor", table_name="extraction_runs")
    op.drop_index("uq_extraction_runs_semantic_key", table_name="extraction_runs")
    op.drop_constraint(
        "ck_extraction_runs_selection_cursor",
        "extraction_runs",
        type_="check",
    )
    op.drop_constraint(
        "ck_extraction_runs_semantic_hashes",
        "extraction_runs",
        type_="check",
    )
    op.drop_constraint(
        "ck_extraction_runs_semantic_identity_complete",
        "extraction_runs",
        type_="check",
    )
    op.drop_column("extraction_runs", "cursor_end_message_version_id")
    op.drop_column("extraction_runs", "cursor_start_message_version_id")
    op.drop_column("extraction_runs", "selection_mode")
    op.drop_column("extraction_runs", "model")
    op.drop_column("extraction_runs", "provider")
    op.drop_column("extraction_runs", "prompt_template_version")
    op.drop_column("extraction_runs", "source_snapshot_hash")
    op.drop_column("extraction_runs", "semantic_key")
