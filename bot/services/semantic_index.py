"""Governed pgvector index for semantic community-memory retrieval."""

from __future__ import annotations

import hashlib
import logging
import math
import time
import uuid
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Literal, Sequence

from sqlalchemy import delete, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from bot.db.models import (
    MessageVersion,
    SemanticIndexRun,
    SemanticRetrievalUnit,
    SemanticRetrievalUnitSource,
)
from bot.db.repos.llm_usage_ledger import LedgerRepo
from bot.services.forget_predicate import forget_excludes_sql_fragment
from bot.services.llm_gateway import (
    EmbeddingBudgetExceeded,
    EmbeddingGatewayConfig,
    embed_texts,
)
from bot.services.llm_providers import ProviderStructuralError, ProviderTransientError
from bot.services.search import SearchHit


logger = logging.getLogger(__name__)

EMBEDDING_PROVIDER = "openai"
EMBEDDING_MODEL_VERSION = "text-embedding-3-small"
DEFAULT_BACKFILL_BATCH_SIZE = 64
DEFAULT_VECTOR_CANDIDATE_LIMIT = 20
MAX_SEMANTIC_EVIDENCE = 5
RRF_K = 60

_FORGET_EXCLUDES = forget_excludes_sql_fragment()

# Only the static _FORGET_EXCLUDES fragment is interpolated; runtime values are bound.
# nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
_REVALIDATE_PROVENANCE_SQL = text(
    f"""
    SELECT mv.id AS message_version_id,
           cm.chat_id
    FROM message_versions mv
    JOIN chat_messages cm
      ON cm.id = mv.chat_message_id
     AND cm.current_version_id = mv.id
    JOIN users author ON author.id = cm.user_id
    WHERE mv.id = ANY(:message_version_ids)
      AND author.is_bot = FALSE
      AND cm.memory_policy = 'normal'
      AND COALESCE(cm.message_kind, 'text') NOT IN ('voice', 'audio')
      AND cm.is_redacted = FALSE
      AND mv.is_redacted = FALSE
      AND {_FORGET_EXCLUDES}
    """
)

_REVALIDATE_CARDS_SQL = text(
    """
    SELECT kc.id::text AS source_id,
           ARRAY_AGG(cs.message_version_id ORDER BY cs.position ASC, cs.id ASC)
               AS message_version_ids
    FROM knowledge_cards kc
    JOIN card_sources cs ON cs.card_id = kc.id
    WHERE kc.id::text = ANY(:source_ids)
      AND kc.card_status = 'approved'
    GROUP BY kc.id
    """
)

# Only the static _FORGET_EXCLUDES fragment is interpolated; runtime values are bound.
# nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
_RECONCILE_INELIGIBLE_SQL = text(
    f"""
    UPDATE semantic_retrieval_units AS unit
    SET invalidated_at = :invalidated_at,
        invalidation_reason = 'source_ineligible'
    WHERE unit.embedding_model = :embedding_model
      AND unit.invalidated_at IS NULL
      AND (CAST(:chat_id AS BIGINT) IS NULL OR unit.chat_id = CAST(:chat_id AS BIGINT))
      AND (
          NOT EXISTS (
              SELECT 1
              FROM semantic_retrieval_unit_sources source_any
              WHERE source_any.unit_id = unit.id
          )
          OR EXISTS (
              SELECT 1
              FROM semantic_retrieval_unit_sources source
              JOIN message_versions mv ON mv.id = source.message_version_id
              JOIN chat_messages cm ON cm.id = mv.chat_message_id
              LEFT JOIN users author ON author.id = cm.user_id
              WHERE source.unit_id = unit.id
                AND (
                    cm.chat_id <> unit.chat_id
                    OR cm.current_version_id IS DISTINCT FROM mv.id
                    OR cm.memory_policy <> 'normal'
                    OR COALESCE(cm.message_kind, 'text') IN ('voice', 'audio')
                    OR cm.is_redacted = TRUE
                    OR mv.is_redacted = TRUE
                    OR author.is_bot IS DISTINCT FROM FALSE
                    OR NOT ({_FORGET_EXCLUDES})
                )
          )
          OR (
              unit.source_type = 'message'
              AND NOT EXISTS (
                  SELECT 1
                  FROM semantic_retrieval_unit_sources source
                  WHERE source.unit_id = unit.id
                    AND source.message_version_id::text = unit.source_id
                    AND source.position = 0
                    AND NOT EXISTS (
                        SELECT 1
                        FROM semantic_retrieval_unit_sources extra_source
                        WHERE extra_source.unit_id = unit.id
                          AND extra_source.message_version_id <> source.message_version_id
                    )
              )
          )
          OR (
              unit.source_type = 'card'
              AND (
                  NOT EXISTS (
                      SELECT 1
                      FROM knowledge_cards card
                      WHERE card.id::text = unit.source_id
                        AND card.card_status = 'approved'
                  )
                  OR EXISTS (
                      (
                          SELECT source.message_version_id, source.position
                          FROM semantic_retrieval_unit_sources source
                          WHERE source.unit_id = unit.id
                      )
                      EXCEPT
                      (
                          SELECT card_source.message_version_id, card_source.position
                          FROM card_sources card_source
                          JOIN knowledge_cards card ON card.id = card_source.card_id
                          WHERE card.id::text = unit.source_id
                      )
                  )
                  OR EXISTS (
                      (
                          SELECT card_source.message_version_id, card_source.position
                          FROM card_sources card_source
                          JOIN knowledge_cards card ON card.id = card_source.card_id
                          WHERE card.id::text = unit.source_id
                      )
                      EXCEPT
                      (
                          SELECT source.message_version_id, source.position
                          FROM semantic_retrieval_unit_sources source
                          WHERE source.unit_id = unit.id
                      )
                  )
              )
          )
      )
    RETURNING unit.id
    """
)

# Only the static _FORGET_EXCLUDES fragment is interpolated; runtime values are bound.
# nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
_ELIGIBLE_MESSAGES_SQL = text(
    f"""
    SELECT mv.id AS message_version_id,
           mv.version_seq,
           mv.content_hash AS source_content_hash,
           cm.chat_id,
           concat_ws(
               E'\n',
               NULLIF(btrim(COALESCE(mv.normalized_text, mv.text, '')), ''),
               NULLIF(btrim(COALESCE(mv.caption, '')), ''),
               CASE
                   WHEN mm.description_status = 'ready'
                   THEN NULLIF(btrim(COALESCE(mm.description, '')), '')
                   ELSE NULL
               END
           ) AS canonical_text,
           CASE WHEN mm.description_status = 'ready' THEN mm.updated_at ELSE NULL END
               AS media_revision
    FROM message_versions mv
    JOIN chat_messages cm
      ON cm.id = mv.chat_message_id
     AND cm.current_version_id = mv.id
    JOIN users author ON author.id = cm.user_id
    LEFT JOIN message_media mm ON mm.chat_message_id = cm.id
    WHERE mv.id > :after_id
      AND (CAST(:chat_id AS BIGINT) IS NULL OR cm.chat_id = CAST(:chat_id AS BIGINT))
      AND author.is_bot = FALSE
      AND cm.memory_policy = 'normal'
      AND COALESCE(cm.message_kind, 'text') NOT IN ('voice', 'audio')
      AND cm.is_redacted = FALSE
      AND mv.is_redacted = FALSE
      AND btrim(
            concat_ws(
                ' ',
                COALESCE(mv.normalized_text, mv.text, ''),
                COALESCE(mv.caption, ''),
                CASE WHEN mm.description_status = 'ready' THEN COALESCE(mm.description, '') ELSE '' END
            )
          ) <> ''
      AND {_FORGET_EXCLUDES}
    ORDER BY mv.id ASC
    LIMIT :limit
    """
)

