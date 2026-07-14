"""Add durable audit rows for static wiki deployments.

Revision ID: 084
Revises: 083
Create Date: 2026-07-14
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "084"
down_revision: Union[str, Sequence[str], None] = "083"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "wiki_static_deployments",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("project", sa.String(length=255), nullable=False),
        sa.Column("branch", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("deployment_url", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_class", sa.String(length=255), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "status IN ('pending','succeeded','failed')",
            name="ck_wiki_static_deployments_status",
        ),
        sa.CheckConstraint(
            "manifest_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_wiki_static_deployments_manifest_sha256",
        ),
        sa.CheckConstraint(
            """
            (status = 'pending'
                AND finished_at IS NULL
                AND deployment_url IS NULL
                AND error_code IS NULL
                AND error_class IS NULL)
            OR (status = 'succeeded'
                AND finished_at IS NOT NULL
                AND deployment_url IS NOT NULL
                AND error_code IS NULL
                AND error_class IS NULL)
            OR (status = 'failed'
                AND finished_at IS NOT NULL
                AND deployment_url IS NULL
                AND error_code IS NOT NULL)
            """,
            name="ck_wiki_static_deployments_terminal_state",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_wiki_static_deployments_status_started",
        "wiki_static_deployments",
        ["status", "started_at"],
        unique=False,
    )
    op.create_index(
        "ix_wiki_static_deployments_success_lookup",
        "wiki_static_deployments",
        ["manifest_sha256", "project", "branch"],
        unique=False,
        postgresql_where=sa.text("status = 'succeeded'"),
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM wiki_static_deployments) THEN
                RAISE EXCEPTION
                    'Cannot downgrade 084: wiki_static_deployments contains audit rows';
            END IF;
        END
        $$
        """
    )
    op.drop_index(
        "ix_wiki_static_deployments_success_lookup",
        table_name="wiki_static_deployments",
    )
    op.drop_index(
        "ix_wiki_static_deployments_status_started",
        table_name="wiki_static_deployments",
    )
    op.drop_table("wiki_static_deployments")
