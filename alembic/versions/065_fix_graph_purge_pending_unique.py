"""fix_graph_purge_pending_unique

CRITICAL-1 fix (T10-06): drop the (forget_event_id, source_table, source_pk) unique
constraint on graph_purge_pending and replace with
(forget_event_id, source_table, source_pk, graph_provenance_id).

The old constraint collapsed multiple graph_provenance rows for the same
(source_table, source_pk) into a single purge_pending row — only the first
provenance row got a purge entry, leaving all other Neo4j nodes alive after
the forget cascade. Invariant #9 violation.

The new constraint allows one purge_pending row PER provenance row, so every
Neo4j node derived from the source is purged atomically.

Revision ID: 065
Revises: 063
Create Date: 2026-05-20
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "065"
down_revision: Union[str, Sequence[str], None] = "063"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "graph_purge_pending"
_OLD_CONSTRAINT = "uq_graph_purge_pending_event_source"
_NEW_CONSTRAINT = "uq_graph_purge_pending_event_source_prov"


def upgrade() -> None:
    # Drop the old three-column unique constraint.
    op.drop_constraint(_OLD_CONSTRAINT, _TABLE, type_="unique")

    # Create the new four-column unique constraint including graph_provenance_id.
    # NULL == NULL for uniqueness purposes in Postgres unique constraints uses
    # NULLS DISTINCT (default); each NULL provenance_id is treated as distinct,
    # which is what we want for rows with no provenance FK.
    op.create_unique_constraint(
        _NEW_CONSTRAINT,
        _TABLE,
        ["forget_event_id", "source_table", "source_pk", "graph_provenance_id"],
    )


def downgrade() -> None:
    op.drop_constraint(_NEW_CONSTRAINT, _TABLE, type_="unique")
    op.create_unique_constraint(
        _OLD_CONSTRAINT,
        _TABLE,
        ["forget_event_id", "source_table", "source_pk"],
    )
