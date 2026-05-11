"""extend_qa_traces_for_llm

T5-04: Phase 5 Wave 2 — qa_traces LLM extension columns + relax
``llm_usage_ledger.prompt_hash`` to NULLABLE so the
``_cascade_llm_usage_ledger`` layer can NULL the hash on user forget
(contracts.md §12.1 — orchestrator ratification 2026-05-02).

Adds to ``qa_traces``:

* ``llm_call_id``    — BIGINT NULL FK → ``llm_usage_ledger.id`` ON DELETE SET NULL
* ``llm_response_summary`` — TEXT NULL (raw answer; protected by cascade)
* ``llm_response_redacted`` — BOOLEAN NULL DEFAULT FALSE
* ``cost_usd``       — NUMERIC(10, 6) NULL

And index ``ix_qa_traces_llm_call_id`` on ``qa_traces(llm_call_id)``.

Drops NOT NULL on ``llm_usage_ledger.prompt_hash`` (response_hash is
already nullable per 024). Required by `_cascade_llm_usage_ledger`
(§8.3) which NULLs both hashes on `target_type='user'` while preserving
cost / token aggregates for budget audit.

Revision ID: 025
Revises: 024
Create Date: 2026-05-11
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "025"
down_revision: Union[str, Sequence[str], None] = "024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── qa_traces LLM extension columns ───────────────────────────────────
    op.add_column(
        "qa_traces",
        sa.Column("llm_call_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "qa_traces",
        sa.Column("llm_response_summary", sa.Text(), nullable=True),
    )
    op.add_column(
        "qa_traces",
        sa.Column(
            "llm_response_redacted",
            sa.Boolean(),
            nullable=True,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "qa_traces",
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=True),
    )
    op.create_foreign_key(
        "fk_qa_traces_llm_call_id",
        source_table="qa_traces",
        referent_table="llm_usage_ledger",
        local_cols=["llm_call_id"],
        remote_cols=["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_qa_traces_llm_call_id",
        "qa_traces",
        ["llm_call_id"],
    )

    # ── llm_usage_ledger.prompt_hash → NULLABLE ───────────────────────────
    # Required by `_cascade_llm_usage_ledger` (§8.3) which NULLs the column on
    # `target_type='user'` while preserving cost/token aggregates for budget
    # audit. `response_hash` was already nullable per migration 024 — no need
    # to ALTER it again.
    op.alter_column(
        "llm_usage_ledger",
        "prompt_hash",
        existing_type=sa.CHAR(64),
        nullable=True,
    )


def downgrade() -> None:
    # ── llm_usage_ledger.prompt_hash → NOT NULL ───────────────────────────
    # The forward path may have set rows to NULL via the cascade. On reverse,
    # backfill those rows with a sentinel sha256(b"<redacted>") so the NOT
    # NULL constraint accepts them. This keeps the rollback non-destructive
    # for audit aggregates while restoring the prior schema shape.
    import hashlib

    sentinel = hashlib.sha256(b"<redacted>").hexdigest()
    op.execute(
        sa.text(
            "UPDATE llm_usage_ledger SET prompt_hash = :s WHERE prompt_hash IS NULL"
        ).bindparams(s=sentinel)
    )
    op.alter_column(
        "llm_usage_ledger",
        "prompt_hash",
        existing_type=sa.CHAR(64),
        nullable=False,
    )

    # ── qa_traces LLM extension columns ───────────────────────────────────
    op.drop_index("ix_qa_traces_llm_call_id", table_name="qa_traces")
    op.drop_constraint(
        "fk_qa_traces_llm_call_id", "qa_traces", type_="foreignkey"
    )
    op.drop_column("qa_traces", "cost_usd")
    op.drop_column("qa_traces", "llm_response_redacted")
    op.drop_column("qa_traces", "llm_response_summary")
    op.drop_column("qa_traces", "llm_call_id")
