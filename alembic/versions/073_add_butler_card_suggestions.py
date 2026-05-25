"""add_butler_card_suggestions

Migration 073: butler_card_suggestions mapping table.

Links a Butler audit row (butler_actions) to the Phase 6 admin-review queue
(extraction_candidates). UNIQUE on butler_action_id enforces the one-action →
one-suggestion invariant: a single /butler request with suggest_card_creation
writes exactly one row here.

extraction_candidate_id is NULLABLE (ON DELETE SET NULL) because the candidate
may be created asynchronously after the Butler suggestion row is written.

Per PHASE12_PLAN_REFRESH.md §4.6.

Revision ID: 073
Revises: 072
Create Date: 2026-05-25
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "073"
down_revision: Union[str, Sequence[str], None] = "072"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE butler_card_suggestions (
  id BIGSERIAL PRIMARY KEY,
  butler_action_id BIGINT NOT NULL REFERENCES butler_actions(id) ON DELETE RESTRICT,
  extraction_candidate_id UUID REFERENCES extraction_candidates(id) ON DELETE SET NULL,
  suggested_card_payload JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_by_user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,

  CONSTRAINT uq_butler_card_suggestions_action UNIQUE (butler_action_id)
)
""")

    # Partial index: only non-NULL extraction_candidate_id rows are queried
    # by the Phase 6 admin reviewer. Rows with NULL are awaiting candidate creation.
    op.execute("""
CREATE INDEX ix_butler_card_suggestions_candidate
  ON butler_card_suggestions(extraction_candidate_id)
  WHERE extraction_candidate_id IS NOT NULL
""")

    op.execute("""
CREATE INDEX ix_butler_card_suggestions_created
  ON butler_card_suggestions(created_at)
""")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS butler_card_suggestions")
