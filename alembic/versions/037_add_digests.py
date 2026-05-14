"""add_digests

T7-01 / Phase 7: daily digest tables — ``digests`` and ``digest_runs``.

``digests`` is the primary record for one generated digest window. Each row
is keyed by ``(type, window_start, window_end)`` for idempotent re-runs.
``body_markdown`` is NULL while ``status='running'`` and NOT NULL in
all user-visible states (draft, posting, posted, redacted, redacted_edit_failed).
``citations`` is a JSONB array of ``{kind, id, position}`` objects — never raw
message text. ``llm_usage_ledger_id`` ties the cost entry to the row; FK is
ON DELETE SET NULL so ledger pruning does not cascade-delete digests.

``digest_runs`` is an append-only audit log of invocations, one row per
``run_digest()`` call. ``digest_id`` is FK ON DELETE SET NULL so it survives
if the parent digest is ever manually removed in a future admin path.

Status enums:
- digests.status: running | draft | posting | posted | failed | skipped |
                  cost_exceeded | skipped_no_destination | redacted |
                  redacted_edit_failed
- digest_runs.status: running | finished | failed | skipped | cost_exceeded |
                      skipped_no_destination

Constraints:
- UNIQUE (type, window_start, window_end) — idempotency key.
- body_markdown NOT NULL enforced for status IN
  ('draft','posting','posted','redacted','redacted_edit_failed').
- posted_chat_id, posted_message_id, posted_at NOT NULL enforced for
  status='posted'.

Indexes:
- ix_digests_status_draft: partial WHERE status='draft' — publisher scan.
- ix_digests_citations_gin: GIN jsonb_path_ops — forget cascade containment.
- ix_digests_posting_started_at: partial WHERE status='posting' — stale reaper.

Rollback drops only digest_runs then digests; no other tables touched.

Revision ID: 037
Revises: 036
Create Date: 2026-05-13
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "037"
down_revision: Union[str, Sequence[str], None] = "036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "digests",
        sa.Column("id", sa.BigInteger(), nullable=False, autoincrement=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("body_markdown", sa.Text(), nullable=True),
        sa.Column(
            "citations",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("llm_usage_ledger_id", sa.BigInteger(), nullable=True),
        sa.Column("posted_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("posted_message_id", sa.BigInteger(), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("posting_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "type",
            "window_start",
            "window_end",
            name="uq_digests_type_window",
        ),
        sa.CheckConstraint(
            "type IN ('daily','weekly')",
            name="ck_digests_type",
        ),
        sa.CheckConstraint(
            "status IN ('running','draft','posting','posted','failed','skipped',"
            "'cost_exceeded','skipped_no_destination','redacted','redacted_edit_failed')",
            name="ck_digests_status",
        ),
        # body_markdown must NOT be NULL in all user-visible states.
        sa.CheckConstraint(
            "status NOT IN ('draft','posting','posted','redacted','redacted_edit_failed')"
            " OR body_markdown IS NOT NULL",
            name="ck_digests_body_markdown_not_null_for_visible_statuses",
        ),
        # posted fields required when status='posted'.
        sa.CheckConstraint(
            "status <> 'posted'"
            " OR (posted_chat_id IS NOT NULL"
            " AND posted_message_id IS NOT NULL"
            " AND posted_at IS NOT NULL)",
            name="ck_digests_posted_fields_required",
        ),
        sa.ForeignKeyConstraint(
            ["llm_usage_ledger_id"],
            ["llm_usage_ledger.id"],
            name="fk_digests_llm_usage_ledger_id",
            ondelete="SET NULL",
        ),
    )

    # Publisher scan: find draft digests ready to publish.
    op.create_index(
        "ix_digests_status_draft",
        "digests",
        ["status"],
        postgresql_where=sa.text("status = 'draft'"),
    )

    # Forget cascade: GIN index for jsonb_path_ops containment checks on citations.
    op.create_index(
        "ix_digests_citations_gin",
        "digests",
        ["citations"],
        postgresql_using="gin",
        postgresql_ops={"citations": "jsonb_path_ops"},
    )

    # Stale-posting reaper: partial index for orphan posting rows.
    op.create_index(
        "ix_digests_posting_started_at",
        "digests",
        ["posting_started_at"],
        postgresql_where=sa.text("status = 'posting'"),
    )

    op.create_table(
        "digest_runs",
        sa.Column("id", sa.BigInteger(), nullable=False, autoincrement=True),
        sa.Column("digest_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('running','finished','failed','skipped',"
            "'cost_exceeded','skipped_no_destination')",
            name="ck_digest_runs_status",
        ),
        sa.ForeignKeyConstraint(
            ["digest_id"],
            ["digests.id"],
            name="fk_digest_runs_digest_id",
            ondelete="SET NULL",
        ),
    )


def downgrade() -> None:
    op.drop_table("digest_runs")
    op.drop_index("ix_digests_posting_started_at", table_name="digests")
    op.drop_index("ix_digests_citations_gin", table_name="digests")
    op.drop_index("ix_digests_status_draft", table_name="digests")
    op.drop_table("digests")
