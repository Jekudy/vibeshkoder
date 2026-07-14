"""Allow weekly digests to publish without manual approval.

Revision ID: 079
Revises: 078
Create Date: 2026-07-14
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "079"
down_revision: Union[str, Sequence[str], None] = "078"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE digests DROP CONSTRAINT ck_digests_approved_audit")
    op.execute(
        """
        ALTER TABLE digests ADD CONSTRAINT ck_digests_approved_audit CHECK (
            status <> 'approved_for_publish'
            OR type <> 'weekly'
            OR (
                published_by_admin_id IS NOT NULL
                AND approved_at IS NOT NULL
            )
        ) NOT VALID
        """
    )
    op.execute("ALTER TABLE digests VALIDATE CONSTRAINT ck_digests_approved_audit")


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM digests
                WHERE type = 'weekly'
                  AND status IN ('posting', 'posted')
                  AND (published_by_admin_id IS NULL OR approved_at IS NULL)
            ) THEN
                RAISE EXCEPTION
                    'Cannot downgrade 079: automatically published weekly digests lack admin attribution';
            END IF;
        END
        $$
        """
    )
    op.execute("ALTER TABLE digests DROP CONSTRAINT ck_digests_approved_audit")
    op.execute(
        """
        ALTER TABLE digests ADD CONSTRAINT ck_digests_approved_audit CHECK (
            status NOT IN ('approved_for_publish','posting','posted')
            OR type <> 'weekly'
            OR (
                published_by_admin_id IS NOT NULL
                AND approved_at IS NOT NULL
            )
        ) NOT VALID
        """
    )
    op.execute("ALTER TABLE digests VALIDATE CONSTRAINT ck_digests_approved_audit")