# Only the static _FORGET_EXCLUDES fragment is interpolated; runtime values are bound.
# nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
_ELIGIBLE_CARDS_SQL = text(
    f"""
    SELECT kc.id AS card_id,
           kc.updated_at,
           kc.approved_at,
           anchor.chat_id,
           concat_ws(E'\n', NULLIF(btrim(kc.title), ''), NULLIF(btrim(kc.body_markdown), ''))
               AS canonical_text,
           ARRAY(
               SELECT cs_list.message_version_id
               FROM card_sources cs_list
               WHERE cs_list.card_id = kc.id
               ORDER BY cs_list.position ASC, cs_list.id ASC
           ) AS message_version_ids
    FROM knowledge_cards kc
    JOIN LATERAL (
        SELECT cm_anchor.chat_id
        FROM card_sources cs_anchor
        JOIN message_versions mv_anchor ON mv_anchor.id = cs_anchor.message_version_id
        JOIN chat_messages cm_anchor ON cm_anchor.id = mv_anchor.chat_message_id
        WHERE cs_anchor.card_id = kc.id
        ORDER BY cs_anchor.position ASC, cs_anchor.id ASC
        LIMIT 1
    ) anchor ON TRUE
    WHERE kc.card_status = 'approved'
      AND kc.id::text > :after_id
      AND (CAST(:chat_id AS BIGINT) IS NULL OR anchor.chat_id = CAST(:chat_id AS BIGINT))
      AND NOT EXISTS (
          SELECT 1
          FROM card_sources cs
          JOIN message_versions mv ON mv.id = cs.message_version_id
          JOIN chat_messages cm ON cm.id = mv.chat_message_id
          LEFT JOIN users author ON author.id = cm.user_id
          WHERE cs.card_id = kc.id
            AND (
                cm.chat_id <> anchor.chat_id
                OR cm.current_version_id IS DISTINCT FROM mv.id
                OR cm.memory_policy <> 'normal'
                OR COALESCE(cm.message_kind, 'text') IN ('voice', 'audio')
                OR cm.is_redacted = TRUE
                OR mv.is_redacted = TRUE
                OR author.is_bot IS DISTINCT FROM FALSE
                OR NOT ({_FORGET_EXCLUDES})
            )
      )
      AND btrim(concat_ws(' ', kc.title, kc.body_markdown)) <> ''
    ORDER BY kc.id::text ASC
    LIMIT :limit
    """
)

# Only the static _FORGET_EXCLUDES fragment is interpolated; runtime values are bound.
# nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
_VECTOR_CANDIDATES_SQL = text(
    f"""
    SELECT unit.id,
           unit.source_type,
           unit.source_id,
           1 - (unit.embedding <=> CAST(:embedding AS vector)) AS similarity
    FROM semantic_retrieval_units unit
    WHERE unit.chat_id = :chat_id
      AND unit.embedding_model = :embedding_model
      AND unit.invalidated_at IS NULL
      AND EXISTS (
          SELECT 1 FROM semantic_retrieval_unit_sources source_any
          WHERE source_any.unit_id = unit.id
      )
      AND (
          unit.source_type = 'message'
          OR (
              unit.source_type = 'card'
              AND EXISTS (
                  SELECT 1
                  FROM knowledge_cards card
                  WHERE card.id::text = unit.source_id
                    AND card.card_status = 'approved'
                    AND ARRAY(
                        SELECT unit_source.message_version_id
                        FROM semantic_retrieval_unit_sources unit_source
                        WHERE unit_source.unit_id = unit.id
                        ORDER BY unit_source.position
                    ) = ARRAY(
                        SELECT card_source.message_version_id
                        FROM card_sources card_source
                        WHERE card_source.card_id = card.id
                        ORDER BY card_source.position, card_source.id
                    )
              )
          )
      )
      AND NOT EXISTS (
          SELECT 1
          FROM semantic_retrieval_unit_sources source
          JOIN message_versions mv ON mv.id = source.message_version_id
          JOIN chat_messages cm ON cm.id = mv.chat_message_id
          LEFT JOIN users author ON author.id = cm.user_id
          WHERE source.unit_id = unit.id
            AND (
                cm.chat_id <> :chat_id
                OR cm.current_version_id IS DISTINCT FROM mv.id
                OR cm.memory_policy <> 'normal'
                OR COALESCE(cm.message_kind, 'text') IN ('voice', 'audio')
                OR cm.is_redacted = TRUE
                OR mv.is_redacted = TRUE
                OR author.is_bot IS DISTINCT FROM FALSE
                OR (CAST(:exclude_chat_message_id AS INTEGER) IS NOT NULL
                    AND cm.id = CAST(:exclude_chat_message_id AS INTEGER))
                OR NOT ({_FORGET_EXCLUDES})
            )
      )
    ORDER BY unit.embedding <=> CAST(:embedding AS vector), unit.id ASC
    LIMIT :limit
    """
)

# Only the static _FORGET_EXCLUDES fragment is interpolated; runtime values are bound.
# nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
_MESSAGE_HIT_SQL = text(
    f"""
    SELECT mv.id AS message_version_id,
           cm.id AS chat_message_id,
           cm.chat_id,
           cm.message_id,
           cm.user_id,
           cm.message_thread_id,
           cm.reply_to_message_id,
           concat_ws(
               ' ',
               NULLIF(btrim(COALESCE(mv.normalized_text, mv.text, '')), ''),
               NULLIF(btrim(COALESCE(mv.caption, '')), ''),
               CASE WHEN mm.description_status = 'ready' THEN mm.description ELSE NULL END
           ) AS snippet,
           mv.captured_at,
           cm.date AS message_date
    FROM semantic_retrieval_units unit
    JOIN semantic_retrieval_unit_sources source ON source.unit_id = unit.id
    JOIN message_versions mv ON mv.id = source.message_version_id
    JOIN chat_messages cm ON cm.id = mv.chat_message_id AND cm.current_version_id = mv.id
    JOIN users author ON author.id = cm.user_id
    LEFT JOIN message_media mm ON mm.chat_message_id = cm.id
    WHERE unit.id = :unit_id
      AND unit.source_type = 'message'
      AND unit.invalidated_at IS NULL
      AND cm.chat_id = :chat_id
      AND author.is_bot = FALSE
      AND cm.memory_policy = 'normal'
      AND COALESCE(cm.message_kind, 'text') NOT IN ('voice', 'audio')
      AND cm.is_redacted = FALSE
      AND mv.is_redacted = FALSE
      AND (CAST(:exclude_chat_message_id AS INTEGER) IS NULL
           OR cm.id <> CAST(:exclude_chat_message_id AS INTEGER))
      AND {_FORGET_EXCLUDES}
    ORDER BY source.position ASC
    LIMIT 1
    """
)

