"""butler_tool_invocations_posted_message_id

Migration 076: Adds posted_message_id (NULLABLE BigInteger) + index to
butler_tool_invocations. Used by update_intro tool to verify Butler ownership
of a previously sent message without querying butler_actions directly.

The column is written by send_intro / schedule_meeting after a successful
bot.send_message() call, so update_intro can look up the posted message
via ButlerToolInvocationRepo.find_by_posted_message_id.

Phase 12 T12-06-fix cycle (C2 update_intro ownership check).

Revision ID: 076
Revises: 075
Create Date: 2026-05-27
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "076"
down_revision: Union[str, Sequence[str], None] = "075"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── butler_tool_invocations: posted_message_id ────────────────────────────
    # NULLABLE: only populated for tools that send Telegram messages (send_intro,
    # schedule_meeting). Tools that don't post messages (recall_evidence,
    # suggest_card_creation) leave this NULL. update_intro uses it for ownership
    # verification via find_by_posted_message_id.
    op.add_column(
        "butler_tool_invocations",
        sa.Column("posted_message_id", sa.BigInteger(), nullable=True),
    )
    # Partial index: only index non-NULL rows (ownership lookups require a real
    # message_id; NULL rows are not candidates for ownership checks).
    op.create_index(
        "ix_butler_tool_invocations_posted_message_id",
        "butler_tool_invocations",
        ["posted_message_id"],
        postgresql_where=sa.text("posted_message_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_butler_tool_invocations_posted_message_id",
        table_name="butler_tool_invocations",
    )
    op.drop_column("butler_tool_invocations", "posted_message_id")
