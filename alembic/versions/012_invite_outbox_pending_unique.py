"""invite_outbox_pending_unique

CRIT-02-r3: a stale duplicate pending outbox row must not be able to terminally
retry and demote an already-vouched or already-added application. Keep only the
oldest pending row per application, then enforce one pending invite intent per
application at the database boundary.

Revision ID: 012
Revises: 011
Create Date: 2026-04-27
"""

from __future__ import annotations

import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "012"
down_revision: Union[str, Sequence[str], None] = "011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

DUPLICATE_PENDING_COUNT_SQL = """
SELECT COALESCE(SUM(duplicate_count - 1), 0) AS duplicate_pending_rows_to_delete
FROM (
    SELECT application_id, COUNT(*) AS duplicate_count
    FROM invite_outbox
    WHERE status = 'pending'
    GROUP BY application_id
    HAVING COUNT(*) > 1
) duplicates
"""


def _is_postgresql() -> bool:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        return True
    logger.warning(
        "Skipping invite_outbox pending unique index migration for %s dialect",
        bind.dialect.name,
    )
    return False


def upgrade() -> None:
    if not _is_postgresql():
        return

    bind = op.get_bind()
    duplicate_count = bind.execute(
        sa.text(DUPLICATE_PENDING_COUNT_SQL)
    ).scalar_one()
    log_fn = logger.warning if duplicate_count > 0 else logger.info
    log_fn(
        "invite_outbox pending duplicate rows to delete before unique index: %s",
        duplicate_count,
    )

    op.execute(
        """
        DELETE FROM invite_outbox AS target
        USING (
            SELECT id
            FROM (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        PARTITION BY application_id
                        ORDER BY created_at ASC, id ASC
                    ) AS row_num
                FROM invite_outbox
                WHERE status = 'pending'
            ) ranked
            WHERE row_num > 1
        ) duplicates
        WHERE target.id = duplicates.id
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ix_invite_outbox_pending_unique
        ON invite_outbox (application_id)
        WHERE status = 'pending'
        """
    )


def downgrade() -> None:
    if not _is_postgresql():
        return

    op.drop_index(
        "ix_invite_outbox_pending_unique",
        table_name="invite_outbox",
        if_exists=True,
    )