# Only the static _FORGET_EXCLUDES fragment is interpolated; runtime values are bound.
# nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
_CARD_HIT_SQL = text(
    f"""
    SELECT kc.id AS card_id,
           anchor.message_version_id,
           anchor.chat_message_id,
           anchor.chat_id,
           anchor.message_id,
           anchor.message_thread_id,
           anchor.reply_to_message_id,
           concat_ws(' ', kc.title, kc.body_markdown) AS snippet,
           kc.approved_at AS captured_at,
           COALESCE(kc.approved_at, anchor.message_date) AS message_date,
           ARRAY_AGG(source.message_version_id ORDER BY source.position ASC)
               AS message_version_ids
    FROM semantic_retrieval_units unit
    JOIN knowledge_cards kc ON kc.id::text = unit.source_id
    JOIN semantic_retrieval_unit_sources source ON source.unit_id = unit.id
    JOIN LATERAL (
        SELECT mv_anchor.id AS message_version_id,
               cm_anchor.id AS chat_message_id,
               cm_anchor.chat_id,
               cm_anchor.message_id,
               cm_anchor.message_thread_id,
               cm_anchor.reply_to_message_id,
               cm_anchor.date AS message_date
        FROM semantic_retrieval_unit_sources source_anchor
        JOIN message_versions mv_anchor ON mv_anchor.id = source_anchor.message_version_id
        JOIN chat_messages cm_anchor ON cm_anchor.id = mv_anchor.chat_message_id
        WHERE source_anchor.unit_id = unit.id
        ORDER BY source_anchor.position ASC
        LIMIT 1
    ) anchor ON TRUE
    WHERE unit.id = :unit_id
      AND unit.source_type = 'card'
      AND unit.invalidated_at IS NULL
      AND kc.card_status = 'approved'
      AND anchor.chat_id = :chat_id
      AND NOT EXISTS (
          SELECT 1
          FROM semantic_retrieval_unit_sources source_check
          JOIN message_versions mv ON mv.id = source_check.message_version_id
          JOIN chat_messages cm ON cm.id = mv.chat_message_id
          LEFT JOIN users author ON author.id = cm.user_id
          WHERE source_check.unit_id = unit.id
            AND (
                cm.chat_id <> :chat_id
                OR cm.current_version_id IS DISTINCT FROM mv.id
                OR cm.memory_policy <> 'normal'
                OR COALESCE(cm.message_kind, 'text') IN ('voice', 'audio')
                OR cm.is_redacted = TRUE
                OR mv.is_redacted = TRUE
                OR author.is_bot IS DISTINCT FROM FALSE
                OR (CAST(:exclude_chat_message_id AS INTEGER) IS NOT NULL
                    AND cm.id = CAST(:exclude_chat_message_id AS INTEGER))
                OR NOT ({_FORGET_EXCLUDES})
            )
      )
    GROUP BY kc.id, anchor.message_version_id, anchor.chat_message_id, anchor.chat_id,
             anchor.message_id, anchor.message_thread_id, anchor.reply_to_message_id,
             anchor.message_date
    HAVING ARRAY_AGG(source.message_version_id ORDER BY source.position ASC) = ARRAY(
        SELECT card_source.message_version_id
        FROM card_sources card_source
        WHERE card_source.card_id = kc.id
        ORDER BY card_source.position, card_source.id
    )
    """
)

_CONVERSATION_ROOTS_SQL = text(
    """
    WITH RECURSIVE requested(origin_message_id) AS (
        SELECT unnest(CAST(:message_ids AS BIGINT[]))
    ), chain AS (
        SELECT requested.origin_message_id,
               message.message_id AS current_message_id,
               message.reply_to_message_id,
               ARRAY[message.message_id]::BIGINT[] AS path,
               0 AS depth
        FROM requested
        JOIN chat_messages message
          ON message.chat_id = :chat_id
         AND message.message_id = requested.origin_message_id
        UNION ALL
        SELECT chain.origin_message_id,
               parent.message_id,
               parent.reply_to_message_id,
               chain.path || parent.message_id,
               chain.depth + 1
        FROM chain
        JOIN chat_messages parent
          ON parent.chat_id = :chat_id
         AND parent.message_id = chain.reply_to_message_id
        WHERE chain.depth < 64
          AND NOT parent.message_id = ANY(chain.path)
    )
    SELECT DISTINCT ON (origin_message_id)
           origin_message_id,
           CASE
               WHEN reply_to_message_id IS NULL OR reply_to_message_id = ANY(path)
               THEN current_message_id
               ELSE reply_to_message_id
           END AS root_message_id
    FROM chain
    ORDER BY origin_message_id, depth DESC
    """
)


@dataclass(frozen=True, slots=True)
class RetrievalDocument:
    source_type: Literal["message", "card"]
    source_id: str
    source_revision: str
    chat_id: int
    canonical_text: str
    content_hash: str
    message_version_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _ExistingUnit:
    id: int
    source_type: str
    source_id: str
    source_revision: str
    chat_id: int
    content_hash: str
    invalidated_at: datetime | None
    embedding_provider: str
    embedding_model_version: str
    embedding_dimensions: int
    embedding: tuple[float, ...]
    llm_usage_ledger_id: int


@dataclass(frozen=True, slots=True)
class BackfillReport:
    run_id: int
    eligible: int
    indexed: int
    skipped: int
    failed: int
    reason_counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class _BatchResult:
    indexed: int
    skipped: int
    reason_counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class HybridSearchResult:
    hits: tuple[SearchHit, ...]
    candidate_ranks: dict[str, dict[str, int]]
    fts_latency_ms: int
    vector_latency_ms: int
    fusion_latency_ms: int
    total_latency_ms: int


def _content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _vector_literal(vector: Sequence[float]) -> str:
    if len(vector) != 1536:
        raise ValueError("query embedding must contain 1536 values")
    values = tuple(float(value) for value in vector)
    if any(not math.isfinite(value) for value in values):
        raise ValueError("query embedding values must be finite")
    return "[" + ",".join(repr(value) for value in values) + "]"


def _source_key(hit: SearchHit) -> str:
    if hit.source_type == "card":
        if hit.card_id is None:
            raise ValueError("card search hit is missing card_id")
        return f"card:{hit.card_id}"
    return f"message:{hit.message_version_id}"


