"""add_butler_audit_triple

Migration 070: butler_actions, butler_tool_invocations, butler_action_confirmations.

Phase 12 (Butler / Action layer) schema foundation — T12-01 Sub-sprint A.
Adds the three core Butler audit tables with all CHECK constraints, FK ON DELETE
actions, and required indexes per PHASE12_PLAN_REFRESH.md §4.5.

Revision ID: 070
Revises: 068
Create Date: 2026-05-25
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "070"
down_revision: Union[str, Sequence[str], None] = "068"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── butler_actions ─────────────────────────────────────────────────────────
    op.execute("""
CREATE TABLE butler_actions (
  id BIGSERIAL PRIMARY KEY,
  action_uuid UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
  -- ON DELETE RESTRICT preserves immutable audit chain: undo writes a NEW row, never
  -- mutates the parent. RESTRICT blocks parent deletion, protecting audit history.
  parent_action_id BIGINT REFERENCES butler_actions(id) ON DELETE RESTRICT,
  -- Scalar tg_id without FK to users(id): affected_user may not have a registered
  -- users row at action-plan time (cross-user intro to non-registered target).
  -- FK enforcement deferred to runtime check in butler service layer.
  requester_tg_id BIGINT NOT NULL,
  chat_id BIGINT NOT NULL,
  action_type TEXT NOT NULL,
  status TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  tool_manifest_version TEXT NOT NULL,
  governance_filter_version TEXT NOT NULL,
  evidence_context_hash TEXT NOT NULL,
  evidence_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  approved_card_source_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  plan_summary TEXT NOT NULL,
  action_args JSONB NOT NULL,
  action_args_hash TEXT NOT NULL,
  result_payload JSONB,
  result_payload_hash TEXT,
  inverse_op_payload JSONB,
  rollback_kind TEXT NOT NULL,
  risk_level TEXT NOT NULL,
  requires_confirmation BOOLEAN NOT NULL DEFAULT TRUE,
  confirmation_policy TEXT NOT NULL DEFAULT 'per_action',
  expires_at TIMESTAMPTZ,
  confirmed_at TIMESTAMPTZ,
  executed_at TIMESTAMPTZ,
  undone_at TIMESTAMPTZ,
  rejection_reason TEXT,
  error_code TEXT,
  error_context JSONB,
  llm_usage_ledger_id BIGINT REFERENCES llm_usage_ledger(id) ON DELETE RESTRICT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT ck_butler_actions_status CHECK (status IN (
    'requested','evidence_loaded','planned','pending_confirmation',
    'confirmed','executing','succeeded',
    'undo_pending','undo_succeeded','undo_failed',
    'rejected','expired','execution_failed','cancelled'
  )),
  CONSTRAINT ck_butler_actions_tool_name CHECK (tool_name IN (
    'recall_evidence','schedule_meeting','send_intro',
    'update_intro','suggest_card_creation'
  )),
  CONSTRAINT ck_butler_actions_rollback_kind CHECK (rollback_kind IN (
    'delete_message','edit_message','followup_correction',
    'cancel_pending','not_reversible'
  )),
  CONSTRAINT ck_butler_actions_risk_level CHECK (risk_level IN ('low','medium','high')),
  CONSTRAINT ck_butler_actions_confirmation_policy CHECK (confirmation_policy IN (
    'per_action','opt_in_by_button'
  )),
  CONSTRAINT ck_butler_actions_action_type CHECK (action_type IN (
    'meeting','intro','intro_update','card_suggestion','recall'
  )),
  CONSTRAINT ck_butler_actions_executed_has_inverse CHECK (
    (status NOT IN ('succeeded','undo_pending','undo_succeeded'))
    OR (inverse_op_payload IS NOT NULL OR rollback_kind = 'not_reversible')
  ),
  CONSTRAINT ck_butler_actions_ledger_required_post_plan CHECK (
    status IN ('rejected','expired','cancelled')
    OR llm_usage_ledger_id IS NOT NULL
  )
)
""")

    op.execute("""
