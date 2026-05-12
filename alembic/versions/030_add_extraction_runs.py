"""add_extraction_runs

T6-01 / Phase 6: per-pass log of LLM extraction over a time window. One row is
written by ``bot/services/extractor.py::run_extraction_pass`` per scheduled or
operator-triggered extraction pass. The row tracks the window boundaries, how
many candidates were produced, terminal status, and an optional FK to the
Phase 5 LLM usage ledger entry that recorded the audited LLM call.

Schema authority: PHASE6_PLAN.md §5.A (030_add_extraction_runs).

Constraints:

* ``candidate_count >= 0`` — extraction passes never write a negative count.
* ``run_status='completed'`` implies both window timestamps are non-null —
  enforces "a completed extraction has a known window" invariant.

Migration numbering: 026-029 are intentionally unused / reserved for any
post-Phase-5 hotfix migrations. Phase 6 starts at 030 to leave that gap.

Revision ID: 030
Revises: 025
Create Date: 2026-05-12
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "030"
down_revision: Union[str, Sequence[str], None] = "025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "extraction_runs",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "ingestion_window_start",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "ingestion_window_end",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "candidate_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "run_status",
            sa.Text(),
            nullable=False,
        ),
        # Optional FK to Phase 5 LLM usage ledger entry that captured the
        # audited LLM call for this run. ON DELETE SET NULL preserves the
        # extraction_runs row even if the ledger row is later anonymized.
        sa.Column(
            "llm_usage_ledger_id",
            sa.BigInteger(),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "run_status IN ('running','completed','failed')",
            name="ck_extraction_runs_status",
        ),
        sa.CheckConstraint(
            "candidate_count >= 0",
            name="ck_extraction_runs_candidate_count_nonneg",
        ),
        # PHASE6_PLAN.md §5.A: completed runs MUST have both window timestamps
        # populated. running/failed rows may have one or both null.
        sa.CheckConstraint(
            "run_status <> 'completed' OR "
            "(ingestion_window_start IS NOT NULL AND ingestion_window_end IS NOT NULL)",
            name="ck_extraction_runs_completed_has_window",
        ),
        sa.ForeignKeyConstraint(
            ["llm_usage_ledger_id"],
            ["llm_usage_ledger.id"],
            name="fk_extraction_runs_llm_usage_ledger_id",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_extraction_runs_created_at",
        "extraction_runs",
        ["created_at"],
    )
    op.create_index(
        "ix_extraction_runs_run_status",
        "extraction_runs",
        ["run_status"],
    )


def downgrade() -> None:
    op.drop_index("ix_extraction_runs_run_status", table_name="extraction_runs")
    op.drop_index("ix_extraction_runs_created_at", table_name="extraction_runs")
    op.drop_table("extraction_runs")
