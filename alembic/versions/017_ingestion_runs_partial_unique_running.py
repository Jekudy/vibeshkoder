"""ingestion_runs_partial_unique_running

T2-NEW-E (#101): Add source_hash column to ingestion_runs and a partial unique
index on (source_hash) WHERE status='running'. This prevents concurrent CLI
invocations from starting two partial runs for the same export file simultaneously.

The source_hash is the SHA-256 of the export file bytes (or a canonical envelope
hash). It is set at import_apply invocation time and used by init_or_resume_run()
to locate an existing partial run for the same source.

Additive migration — existing rows get source_hash=NULL (compatible with nullable).

Revision ID: 017
Revises: 016
Create Date: 2026-04-28
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "017"
down_revision: Union[str, Sequence[str], None] = "016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add source_hash column — nullable so existing live/dry_run rows survive untouched.
    op.add_column(
        "ingestion_runs",
        sa.Column("source_hash", sa.String(length=128), nullable=True),
    )

    # Partial unique index: at most one RUNNING import run per source_hash.
    # WHERE status='running' so completed/failed rows for the same source are allowed
    # (operator may re-import the same file after completing a prior run).
    #
    # Created CONCURRENTLY (in an autocommit block) so the table is not locked
    # for writes during index creation on a live database.
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_ingestion_runs_source_hash_running",
            "ingestion_runs",
            ["source_hash"],
            unique=True,
            postgresql_where=sa.text("status = 'running'"),
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_ingestion_runs_source_hash_running",
            table_name="ingestion_runs",
            postgresql_concurrently=True,
        )
    op.drop_column("ingestion_runs", "source_hash")
