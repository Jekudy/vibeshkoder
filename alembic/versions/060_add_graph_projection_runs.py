"""add_graph_projection_runs

Phase 10 / W0-A foundation: creates graph_projection_runs table.

This table is the Postgres-side audit anchor for all Phase 10 Neo4j graph
projection work. Every projector run (dry_run, incremental, full_rebuild,
repair) creates one row here and updates it as the run progresses.

Tables graph_provenance (061), graph_edges (062), and graph_purge_pending (063)
all carry a FK to this table's id. Migration 064 adds call_type to
llm_usage_ledger and references the reserved call_type values documented in
bot/services/graph_common.py::RESERVED_LEDGER_CALL_TYPES.

Revision ID: 060
Revises: 055
Create Date: 2026-05-20
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "060"
down_revision: Union[str, Sequence[str], None] = "055"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "graph_projection_runs"
_CK_MODE = "ck_graph_projection_runs_mode"
_CK_STATUS = "ck_graph_projection_runs_status"
_IX_STARTED_AT = "ix_graph_projection_runs_started_at"
_IX_STATUS = "ix_graph_projection_runs_status"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="running"),
        sa.Column("source_cutoff_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_card_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_message_version_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("projected_node_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("projected_edge_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_policy_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_budget_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("llm_prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("llm_completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "estimated_cost_usd", sa.Numeric(precision=10, scale=6), nullable=False,
            server_default="0"
        ),
        sa.Column(
            "actual_cost_usd", sa.Numeric(precision=10, scale=6), nullable=False,
            server_default="0"
        ),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now()
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_context", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now()
        ),
        sa.Column("started_by", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "mode IN ('dry_run', 'incremental', 'full_rebuild', 'repair')",
            name=_CK_MODE,
        ),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'cancelled', "
            "'cost_exceeded', 'dry_run_complete')",
            name=_CK_STATUS,
        ),
    )

    # Index on started_at DESC for chronological listing queries
    op.create_index(
        _IX_STARTED_AT,
        _TABLE,
        [sa.text("started_at DESC")],
    )

    # Partial index: only index rows in monitoring-relevant states
    op.create_index(
        _IX_STATUS,
        _TABLE,
        ["status"],
        postgresql_where=sa.text("status IN ('running', 'failed')"),
    )


def downgrade() -> None:
    op.drop_index(_IX_STATUS, table_name=_TABLE)
    op.drop_index(_IX_STARTED_AT, table_name=_TABLE)
    op.drop_table(_TABLE)
