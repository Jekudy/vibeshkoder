"""Make one intro refresh delivery claim per user and shared wave.

Revision ID: 092
Revises: 091
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "092"
down_revision: Union[str, Sequence[str], None] = "091"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_intro_refresh_tracking_user_cycle",
        "intro_refresh_tracking",
        ["user_id", "cycle_started_at"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_intro_refresh_tracking_user_cycle",
        "intro_refresh_tracking",
        type_="unique",
    )
