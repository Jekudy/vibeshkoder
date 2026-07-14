"""Retire legacy forget policy while preserving its audit trail.

Revision ID: 082
Revises: 081
Create Date: 2026-07-14
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "082"
down_revision: Union[str, Sequence[str], None] = "081"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_forget_events_status", "forget_events", type_="check")
    op.create_check_constraint(
        "ck_forget_events_status",
        "forget_events",
        "status IN ('pending','processing','completed','failed','superseded')",
    )
    op.execute(
        """
        UPDATE forget_events
           SET status = 'superseded',
               cascade_status = COALESCE(cascade_status, '{}'::jsonb)
                   || '{"phase13":"superseded_complete_history"}'::jsonb,
               updated_at = now()
         WHERE status IN ('pending','processing','completed')
        """
    )
    op.execute("UPDATE offrecord_marks SET status='revoked' WHERE status='active'")

    op.drop_constraint(
        "uq_message_versions_chat_message_content_hash",
        "message_versions",
        type_="unique",
    )
    op.create_index(
        "uq_message_versions_chat_message_content_hash_active",
        "message_versions",
        ["chat_message_id", "content_hash"],
        unique=True,
        postgresql_where=sa.text("is_redacted = false"),
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM forget_events
                WHERE status = 'superseded'
                   OR cascade_status @> '{"phase13":"superseded_complete_history"}'::jsonb
            ) THEN
                RAISE EXCEPTION
                    'Cannot downgrade 082: superseded forget-event provenance cannot be restored';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM offrecord_marks
                WHERE status = 'revoked'
            ) THEN
                RAISE EXCEPTION
                    'Cannot downgrade 082: revoked offrecord marks may contain phase13 state';
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM message_versions
                 GROUP BY chat_message_id, content_hash
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'Cannot downgrade 082: restored and redacted versions share a content hash';
            END IF;
        END
        $$
        """
    )
    op.drop_index(
        "uq_message_versions_chat_message_content_hash_active",
        table_name="message_versions",
    )
    op.create_unique_constraint(
        "uq_message_versions_chat_message_content_hash",
        "message_versions",
        ["chat_message_id", "content_hash"],
    )
    op.drop_constraint("ck_forget_events_status", "forget_events", type_="check")
    op.create_check_constraint(
        "ck_forget_events_status",
        "forget_events",
        "status IN ('pending','processing','completed','failed')",
    )
