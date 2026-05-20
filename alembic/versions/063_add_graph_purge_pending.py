"""add_graph_purge_pending

Phase 10 / T10-06: async purge queue for Neo4j bolt DELETE.

Written atomically in the same Postgres transaction as the Postgres-side cascade
(forget_event commit). graph_purge_worker consumes rows, marks purged_at on
success or failed_at+error on failure. graph_query.py checks this table before
any Neo4j traversal: if any non-purged row exists for nodes in the result set →
return abstained=True (fail-closed per RFC-001:415).

Migration chain note: 062 → 064 → 063
064 (add_llm_ledger_call_type) shipped in T10-03 before this sprint. Alembic
supports non-contiguous revision IDs; this revision extends the 064 head.

Revision ID: 063
Revises: 064
Create Date: 2026-05-20
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "063"
down_revision: Union[str, Sequence[str], None] = "064"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "graph_purge_pending"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("forget_event_id", sa.BigInteger, nullable=False),
        sa.Column("source_table", sa.Text, nullable=False),
        sa.Column("source_pk", sa.Text, nullable=False),
        # known at enqueue time if provenance row exists
        sa.Column("graph_node_key", sa.Text, nullable=True),
        sa.Column("graph_edge_key", sa.Text, nullable=True),
        sa.Column(
            "graph_provenance_id",
            sa.BigInteger,
            sa.ForeignKey("graph_provenance.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "enqueued_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("purged_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("failed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column(
            "retry_count",
            sa.SmallInteger,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.CheckConstraint(
            "source_table IN ('message_versions', 'knowledge_cards', 'card_sources')",
            name="ck_graph_purge_pending_source_table",
        ),
        sa.UniqueConstraint(
            "forget_event_id",
            "source_table",
            "source_pk",
            name="uq_graph_purge_pending_event_source",
        ),
    )

    # Worker queue: fetch pending (non-purged, non-failed) rows ordered by enqueue time
    op.create_index(
        "ix_graph_purge_pending_queue",
        _TABLE,
        ["enqueued_at"],
        postgresql_where=sa.text("purged_at IS NULL AND failed_at IS NULL"),
    )

    # Fail-closed check in graph_query.py: are there pending purge rows?
    op.create_index(
        "ix_graph_purge_pending_node_key",
        _TABLE,
        ["graph_node_key"],
        postgresql_where=sa.text("purged_at IS NULL"),
    )

    # Support forget_event_id lookup
    op.create_index(
        "ix_graph_purge_pending_forget_event",
        _TABLE,
        ["forget_event_id"],
    )

    # Support source_table + source_pk lookup
    op.create_index(
        "ix_graph_purge_pending_source",
        _TABLE,
        ["source_table", "source_pk"],
    )


def downgrade() -> None:
    op.drop_index("ix_graph_purge_pending_source", table_name=_TABLE)
    op.drop_index("ix_graph_purge_pending_forget_event", table_name=_TABLE)
    op.drop_index("ix_graph_purge_pending_node_key", table_name=_TABLE)
    op.drop_index("ix_graph_purge_pending_queue", table_name=_TABLE)
    op.drop_table(_TABLE)
