"""add_wiki_page_message_sources

T9-01 / Phase 9: FK-normalized junction table for wiki page →
message_version citations (PHASE9_PLAN.md §5.A; Codex audit HIGH I —
replaces draft JSONB array with FK-enforced join).

ON DELETE RESTRICT on message_version_id: a message_version cannot be
hard-deleted while referenced by any wiki page. message_versions are
redacted not deleted via forget_cascade; the wiki cascade layer
(_cascade_wiki_pages) handles re-validation when an mv becomes
forgotten/offrecord/redacted.

This table is the reverse-index that the _cascade_wiki_pages layer scans
to find affected wiki pages when a forget event hits an mv_id.

Revision ID: 054
Revises: 053
Create Date: 2026-05-17
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "054"
down_revision: Union[str, Sequence[str], None] = "053"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "wiki_page_message_sources",
        sa.Column("wiki_page_id", UUID(as_uuid=True), nullable=False),
        sa.Column("message_version_id", sa.BigInteger(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint(
            "wiki_page_id",
            "message_version_id",
            name="pk_wiki_page_message_sources",
        ),
        sa.ForeignKeyConstraint(
            ["wiki_page_id"],
            ["wiki_pages.id"],
            name="fk_wiki_page_message_sources_wiki_page_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["message_version_id"],
            ["message_versions.id"],
            name="fk_wiki_page_message_sources_message_version_id",
            ondelete="RESTRICT",
        ),
    )
    # Reverse lookup for forget cascade: "which wiki pages cite this mv?"
    op.create_index(
        "ix_wiki_page_message_sources_mv_id",
        "wiki_page_message_sources",
        ["message_version_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_wiki_page_message_sources_mv_id",
        table_name="wiki_page_message_sources",
    )
    op.drop_table("wiki_page_message_sources")
