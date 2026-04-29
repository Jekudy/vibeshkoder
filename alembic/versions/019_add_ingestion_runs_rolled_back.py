"""add_ingestion_runs_rolled_back

T2-NEW-G / #104: allow rollback audit rows in ingestion_runs and enforce one
``run_type='rolled_back'`` audit row per original import run.

Revision ID: 019
Revises: 018
Create Date: 2026-04-29
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "019"
down_revision: Union[str, Sequence[str], None] = "018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_ingestion_runs_run_type",
        "ingestion_runs",
        type_="check",
    )
    op.create_check_constraint(
        "ck_ingestion_runs_run_type",
        "ingestion_runs",
        "run_type IN ('live','import','dry_run','cancelled','rolled_back')",
    )

    with op.get_context().autocommit_block():
        op.create_index(
            "ix_ingestion_runs_rollback_original_unique",
            "ingestion_runs",
            [sa.text("((stats_json::jsonb ->> 'original_run_id'))")],
            unique=True,
            postgresql_where=sa.text("run_type = 'rolled_back'"),
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    """Emergency-only downgrade; not expected in normal flow.

    WARNING: if any ingestion_runs rows with run_type='rolled_back' exist when
    downgrade runs, the recreated CHECK constraint will fail. Operators must DELETE
    such audit rows manually before downgrade.
    """
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_ingestion_runs_rollback_original_unique",
            table_name="ingestion_runs",
            postgresql_concurrently=True,
        )

    op.drop_constraint(
        "ck_ingestion_runs_run_type",
        "ingestion_runs",
        type_="check",
    )
    op.create_check_constraint(
        "ck_ingestion_runs_run_type",
        "ingestion_runs",
        "run_type IN ('live','import','dry_run','cancelled')",
    )