async def list_eligible_message_documents(
    session: AsyncSession,
    *,
    after_id: int = 0,
    limit: int = DEFAULT_BACKFILL_BATCH_SIZE,
    chat_id: int | None = None,
) -> list[RetrievalDocument]:
    if after_id < 0 or limit < 1:
        raise ValueError("invalid message backfill cursor or limit")
    result = await session.execute(
        _ELIGIBLE_MESSAGES_SQL,
        {"after_id": after_id, "limit": limit, "chat_id": chat_id},
    )
    documents: list[RetrievalDocument] = []
    for row in result.mappings().all():
        canonical_text = str(row["canonical_text"]).strip()
        media_revision = row["media_revision"]
        revision = f"v{row['version_seq']}:media:{media_revision.isoformat() if media_revision else 'none'}"
        documents.append(
            RetrievalDocument(
                source_type="message",
                source_id=str(row["message_version_id"]),
                source_revision=revision,
                chat_id=int(row["chat_id"]),
                canonical_text=canonical_text,
                content_hash=_content_hash(canonical_text),
                message_version_ids=(int(row["message_version_id"]),),
            )
        )
    return documents


async def list_eligible_card_documents(
    session: AsyncSession,
    *,
    after_id: str = "",
    limit: int = DEFAULT_BACKFILL_BATCH_SIZE,
    chat_id: int | None = None,
) -> list[RetrievalDocument]:
    if limit < 1:
        raise ValueError("card backfill limit must be positive")
    result = await session.execute(
        _ELIGIBLE_CARDS_SQL,
        {"after_id": after_id, "limit": limit, "chat_id": chat_id},
    )
    documents: list[RetrievalDocument] = []
    for row in result.mappings().all():
        canonical_text = str(row["canonical_text"]).strip()
        updated_at = row["updated_at"]
        approved_at = row["approved_at"]
        documents.append(
            RetrievalDocument(
                source_type="card",
                source_id=str(row["card_id"]),
                source_revision=(
                    f"updated:{updated_at.isoformat()}:"
                    f"approved:{approved_at.isoformat() if approved_at else 'none'}"
                ),
                chat_id=int(row["chat_id"]),
                canonical_text=canonical_text,
                content_hash=_content_hash(canonical_text),
                message_version_ids=tuple(int(value) for value in row["message_version_ids"]),
            )
        )
    return documents


async def _load_existing_units(
    session: AsyncSession,
    *,
    documents: Sequence[RetrievalDocument],
    embedding_model: str,
) -> dict[tuple[str, str], list[_ExistingUnit]]:
    source_keys = sorted({(document.source_type, document.source_id) for document in documents})
    if not source_keys:
        return {}
    result = await session.execute(
        select(
            SemanticRetrievalUnit.id,
            SemanticRetrievalUnit.source_type,
            SemanticRetrievalUnit.source_id,
            SemanticRetrievalUnit.source_revision,
            SemanticRetrievalUnit.chat_id,
            SemanticRetrievalUnit.content_hash,
            SemanticRetrievalUnit.invalidated_at,
            SemanticRetrievalUnit.embedding_provider,
            SemanticRetrievalUnit.embedding_model_version,
            SemanticRetrievalUnit.embedding_dimensions,
            SemanticRetrievalUnit.embedding,
            SemanticRetrievalUnit.llm_usage_ledger_id,
        )
        .where(
            tuple_(
                SemanticRetrievalUnit.source_type,
                SemanticRetrievalUnit.source_id,
            ).in_(source_keys),
            SemanticRetrievalUnit.embedding_model == embedding_model,
        )
        .order_by(SemanticRetrievalUnit.id.desc())
    )
    units_by_source: dict[tuple[str, str], list[_ExistingUnit]] = {}
    for row in result:
        unit = _ExistingUnit(
            id=int(row.id),
            source_type=str(row.source_type),
            source_id=str(row.source_id),
            source_revision=str(row.source_revision),
            chat_id=int(row.chat_id),
            content_hash=str(row.content_hash),
            invalidated_at=row.invalidated_at,
            embedding_provider=str(row.embedding_provider),
            embedding_model_version=str(row.embedding_model_version),
            embedding_dimensions=int(row.embedding_dimensions),
            embedding=tuple(float(value) for value in row.embedding),
            llm_usage_ledger_id=int(row.llm_usage_ledger_id),
        )
        units_by_source.setdefault((unit.source_type, unit.source_id), []).append(unit)
    return units_by_source


async def _load_parent_message_units(
    session: AsyncSession,
    *,
    documents: Sequence[RetrievalDocument],
    embedding_model: str,
) -> dict[str, list[_ExistingUnit]]:
    message_version_ids = sorted(
        int(document.source_id) for document in documents if document.source_type == "message"
    )
    if not message_version_ids:
        return {}
    current_version = aliased(MessageVersion)
    old_version = aliased(MessageVersion)
    result = await session.execute(
        select(
            current_version.id.label("current_message_version_id"),
            SemanticRetrievalUnit.id,
            SemanticRetrievalUnit.source_type,
            SemanticRetrievalUnit.source_id,
            SemanticRetrievalUnit.source_revision,
            SemanticRetrievalUnit.chat_id,
            SemanticRetrievalUnit.content_hash,
            SemanticRetrievalUnit.invalidated_at,
            SemanticRetrievalUnit.embedding_provider,
            SemanticRetrievalUnit.embedding_model_version,
            SemanticRetrievalUnit.embedding_dimensions,
            SemanticRetrievalUnit.embedding,
            SemanticRetrievalUnit.llm_usage_ledger_id,
        )
        .join(
            old_version,
            (old_version.chat_message_id == current_version.chat_message_id)
            & (old_version.id != current_version.id),
        )
        .join(
            SemanticRetrievalUnitSource,
            SemanticRetrievalUnitSource.message_version_id == old_version.id,
        )
        .join(
            SemanticRetrievalUnit,
            SemanticRetrievalUnit.id == SemanticRetrievalUnitSource.unit_id,
        )
        .where(
            current_version.id.in_(message_version_ids),
            SemanticRetrievalUnit.source_type == "message",
            SemanticRetrievalUnit.embedding_model == embedding_model,
        )
        .order_by(current_version.id, SemanticRetrievalUnit.id.desc())
    )
    units_by_current_source: dict[str, list[_ExistingUnit]] = {}
    for row in result.mappings():
        unit = _ExistingUnit(
            id=int(row["id"]),
            source_type=str(row["source_type"]),
            source_id=str(row["source_id"]),
            source_revision=str(row["source_revision"]),
            chat_id=int(row["chat_id"]),
            content_hash=str(row["content_hash"]),
            invalidated_at=row["invalidated_at"],
            embedding_provider=str(row["embedding_provider"]),
            embedding_model_version=str(row["embedding_model_version"]),
            embedding_dimensions=int(row["embedding_dimensions"]),
            embedding=tuple(float(value) for value in row["embedding"]),
            llm_usage_ledger_id=int(row["llm_usage_ledger_id"]),
        )
        units_by_current_source.setdefault(str(row["current_message_version_id"]), []).append(unit)
    return units_by_current_source


def _find_reusable_unit(
    *,
    document: RetrievalDocument,
    units_by_source: dict[tuple[str, str], list[_ExistingUnit]],
) -> _ExistingUnit | None:
    candidates = units_by_source.get((document.source_type, document.source_id), ())
    for candidate in candidates:
        if (
            candidate.source_revision == document.source_revision
            and candidate.content_hash == document.content_hash
        ):
            return candidate
    for candidate in candidates:
        if candidate.content_hash == document.content_hash:
            return candidate
    return None


