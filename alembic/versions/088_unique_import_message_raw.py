"""Enforce one canonical synthetic import raw row per source message.

Revision ID: 088
Revises: 087
Create Date: 2026-07-15
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "088"
down_revision: Union[str, Sequence[str], None] = "087"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX_NAME = "uq_telegram_updates_import_message_source"
_PREDICATE = (
    "update_id IS NULL "
    "AND update_type = 'import_message' "
    "AND chat_id IS NOT NULL "
    "AND message_id IS NOT NULL"
)


def upgrade() -> None:
    op.create_index(
        _INDEX_NAME,
        "telegram_updates",
        ["update_type", "chat_id", "message_id"],
        unique=True,
        postgresql_where=sa.text(_PREDICATE),
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="telegram_updates")
