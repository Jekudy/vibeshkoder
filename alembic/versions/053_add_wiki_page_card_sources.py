"""add_wiki_page_card_sources

T9-01 / Phase 9: FK-normalized junction table for wiki page → knowledge
card citations (PHASE9_PLAN.md §5.A; Codex audit HIGH I — replaces draft
JSONB array with FK-enforced join).

ON DELETE RESTRICT on card_id: a card cannot be hard-deleted while
referenced by any wiki page. Cards are archived not deleted via
forget_cascade; the wiki cascade layer (_cascade_wiki_pages) handles
re-validation when a card transitions to non-approved.

Revision ID: 053
Revises: 052
Create Date: 2026-05-17
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "053"
down_revision: Union[str, Sequence[str], None] = "052"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "wiki_page_card_sources",
        sa.Column("wiki_page_id", UUID(as_uuid=True), nullable=False),
        sa.Column("card_id", UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint(
            "wiki_page_id",
            "card_id",
            name="pk_wiki_page_card_sources",
        ),
        sa.ForeignKeyConstraint(
            ["wiki_page_id"],
            ["wiki_pages.id"],
            name="fk_wiki_page_card_sources_wiki_page_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["card_id"],
            ["knowledge_cards.id"],
            name="fk_wiki_page_card_sources_card_id",
            ondelete="RESTRICT",
        ),
    )
    # Reverse lookup for forget cascade: "which wiki pages cite this card?"
    op.create_index(
        "ix_wiki_page_card_sources_card_id",
        "wiki_page_card_sources",
        ["card_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_wiki_page_card_sources_card_id",
        table_name="wiki_page_card_sources",
    )
    op.drop_table("wiki_page_card_sources")
