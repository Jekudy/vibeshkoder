"""align_message_versions_search_tsv

T4-01 follow-up: align the FTS schema with the Phase 4 plan after PR #151.

Migration 020 shipped a generated ``tsv`` column over ``text + caption`` with a
partial index. The ratified Phase 4 plan uses ``search_tsv`` over
``normalized_text + caption`` and keeps governance as a runtime query filter.

Revision ID: 021
Revises: 020
Create Date: 2026-04-30
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "021"
down_revision: Union[str, Sequence[str], None] = "020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("idx_message_versions_tsv", table_name="message_versions")
    op.drop_column("message_versions", "tsv")

    op.add_column(
        "message_versions",
        sa.Column(
            "search_tsv",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('russian', coalesce(normalized_text,'') || ' ' || coalesce(caption,''))",
                persisted=True,
            ),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_message_versions_search_tsv",
        "message_versions",
        ["search_tsv"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_message_versions_search_tsv", table_name="message_versions")
    op.drop_column("message_versions", "search_tsv")

    op.add_column(
        "message_versions",
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('russian', coalesce(text,'') || ' ' || coalesce(caption,''))",
                persisted=True,
            ),
            nullable=True,
        ),
    )
    op.create_index(
        "idx_message_versions_tsv",
        "message_versions",
        ["tsv"],
        postgresql_using="gin",
        postgresql_where=sa.text("is_redacted = FALSE"),
    )
