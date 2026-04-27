"""add_users_imported_flag

T2-NEW-B: adds ``users.is_imported_only`` boolean column to support ghost users created
during Telegram Desktop import. Ghost users (deleted Telegram accounts or anonymous
channel posts) are never merged with live users — the flag makes them explicitly
identifiable so all downstream code can apply the privacy rule.

``is_imported_only`` defaults to False for all existing rows. New live users created
by the gatekeeper flow also default to False. Only the import service sets this to True
(via ``_create_ghost_user`` / ``_get_or_create_anonymous_channel_user``).

The sparse partial index ``ix_users_is_imported_only_true`` covers only True rows, keeping
it small since the overwhelming majority of rows are live users (is_imported_only=False).

Revision ID: 013
Revises: 012
Create Date: 2026-04-27
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "013"
down_revision: Union[str, Sequence[str], None] = "012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "is_imported_only",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # Sparse index — only ghost rows (is_imported_only = TRUE) are indexed.
    op.create_index(
        "ix_users_is_imported_only_true",
        "users",
        ["is_imported_only"],
        postgresql_where=sa.text("is_imported_only = true"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_users_is_imported_only_true",
        table_name="users",
    )
    op.drop_column("users", "is_imported_only")