def _find_parent_message_unit(
    *,
    document: RetrievalDocument,
    units_by_current_source: dict[str, list[_ExistingUnit]],
) -> _ExistingUnit | None:
    if document.source_type != "message":
        return None
    return next(
        (
            candidate
            for candidate in units_by_current_source.get(document.source_id, ())
            if candidate.content_hash == document.content_hash
        ),
        None,
    )


async def _acquire_document_locks(
    session: AsyncSession,
    *,
    documents: Sequence[RetrievalDocument],
    existing_message_version_ids: Sequence[int] = (),
) -> None:
    if session.bind is None or session.bind.dialect.name != "postgresql":
        return
    from bot.services.forget_cascade import _p6_mvid_advisory_lock_id

    lock_ids = sorted(
        {
            _p6_mvid_advisory_lock_id(message_version_id)
            for document in documents
            for message_version_id in document.message_version_ids
        }
        | {
            _p6_mvid_advisory_lock_id(message_version_id)
            for message_version_id in existing_message_version_ids
        }
    )
    for lock_id in lock_ids:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": lock_id},
        )


async def _revalidate_documents(
    session: AsyncSession,
    *,
    documents: Sequence[RetrievalDocument],
) -> list[RetrievalDocument]:
    message_version_ids = sorted(
        {
            message_version_id
            for document in documents
            for message_version_id in document.message_version_ids
        }
    )
    if not message_version_ids:
        return []
    provenance_result = await session.execute(
        _REVALIDATE_PROVENANCE_SQL,
        {"message_version_ids": message_version_ids},
    )
    valid_chat_by_message_version_id = {
        int(row["message_version_id"]): int(row["chat_id"]) for row in provenance_result.mappings()
    }

    card_documents = [document for document in documents if document.source_type == "card"]
    card_sources: dict[str, tuple[int, ...]] = {}
    if card_documents:
        cards_result = await session.execute(
            _REVALIDATE_CARDS_SQL,
            {"source_ids": sorted({document.source_id for document in card_documents})},
        )
        card_sources = {
            str(row["source_id"]): tuple(int(value) for value in row["message_version_ids"])
            for row in cards_result.mappings()
        }

    valid: list[RetrievalDocument] = []
    for document in documents:
        if not document.message_version_ids or any(
            valid_chat_by_message_version_id.get(message_version_id) != document.chat_id
            for message_version_id in document.message_version_ids
        ):
            continue
        if document.source_type == "message":
            if document.message_version_ids != (int(document.source_id),):
                continue
        elif card_sources.get(document.source_id) != document.message_version_ids:
            continue
        valid.append(document)
    return valid


async def _load_unit_sources(
    session: AsyncSession,
    *,
    unit_ids: Sequence[int],
) -> dict[int, tuple[int, ...]]:
    if not unit_ids:
        return {}
    result = await session.execute(
        select(
            SemanticRetrievalUnitSource.unit_id,
            SemanticRetrievalUnitSource.message_version_id,
        )
        .where(SemanticRetrievalUnitSource.unit_id.in_(unit_ids))
        .order_by(
            SemanticRetrievalUnitSource.unit_id,
            SemanticRetrievalUnitSource.position,
        )
    )
    sources: dict[int, list[int]] = {}
    for row in result:
        sources.setdefault(int(row.unit_id), []).append(int(row.message_version_id))
    return {unit_id: tuple(message_version_ids) for unit_id, message_version_ids in sources.items()}


async def _invalidate_previous_documents(
    session: AsyncSession,
    *,
    document: RetrievalDocument,
    embedding_model: str,
) -> None:
    now = datetime.now(timezone.utc)
    if document.source_type == "message":
        # Message source_id is the immutable message_version id.  An edit gets a
        # new source_id, so invalidating only by source_id would leave the old
        # unit active forever.  Follow provenance to the parent chat_message and
        # invalidate every earlier active version for that same source message.
        await session.execute(
            text(
                """
                UPDATE semantic_retrieval_units AS unit
                SET invalidated_at = :invalidated_at,
                    invalidation_reason = 'source_revised'
                WHERE unit.source_type = 'message'
                  AND unit.embedding_model = :embedding_model
                  AND unit.invalidated_at IS NULL
                  AND (
                      unit.source_revision <> :source_revision
                      OR unit.content_hash <> :content_hash
                  )
                  AND EXISTS (
                      SELECT 1
                      FROM semantic_retrieval_unit_sources old_source
                      JOIN message_versions old_mv
                        ON old_mv.id = old_source.message_version_id
                      JOIN message_versions current_mv
                        ON current_mv.id = :current_message_version_id
                       AND current_mv.chat_message_id = old_mv.chat_message_id
                      WHERE old_source.unit_id = unit.id
                  )
                """
            ),
            {
                "invalidated_at": now,
                "embedding_model": embedding_model,
                "source_revision": document.source_revision,
                "content_hash": document.content_hash,
                "current_message_version_id": int(document.source_id),
            },
        )
        return
    await session.execute(
        update(SemanticRetrievalUnit)
        .where(
            SemanticRetrievalUnit.source_type == document.source_type,
            SemanticRetrievalUnit.source_id == document.source_id,
            SemanticRetrievalUnit.embedding_model == embedding_model,
            SemanticRetrievalUnit.invalidated_at.is_(None),
        )
        .where(
            (SemanticRetrievalUnit.source_revision != document.source_revision)
            | (SemanticRetrievalUnit.content_hash != document.content_hash)
        )
        .values(invalidated_at=now, invalidation_reason="source_revised")
    )


async def _reuse_document(
    session: AsyncSession,
    *,
    document: RetrievalDocument,
    unit: _ExistingUnit,
    existing_sources: tuple[int, ...],
    embedding_model: str,
) -> None:
    await _invalidate_previous_documents(
        session,
        document=document,
        embedding_model=embedding_model,
    )
    if (
        unit.source_revision != document.source_revision
        or unit.chat_id != document.chat_id
        or unit.invalidated_at is not None
    ):
        await session.execute(
            update(SemanticRetrievalUnit)
            .where(SemanticRetrievalUnit.id == unit.id)
            .values(
                source_revision=document.source_revision,
                chat_id=document.chat_id,
                invalidated_at=None,
                invalidation_reason=None,
            )
        )
    if existing_sources != document.message_version_ids:
        await session.execute(
            delete(SemanticRetrievalUnitSource).where(
                SemanticRetrievalUnitSource.unit_id == unit.id
            )
        )
        session.add_all(
            [
                SemanticRetrievalUnitSource(
                    unit_id=unit.id,
                    message_version_id=message_version_id,
                    position=position,
                )
                for position, message_version_id in enumerate(document.message_version_ids)
            ]
        )
    await session.flush()


