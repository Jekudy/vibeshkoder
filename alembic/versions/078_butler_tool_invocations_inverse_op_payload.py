"""butler_tool_invocations_inverse_op_payload

Migration 078: Add inverse_op_payload JSONB column to butler_tool_invocations.

T12-07 fix C1: execute_undo reads inv.inverse_op_payload to determine the
rollback_kind and parameters for each step. Without the DB column, the ORM
attribute does not exist and every real undo invocation raises AttributeError.

Revision ID: 078
Revises: 077
Create Date: 2026-05-27
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "078"
down_revision: Union[str, Sequence[str], None] = "077"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "butler_tool_invocations",
        sa.Column(
            "inverse_op_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("butler_tool_invocations", "inverse_op_payload")
