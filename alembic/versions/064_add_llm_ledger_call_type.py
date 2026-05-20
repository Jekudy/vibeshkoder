"""add_llm_ledger_call_type

Phase 10 / T10-03: adds call_type column to llm_usage_ledger for per-bucket
cost accounting.

- 'graph_projection' bucket is separate from shared QA/digest budget.
- Daily ceiling SQL filters on call_type='graph_projection'.
- Backfill: qa_trace-linked rows → 'qa_synthesis'; rest remain 'unknown'.

Revision ID: 064
Revises: 062
Create Date: 2026-05-20
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "064"
down_revision: Union[str, Sequence[str], None] = "062"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "llm_usage_ledger"
_COLUMN = "call_type"
_IX_CALL_TYPE = "ix_llm_usage_ledger_call_type_created_at"


def upgrade() -> None:
    # Add call_type column with 'unknown' default for existing rows.
    op.add_column(
        _TABLE,
        sa.Column(
            _COLUMN,
            sa.String(32),
            nullable=False,
            server_default="unknown",
        ),
    )

    # Backfill: rows linked to a qa_trace → 'qa_synthesis'.
    # Digest-linked rows have qa_trace_id=NULL; they remain 'unknown' per spec
    # ("All remaining 'unknown' rows retain 'unknown' for audit trail integrity").
    #
    # Backfill is a single UPDATE — acceptable for current ledger size (< 100k rows).
    # If ledger grows past ~1M rows, refactor to batched UPDATE WHERE id BETWEEN N AND N+10000.
    # Tracked as Phase 10.5 carryover.
    op.execute(
        """
        UPDATE llm_usage_ledger
        SET call_type = 'qa_synthesis'
        WHERE qa_trace_id IS NOT NULL
          AND call_type = 'unknown'
        """
    )

    # Composite index supports the daily-ceiling query:
    #   WHERE call_type = 'graph_projection' AND created_at >= day_start
    op.create_index(
        _IX_CALL_TYPE,
        _TABLE,
        [_COLUMN, "created_at"],
    )


def downgrade() -> None:
    op.drop_index(_IX_CALL_TYPE, table_name=_TABLE)
    op.drop_column(_TABLE, _COLUMN)
