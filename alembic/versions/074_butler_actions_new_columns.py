"""butler_actions_new_columns

Migration 074: Adds query, visibility_scope, plan_payload to butler_actions;
adds confirmation_token (+UNIQUE index) to butler_action_confirmations.

Phase 12 T12-04 fix cycle (C1 confirmation_token, C2 query+visibility_scope, C3 plan_payload).

Revision ID: 074
Revises: 073
Create Date: 2026-05-26
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "074"
down_revision: Union[str, Sequence[str], None] = "073"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── butler_actions: query ──────────────────────────────────────────────────
    # Empty string default: backfills existing rows and allows callers that don't
    # supply query= to still insert without violating NOT NULL. New rows always
    # pass an explicit value; the server_default is a safety net.
    op.add_column(
        "butler_actions",
        sa.Column("query", sa.Text(), nullable=False, server_default=""),
    )

    # ── butler_actions: visibility_scope ──────────────────────────────────────
    # Default 'member' matches the most common invocation scope.
    op.add_column(
        "butler_actions",
        sa.Column(
            "visibility_scope",
            sa.Text(),
            nullable=False,
            server_default="member",
        ),
    )
    op.create_check_constraint(
        "ck_butler_actions_visibility_scope",
        "butler_actions",
        "visibility_scope IN ('member','admin','self')",
    )

    # ── butler_actions: plan_payload ──────────────────────────────────────────
    # Stores the full ButlerPlan as JSONB for execute-time replay.
    # Empty-object default covers backfill and legacy inserts.
    op.add_column(
        "butler_actions",
        sa.Column(
            "plan_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )

    # ── butler_action_confirmations: confirmation_token ───────────────────────
    # Opaque per-confirmation token generated with secrets.token_urlsafe(32).
    # UNIQUE ensures tokens cannot be re-presented across different rows.
    # Empty string default covers legacy rows; new rows always supply a fresh token.
    op.add_column(
        "butler_action_confirmations",
        sa.Column("confirmation_token", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index(
        "uq_butler_action_confirmations_token",
        "butler_action_confirmations",
        ["confirmation_token"],
        unique=True,
    )


def downgrade() -> None:
    # Reverse order from upgrade
    op.drop_index("uq_butler_action_confirmations_token", table_name="butler_action_confirmations")
    op.drop_column("butler_action_confirmations", "confirmation_token")
    op.drop_constraint(
        "ck_butler_actions_visibility_scope", "butler_actions", type_="check"
    )
    op.drop_column("butler_actions", "plan_payload")
    op.drop_column("butler_actions", "visibility_scope")
    op.drop_column("butler_actions", "query")
