"""add_gateway_error_to_extraction_runs

Phase 6.5 M-2 (#262 M-2): persist provider failure reason in the DB so
the audit trail captures gateway errors that previously appeared only as a
successful ledger entry with empty candidates.

Before this migration, ``extract_candidates`` (llm_gateway.py) returned
``{candidates: [], llm_usage_ledger_id: <id>}`` on provider failure.
The extractor saw a non-None ledger_id and wrote run_status='completed',
masking the failure entirely.

After this migration:

* ``extract_candidates`` returns ``{..., "gateway_error": str}`` on failure.
* The extractor inspects ``gateway_error`` BEFORE the ledger-id check.
* Non-null ``gateway_error`` → run_status='failed' + gateway_error persisted.
* Ledger-id is still stored for cost accounting (cost-accounting preserved).

Schema change:

* New nullable column ``extraction_runs.gateway_error TEXT``.
* NULL = no error (successful call OR empty-bundle short-circuit).
* Non-NULL = provider-level error message (truncated to 2000 chars in gateway).

Revision ID: 036
Revises: 035
Create Date: 2026-05-13
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "036"
down_revision: Union[str, Sequence[str], None] = "035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "extraction_runs",
        sa.Column("gateway_error", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("extraction_runs", "gateway_error")
