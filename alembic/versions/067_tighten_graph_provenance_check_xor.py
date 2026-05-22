"""tighten_graph_provenance_check_xor

10.5-9: Tighten ck_graph_provenance_has_source from OR to XOR.

Before: source_message_version_id IS NOT NULL OR source_card_id IS NOT NULL
After:  (source_message_version_id IS NOT NULL AND source_card_id IS NULL)
        OR
        (source_message_version_id IS NULL AND source_card_id IS NOT NULL)

Pre-flight guard: aborts if any existing rows violate XOR (both sources
non-NULL simultaneously). This prevents silent data corruption on upgrade.

Revision ID: 067
Revises: 066
Create Date: 2026-05-22
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "067"
down_revision: Union[str, Sequence[str], None] = "066"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "graph_provenance"
_CK_HAS_SOURCE = "ck_graph_provenance_has_source"

_XOR_EXPR = (
    "(source_message_version_id IS NOT NULL AND source_card_id IS NULL)"
    " OR "
    "(source_message_version_id IS NULL AND source_card_id IS NOT NULL)"
)
_OR_EXPR = "source_message_version_id IS NOT NULL OR source_card_id IS NOT NULL"


def upgrade() -> None:
    # ── Pre-flight guard ──────────────────────────────────────────────────────
    # Abort if any rows violate XOR (both sources simultaneously non-NULL).
    # Manually triage and fix data before re-running this migration.
    bind = op.get_bind()
    result = bind.execute(
        sa.text(
            """
            SELECT count(*)
            FROM graph_provenance
            WHERE source_message_version_id IS NOT NULL
              AND source_card_id IS NOT NULL
            """
        )
    ).scalar()
    if result and result > 0:
        raise RuntimeError(
            f"Migration 067 cannot proceed: {result} rows violate XOR "
            "(both source_message_version_id and source_card_id non-NULL). "
            "Manually triage and fix data before re-running."
        )

    # ── Swap constraint: OR → XOR ─────────────────────────────────────────────
    op.execute(
        f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_CK_HAS_SOURCE}"
    )
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CK_HAS_SOURCE} CHECK ({_XOR_EXPR})"
    )


def downgrade() -> None:
    # Restore OR constraint (pre-067 semantics).
    op.execute(
        f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_CK_HAS_SOURCE}"
    )
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CK_HAS_SOURCE} CHECK ({_OR_EXPR})"
    )
