"""add_llm_ledger_call_type_check

Migration 071: Add CHECK constraint on llm_usage_ledger.call_type.

Migration 064 added the call_type column with a server default of 'unknown'
but NO CHECK constraint. Phase 12 ratifies the allow-list with 8 values
(including the new butler_decision, butler_summary, and extract_candidates).

Uses NOT VALID + VALIDATE pattern (same as migration 038) so the constraint
is added without a full-table scan blocking writes during ALTER.

Pre-flight: verify no existing rows would violate the constraint before adding it.

Revision ID: 071
Revises: 070
Create Date: 2026-05-25
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "071"
down_revision: Union[str, Sequence[str], None] = "070"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Python-side tuple used only for the pre-flight Python set check (not injected into SQL).
ALLOWED_CALL_TYPES = (
    "unknown",
    "qa_synthesis",
    "digest_daily",
    "digest_weekly",
    "graph_projection",
    "extract_candidates",
    "butler_decision",
    "butler_summary",
)

# Hardcoded SQL literal — no f-string / no .format() so there is no injection surface.
ALLOWED_CALL_TYPES_SQL = (
    "('unknown','qa_synthesis','digest_daily','digest_weekly',"
    "'graph_projection','extract_candidates','butler_decision','butler_summary')"
)


def upgrade() -> None:
    # Pre-flight: fail fast if any existing rows violate the new constraint.
    # This protects against running the migration on a DB with unknown call_type values
    # that were inserted before the CHECK was added.
    # SQL string is a hardcoded literal — no f-string / no user-supplied input.
    op.execute(
        "DO $$ "
        "DECLARE "
        "  violating_count INTEGER; "
        "BEGIN "
        "  SELECT COUNT(*) INTO violating_count "
        "  FROM llm_usage_ledger "
        "  WHERE call_type NOT IN "
        + ALLOWED_CALL_TYPES_SQL
        + "; "
        "  IF violating_count > 0 THEN "
        "    RAISE EXCEPTION "
        "      'Migration 071 pre-flight failed: % rows in llm_usage_ledger have call_type "
        "values not in the new allow-list. Fix the data before applying this migration.', "
        "      violating_count; "
        "  END IF; "
        "END $$;"
    )

    # Add CHECK constraint with NOT VALID (no full-table scan blocking writes).
    # SQL string is a hardcoded literal.
    op.execute(
        "ALTER TABLE llm_usage_ledger "
        "  ADD CONSTRAINT ck_llm_usage_ledger_call_type CHECK (call_type IN "
        + ALLOWED_CALL_TYPES_SQL
        + ") "
        "  NOT VALID"
    )

    # Validate the constraint separately so it can be re-validated post-deploy
    # without blocking writes during the initial ALTER.
    op.execute("""
ALTER TABLE llm_usage_ledger
  VALIDATE CONSTRAINT ck_llm_usage_ledger_call_type
""")


def downgrade() -> None:
    op.execute("ALTER TABLE llm_usage_ledger DROP CONSTRAINT IF EXISTS ck_llm_usage_ledger_call_type")
