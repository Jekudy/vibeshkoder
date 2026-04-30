"""add_qa_traces

T4-05: add the Phase 4 q&a audit table. Each row records who asked, where the
question was asked, whether the query text was redacted, which evidence ids were
returned, and whether the q&a layer abstained.

Revision ID: 022_add_qa_traces
Revises: 021
Create Date: 2026-04-30
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "022_add_qa_traces"
down_revision: Union[str, Sequence[str], None] = "021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "qa_traces",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_tg_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "query_redacted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("query_text", sa.Text(), nullable=True),
        sa.Column(
            "evidence_ids",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "abstained",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_qa_traces_user_tg_id", "qa_traces", ["user_tg_id"])
    op.create_index(
        "ix_qa_traces_chat_id_created_at",
        "qa_traces",
        ["chat_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_qa_traces_chat_id_created_at", table_name="qa_traces")
    op.drop_index("ix_qa_traces_user_tg_id", table_name="qa_traces")
    op.drop_table("qa_traces")
