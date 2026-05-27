"""butler_undo_invocations

Migration 077: Add butler_undo_invocations table for per-step undo audit,
and widen ck_butler_actions_status CHECK to include 'undone'.

T12-07: /butler_undo + 5 rollback kinds.

Revision ID: 077
Revises: 076
Create Date: 2026-05-27
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "077"
down_revision: Union[str, Sequence[str], None] = "076"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Widen butler_actions status CHECK to add 'undone' ─────────────────────
    # PostgreSQL does not support ALTER CONSTRAINT — drop old + recreate new.
    op.drop_constraint(
        "ck_butler_actions_status",
        "butler_actions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_butler_actions_status",
        "butler_actions",
        "status IN ("
        "'requested','evidence_loaded','planned','pending_confirmation',"
        "'confirmed','executing','succeeded','undone',"
        "'undo_pending','undo_succeeded','undo_failed',"
        "'rejected','expired','execution_failed','cancelled'"
        ")",
    )

    # ── butler_undo_invocations table ─────────────────────────────────────────
    op.create_table(
        "butler_undo_invocations",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "butler_action_id",
            sa.BigInteger,
            sa.ForeignKey("butler_actions.id", ondelete="RESTRICT", name="fk_butler_undo_action_id"),
            nullable=False,
        ),
        sa.Column(
            "butler_tool_invocation_id",
            sa.BigInteger,
            sa.ForeignKey(
                "butler_tool_invocations.id",
                ondelete="RESTRICT",
                name="fk_butler_undo_invocation_id",
            ),
            nullable=False,
        ),
        sa.Column("requester_user_id", sa.BigInteger, nullable=False),
        sa.Column(
            "rollback_kind",
            sa.Text,
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Text,
            nullable=False,
            server_default="pending",
        ),
        sa.Column("error_kind", sa.Text, nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )

    # Check constraints
    op.create_check_constraint(
        "ck_butler_undo_invocations_rollback_kind",
        "butler_undo_invocations",
        "rollback_kind IN ('not_reversible','delete_message','edit_message','followup_correction','cancel_pending')",
    )
    op.create_check_constraint(
        "ck_butler_undo_invocations_status",
        "butler_undo_invocations",
        "status IN ('pending','succeeded','failed','skipped_not_reversible')",
    )

    # UNIQUE index for idempotency
    op.create_unique_constraint(
        "uq_butler_undo_invocations_action_invocation",
        "butler_undo_invocations",
        ["butler_action_id", "butler_tool_invocation_id"],
    )

    # Index for lookup by action_id
    op.create_index(
        "ix_butler_undo_invocations_butler_action_id",
        "butler_undo_invocations",
        ["butler_action_id"],
    )


def downgrade() -> None:
    # Remove undo table
    op.drop_index("ix_butler_undo_invocations_butler_action_id", table_name="butler_undo_invocations")
    op.drop_constraint("uq_butler_undo_invocations_action_invocation", "butler_undo_invocations", type_="unique")
    op.drop_constraint("ck_butler_undo_invocations_status", "butler_undo_invocations", type_="check")
    op.drop_constraint("ck_butler_undo_invocations_rollback_kind", "butler_undo_invocations", type_="check")
    op.drop_table("butler_undo_invocations")

    # Revert status CHECK (remove 'undone')
    op.drop_constraint("ck_butler_actions_status", "butler_actions", type_="check")
    op.create_check_constraint(
        "ck_butler_actions_status",
        "butler_actions",
        "status IN ("
        "'requested','evidence_loaded','planned','pending_confirmation',"
        "'confirmed','executing','succeeded',"
        "'undo_pending','undo_succeeded','undo_failed',"
        "'rejected','expired','execution_failed','cancelled'"
        ")",
    )
