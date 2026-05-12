"""add_extraction_decisions

T6-01 / Phase 6: audit trail of admin ``/approve`` / ``/reject`` actions
against ``extraction_candidates``. Exactly one terminal decision is allowed
per candidate (``UNIQUE(candidate_id)``); appeals are out of scope (see
PHASE6_PLAN.md §11) and would require adding a ``decision_seq INT`` column
and switching to a composite UNIQUE.

PHASE6_PLAN.md §5.A:

* ``action='rejected'`` records a manual reject.
* ``action='approved'`` records a successful promotion.
* R3-block (deterministic re-validation aborts approval) is NOT a decision:
  it leaves the candidate ``pending`` and writes NO row here. See §5.C and
  §8.

FK semantics:

* ``candidate_id`` → ON DELETE CASCADE (deleting the candidate scrubs its
  decision audit; in practice candidates are not deleted, only set to a
  terminal status).
* ``decided_by`` → ON DELETE SET NULL (audit row survives admin soft-delete).
  ``decided_by_username`` is a NOT NULL audit shadow snapshotted at decision
  time so the human-readable record survives the FK nullification.

Revision ID: 034
Revises: 033
Create Date: 2026-05-12
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "034"
down_revision: Union[str, Sequence[str], None] = "033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "extraction_decisions",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "candidate_id",
            UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "action",
            sa.Text(),
            nullable=False,
        ),
        sa.Column(
            "reason",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "decided_by",
            sa.BigInteger(),
            nullable=True,
        ),
        # Audit shadow: NOT NULL, snapshotted at decision time so the
        # human-readable record survives the decided_by FK SET NULL on
        # admin soft-delete.
        sa.Column(
            "decided_by_username",
            sa.Text(),
            nullable=False,
        ),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "action IN ('approved','rejected')",
            name="ck_extraction_decisions_action",
        ),
        # One terminal decision per candidate. Appeals out of scope (see §11).
        sa.UniqueConstraint(
            "candidate_id",
            name="uq_extraction_decisions_candidate_id",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["extraction_candidates.id"],
            name="fk_extraction_decisions_candidate_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["decided_by"],
            ["users.id"],
            name="fk_extraction_decisions_decided_by",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_extraction_decisions_decided_at",
        "extraction_decisions",
        ["decided_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_extraction_decisions_decided_at",
        table_name="extraction_decisions",
    )
    op.drop_table("extraction_decisions")
