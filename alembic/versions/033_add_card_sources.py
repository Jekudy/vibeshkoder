"""add_card_sources

T6-01 / Phase 6: FK-normalized link between a ``knowledge_cards`` row and a
``message_versions`` row, so card citations can be traced back to source
messages without scanning JSONB arrays.

PHASE6_PLAN.md §5.A (D1): replaces the DRAFT's inline
``source_message_version_ids jsonb`` column on ``knowledge_cards``. The
candidate's staging JSONB (``extraction_candidates.source_message_version_ids``)
is promoted to one row per element here at ``/approve`` time (§5.C step 6).

FK semantics:

* ``card_id`` → ON DELETE CASCADE (deleting a card scrubs its source links).
* ``message_version_id`` → ON DELETE RESTRICT (prevents accidental orphan
  deletes; the §5.A.5 cascade demote path DELETEs the ``card_sources`` row
  explicitly rather than deleting the ``message_versions`` row).

Indexes:

* ``UNIQUE(card_id, message_version_id)`` — at most one link per pair;
  promotes idempotency of ``/approve`` re-runs.
* Reverse index on ``message_version_id`` — supports
  ``_cascade_card_sources_on_forget`` (§5.A.5) which selects affected card
  ids by ``message_version_id`` and demotes the card if remaining count
  drops to 0.

Revision ID: 033
Revises: 032
Create Date: 2026-05-12
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "033"
down_revision: Union[str, Sequence[str], None] = "032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "card_sources",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "card_id",
            UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "message_version_id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "position",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "card_id",
            "message_version_id",
            name="uq_card_sources_card_id_message_version_id",
        ),
        sa.ForeignKeyConstraint(
            ["card_id"],
            ["knowledge_cards.id"],
            name="fk_card_sources_card_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["message_version_id"],
            ["message_versions.id"],
            name="fk_card_sources_message_version_id",
            ondelete="RESTRICT",
        ),
    )
    # PHASE6_PLAN.md §5.A.5 (cascade demote): reverse lookup on
    # message_version_id resolves "which cards lose a source" without a seq
    # scan when a forget_event lands on a popular source row.
    op.create_index(
        "ix_card_sources_message_version_id",
        "card_sources",
        ["message_version_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_card_sources_message_version_id", table_name="card_sources")
    op.drop_table("card_sources")
