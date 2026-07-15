"""Add durable fail-closed claims for paid image descriptions.

Revision ID: 086
Revises: 085
Create Date: 2026-07-14
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "086"
down_revision: Union[str, Sequence[str], None] = "085"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _add_status_constraint(*, include_processing: bool) -> None:
    values = "'pending','ready','failed','missing_source'"
    if include_processing:
        values = "'pending','processing','ready','failed','missing_source'"
    op.execute(
        "ALTER TABLE message_media "
        "ADD CONSTRAINT ck_message_media_description_status "
        f"CHECK (description_status IN ({values})) NOT VALID"
    )
    op.execute("ALTER TABLE message_media VALIDATE CONSTRAINT ck_message_media_description_status")


def upgrade() -> None:
    op.add_column(
        "message_media",
        sa.Column("description_claim_token", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "message_media",
        sa.Column("description_claimed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("ALTER TABLE message_media DROP CONSTRAINT ck_message_media_description_status")
    _add_status_constraint(include_processing=True)
    op.execute(
        "ALTER TABLE message_media "
        "ADD CONSTRAINT ck_message_media_processing_claim "
        "CHECK ((description_status = 'processing') = "
        "(description_claim_token IS NOT NULL AND description_claimed_at IS NOT NULL)) "
        "NOT VALID"
    )
    op.execute("ALTER TABLE message_media VALIDATE CONSTRAINT ck_message_media_processing_claim")


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM message_media
                WHERE description_status = 'processing'
                   OR description_claim_token IS NOT NULL
                   OR description_claimed_at IS NOT NULL
            ) THEN
                RAISE EXCEPTION
                    'Cannot downgrade 086: durable image claims are present';
            END IF;
        END
        $$
        """
    )
    op.execute("ALTER TABLE message_media DROP CONSTRAINT ck_message_media_processing_claim")
    op.execute("ALTER TABLE message_media DROP CONSTRAINT ck_message_media_description_status")
    _add_status_constraint(include_processing=False)
    op.drop_column("message_media", "description_claimed_at")
    op.drop_column("message_media", "description_claim_token")
