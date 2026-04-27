"""add_forget_events

T3-01: foundation for Phase 3 governance — a ``forget_events`` table records every
forget/tombstone request issued by users, admins, or the system. Each row tracks
WHAT is being forgotten (target_type / target_id / tombstone_key), WHO issued the
request (actor_user_id / authorized_by), WHY (reason), and its current progress
through the cascade layers (status / cascade_status).

Revision ID: 013
Revises: 012
Create Date: 2026-04-27
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "013"
down_revision: Union[str, Sequence[str], None] = "012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "forget_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.String(length=255), nullable=True),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=True),
        sa.Column("authorized_by", sa.String(length=64), nullable=False),
        sa.Column("tombstone_key", sa.String(length=512), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "policy",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'forgotten'"),
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        # JSONB (not JSON): enables future GIN indexing on per-layer cascade progress.
        sa.Column("cascade_status", JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tombstone_key", name="uq_forget_events_tombstone_key"),
        sa.CheckConstraint(
            "target_type IN ('message','message_hash','user','export')",
            name="ck_forget_events_target_type",
        ),
        sa.CheckConstraint(
            "authorized_by IN ('self','admin','system','gdpr_request')",
            name="ck_forget_events_authorized_by",
        ),
        sa.CheckConstraint(
            "policy IN ('forgotten','offrecord_propagated')",
            name="ck_forget_events_policy",
        ),
        sa.CheckConstraint(
            "status IN ('pending','processing','completed','failed')",
            name="ck_forget_events_status",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_forget_events_actor_user_id",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_forget_events_status_created_at",
        "forget_events",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_forget_events_target_type_target_id",
        "forget_events",
        ["target_type", "target_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_forget_events_target_type_target_id", table_name="forget_events"
    )
    op.drop_index("ix_forget_events_status_created_at", table_name="forget_events")
    op.drop_table("forget_events")
