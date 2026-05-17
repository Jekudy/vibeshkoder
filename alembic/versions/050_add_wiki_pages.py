"""add_wiki_pages

T9-01 / Phase 9: core wiki page store.

Creates ``wiki_pages`` table with:
- UUID PK, slug UNIQUE, title, body_markdown
- Generated tsvector ``body_tsv`` for Russian FTS (GIN-indexed)
- Page lifecycle columns: page_status, visibility, public_enabled, robots_policy
- Validation optimization columns: validation_status, last_validated_at,
  invalidated_at, invalidated_by_forget_event_id
- Attribution: created_by_user_id, reviewed_by_user_id, reviewed_at
- Audit: created_at, updated_at

CHECK constraints (inline; VARCHAR + CHECK per project convention):
* ck_wiki_pages_page_status — page_status IN ('draft','reviewed','stale','archived')
* ck_wiki_pages_visibility — visibility IN ('member','admin','public_candidate')
* ck_wiki_pages_robots_policy — robots_policy IN ('noindex','index')
* ck_wiki_pages_validation_status — validation_status IN ('valid','stale','invalid')
* ck_wiki_pages_public_requires_reviewed — public_enabled=true requires
  page_status IN ('reviewed', 'stale')
* ck_wiki_pages_stale_not_public — page_status='stale' requires public_enabled=false
* ck_wiki_pages_robots_index_requires_public — robots_policy='index' requires
  public_enabled=true

Indexes:
* GIN on body_tsv
* btree on slug (implicit via UNIQUE)
* btree on page_status WHERE page_status='reviewed' (partial)
* btree on (public_enabled, page_status) WHERE public_enabled=true (partial)
* btree on last_validated_at

Rollback: DROP TABLE wiki_pages CASCADE.

Revision ID: 050
Revises: 038
Create Date: 2026-05-16
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TSVECTOR, UUID

revision: str = "050"
down_revision: Union[str, Sequence[str], None] = "038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "wiki_pages",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("slug", sa.String(255), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column(
            "body_markdown",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
        # Generated tsvector column: Russian FTS over title + body_markdown.
        # Matches the Phase 4 baseline (message_versions.search_tsv uses the
        # same 'russian' config). Separate from /recall canonical evidence —
        # wiki pages are derived and editable (PHASE9_PLAN.md §5.B).
        sa.Column(
            "body_tsv",
            TSVECTOR(),
            sa.Computed(
                "to_tsvector('russian', coalesce(title, '') || ' ' || coalesce(body_markdown, ''))",
                persisted=True,
            ),
            nullable=True,
        ),
        sa.Column(
            "page_status",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'draft'"),
        ),
        sa.Column(
            "visibility",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'member'"),
        ),
        sa.Column(
            "public_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "robots_policy",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'noindex'"),
        ),
        sa.Column(
            "validation_status",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'valid'"),
        ),
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidated_by_forget_event_id", sa.BigInteger(), nullable=True),
        sa.Column("created_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column("reviewed_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint("slug", name="uq_wiki_pages_slug"),
        # page_status enum guard
        sa.CheckConstraint(
            "page_status IN ('draft','reviewed','stale','archived')",
            name="ck_wiki_pages_page_status",
        ),
        # visibility enum guard
        sa.CheckConstraint(
            "visibility IN ('member','admin','public_candidate')",
            name="ck_wiki_pages_visibility",
        ),
        # robots_policy enum guard
        sa.CheckConstraint(
            "robots_policy IN ('noindex','index')",
            name="ck_wiki_pages_robots_policy",
        ),
        # validation_status enum guard
        sa.CheckConstraint(
            "validation_status IN ('valid','stale','invalid')",
            name="ck_wiki_pages_validation_status",
        ),
        # public_enabled=true requires page_status='reviewed' or 'stale'.
        # stale pages retain public_enabled until cascade or admin explicitly
        # unpublishes; the stale transition itself forces public_enabled=false
        # (see _cascade_wiki_pages in PHASE9_PLAN.md §5.F).
        sa.CheckConstraint(
            "public_enabled = false OR page_status IN ('reviewed', 'stale')",
            name="ck_wiki_pages_public_requires_reviewed",
        ),
        # page_status='stale' requires public_enabled=false.
        sa.CheckConstraint(
            "page_status <> 'stale' OR public_enabled = false",
            name="ck_wiki_pages_stale_not_public",
        ),
        # robots_policy='index' requires public_enabled=true.
        sa.CheckConstraint(
            "robots_policy <> 'index' OR public_enabled = true",
            name="ck_wiki_pages_robots_index_requires_public",
        ),
        # FK: invalidated_by_forget_event_id → forget_events.id
        sa.ForeignKeyConstraint(
            ["invalidated_by_forget_event_id"],
            ["forget_events.id"],
            name="fk_wiki_pages_invalidated_by_forget_event_id",
            ondelete="SET NULL",
        ),
        # FK: created_by_user_id → users.id
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_wiki_pages_created_by_user_id",
            ondelete="SET NULL",
        ),
        # FK: reviewed_by_user_id → users.id
        sa.ForeignKeyConstraint(
            ["reviewed_by_user_id"],
            ["users.id"],
            name="fk_wiki_pages_reviewed_by_user_id",
            ondelete="SET NULL",
        ),
    )

    # GIN index on body_tsv — powers /wiki/search (MEDIUM E).
    op.create_index(
        "ix_wiki_pages_body_tsv",
        "wiki_pages",
        ["body_tsv"],
        postgresql_using="gin",
    )
    # Partial btree on page_status='reviewed' — member listing query filter.
    op.create_index(
        "ix_wiki_pages_status_reviewed",
        "wiki_pages",
        ["page_status"],
        postgresql_where=sa.text("page_status = 'reviewed'"),
    )
    # Partial btree on public_enabled + page_status WHERE public_enabled=true —
    # public surface listing.
    op.create_index(
        "ix_wiki_pages_public_enabled",
        "wiki_pages",
        ["public_enabled", "page_status"],
        postgresql_where=sa.text("public_enabled = true"),
    )
    # btree on last_validated_at — validation sweep queries.
    op.create_index(
        "ix_wiki_pages_last_validated",
        "wiki_pages",
        ["last_validated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_wiki_pages_last_validated", table_name="wiki_pages")
    op.drop_index("ix_wiki_pages_public_enabled", table_name="wiki_pages")
    op.drop_index("ix_wiki_pages_status_reviewed", table_name="wiki_pages")
    op.drop_index("ix_wiki_pages_body_tsv", table_name="wiki_pages")
    op.drop_table("wiki_pages")
