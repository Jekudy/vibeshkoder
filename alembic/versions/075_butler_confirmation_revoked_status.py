"""butler_confirmation_revoked_status

Migration 075: Widens ck_butler_action_confirmations_status CHECK constraint to
include 'revoked'. Required for revoke_affected_user_consent (T12-05-fix C1).

Previous CHECK: ('pending','confirmed','rejected','expired','cancelled')
New CHECK:      ('pending','confirmed','rejected','expired','cancelled','revoked')

Revision ID: 075
Revises: 074
Create Date: 2026-05-27
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "075"
down_revision: Union[str, Sequence[str], None] = "074"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # PostgreSQL does not support ALTER CONSTRAINT — drop old + recreate new.
    op.drop_constraint(
        "ck_butler_action_confirmations_status",
        "butler_action_confirmations",
        type_="check",
    )
    op.create_check_constraint(
        "ck_butler_action_confirmations_status",
        "butler_action_confirmations",
        "status IN ('pending','confirmed','rejected','expired','cancelled','revoked')",
    )


def downgrade() -> None:
    # Reverse: remove 'revoked' from the allowed values.
    # Guard: if any 'revoked' rows exist, the downgrade would violate the CHECK.
    # Caller must ensure no 'revoked' rows before downgrading.
    op.drop_constraint(
        "ck_butler_action_confirmations_status",
        "butler_action_confirmations",
        type_="check",
    )
    op.create_check_constraint(
        "ck_butler_action_confirmations_status",
        "butler_action_confirmations",
        "status IN ('pending','confirmed','rejected','expired','cancelled')",
    )
