"""add_wiki_publication_log

T9-01 / Phase 9: append-only audit trail for wiki page publication events.

No UPDATE or DELETE on this table — ever. Any new publication state change
creates a new row (PHASE9_PLAN.md §5.A Migration 052).

Records: publish, unpublish, robots_index, robots_noindex, legacy_cookie_grace.
``source_check_result`` stores the structured validation payload from
``wiki_governance.validate_sources(...)`` serialized as JSONB.

The column ``source_check_result`` is NOT NULL in the schema spec from the plan
(``JSONB NOT NULL``), so we enforce NOT NULL here. The publication service must
always pass the check result (even ``{"valid": true, ...}`` for a clean publish).

Rollback: DROP TABLE wiki_publication_log.

Revision ID: 052
Revises: 051
Create Date: 2026-05-16
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "052"
down_revision: Union[str, Sequence[str], None] = "051"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "wiki_publication_log",
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
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=True),
        sa.Column("prior_public_enabled", sa.Boolean(), nullable=False),
        sa.Column("new_public_enabled", sa.Boolean(), nullable=False),
        sa.Column("prior_robots_policy", sa.Text(), nullable=False),
        sa.Column("new_robots_policy", sa.Text(), nullable=False),
        # Structured validation payload from wiki_governance.validate_sources().
        # NOT NULL: every publication event must record the source check state.
        sa.Column("source_check_result", JSONB(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "action IN ('publish','unpublish','robots_index','robots_noindex','legacy_cookie_grace')",
            name="ck_wiki_pub_log_action",
        ),
        # FK: wiki_page_id → wiki_pages(id) ON DELETE CASCADE — log dies with page.
        sa.ForeignKeyConstraint(
            ["wiki_page_id"],
            ["wiki_pages.id"],
            name="fk_wiki_pub_log_wiki_page_id",
            ondelete="CASCADE",
        ),
        # FK: actor_user_id → users(id) ON DELETE SET NULL — audit survives user delete.
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_wiki_pub_log_actor_user_id",
            ondelete="SET NULL",
        ),
    )
    # Latest-first audit log query per page.
    op.create_index(
        "ix_wiki_pub_log_page_id",
        "wiki_publication_log",
        ["wiki_page_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_wiki_pub_log_page_id", table_name="wiki_publication_log")
    op.drop_table("wiki_publication_log")
