"""add_extraction_runs_operator_user_id

T6-02 / Phase 6 (Codex HIGH #5): durable audit marker for the admin who
triggered an extraction pass via ``/admin_extract``. Previously
``operator_user_id`` lived only in structured logs, which are not
reliable for long-lived audit trails (rotation, retention, third-party
collection). PHASE6_PLAN.md §5.C requires a DB-level marker so reviewers
can attribute an extraction back to the operator who ran it.

Schema:

* New nullable column ``extraction_runs.operator_user_id BIGINT``.
* NULL = scheduler-driven tick (no operator). Non-NULL = admin handler
  invocation; value is the Telegram user id of the requester.

No FK to ``users.id`` is added intentionally — the value is the
operator's Telegram id (``users.telegram_id`` in this codebase, NOT
``users.id``), and ``users`` rows may be soft-deleted; the audit shadow
must survive that. If a future ticket wants strict referential integrity
it would join via ``telegram_id`` lookup at read time.

Migration numbering: 030-034 are reserved for the T6-01 schema landings
(extraction_runs / candidates / cards / card_sources / decisions). 035
is the next free slot.

Revision ID: 035
Revises: 034
Create Date: 2026-05-12
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "035"
down_revision: Union[str, Sequence[str], None] = "034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "extraction_runs",
        sa.Column(
            "operator_user_id",
            sa.BigInteger(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("extraction_runs", "operator_user_id")
