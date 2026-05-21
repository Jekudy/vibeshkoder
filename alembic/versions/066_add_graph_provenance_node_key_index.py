"""add_graph_provenance_node_key_index

FIX-3 / CRITICAL-3 (T10-05 review): add partial index on
graph_provenance(graph_node_key) WHERE purged_at IS NULL.

Without this index, _resolve_provenance_for_nodes and sources_for_path
perform full-table scans on every traversal, which degrades performance as
the provenance table grows.

The partial index covers only active (non-purged) rows, matching the query
filter used in both lookup paths.

Revision ID: 066
Revises: 065
Create Date: 2026-05-21
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "066"
down_revision = "065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX ix_graph_provenance_node_key
        ON graph_provenance(graph_node_key)
        WHERE purged_at IS NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_graph_provenance_node_key")
