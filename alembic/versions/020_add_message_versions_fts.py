"""add_message_versions_fts

T4-01: add PostgreSQL full-text search infrastructure for Phase 4 search.

The generated vector is built from the version body, not from ``chat_messages``,
so Phase 4 citations can continue to point at stable ``message_version_id`` rows.
The GIN index is partial on ``is_redacted = FALSE`` because the column already
exists on ``message_versions``; runtime queries still apply the same filter.

Revision ID: 020
Revises: 019
Create Date: 2026-04-30
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "020"
down_revision: Union[str, Sequence[str], None] = "019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.drop_index("idx_message_versions_tsv", table_name="message_versions")
    op.drop_column("message_versions", "tsv")
