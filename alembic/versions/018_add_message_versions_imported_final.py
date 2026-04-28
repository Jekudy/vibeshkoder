"""add_message_versions_imported_final

T2-NEW-H / T2-03 (#103, #106): adds ``message_versions.imported_final`` Boolean column.

Per ``docs/memory-system/import-edit-history.md`` §5.1, every row written by an import
apply run (#103 / Stream Delta) sets ``imported_final=TRUE``. Live ingestion rows leave
the column at its server default (``FALSE``).

The flag denormalises provenance so downstream consumers (q&a, search, citations)
can filter on a single column instead of joining ``telegram_updates`` →
``ingestion_runs.run_type``. The FK chain remains the audit trail of last resort
(documented invariant in ``import-edit-history.md`` §4).

Additive migration — existing rows acquire ``imported_final=FALSE`` via the
server_default without an explicit UPDATE.

Revision ID: 018
Revises: 017
Create Date: 2026-04-28
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "018"
down_revision: Union[str, Sequence[str], None] = "017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "message_versions",
        sa.Column(
            "imported_final",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("message_versions", "imported_final")
