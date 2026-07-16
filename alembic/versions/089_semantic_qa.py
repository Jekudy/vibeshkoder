"""Add pgvector-backed semantic Q&A storage and durable daily quota.

Revision ID: 089
Revises: 088
Create Date: 2026-07-16
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "089"
down_revision: Union[str, Sequence[str], None] = "088"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_CALL_TYPES = (
    "'unknown','qa_synthesis','digest_daily','digest_weekly',"
    "'graph_projection','extract_candidates','butler_decision','butler_summary',"
    "'wiki_compilation','image_description'"
)
_NEW_CALL_TYPES = _OLD_CALL_TYPES + ",'semantic_embedding'"


def _add_call_type_constraint(values: str) -> None:
    op.execute(
        "ALTER TABLE llm_usage_ledger "
        "ADD CONSTRAINT ck_llm_usage_ledger_call_type "
        f"CHECK (call_type IN ({values})) NOT VALID"
    )
    op.execute("ALTER TABLE llm_usage_ledger VALIDATE CONSTRAINT ck_llm_usage_ledger_call_type")


def upgrade() -> None:
    # Extension files must already exist in the same-major PostgreSQL image. Repo/CI
    # stay on PG16; the production preflight switches its existing PG15 service to
    # the pinned PG15+pgvector image before this migration runs.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.add_column("users", sa.Column("is_bot", sa.Boolean(), nullable=True))
    op.execute(
        """
        WITH author_evidence (user_id, is_bot) AS (
            SELECT cm.user_id,
                   CASE cm.raw_json #>> '{from_user,is_bot}'
                       WHEN 'true' THEN TRUE
                       WHEN 'false' THEN FALSE
                   END
            FROM chat_messages AS cm
            WHERE cm.raw_json #>> '{from_user,is_bot}' IN ('true', 'false')

            UNION ALL

            SELECT u.id,
                   CASE tu.raw_json #>> '{message,from_user,is_bot}'
                       WHEN 'true' THEN TRUE
                       WHEN 'false' THEN FALSE
                   END
            FROM telegram_updates AS tu
            JOIN users AS u
              ON u.id::text = tu.raw_json #>> '{message,from_user,id}'
            WHERE tu.raw_json #>> '{message,from_user,is_bot}' IN ('true', 'false')

            UNION ALL

            SELECT u.id,
                   CASE tu.raw_json #>> '{edited_message,from_user,is_bot}'
                       WHEN 'true' THEN TRUE
                       WHEN 'false' THEN FALSE
                   END
            FROM telegram_updates AS tu
            JOIN users AS u
              ON u.id::text = tu.raw_json #>> '{edited_message,from_user,id}'
            WHERE tu.raw_json #>> '{edited_message,from_user,is_bot}' IN ('true', 'false')
        ),
        resolved_authors AS (
            SELECT user_id, bool_or(is_bot) AS is_bot
            FROM author_evidence
            GROUP BY user_id
        )
        UPDATE users AS u
        SET is_bot = source.is_bot
        FROM resolved_authors AS source
        WHERE u.id = source.user_id
        """
    )

    op.execute("ALTER TABLE llm_usage_ledger DROP CONSTRAINT ck_llm_usage_ledger_call_type")
    _add_call_type_constraint(_NEW_CALL_TYPES)
    op.execute(
        "ALTER TABLE llm_usage_ledger "
        "ADD CONSTRAINT ck_llm_usage_ledger_nonnegative_usage "
        "CHECK (tokens_in >= 0 AND tokens_out >= 0 AND cost_usd >= 0 AND latency_ms >= 0) "
        "NOT VALID"
    )
    op.execute(
        "ALTER TABLE llm_usage_ledger VALIDATE CONSTRAINT ck_llm_usage_ledger_nonnegative_usage"
    )

    op.create_table(
        "semantic_index_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_type", sa.String(length=32), nullable=False),
        sa.Column("embedding_provider", sa.String(length=64), nullable=False),
        sa.Column("embedding_model", sa.String(length=128), nullable=False),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("eligible_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("indexed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "reason_counts",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("cursor", sa.String(length=255), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "run_type IN ('backfill','reindex')", name="ck_semantic_index_runs_type"
        ),
        sa.CheckConstraint(
            "status IN ('running','completed','failed')",
            name="ck_semantic_index_runs_status",
        ),
        sa.CheckConstraint("embedding_dimensions > 0", name="ck_semantic_index_runs_dimensions"),
        sa.PrimaryKeyConstraint("id", name="pk_semantic_index_runs"),
    )

    op.create_table(
        "semantic_retrieval_units",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("source_revision", sa.String(length=128), nullable=False),
        sa.Column("chunk_index", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("chunk_count", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("content_hash", sa.String(length=128), nullable=False),
        sa.Column("embedding_provider", sa.String(length=64), nullable=False),
        sa.Column("embedding_model", sa.String(length=128), nullable=False),
        sa.Column("embedding_model_version", sa.String(length=64), nullable=False),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=False),
        sa.Column("embedding", Vector(1536), nullable=False),
        sa.Column("llm_usage_ledger_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "indexed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidation_reason", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "source_type IN ('message','card')", name="ck_semantic_units_source_type"
        ),
        sa.CheckConstraint("embedding_dimensions = 1536", name="ck_semantic_units_dimensions"),
        sa.CheckConstraint(
            "chunk_index >= 0 AND chunk_count > 0 AND chunk_index < chunk_count",
            name="ck_semantic_units_chunk_bounds",
        ),
        sa.CheckConstraint(
            "(invalidated_at IS NULL) = (invalidation_reason IS NULL)",
            name="ck_semantic_units_invalidation_pair",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_semantic_retrieval_units"),
        sa.ForeignKeyConstraint(
            ["llm_usage_ledger_id"],
            ["llm_usage_ledger.id"],
            name="fk_semantic_units_llm_usage_ledger_id",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "source_type",
            "source_id",
            "source_revision",
            "chunk_index",
            "content_hash",
            "embedding_model",
            name="uq_semantic_units_identity",
        ),
    )
    op.create_index(
        "ix_semantic_units_chat_active",
        "semantic_retrieval_units",
        ["chat_id", "invalidated_at"],
    )
    op.create_index(
        "ix_semantic_units_source",
        "semantic_retrieval_units",
        ["source_type", "source_id"],
    )

    op.create_table(
        "semantic_retrieval_unit_sources",
        sa.Column("unit_id", sa.BigInteger(), nullable=False),
        sa.Column("message_version_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["unit_id"],
            ["semantic_retrieval_units.id"],
            name="fk_semantic_unit_sources_unit_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["message_version_id"],
            ["message_versions.id"],
            name="fk_semantic_unit_sources_message_version_id",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("position >= 0", name="ck_semantic_unit_sources_position"),
        sa.PrimaryKeyConstraint("unit_id", "message_version_id", name="pk_semantic_unit_sources"),
        sa.UniqueConstraint("unit_id", "position", name="uq_semantic_unit_sources_position"),
    )
    op.create_index(
        "ix_semantic_unit_sources_message_version_id",
        "semantic_retrieval_unit_sources",
        ["message_version_id"],
    )

    op.create_table(
        "semantic_qa_attempts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("user_tg_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("source_chat_message_id", sa.Integer(), nullable=True),
        sa.Column("local_day", sa.Date(), nullable=False),
        sa.Column("slot_number", sa.SmallInteger(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=True),
        sa.Column("qa_trace_id", sa.BigInteger(), nullable=True),
        sa.Column("embedding_llm_call_id", sa.BigInteger(), nullable=True),
        sa.Column("synthesis_llm_call_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "reserved_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("delivery_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["source_chat_message_id"],
            ["chat_messages.id"],
            name="fk_semantic_qa_attempts_source_chat_message_id",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["qa_trace_id"],
            ["qa_traces.id"],
            name="fk_semantic_qa_attempts_qa_trace_id",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["embedding_llm_call_id"],
            ["llm_usage_ledger.id"],
            name="fk_semantic_qa_attempts_embedding_call_id",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["synthesis_llm_call_id"],
            ["llm_usage_ledger.id"],
            name="fk_semantic_qa_attempts_synthesis_call_id",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "slot_number IS NULL OR slot_number IN (1,2)", name="ck_semantic_attempts_slot"
        ),
        sa.CheckConstraint(
            "status IN ('denied','reserved','consumed','released')",
            name="ck_semantic_attempts_status",
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('answered','abstained','technical_failure','quota_denied')",
            name="ck_semantic_attempts_outcome",
        ),
        sa.CheckConstraint(
            "(status = 'denied' AND slot_number IS NULL AND outcome = 'quota_denied' "
            "AND delivery_started_at IS NULL) OR "
            "(status = 'reserved' AND slot_number IS NOT NULL AND finalized_at IS NULL AND "
            "((outcome IS NULL AND delivery_started_at IS NULL) OR "
            "(outcome IN ('answered','abstained') AND delivery_started_at IS NOT NULL))) OR "
            "(status = 'consumed' AND slot_number IS NOT NULL "
            "AND outcome IN ('answered','abstained') AND delivery_started_at IS NOT NULL "
            "AND finalized_at IS NOT NULL) OR "
            "(status = 'released' AND slot_number IS NOT NULL "
            "AND outcome = 'technical_failure' AND finalized_at IS NOT NULL)",
            name="ck_semantic_attempts_state",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_semantic_qa_attempts"),
        sa.UniqueConstraint("idempotency_key", name="uq_semantic_qa_attempts_idempotency_key"),
    )
    op.create_index(
        "uq_semantic_qa_attempts_active_slot",
        "semantic_qa_attempts",
        ["user_tg_id", "local_day", "slot_number"],
        unique=True,
        postgresql_where=sa.text("status IN ('reserved','consumed')"),
    )
    op.create_index(
        "ix_semantic_qa_attempts_user_day",
        "semantic_qa_attempts",
        ["user_tg_id", "local_day", "status"],
    )

    op.create_table(
        "semantic_retrieval_traces",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("attempt_id", sa.BigInteger(), nullable=False),
        sa.Column("qa_trace_id", sa.BigInteger(), nullable=True),
        sa.Column("query_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("embedding_model", sa.String(length=128), nullable=False),
        sa.Column("retrieval_mode", sa.String(length=32), nullable=False),
        sa.Column("candidate_ranks", JSONB(), nullable=False),
        sa.Column("result_source_ids", JSONB(), nullable=False),
        sa.Column("fts_latency_ms", sa.Integer(), nullable=False),
        sa.Column("vector_latency_ms", sa.Integer(), nullable=False),
        sa.Column("fusion_latency_ms", sa.Integer(), nullable=False),
        sa.Column("total_latency_ms", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["semantic_qa_attempts.id"],
            name="fk_semantic_retrieval_traces_attempt_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["qa_trace_id"],
            ["qa_traces.id"],
            name="fk_semantic_retrieval_traces_qa_trace_id",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "retrieval_mode IN ('hybrid','fts_fallback','shadow')",
            name="ck_semantic_retrieval_traces_mode",
        ),
        sa.CheckConstraint(
            "fts_latency_ms >= 0 AND vector_latency_ms >= 0 AND fusion_latency_ms >= 0 "
            "AND total_latency_ms >= 0",
            name="ck_semantic_retrieval_traces_latency",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_semantic_retrieval_traces"),
        sa.UniqueConstraint("attempt_id", name="uq_semantic_retrieval_traces_attempt_id"),
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM semantic_index_runs)
               OR EXISTS (SELECT 1 FROM semantic_retrieval_units)
               OR EXISTS (SELECT 1 FROM semantic_retrieval_unit_sources)
               OR EXISTS (SELECT 1 FROM semantic_qa_attempts)
               OR EXISTS (SELECT 1 FROM semantic_retrieval_traces)
               OR EXISTS (
                    SELECT 1 FROM llm_usage_ledger
                    WHERE call_type = 'semantic_embedding'
               ) THEN
                RAISE EXCEPTION
                    'Cannot downgrade 089 with semantic Q&A audit data; disable the flag and restore the pre-migration backup';
            END IF;
        END
        $$
        """
    )
    op.drop_table("semantic_retrieval_traces")
    op.drop_index("ix_semantic_qa_attempts_user_day", table_name="semantic_qa_attempts")
    op.drop_index("uq_semantic_qa_attempts_active_slot", table_name="semantic_qa_attempts")
    op.drop_table("semantic_qa_attempts")
    op.drop_index(
        "ix_semantic_unit_sources_message_version_id",
        table_name="semantic_retrieval_unit_sources",
    )
    op.drop_table("semantic_retrieval_unit_sources")
    op.drop_index("ix_semantic_units_source", table_name="semantic_retrieval_units")
    op.drop_index("ix_semantic_units_chat_active", table_name="semantic_retrieval_units")
    op.drop_table("semantic_retrieval_units")
    op.drop_table("semantic_index_runs")
    op.drop_constraint(
        "ck_llm_usage_ledger_nonnegative_usage",
        "llm_usage_ledger",
        type_="check",
    )
    op.execute("ALTER TABLE llm_usage_ledger DROP CONSTRAINT ck_llm_usage_ledger_call_type")
    _add_call_type_constraint(_OLD_CALL_TYPES)
    op.drop_column("users", "is_bot")
    # The extension is shared database infrastructure. Do not drop it during an
    # application downgrade; other schemas may legitimately depend on it.
