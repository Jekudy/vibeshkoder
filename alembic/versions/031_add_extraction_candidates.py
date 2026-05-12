"""add_extraction_candidates

T6-01 / Phase 6: LLM-extracted fact pending human review. One row per
candidate produced during a run; ``status`` flows pending → approved /
rejected / superseded via the admin handlers (T6-04).

PHASE6_PLAN.md §5.A — table renamed from DRAFT's ``memory_candidates`` per
decision D2. Phase 8 ``memory_candidates`` (reflection cluster queue) is a
distinct concept.

Constraints:

* ``status='pending'`` implies ``reviewed_by IS NULL`` AND
  ``reviewed_at IS NULL``.
* Any terminal status (approved/rejected/superseded) implies both
  reviewer columns are set.
* ``source_message_version_ids`` is a JSONB array (validated via
  ``jsonb_typeof`` CHECK). It is **staging only** on the candidate row —
  promoted to ``card_sources`` FK rows at ``/approve`` time (033).

FK semantics:

* ``extraction_run_id`` → ON DELETE SET NULL (audit row survives run
  cleanup; orphan candidate still tracked).
* ``reviewed_by`` → ON DELETE SET NULL (audit row survives admin
  soft-delete).

Revision ID: 031
Revises: 030
Create Date: 2026-05-12
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "031"
down_revision: Union[str, Sequence[str], None] = "030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "extraction_candidates",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "extraction_run_id",
            UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "candidate_json",
            JSONB(),
            nullable=False,
        ),
        sa.Column(
            "source_message_version_ids",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
        ),
        sa.Column(
            "reviewed_by",
            sa.BigInteger(),
            nullable=True,
        ),
        sa.Column(
            "reviewed_at",
            sa.DateTime(timezone=True),
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
            "status IN ('pending','approved','rejected','superseded')",
            name="ck_extraction_candidates_status",
        ),
        # source_message_version_ids must be a JSON array (defends against
        # accidental object/scalar writes that would break the §5.C
        # /approve transaction which iterates over the array).
        sa.CheckConstraint(
            "jsonb_typeof(source_message_version_ids) = 'array'",
            name="ck_extraction_candidates_source_ids_is_array",
        ),
        # Reviewer audit invariants: pending rows must NOT have a reviewer;
        # terminal rows MUST have both reviewer columns set.
        sa.CheckConstraint(
            "(status = 'pending' AND reviewed_by IS NULL AND reviewed_at IS NULL) OR "
            "(status IN ('approved','rejected','superseded') "
            " AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)",
            name="ck_extraction_candidates_reviewer_consistency",
        ),
        sa.ForeignKeyConstraint(
            ["extraction_run_id"],
            ["extraction_runs.id"],
            name="fk_extraction_candidates_extraction_run_id",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by"],
            ["users.id"],
            name="fk_extraction_candidates_reviewed_by",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_extraction_candidates_status",
        "extraction_candidates",
        ["status"],
    )
    op.create_index(
        "ix_extraction_candidates_extraction_run_id",
        "extraction_candidates",
        ["extraction_run_id"],
    )
    op.create_index(
        "ix_extraction_candidates_created_at",
        "extraction_candidates",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_extraction_candidates_created_at",
        table_name="extraction_candidates",
    )
    op.drop_index(
        "ix_extraction_candidates_extraction_run_id",
        table_name="extraction_candidates",
    )
    op.drop_index(
        "ix_extraction_candidates_status",
        table_name="extraction_candidates",
    )
    op.drop_table("extraction_candidates")
