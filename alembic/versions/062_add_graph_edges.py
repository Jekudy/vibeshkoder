"""add_graph_edges

Phase 10 / T10-02: creates graph_edges table.

Postgres-side edge registry for idempotency, drift detection, and cascade
lookup. Neo4j holds the traversable graph; this table proves every Neo4j edge
has a Postgres-side provenance record.

Predicate vocabulary is CHECK-constrained to ALLOWED_PREDICATES from
bot/services/graph_common.py (same list).

Revision ID: 062
Revises: 061
Create Date: 2026-05-20
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "062"
down_revision: Union[str, Sequence[str], None] = "061"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "graph_edges"
_CK_PREDICATE = "ck_graph_edges_predicate"
_CK_CONFIDENCE = "ck_graph_edges_confidence"
_IX_ACTIVE = "ix_graph_edges_active"
_UQ_KEY = "uq_graph_edges_key"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("graph_provenance_id", sa.BigInteger(), nullable=False),
        sa.Column("subject_node_key", sa.Text(), nullable=False),
        sa.Column("predicate", sa.Text(), nullable=False),
        sa.Column("object_node_key", sa.Text(), nullable=False),
        # stable MERGE key: SHA-256(subject+predicate+object)
        sa.Column("edge_key", sa.Text(), nullable=False),
        sa.Column(
            "confidence_score",
            sa.Numeric(precision=3, scale=2),
            nullable=False,
            server_default="0.50",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now()
        ),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["graph_provenance_id"],
            ["graph_provenance.id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "predicate IN ("
            "'MENTIONS', 'AUTHORED', 'KNOWS_ABOUT', 'ASKED', 'ANSWERED', "
            "'DECIDED', 'RELATED_TO', 'SUPPORTS', 'DERIVED_FROM', "
            "'PART_OF', 'CONTRADICTS', 'SUPERSEDES'"
            ")",
            name=_CK_PREDICATE,
        ),
        sa.CheckConstraint(
            "confidence_score >= 0.00 AND confidence_score <= 1.00",
            name=_CK_CONFIDENCE,
        ),
    )

    # Drift detection: Neo4j edge count vs graph_edges count must match
    op.create_index(
        _IX_ACTIVE,
        _TABLE,
        ["graph_provenance_id"],
        postgresql_where=sa.text("purged_at IS NULL"),
    )

    # Idempotent MERGE lookup: is this edge already projected?
    op.create_index(
        _UQ_KEY,
        _TABLE,
        ["edge_key"],
        unique=True,
        postgresql_where=sa.text("purged_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(_UQ_KEY, table_name=_TABLE)
    op.drop_index(_IX_ACTIVE, table_name=_TABLE)
    op.drop_table(_TABLE)