async def _store_document(
    session: AsyncSession,
    *,
    document: RetrievalDocument,
    vector: Sequence[float],
    config: EmbeddingGatewayConfig,
    llm_usage_ledger_id: int,
    embedding_provider: str,
    embedding_model_version: str,
    embedding_dimensions: int,
) -> bool:
    await _invalidate_previous_documents(
        session,
        document=document,
        embedding_model=config.model,
    )
    statement = (
        pg_insert(SemanticRetrievalUnit)
        .values(
            source_type=document.source_type,
            source_id=document.source_id,
            source_revision=document.source_revision,
            chat_id=document.chat_id,
            content_hash=document.content_hash,
            embedding_provider=embedding_provider,
            embedding_model=config.model,
            embedding_model_version=embedding_model_version,
            embedding_dimensions=embedding_dimensions,
            embedding=list(vector),
            llm_usage_ledger_id=llm_usage_ledger_id,
        )
        .on_conflict_do_nothing(constraint="uq_semantic_units_identity")
        .returning(SemanticRetrievalUnit.id)
    )
    result = await session.execute(statement)
    unit_id = result.scalar_one_or_none()
    if unit_id is None:
        return False
    session.add_all(
        [
            SemanticRetrievalUnitSource(
                unit_id=unit_id,
                message_version_id=message_version_id,
                position=position,
            )
            for position, message_version_id in enumerate(document.message_version_ids)
        ]
    )
    await session.flush()
    return True


async def _index_batch_locked(
    session: AsyncSession,
    *,
    documents: Sequence[RetrievalDocument],
    config: EmbeddingGatewayConfig,
    provider: Any | None,
) -> _BatchResult:
    batch = tuple(documents)
    if not batch:
        return _BatchResult(indexed=0, skipped=0, reason_counts={})
    # The batch may have been listed before this worker acquired the per-source
    # locks. Re-read governance state before any provider call so a forget/edit
    # transaction that won the lock can never leak stale text to OpenAI.
    prevalidated_documents = await _revalidate_documents(session, documents=batch)
    units_before_embedding = await _load_existing_units(
        session,
        documents=prevalidated_documents,
        embedding_model=config.model,
    )
    parent_units_before_embedding = await _load_parent_message_units(
        session,
        documents=prevalidated_documents,
        embedding_model=config.model,
    )
    reusable_before_embedding = {
        document: unit
        for document in prevalidated_documents
        if (
            unit := _find_reusable_unit(
                document=document,
                units_by_source=units_before_embedding,
            )
            or _find_parent_message_unit(
                document=document,
                units_by_current_source=parent_units_before_embedding,
            )
        )
        is not None
    }
    missing = [
        document for document in prevalidated_documents if document not in reusable_before_embedding
    ]
    embedding_result = None
    vectors_by_document: dict[RetrievalDocument, Sequence[float]] = {}
    if missing:
        embedding_result = await embed_texts(
            session,
            inputs=[document.canonical_text for document in missing],
            config=config,
            ledger_repo=LedgerRepo(),
            provider=provider,
        )
        vectors_by_document = dict(zip(missing, embedding_result.vectors, strict=True))
    embedded_documents = set(missing)

    valid_documents = await _revalidate_documents(
        session,
        documents=prevalidated_documents,
    )
    if blocked_count := len(batch) - len(valid_documents):
        logger.info(
            "semantic_index_documents_skipped_governance",
            extra={"batch_size": len(batch), "blocked_count": blocked_count},
        )
    valid_set = set(valid_documents)
    units_under_lock = await _load_existing_units(
        session,
        documents=valid_documents,
        embedding_model=config.model,
    )
    parent_units_under_lock = await _load_parent_message_units(
        session,
        documents=valid_documents,
        embedding_model=config.model,
    )
    reusable_by_document = {
        document: unit
        for document in valid_documents
        if (
            unit := _find_reusable_unit(
                document=document,
                units_by_source=units_under_lock,
            )
        )
        is not None
    }
    parent_reusable_by_document = {
        document: unit
        for document in valid_documents
        if document not in reusable_by_document
        and (
            unit := _find_parent_message_unit(
                document=document,
                units_by_current_source=parent_units_under_lock,
            )
        )
        is not None
    }
    sources_by_unit = await _load_unit_sources(
        session,
        unit_ids=sorted(
            {
                unit.id
                for unit in (
                    *reusable_by_document.values(),
                    *parent_reusable_by_document.values(),
                )
            }
        ),
    )

    reasons: Counter[str] = Counter()
    for document in batch:
        if document not in valid_set:
            reasons["skipped:governance_race"] += 1
            continue
        reusable = reusable_by_document.get(document)
        if reusable is not None:
            existing_sources = sources_by_unit.get(reusable.id, ())
            unchanged = (
                reusable.source_revision == document.source_revision
                and reusable.chat_id == document.chat_id
                and reusable.invalidated_at is None
                and existing_sources == document.message_version_ids
            )
            if unchanged:
                reasons[
                    "skipped:conflict" if document in embedded_documents else "skipped:unchanged"
                ] += 1
                continue
            await _reuse_document(
                session,
                document=document,
                unit=reusable,
                existing_sources=existing_sources,
                embedding_model=config.model,
            )
            reasons["indexed:reused_embedding"] += 1
            continue
        parent_reusable = parent_reusable_by_document.get(document)
        if parent_reusable is not None:
            stored = await _store_document(
                session,
                document=document,
                vector=parent_reusable.embedding,
                config=config,
                llm_usage_ledger_id=parent_reusable.llm_usage_ledger_id,
                embedding_provider=parent_reusable.embedding_provider,
                embedding_model_version=parent_reusable.embedding_model_version,
                embedding_dimensions=parent_reusable.embedding_dimensions,
            )
            reasons["indexed:reused_embedding" if stored else "skipped:conflict"] += 1
            continue
        if document not in vectors_by_document or embedding_result is None:
            reasons["skipped:conflict"] += 1
            continue
        if await _store_document(
            session,
            document=document,
            vector=vectors_by_document[document],
            config=config,
            llm_usage_ledger_id=embedding_result.llm_usage_ledger_id,
            embedding_provider=EMBEDDING_PROVIDER,
            embedding_model_version=EMBEDDING_MODEL_VERSION,
            embedding_dimensions=config.dimensions,
        ):
            reasons["indexed:new_embedding"] += 1
        else:
            reasons["skipped:conflict"] += 1
    indexed = reasons["indexed:new_embedding"] + reasons["indexed:reused_embedding"]
    skipped = (
        reasons["skipped:unchanged"]
        + reasons["skipped:governance_race"]
        + reasons["skipped:conflict"]
    )
    return _BatchResult(indexed=indexed, skipped=skipped, reason_counts=dict(reasons))


async def _index_batch(
    session: AsyncSession,
    *,
    documents: Sequence[RetrievalDocument],
    config: EmbeddingGatewayConfig,
    provider: Any | None,
) -> _BatchResult:
    """Serialize each source claim before checking cache or calling OpenAI."""

    batch = tuple(documents)
    if not batch:
        return _BatchResult(indexed=0, skipped=0, reason_counts={})
    from bot.services.advisory_locks import hold_session_advisory_locks
    from bot.services.forget_cascade import _p6_mvid_advisory_lock_id

    lock_ids = (
        _p6_mvid_advisory_lock_id(message_version_id)
        for document in batch
        for message_version_id in document.message_version_ids
    )
    async with hold_session_advisory_locks(session, lock_ids):
        result = await _index_batch_locked(
            session,
            documents=batch,
            config=config,
            provider=provider,
        )
        # Publish the claimed identity before releasing the dedicated lock, so
        # a waiting backfill observes it and never dispatches a duplicate call.
        await session.commit()
        return result


