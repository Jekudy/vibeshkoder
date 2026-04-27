"""add_offrecord_marks

T1-13: persistent audit table for ``#nomem`` / ``#offrecord`` detections. Each row
records WHO triggered the mark (set_by_user_id), WHAT (mark_type), WHERE
(scope_type / scope_id, chat_message_id, thread_id), HOW (detected_by) and WHEN
(detected_at). Status lifecycle: active → expired | revoked. Phase 3 admin actions
extend this with revoke flows.

Revision ID: 011
Revises: 010
Create Date: 2026-04-27

Renumbered from 009 to 011 after rebase onto main: a parallel security-fix branch
merged 009_bind_application_invites + 010_add_invite_outbox while Sprint 15 was
under review. The detector + offrecord_marks work has no dependency on those
migrations — adjusting only the head pointer keeps things linear.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "011"
down_revision: Union[str, Sequence[str], None] = "010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "offrecord_marks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("mark_type", sa.String(length=32), nullable=False),
        sa.Column("scope_type", sa.String(length=32), nullable=False),
        sa.Column("scope_id", sa.String(length=255), nullable=True),
        sa.Column("chat_message_id", sa.Integer(), nullable=True),
        sa.Column("thread_id", sa.BigInteger(), nullable=True),
        sa.Column("set_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("detected_by", sa.String(length=128), nullable=False),
        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "mark_type IN ('nomem','offrecord')",
            name="ck_offrecord_marks_mark_type",
        ),
        sa.CheckConstraint(
            "scope_type IN ('message','thread','chat')",
            name="ck_offrecord_marks_scope_type",
        ),
        sa.CheckConstraint(
            "status IN ('active','expired','revoked')",
            name="ck_offrecord_marks_status",
        ),
        sa.ForeignKeyConstraint(
            ["chat_message_id"],
            ["chat_messages.id"],
            name="fk_offrecord_marks_chat_message_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["set_by_user_id"],
            ["users.id"],
            name="fk_offrecord_marks_set_by_user_id",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_offrecord_marks_mark_type_status",
        "offrecord_marks",
        ["mark_type", "status"],
    )
    op.create_index(
        "ix_offrecord_marks_chat_message_id",
        "offrecord_marks",
        ["chat_message_id"],
    )
    op.create_index(
        "ix_offrecord_marks_scope",
        "offrecord_marks",
        ["scope_type", "scope_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_offrecord_marks_scope", table_name="offrecord_marks")
    op.drop_index(
        "ix_offrecord_marks_chat_message_id", table_name="offrecord_marks"
    )
    op.drop_index(
        "ix_offrecord_marks_mark_type_status", table_name="offrecord_marks"
    )
    op.drop_table("offrecord_marks")
