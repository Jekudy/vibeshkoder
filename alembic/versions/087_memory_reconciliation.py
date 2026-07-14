"""Add explicit operator reconciliation for ambiguous paid memory calls.

Revision ID: 087
Revises: 086
Create Date: 2026-07-14
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "087"
down_revision: Union[str, Sequence[str], None] = "086"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _reason_and_evidence_constraints(prefix: str) -> tuple[sa.CheckConstraint, ...]:
    return (
        sa.CheckConstraint(
            "length(trim(reason)) BETWEEN 1 AND 500",
            name=f"ck_{prefix}_reason_bounded",
        ),
        sa.CheckConstraint(
            "evidence_hash IS NULL OR evidence_hash ~ '^[0-9a-f]{64}$'",
            name=f"ck_{prefix}_evidence_hash",
        ),
    )


def upgrade() -> None:
    op.add_column(
        "extraction_runs",
        sa.Column("attempt_no", sa.Integer(), nullable=True, server_default=sa.text("1")),
    )
    op.add_column(
        "extraction_runs",
        sa.Column("retry_of_run_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "extraction_runs",
        sa.Column(
            "dispatch_state",
            sa.String(length=32),
            nullable=True,
            server_default=sa.text("'not_dispatched'"),
        ),
    )
    op.execute(
        """
        UPDATE extraction_runs
        SET attempt_no = 1,
            dispatch_state = CASE
                WHEN run_status = 'completed' THEN 'response_received'
                WHEN run_status = 'running' THEN 'unknown'
                WHEN provider IS NOT NULL OR llm_usage_ledger_id IS NOT NULL THEN 'unknown'
                ELSE 'not_dispatched'
            END
        """
    )
    op.alter_column("extraction_runs", "attempt_no", nullable=False)
    op.alter_column("extraction_runs", "dispatch_state", nullable=False)
    op.create_check_constraint(
        "ck_extraction_runs_attempt_no_positive",
        "extraction_runs",
        "attempt_no >= 1",
    )
    op.create_check_constraint(
        "ck_extraction_runs_retry_link",
        "extraction_runs",
        "(attempt_no = 1 AND retry_of_run_id IS NULL) "
        "OR (attempt_no > 1 AND retry_of_run_id IS NOT NULL AND retry_of_run_id <> id)",
    )
    op.create_check_constraint(
        "ck_extraction_runs_dispatch_state",
        "extraction_runs",
        "dispatch_state IN ('not_dispatched','rejected_pre_accept','response_received','unknown')",
    )
    op.create_foreign_key(
        "fk_extraction_runs_retry_of_run_id",
        "extraction_runs",
        "extraction_runs",
        ["retry_of_run_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_index("uq_extraction_runs_semantic_key", table_name="extraction_runs")
    op.create_unique_constraint(
        "uq_extraction_runs_semantic_attempt",
        "extraction_runs",
        ["semantic_key", "attempt_no"],
    )
    op.drop_constraint(
        "ck_extraction_runs_selection_cursor",
        "extraction_runs",
        type_="check",
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
        "AND source_chat_id IS NOT NULL "
        "AND cursor_start_message_version_id IS NOT NULL "
        "AND cursor_end_message_version_id IS NOT NULL "
        "AND cursor_start_message_version_id >= 0 "
        "AND cursor_end_message_version_id >= cursor_start_message_version_id)",
    )

    op.create_table(
        "extraction_run_resolutions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("evidence_hash", sa.CHAR(length=64), nullable=True),
        sa.Column(
            "accept_memory_gap",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "action IN ('safe_retry','risk_accepted_retry','abandon')",
            name="ck_extraction_run_resolutions_action",
        ),
        sa.CheckConstraint(
            "(action = 'abandon' AND accept_memory_gap) "
            "OR (action <> 'abandon' AND NOT accept_memory_gap)",
            name="ck_extraction_run_resolutions_gap_acceptance",
        ),
        *_reason_and_evidence_constraints("extraction_run_resolutions"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["extraction_runs.id"],
            name="fk_extraction_run_resolutions_run_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_extraction_run_resolutions_actor_user_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", name="uq_extraction_run_resolutions_run_id"),
    )

    op.create_table(
        "image_description_resolutions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("message_media_id", sa.BigInteger(), nullable=False),
        sa.Column("attempt_no", sa.SmallInteger(), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("evidence_hash", sa.CHAR(length=64), nullable=True),
        sa.Column(
            "accept_memory_gap",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "action IN ('risk_accepted_retry','abandon')",
            name="ck_image_description_resolutions_action",
        ),
        sa.CheckConstraint(
            "attempt_no >= 1",
            name="ck_image_description_resolutions_attempt_no_positive",
        ),
        sa.CheckConstraint(
            "(action = 'abandon' AND accept_memory_gap) "
            "OR (action <> 'abandon' AND NOT accept_memory_gap)",
            name="ck_image_description_resolutions_gap_acceptance",
        ),
        *_reason_and_evidence_constraints("image_description_resolutions"),
        sa.ForeignKeyConstraint(
            ["message_media_id"],
            ["message_media.id"],
            name="fk_image_description_resolutions_message_media_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_image_description_resolutions_actor_user_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "message_media_id",
            "attempt_no",
            name="uq_image_description_resolutions_media_attempt",
        ),
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM extraction_run_resolutions) THEN
                RAISE EXCEPTION
                    'Cannot downgrade 087: extraction reconciliation audit rows exist';
            END IF;
            IF EXISTS (SELECT 1 FROM image_description_resolutions) THEN
                RAISE EXCEPTION
                    'Cannot downgrade 087: image reconciliation audit rows exist';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM extraction_runs
                WHERE attempt_no <> 1
                   OR retry_of_run_id IS NOT NULL
                   OR dispatch_state = 'rejected_pre_accept'
            ) THEN
                RAISE EXCEPTION
                    'Cannot downgrade 087: retry or dispatch rollout data exists';
            END IF;
        END
        $$
        """
    )

    op.drop_table("image_description_resolutions")
    op.drop_table("extraction_run_resolutions")

    op.drop_constraint(
        "ck_extraction_runs_selection_cursor",
        "extraction_runs",
        type_="check",
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
    op.drop_constraint(
        "uq_extraction_runs_semantic_attempt",
        "extraction_runs",
        type_="unique",
    )
    op.create_index(
        "uq_extraction_runs_semantic_key",
        "extraction_runs",
        ["semantic_key"],
        unique=True,
        postgresql_where=sa.text("semantic_key IS NOT NULL"),
    )
    op.drop_constraint(
        "fk_extraction_runs_retry_of_run_id",
        "extraction_runs",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_extraction_runs_dispatch_state",
        "extraction_runs",
        type_="check",
    )
    op.drop_constraint(
        "ck_extraction_runs_retry_link",
        "extraction_runs",
        type_="check",
    )
    op.drop_constraint(
        "ck_extraction_runs_attempt_no_positive",
        "extraction_runs",
        type_="check",
    )
    op.drop_column("extraction_runs", "dispatch_state")
    op.drop_column("extraction_runs", "retry_of_run_id")
    op.drop_column("extraction_runs", "attempt_no")
