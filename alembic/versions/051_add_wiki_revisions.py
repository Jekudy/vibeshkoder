"""add_wiki_revisions

T9-01 / Phase 9: edit-history audit trail for wiki pages.

Mirrors ``message_versions`` discipline (PHASE9_PLAN.md §5.A Q3 ratification).
The current body is always on ``wiki_pages.body_markdown``; ``wiki_revisions``
is the immutable audit trail only.

Source snapshot kept as JSONB (not FK-normalized) because revisions are
historical — FK targets (cards, mvids) may be deleted post-hoc, and RESTRICT
would block cascade. The ``_cascade_wiki_revisions`` layer masks
``body_markdown`` for forgotten sources (I7e binding test).

Columns:
* id UUID PK
* wiki_page_id UUID NOT NULL FK wiki_pages(id) ON DELETE CASCADE
* revision_seq INT NOT NULL
* body_markdown TEXT NOT NULL  (may be [CONTENT_REDACTED: forget_event_id={n}])
* revision_status VARCHAR(32) CHECK IN ('active','forgotten_redacted') DEFAULT 'active'
* source_message_version_ids_snapshot JSONB DEFAULT '[]'
* source_card_ids_snapshot JSONB DEFAULT '[]'
* revision_sources_resolved_at TIMESTAMPTZ NULL
* edited_by_user_id BIGINT FK users(id) ON DELETE SET NULL
* edited_at TIMESTAMPTZ DEFAULT now()
* edit_reason TEXT NULL
* redacted_at TIMESTAMPTZ NULL
* redacted_by_forget_event_id BIGINT FK forget_events(id) ON DELETE SET NULL
* created_at TIMESTAMPTZ DEFAULT now()

Constraints:
* UNIQUE(wiki_page_id, revision_seq) — one slot per sequence number per page.
* ck_wiki_revisions_revision_status — revision_status IN ('active','forgotten_redacted')

Indexes:
* btree on (wiki_page_id, revision_seq DESC) — latest-first queries.

Rollback: DROP TABLE wiki_revisions.

Revision ID: 051
Revises: 050
Create Date: 2026-05-16
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "051"
down_revision: Union[str, Sequence[str], None] = "050"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "wiki_revisions",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "wiki_page_id",
            UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("revision_seq", sa.Integer(), nullable=False),
        sa.Column("body_markdown", sa.Text(), nullable=False),
        sa.Column(
            "revision_status",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        # Immutable snapshot of card UUIDs cited at edit time.
        sa.Column(
            "source_card_ids_snapshot",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        # Immutable snapshot of message_version_id integers cited at edit time.
        sa.Column(
            "source_message_version_ids_snapshot",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        # Tracks when this revision's sources were last re-validated against
        # current forget_events state (I7e binding test).
        sa.Column(
            "revision_sources_resolved_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("edited_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "edited_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("edit_reason", sa.Text(), nullable=True),
        sa.Column("redacted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("redacted_by_forget_event_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "wiki_page_id",
            "revision_seq",
            name="uq_wiki_revisions_page_seq",
        ),
        sa.CheckConstraint(
            "revision_status IN ('active','forgotten_redacted')",
            name="ck_wiki_revisions_revision_status",
        ),
        # FK: wiki_page_id → wiki_pages(id) CASCADE — revision dies with page.
        sa.ForeignKeyConstraint(
            ["wiki_page_id"],
            ["wiki_pages.id"],
            name="fk_wiki_revisions_wiki_page_id",
            ondelete="CASCADE",
        ),
        # FK: edited_by_user_id → users(id) SET NULL — audit survives user delete.
        sa.ForeignKeyConstraint(
            ["edited_by_user_id"],
            ["users.id"],
            name="fk_wiki_revisions_edited_by_user_id",
            ondelete="SET NULL",
        ),
        # FK: redacted_by_forget_event_id → forget_events(id) SET NULL.
        sa.ForeignKeyConstraint(
            ["redacted_by_forget_event_id"],
            ["forget_events.id"],
            name="fk_wiki_revisions_redacted_by_forget_event_id",
            ondelete="SET NULL",
        ),
    )
    # Latest-first revision query: ORDER BY revision_seq DESC.
    op.create_index(
        "ix_wiki_revisions_page_seq_desc",
        "wiki_revisions",
        ["wiki_page_id", sa.text("revision_seq DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_wiki_revisions_page_seq_desc", table_name="wiki_revisions")
    op.drop_table("wiki_revisions")
