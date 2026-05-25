"""add_butler_rate_buckets

Migration 072: butler_rate_buckets table for atomic per-user/per-chat rate limiting.

Stores calendar-window (not rolling) rate buckets keyed by (bucket_kind, scope_id,
bucket_key). The UNIQUE constraint on (bucket_kind, scope_id, bucket_key) enables the
atomic ON CONFLICT upsert pattern described in PHASE12_PLAN_REFRESH.md §5.2:

    INSERT ... ON CONFLICT (bucket_kind, scope_id, bucket_key)
    DO UPDATE SET count = count + 1, updated_at = NOW()
    WHERE count < ceiling
    RETURNING id, count, ceiling

Empty RETURNING → ceiling already reached (action rejected).
Non-empty RETURNING → count durably incremented.

Revision ID: 072
Revises: 071
Create Date: 2026-05-25
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "072"
down_revision: Union[str, Sequence[str], None] = "071"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE butler_rate_buckets (
  id BIGSERIAL PRIMARY KEY,
  bucket_kind TEXT NOT NULL,
  scope_id BIGINT NOT NULL,
  bucket_key TEXT NOT NULL,
  window_start TIMESTAMPTZ NOT NULL,
  window_end TIMESTAMPTZ NOT NULL,
  count INT NOT NULL DEFAULT 0,
  ceiling INT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT ck_butler_rate_buckets_kind CHECK (bucket_kind IN (
    'user_plans_day','user_execs_day','chat_actions_day',
    'tool_hour:recall_evidence','tool_hour:schedule_meeting',
    'tool_hour:send_intro','tool_hour:update_intro','tool_hour:suggest_card_creation'
  )),
  CONSTRAINT ck_butler_rate_buckets_window_positive CHECK (window_end > window_start),
  CONSTRAINT ck_butler_rate_buckets_count_nonneg_under_ceiling
    CHECK (count >= 0 AND count <= ceiling),
  CONSTRAINT ck_butler_rate_buckets_ceiling_positive CHECK (ceiling > 0),

  CONSTRAINT uq_butler_rate_buckets_kind_scope_key UNIQUE (bucket_kind, scope_id, bucket_key)
)
""")

    op.execute("""
CREATE INDEX ix_butler_rate_buckets_window_end
  ON butler_rate_buckets(window_end)
""")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS butler_rate_buckets")