async def _reconcile_ineligible_units(
    session: AsyncSession,
    *,
    embedding_model: str,
    chat_id: int | None,
) -> int:
    source_result = await session.execute(
        select(SemanticRetrievalUnitSource.message_version_id)
        .join(
            SemanticRetrievalUnit,
            SemanticRetrievalUnit.id == SemanticRetrievalUnitSource.unit_id,
        )
        .where(
            SemanticRetrievalUnit.embedding_model == embedding_model,
            SemanticRetrievalUnit.invalidated_at.is_(None),
        )
        .where(True if chat_id is None else SemanticRetrievalUnit.chat_id == chat_id)
    )
    await _acquire_document_locks(
        session,
        documents=(),
        existing_message_version_ids=sorted(
            {int(message_version_id) for message_version_id in source_result.scalars()}
        ),
    )
    result = await session.execute(
        _RECONCILE_INELIGIBLE_SQL,
        {
            "invalidated_at": datetime.now(timezone.utc),
            "embedding_model": embedding_model,
            "chat_id": chat_id,
        },
    )
    invalidated = len(result.scalars().all())
    if invalidated:
        logger.info(
            "semantic_index_units_reconciled",
            extra={"embedding_model": embedding_model, "chat_id": chat_id, "count": invalidated},
        )
    return invalidated


async def backfill_semantic_index(
    session: AsyncSession,
    *,
    config: EmbeddingGatewayConfig,
    provider: Any | None = None,
    batch_size: int = DEFAULT_BACKFILL_BATCH_SIZE,
    chat_id: int | None = None,
) -> BackfillReport:
    """Index every currently eligible source; stop on the first failed batch."""

    if batch_size < 1 or batch_size > 100:
        raise ValueError("semantic backfill batch_size must be between 1 and 100")
    run = SemanticIndexRun(
        run_type="backfill",
        embedding_provider=EMBEDDING_PROVIDER,
        embedding_model=config.model,
        embedding_dimensions=config.dimensions,
        status="running",
        eligible_count=0,
        indexed_count=0,
        skipped_count=0,
        failed_count=0,
        reason_counts={},
    )
    session.add(run)
    await session.flush()
    run_id = run.id
    # Persist the audit shell before eligibility/provider work starts. This
    # leaves an operator-visible run even when the first batch fails.
    await session.commit()

    eligible = indexed = skipped = failed = 0
    reasons: Counter[str] = Counter()
    try:
        message_cursor = 0
        while True:
            documents = await list_eligible_message_documents(
                session,
                after_id=message_cursor,
                limit=batch_size,
                chat_id=chat_id,
            )
            if not documents:
                break
            eligible += len(documents)
            batch_result = await _index_batch(
                session,
                documents=documents,
                config=config,
                provider=provider,
            )
            indexed += batch_result.indexed
            skipped += batch_result.skipped
            reasons.update(batch_result.reason_counts)
            message_cursor = int(documents[-1].source_id)
            run.cursor = f"message:{message_cursor}"
            run.eligible_count = eligible
            run.indexed_count = indexed
            run.skipped_count = skipped
            run.reason_counts = dict(reasons)
            await session.commit()

        card_cursor = ""
        while True:
            documents = await list_eligible_card_documents(
                session,
                after_id=card_cursor,
                limit=batch_size,
                chat_id=chat_id,
            )
            if not documents:
                break
            eligible += len(documents)
            batch_result = await _index_batch(
                session,
                documents=documents,
                config=config,
                provider=provider,
            )
            indexed += batch_result.indexed
            skipped += batch_result.skipped
            reasons.update(batch_result.reason_counts)
            card_cursor = documents[-1].source_id
            run.cursor = f"card:{card_cursor}"
            run.eligible_count = eligible
            run.indexed_count = indexed
            run.skipped_count = skipped
            run.reason_counts = dict(reasons)
            await session.commit()

        invalidated = await _reconcile_ineligible_units(
            session,
            embedding_model=config.model,
            chat_id=chat_id,
        )
        if invalidated:
            reasons["invalidated:ineligible"] += invalidated
    except (
        EmbeddingBudgetExceeded,
        LookupError,
        ProviderStructuralError,
        ProviderTransientError,
        ValueError,
    ) as exc:
        # Every eligible document must be accounted for as indexed, skipped,
        # or failed. A provider error aborts the whole current batch, not one
        # synthetic row in the audit report.
        failed = max(0, eligible - indexed - skipped)
        reasons[f"failed:{type(exc).__name__}"] += max(1, failed)
        run.status = "failed"
        run.eligible_count = eligible
        run.indexed_count = indexed
        run.skipped_count = skipped
        run.failed_count = failed
        run.reason_counts = dict(reasons)
        run.completed_at = datetime.now(timezone.utc)
        await session.commit()
        raise
    except SQLAlchemyError as exc:
        await session.rollback()
        persisted_run = await session.get(SemanticIndexRun, run_id)
        if persisted_run is None:
            raise LookupError("semantic index run audit row is missing") from exc
        persisted_run.status = "failed"
        failed = max(0, eligible - indexed - skipped)
        persisted_run.failed_count = failed
        reasons[f"failed:{type(exc).__name__}"] += max(1, failed)
        persisted_run.reason_counts = dict(reasons)
        persisted_run.completed_at = datetime.now(timezone.utc)
        await session.commit()
        logger.error(
            "semantic_index_database_failed",
            extra={"run_id": run_id, "error_class": type(exc).__name__},
        )
        raise

    run.status = "completed"
    run.cursor = None
    run.eligible_count = eligible
    run.indexed_count = indexed
    run.skipped_count = skipped
    run.failed_count = failed
    run.reason_counts = dict(reasons)
    run.completed_at = datetime.now(timezone.utc)
    await session.commit()
    return BackfillReport(
        run_id=run.id,
        eligible=eligible,
        indexed=indexed,
        skipped=skipped,
        failed=failed,
        reason_counts=dict(reasons),
    )


