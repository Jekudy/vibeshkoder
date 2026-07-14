"""Add canonical topic slug to knowledge cards.

Revision ID: 083
Revises: 082
Create Date: 2026-07-14
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "083"
down_revision: Union[str, Sequence[str], None] = "082"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "extraction_runs",
        sa.Column("source_chat_id", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_extraction_runs_source_chat_id_window_end",
        "extraction_runs",
        ["source_chat_id", "ingestion_window_end"],
        postgresql_where=sa.text("source_chat_id IS NOT NULL"),
    )
    op.add_column(
        "knowledge_cards",
        sa.Column("topic_slug", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "ck_knowledge_cards_topic_slug",
        "knowledge_cards",
        "topic_slug IS NULL OR ("
        "char_length(topic_slug) BETWEEN 1 AND 100 "
        "AND topic_slug = lower(topic_slug) "
        "AND topic_slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'"
        ")",
    )
    op.create_index(
        "ix_knowledge_cards_topic_slug",
        "knowledge_cards",
        ["topic_slug"],
        postgresql_where=sa.text("topic_slug IS NOT NULL"),
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM knowledge_cards
                WHERE topic_slug IS NOT NULL
            ) THEN
                RAISE EXCEPTION
                    'Cannot downgrade 083: knowledge_cards.topic_slug contains rollout data';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM extraction_runs
                WHERE source_chat_id IS NOT NULL
            ) THEN
                RAISE EXCEPTION
                    'Cannot downgrade 083: extraction_runs.source_chat_id contains rollout data';
            END IF;
        END
        $$
        """
    )
    op.drop_index("ix_knowledge_cards_topic_slug", table_name="knowledge_cards")
    op.drop_constraint(
        "ck_knowledge_cards_topic_slug",
        "knowledge_cards",
        type_="check",
    )
    op.drop_column("knowledge_cards", "topic_slug")
    op.drop_index(
        "ix_extraction_runs_source_chat_id_window_end",
        table_name="extraction_runs",
    )
    op.drop_column("extraction_runs", "source_chat_id")