CREATE INDEX ix_butler_actions_requester_created
  ON butler_actions(requester_tg_id, created_at DESC)
""")
    op.execute("""
CREATE INDEX ix_butler_actions_chat_created
  ON butler_actions(chat_id, created_at DESC)
""")
    op.execute("""
CREATE INDEX ix_butler_actions_status_expires
  ON butler_actions(status, expires_at)
  WHERE status IN ('pending_confirmation','planned')
""")
    op.execute("""
CREATE INDEX ix_butler_actions_parent
  ON butler_actions(parent_action_id)
  WHERE parent_action_id IS NOT NULL
""")
    op.execute("""
CREATE INDEX ix_butler_actions_llm_ledger
  ON butler_actions(llm_usage_ledger_id)
  WHERE llm_usage_ledger_id IS NOT NULL
""")

    # ── butler_tool_invocations ────────────────────────────────────────────────
    op.execute("""
CREATE TABLE butler_tool_invocations (
  id BIGSERIAL PRIMARY KEY,
  action_id BIGINT NOT NULL REFERENCES butler_actions(id) ON DELETE RESTRICT,
  tool_name TEXT NOT NULL,
  invocation_seq INT NOT NULL DEFAULT 1,
  idempotency_key TEXT NOT NULL UNIQUE,
  request_payload JSONB NOT NULL,
  request_payload_hash TEXT NOT NULL,
  response_payload JSONB,
  response_payload_hash TEXT,
  status TEXT NOT NULL,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  error_code TEXT,
  error_context JSONB,

  CONSTRAINT ck_butler_tool_invocations_tool_name CHECK (tool_name IN (
    'recall_evidence','schedule_meeting','send_intro',
    'update_intro','suggest_card_creation'
  )),
  CONSTRAINT ck_butler_tool_invocations_status CHECK (status IN (
    'pending','running','succeeded','failed','rolled_back'
  )),
  CONSTRAINT ck_butler_tool_invocations_seq_positive CHECK (invocation_seq >= 1)
)
""")

    op.execute("""
CREATE INDEX ix_butler_tool_invocations_action
  ON butler_tool_invocations(action_id)
""")
    op.execute("""
CREATE INDEX ix_butler_tool_invocations_status
  ON butler_tool_invocations(status)
""")

    # ── butler_action_confirmations ────────────────────────────────────────────
    op.execute("""
CREATE TABLE butler_action_confirmations (
  id BIGSERIAL PRIMARY KEY,
  action_id BIGINT NOT NULL REFERENCES butler_actions(id) ON DELETE RESTRICT,
  confirmer_tg_id BIGINT NOT NULL,
  confirmation_role TEXT NOT NULL,
  status TEXT NOT NULL,
  confirmation_message_chat_id BIGINT,
  confirmation_message_id BIGINT,
  preview_payload_hash TEXT NOT NULL,
  confirmed_at TIMESTAMPTZ,
  rejected_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT ck_butler_action_confirmations_role CHECK (confirmation_role IN (
    'requester','affected_user','admin','rollback_requester'
  )),
  CONSTRAINT ck_butler_action_confirmations_status CHECK (status IN (
    'pending','confirmed','rejected','expired','cancelled'
  ))
)
""")

    op.execute("""
CREATE INDEX ix_butler_action_confirmations_action
  ON butler_action_confirmations(action_id)
""")
    op.execute("""
CREATE INDEX ix_butler_action_confirmations_status_expires
  ON butler_action_confirmations(status, expires_at)
  WHERE status = 'pending'
""")


def downgrade() -> None:
    # Drop in reverse dependency order.
    op.execute("DROP TABLE IF EXISTS butler_action_confirmations")
    op.execute("DROP TABLE IF EXISTS butler_tool_invocations")
    op.execute("DROP TABLE IF EXISTS butler_actions")
