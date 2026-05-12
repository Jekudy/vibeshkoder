"""add_knowledge_cards

T6-01 / Phase 6: admin-approved canonical knowledge unit. Citation-eligible
only when ``card_status='approved'`` AND at least one ``card_sources`` row
exists (033). Body is stored as Telegram MarkdownV2 (PHASE6_PLAN.md Q1) and
indexed for Russian FTS via a generated tsvector column.

PHASE6_PLAN.md §5.A — Q3 collapsed ``deprecated`` into ``archived`` with
nullable ``archived_reason`` (populated only when status=archived).

Constraints:

* ``card_status IN ('draft','approved','archived')`` — Q3 final set.
* ``card_status='approved'`` implies both ``approved_by_user_id`` and
  ``approved_at`` are set. Non-approved rows are NOT citation-eligible
  (enforced at the search layer, not the schema, to keep promotion atomic
  in §5.C step 5).

FTS:

* ``body_tsv`` is ``GENERATED ALWAYS AS to_tsvector('russian', body_markdown) STORED``
  to match the Phase 4 baseline (``message_versions.search_tsv`` uses the
  same ``'russian'`` config, see ``alembic/versions/020`` /
  ``021_align_message_versions_search_tsv.py``).
* GIN index on ``body_tsv`` so card hits join the Phase 4 search pipeline
  cheaply (T6-06).

Source-set requirement (§5.A): the non-empty source set is enforced via
``card_sources`` (033), NOT as a column-level constraint on this table.
This keeps the ``/approve`` promotion transaction atomic — the candidate's
status flips to ``approved`` and the ``card_sources`` rows are inserted in
the same INSERT batch (§5.C step 5+6).

Revision ID: 032
Revises: 031
Create Date: 2026-05-12
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import UUID

revision: str = "032"
down_revision: Union[str, Sequence[str], None] = "031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "knowledge_cards",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body_markdown", sa.Text(), nullable=False),
        # PHASE6_PLAN.md §5.A: 'russian' config matches the Phase 4 baseline.
        # GENERATED ALWAYS AS ... STORED so application code never sees a
        # stale or null tsvector for inserted rows.
        sa.Column(
            "body_tsv",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('russian', coalesce(body_markdown, ''))",
                persisted=True,
            ),
            nullable=True,
        ),
        sa.Column(
            "card_status",
            sa.Text(),
            nullable=False,
        ),
        sa.Column(
            "archived_reason",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "approved_by_user_id",
            sa.BigInteger(),
            nullable=True,
        ),
        sa.Column(
            "approved_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "card_status IN ('draft','approved','archived')",
            name="ck_knowledge_cards_status",
        ),
        # PHASE6_PLAN.md §5.A: approved cards MUST have approver attribution.
        sa.CheckConstraint(
            "card_status <> 'approved' OR "
            "(approved_by_user_id IS NOT NULL AND approved_at IS NOT NULL)",
            name="ck_knowledge_cards_approved_attribution",
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_user_id"],
            ["users.id"],
            name="fk_knowledge_cards_approved_by_user_id",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_knowledge_cards_body_tsv",
        "knowledge_cards",
        ["body_tsv"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_knowledge_cards_card_status",
        "knowledge_cards",
        ["card_status"],
    )
    op.create_index(
        "ix_knowledge_cards_created_at",
        "knowledge_cards",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_cards_created_at", table_name="knowledge_cards")
    op.drop_index("ix_knowledge_cards_card_status", table_name="knowledge_cards")
    op.drop_index("ix_knowledge_cards_body_tsv", table_name="knowledge_cards")
    op.drop_table("knowledge_cards")
