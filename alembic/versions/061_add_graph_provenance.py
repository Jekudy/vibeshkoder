"""add_graph_provenance

Phase 10 / T10-02: creates graph_provenance table.

Maps each projected source (message_version or knowledge_card) to the Neo4j
graph. Used by forget cascade (logical source_table/source_pk lookup) and by
drift detection (active non-purged rows vs Neo4j node count).

source_table / source_pk are intentionally NOT typed FK columns — they are
logical application-code refs queried by _cascade_graph_provenance. The FK ON
DELETE CASCADE on source_card_id / source_message_version_id is a safety net
only. See PHASE10_PLAN.md §5.A for rationale.

Revision ID: 061
Revises: 060
Create Date: 2026-05-20
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "061"
down_revision: Union[str, Sequence[str], None] = "060"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "graph_provenance"
_CK_SOURCE_TABLE = "ck_graph_provenance_source_table"
_CK_HAS_SOURCE = "ck_graph_provenance_has_source"
_CK_GRAPH_STORE = "ck_graph_provenance_graph_store"
_IX_MVID = "ix_graph_provenance_mvid"
_IX_CARD_ID = "ix_graph_provenance_card_id"
_IX_ACTIVE = "ix_graph_provenance_active"
_UQ_TRIPLE = "uq_graph_provenance_triple"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("projection_run_id", sa.BigInteger(), nullable=False),
        sa.Column("source_table", sa.Text(), nullable=False),
        sa.Column("source_pk", sa.Text(), nullable=False),
        sa.Column("source_message_version_id", sa.BigInteger(), nullable=True),
        sa.Column("source_card_id", sa.Uuid(), nullable=True),
        sa.Column("source_content_hash", sa.Text(), nullable=True),
        sa.Column(
            "graph_store", sa.Text(), nullable=False, server_default="neo4j"
        ),
        sa.Column("graph_node_key", sa.Text(), nullable=True),
        sa.Column("graph_edge_key", sa.Text(), nullable=True),
        sa.Column("triple_hash", sa.Text(), nullable=True),
        sa.Column(
            "governance_policy", sa.Text(), nullable=False, server_default="normal"
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now()
        ),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purge_reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["projection_run_id"],
            ["graph_projection_runs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_message_version_id"],
            ["message_versions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_card_id"],
            ["knowledge_cards.id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "source_table IN ('message_versions', 'knowledge_cards')",
            name=_CK_SOURCE_TABLE,
        ),
        sa.CheckConstraint(
            "source_message_version_id IS NOT NULL OR source_card_id IS NOT NULL",
            name=_CK_HAS_SOURCE,
        ),
        sa.CheckConstraint(
            "graph_store IN ('neo4j', 'networkx_dev')",
            name=_CK_GRAPH_STORE,
        ),
    )

    # Cascade forget lookup: find all graph provenance rows for a given message_version
    op.create_index(
        _IX_MVID,
        _TABLE,
        ["source_message_version_id"],
        postgresql_where=sa.text("source_message_version_id IS NOT NULL"),
    )

    # Cascade forget lookup: find all graph provenance rows for a given card
    op.create_index(
        _IX_CARD_ID,
        _TABLE,
        ["source_card_id"],
        postgresql_where=sa.text("source_card_id IS NOT NULL"),
    )

    # Drift detection: active (non-purged) provenance rows
    op.create_index(
        _IX_ACTIVE,
        _TABLE,
        ["projection_run_id"],
        postgresql_where=sa.text("purged_at IS NULL"),
    )

    # Idempotency: stable triple key within a projection run
    op.create_index(
        _UQ_TRIPLE,
        _TABLE,
        ["source_table", "source_pk", "triple_hash"],
        unique=True,
        postgresql_where=sa.text("purged_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(_UQ_TRIPLE, table_name=_TABLE)
    op.drop_index(_IX_ACTIVE, table_name=_TABLE)
    op.drop_index(_IX_CARD_ID, table_name=_TABLE)
    op.drop_index(_IX_MVID, table_name=_TABLE)
    op.drop_table(_TABLE)