async def vector_search(
    session: AsyncSession,
    *,
    query_embedding: Sequence[float],
    chat_id: int,
    embedding_model: str,
    limit: int = DEFAULT_VECTOR_CANDIDATE_LIMIT,
    exclude_chat_message_id: int | None = None,
) -> list[SearchHit]:
    if limit < 1:
        raise ValueError("vector search limit must be positive")
    result = await session.execute(
        _VECTOR_CANDIDATES_SQL,
        {
            "embedding": _vector_literal(query_embedding),
            "chat_id": chat_id,
            "embedding_model": embedding_model,
            "exclude_chat_message_id": exclude_chat_message_id,
            "limit": limit,
        },
    )
    hits: list[SearchHit] = []
    for candidate in result.mappings().all():
        params = {
            "unit_id": candidate["id"],
            "chat_id": chat_id,
            "exclude_chat_message_id": exclude_chat_message_id,
        }
        if candidate["source_type"] == "message":
            hit_result = await session.execute(_MESSAGE_HIT_SQL, params)
            row = hit_result.mappings().one_or_none()
            if row is None:
                continue
            hits.append(
                SearchHit(
                    message_version_id=int(row["message_version_id"]),
                    chat_message_id=int(row["chat_message_id"]),
                    chat_id=int(row["chat_id"]),
                    message_id=int(row["message_id"]),
                    user_id=int(row["user_id"]),
                    snippet=str(row["snippet"])[:400],
                    ts_rank=float(candidate["similarity"]),
                    captured_at=row["captured_at"],
                    message_date=row["message_date"],
                    message_thread_id=row["message_thread_id"],
                    reply_to_message_id=row["reply_to_message_id"],
                )
            )
            continue
        if candidate["source_type"] != "card":
            raise ValueError("semantic unit has unsupported source_type")
        hit_result = await session.execute(_CARD_HIT_SQL, params)
        row = hit_result.mappings().one_or_none()
        if row is None:
            continue
        hits.append(
            SearchHit(
                message_version_id=int(row["message_version_id"]),
                chat_message_id=int(row["chat_message_id"]),
                chat_id=int(row["chat_id"]),
                message_id=int(row["message_id"]),
                user_id=None,
                snippet=str(row["snippet"])[:400],
                ts_rank=float(candidate["similarity"]),
                captured_at=row["captured_at"],
                message_date=row["message_date"],
                source_type="card",
                card_id=uuid.UUID(str(row["card_id"])),
                card_source_message_version_ids=tuple(
                    int(value) for value in row["message_version_ids"]
                ),
                message_thread_id=row["message_thread_id"],
                reply_to_message_id=row["reply_to_message_id"],
            )
        )
    return hits


async def _with_conversation_roots(
    session: AsyncSession,
    *,
    chat_id: int,
    hits: Sequence[SearchHit],
) -> list[SearchHit]:
    if not any(hit.reply_to_message_id is not None for hit in hits):
        return list(hits)
    result = await session.execute(
        _CONVERSATION_ROOTS_SQL,
        {
            "chat_id": chat_id,
            "message_ids": sorted({hit.message_id for hit in hits}),
        },
    )
    roots = {
        int(row["origin_message_id"]): int(row["root_message_id"]) for row in result.mappings()
    }
    return [replace(hit, conversation_root_message_id=roots.get(hit.message_id)) for hit in hits]


def reciprocal_rank_fusion(
    *,
    vector_hits: Sequence[SearchHit],
    fts_hits: Sequence[SearchHit],
    limit: int = MAX_SEMANTIC_EVIDENCE,
    rrf_k: int = RRF_K,
) -> tuple[list[SearchHit], dict[str, dict[str, int]]]:
    if limit < 1 or rrf_k < 1:
        raise ValueError("RRF limit and k must be positive")
    scores: dict[str, float] = {}
    rank_meta: dict[str, dict[str, int]] = {}
    hits_by_key: dict[str, SearchHit] = {}
    for branch_name, branch_hits in (("vector", vector_hits), ("fts", fts_hits)):
        seen: set[str] = set()
        for rank, hit in enumerate(branch_hits, start=1):
            key = _source_key(hit)
            if key in seen:
                continue
            seen.add(key)
            hits_by_key.setdefault(key, hit)
            scores[key] = scores.get(key, 0.0) + 1.0 / (rrf_k + rank)
            rank_meta.setdefault(key, {})[branch_name] = rank

    ordered_keys = sorted(scores, key=lambda key: (-scores[key], key))
    selected: list[SearchHit] = []
    author_counts: dict[int, int] = {}
    thread_counts: dict[int, int] = {}
    conversation_counts: dict[int, int] = {}
    card_count = 0
    for key in ordered_keys:
        hit = hits_by_key[key]
        if hit.source_type == "card":
            if card_count >= 2:
                continue
        elif hit.user_id is not None:
            if author_counts.get(hit.user_id, 0) >= 2:
                continue
        if hit.message_thread_id is not None and thread_counts.get(hit.message_thread_id, 0) >= 2:
            continue
        conversation_id = hit.conversation_root_message_id or hit.reply_to_message_id
        if conversation_id is not None and conversation_counts.get(conversation_id, 0) >= 2:
            continue
        if hit.source_type == "card":
            card_count += 1
        elif hit.user_id is not None:
            author_counts[hit.user_id] = author_counts.get(hit.user_id, 0) + 1
        if hit.message_thread_id is not None:
            thread_counts[hit.message_thread_id] = thread_counts.get(hit.message_thread_id, 0) + 1
        if conversation_id is not None:
            conversation_counts[conversation_id] = conversation_counts.get(conversation_id, 0) + 1
        selected.append(hit)
        if len(selected) == limit:
            break
    return selected, rank_meta


async def hybrid_search(
    session: AsyncSession,
    *,
    query: str,
    query_embedding: Sequence[float],
    chat_id: int,
    embedding_model: str,
    exclude_chat_message_id: int | None = None,
    candidate_limit: int = DEFAULT_VECTOR_CANDIDATE_LIMIT,
    limit: int = MAX_SEMANTIC_EVIDENCE,
) -> HybridSearchResult:
    from bot.services.search import search_messages

    started = time.monotonic()
    fts_started = time.monotonic()
    fts_hits = await search_messages(
        session,
        query,
        chat_id=chat_id,
        limit=candidate_limit,
        include_cards=True,
        exclude_chat_message_id=exclude_chat_message_id,
        human_only=True,
    )
    fts_latency_ms = int((time.monotonic() - fts_started) * 1000)

    vector_started = time.monotonic()
    vector_hits = await vector_search(
        session,
        query_embedding=query_embedding,
        chat_id=chat_id,
        embedding_model=embedding_model,
        limit=candidate_limit,
        exclude_chat_message_id=exclude_chat_message_id,
    )
    vector_latency_ms = int((time.monotonic() - vector_started) * 1000)

    fusion_started = time.monotonic()
    enriched_hits = await _with_conversation_roots(
        session,
        chat_id=chat_id,
        hits=[*fts_hits, *vector_hits],
    )
    enriched_fts_hits = enriched_hits[: len(fts_hits)]
    enriched_vector_hits = enriched_hits[len(fts_hits) :]
    hits, candidate_ranks = reciprocal_rank_fusion(
        vector_hits=enriched_vector_hits,
        fts_hits=enriched_fts_hits,
        limit=limit,
    )
    fusion_latency_ms = int((time.monotonic() - fusion_started) * 1000)
    return HybridSearchResult(
        hits=tuple(hits),
        candidate_ranks=candidate_ranks,
        fts_latency_ms=fts_latency_ms,
        vector_latency_ms=vector_latency_ms,
        fusion_latency_ms=fusion_latency_ms,
        total_latency_ms=int((time.monotonic() - started) * 1000),
    )


__all__ = [
    "BackfillReport",
    "HybridSearchResult",
    "RetrievalDocument",
    "backfill_semantic_index",
    "hybrid_search",
    "list_eligible_card_documents",
    "list_eligible_message_documents",
    "reciprocal_rank_fusion",
    "vector_search",
]
