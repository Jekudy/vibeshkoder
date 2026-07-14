"""Add image memory storage and rollout ledger call types.

Revision ID: 080
Revises: 079
Create Date: 2026-07-14
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "080"
down_revision: Union[str, Sequence[str], None] = "079"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_CALL_TYPES = (
    "'unknown','qa_synthesis','digest_daily','digest_weekly',"
    "'graph_projection','extract_candidates','butler_decision','butler_summary'"
)
_NEW_CALL_TYPES = _OLD_CALL_TYPES + ",'wiki_compilation','image_description'"


def _add_call_type_constraint(values: str) -> None:
    op.execute(
        "ALTER TABLE llm_usage_ledger "
        "ADD CONSTRAINT ck_llm_usage_ledger_call_type "
        f"CHECK (call_type IN ({values})) NOT VALID"
    )
    op.execute("ALTER TABLE llm_usage_ledger VALIDATE CONSTRAINT ck_llm_usage_ledger_call_type")


def upgrade() -> None:
    op.execute("ALTER TABLE llm_usage_ledger DROP CONSTRAINT ck_llm_usage_ledger_call_type")
    _add_call_type_constraint(_NEW_CALL_TYPES)

    op.create_table(
        "message_media",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("chat_message_id", sa.Integer(), nullable=False),
        sa.Column("media_kind", sa.String(length=32), nullable=False),
        sa.Column("telegram_file_id", sa.Text(), nullable=True),
        sa.Column("telegram_file_unique_id", sa.Text(), nullable=True),
        sa.Column("source_message_url", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("description_status", sa.String(length=32), nullable=False),
        sa.Column("description_model", sa.String(length=128), nullable=True),
        sa.Column("llm_usage_ledger_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("media_kind = 'photo'", name="ck_message_media_kind"),
        sa.CheckConstraint(
            "description_status IN ('pending','ready','failed','missing_source')",
            name="ck_message_media_description_status",
        ),
        sa.ForeignKeyConstraint(
            ["chat_message_id"],
            ["chat_messages.id"],
            name="fk_message_media_chat_message_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["llm_usage_ledger_id"],
            ["llm_usage_ledger.id"],
            name="fk_message_media_llm_usage_ledger_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_message_media"),
        sa.UniqueConstraint("chat_message_id", name="uq_message_media_chat_message_id"),
    )
    op.create_index(
        "ix_message_media_description_status",
        "message_media",
        ["description_status"],
        unique=False,
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM message_media) THEN
                RAISE EXCEPTION
                    'Cannot downgrade 080: message_media contains rollout data';
            END IF;
            IF EXISTS (
                SELECT 1 FROM llm_usage_ledger
                WHERE call_type IN ('wiki_compilation','image_description')
            ) THEN
                RAISE EXCEPTION
                    'Cannot downgrade 080: rollout ledger rows use new call types';
            END IF;
        END
        $$
        """
    )
    op.drop_index("ix_message_media_description_status", table_name="message_media")
    op.drop_table("message_media")
    op.execute("ALTER TABLE llm_usage_ledger DROP CONSTRAINT ck_llm_usage_ledger_call_type")
    _add_call_type_constraint(_OLD_CALL_TYPES)
