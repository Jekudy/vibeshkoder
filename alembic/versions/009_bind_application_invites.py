"""bind_application_invites

CRIT-01: record the Telegram user an invite was issued to, so chat admission can reject
forwarded bearer invite links. The column is additive and nullable for safe deploys.
Existing vouched/added applications are backfilled to their applicant id so in-flight
legitimate invites keep working after the migration.

Revision ID: 009
Revises: 008
Create Date: 2026-04-27
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "009"
down_revision: Union[str, Sequence[str], None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "applications",
        sa.Column("invite_user_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_applications_invite_user_id",
        "applications",
        "users",
        ["invite_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute(
        """
        UPDATE applications
        SET invite_user_id = user_id
        WHERE status IN ('vouched', 'added')
          AND invite_user_id IS NULL
        """
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_applications_invite_user_id",
        "applications",
        type_="foreignkey",
    )
    op.drop_column("applications", "invite_user_id")
