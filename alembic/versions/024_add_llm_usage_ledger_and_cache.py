"""add_llm_usage_ledger_and_cache

T5-02: Phase 5 Wave 1 schema — llm_usage_ledger (per-call audit) and
llm_synthesis_cache (DB-backed answer cache with GDPR cascade support).

Revision ID: 024
Revises: 023
Create Date: 2026-05-02
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "024"
down_revision: Union[str, Sequence[str], None] = "023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── llm_usage_ledger ────────────────────────────────────────────────────
    op.create_table(
        "llm_usage_ledger",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("qa_trace_id", sa.BigInteger(), nullable=True),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("prompt_hash", sa.CHAR(64), nullable=False),
        sa.Column("response_hash", sa.CHAR(64), nullable=True),
        sa.Column(
            "tokens_in",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "tokens_out",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "cost_usd",
            sa.Numeric(10, 6),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "latency_ms",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("request_id", sa.String(128), nullable=True),
        sa.Column(
            "cache_hit",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("error", sa.String(255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["qa_trace_id"],
            ["qa_traces.id"],
            name="fk_llm_usage_ledger_qa_trace_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_llm_usage_ledger_qa_trace_id",
        "llm_usage_ledger",
        ["qa_trace_id"],
    )
    op.create_index(
        "ix_llm_usage_ledger_model_created_at",
        "llm_usage_ledger",
        ["model", "created_at"],
    )
    op.create_index(
        "ix_llm_usage_ledger_created_at",
        "llm_usage_ledger",
        ["created_at"],
    )

    # ── llm_synthesis_cache ─────────────────────────────────────────────────
    op.create_table(
        "llm_synthesis_cache",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("input_hash", sa.CHAR(64), nullable=False),
        sa.Column("answer_text", sa.Text(), nullable=False),
        sa.Column("citation_ids", JSONB(), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_hit_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "hit_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("input_hash", name="uq_llm_synthesis_cache_input_hash"),
    )


def downgrade() -> None:
    # Drop cache first (no FK dependencies), then ledger.
    op.drop_table("llm_synthesis_cache")

    op.drop_index("ix_llm_usage_ledger_created_at", table_name="llm_usage_ledger")
    op.drop_index("ix_llm_usage_ledger_model_created_at", table_name="llm_usage_ledger")
    op.drop_index("ix_llm_usage_ledger_qa_trace_id", table_name="llm_usage_ledger")
    op.drop_table("llm_usage_ledger")
