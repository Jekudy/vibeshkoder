"""wiki_pub_log_legacy_grace_nullable

HIGH-2 / Codex MEDIUM #2 fix: legacy_cookie_grace audit rows must not carry
a wiki_page_id because they are session-level events, not page-level events.
Migration 052 declared wiki_page_id NOT NULL with FK to wiki_pages.id. Any
insert with a random UUID that has no matching wiki_pages row raises
ForeignKeyViolation, causing every audit insert to fail silently.

Fix:
  1. Make wiki_publication_log.wiki_page_id NULLABLE.
  2. Add CHECK constraint: page-action rows MUST have wiki_page_id (existing
     behaviour preserved); only legacy_cookie_grace rows MAY have NULL.
     Constraint: (wiki_page_id IS NOT NULL OR action = 'legacy_cookie_grace')
  3. The FK fk_wiki_pub_log_wiki_page_id is preserved as-is — PostgreSQL
     honours FK checks only for non-NULL values, so NULL is always allowed
     regardless of the FK (SQL standard §4.17).

Downgrade notes:
  Drops the CHECK constraint and restores NOT NULL on wiki_page_id.
  WILL FAIL if any legacy_cookie_grace rows exist (they have wiki_page_id=NULL
  which cannot satisfy NOT NULL). Delete or back-fill those rows before downgrading.

Revision ID: 055
Revises: 054
Create Date: 2026-05-19
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "055"
down_revision: Union[str, Sequence[str], None] = "054"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CHECK_NAME = "ck_wiki_pub_log_page_id_or_legacy_grace"


def upgrade() -> None:
    # 1. Drop the NOT NULL constraint by altering the column to NULLABLE.
    op.alter_column(
        "wiki_publication_log",
        "wiki_page_id",
        existing_type=UUID(as_uuid=True),
        nullable=True,
    )

    # 2. Add CHECK: page-action rows still require wiki_page_id; only
    #    legacy_cookie_grace rows may have NULL.
    op.create_check_constraint(
        _CHECK_NAME,
        "wiki_publication_log",
        "wiki_page_id IS NOT NULL OR action = 'legacy_cookie_grace'",
    )


def downgrade() -> None:
    # Drop the CHECK constraint added in upgrade.
    op.drop_constraint(_CHECK_NAME, "wiki_publication_log", type_="check")

    # Restore NOT NULL. This will fail with:
    #   ERROR: column "wiki_page_id" of relation "wiki_publication_log" contains null values
    # if any legacy_cookie_grace rows (wiki_page_id IS NULL) exist. Back-fill or delete
    # those rows before running this downgrade.
    op.alter_column(
        "wiki_publication_log",
        "wiki_page_id",
        existing_type=UUID(as_uuid=True),
        nullable=False,
    )
