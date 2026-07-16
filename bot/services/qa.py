from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import SemanticRetrievalTrace
from bot.db.repos.llm_usage_ledger import LedgerRepo
from bot.db.repos.qa_trace import QaTraceRepo
from bot.db.repos.semantic_quota import SemanticQuotaRepo
from bot.services.evidence import EvidenceBundle
from bot.services.llm_gateway import embed_texts, load_embedding_gateway_config
from bot.services.semantic_index import HybridSearchResult, hybrid_search
from bot.services.search import search_messages


logger = logging.getLogger(__name__)


def _semantic_evidence_provenance_ids(bundle: EvidenceBundle) -> list[int]:
    """Persist every card source in the privacy-cascade audit binding."""

    return list(
        dict.fromkeys(
            source_id
            for item in bundle.items
            for source_id in (
                (item.message_version_id, *item.card_source_message_version_ids)
                if item.source_type == "card"
                else (item.message_version_id,)
            )
        )
    )


@dataclass(frozen=True)
class QaResult:
    bundle: EvidenceBundle
    query_redacted: bool


@dataclass(frozen=True)
class SemanticQaResult:
    bundle: EvidenceBundle
    query_redacted: bool
    embedding_llm_call_id: int
    embedding_cost_usd: Decimal
    retrieval: HybridSearchResult


class SemanticRetrievalError(RuntimeError):
    """Expected database failure after a paid embedding was durably audited."""

    def __init__(self, *, embedding_llm_call_id: int) -> None:
        super().__init__("semantic hybrid retrieval failed")
        self.embedding_llm_call_id = embedding_llm_call_id


async def run_qa(
    session: AsyncSession,
    *,
    query: str,
    chat_id: int,
    redact_query_in_audit: bool,
    limit: int = 3,
    exclude_chat_message_id: int | None = None,
    human_only: bool = False,
) -> QaResult:
    hits = await search_messages(
        session,
        query,
        chat_id=chat_id,
        limit=limit,
        exclude_chat_message_id=exclude_chat_message_id,
        human_only=human_only,
    )
    bundle = EvidenceBundle.from_hits(query, chat_id, hits)
    return QaResult(bundle=bundle, query_redacted=redact_query_in_audit)


async def run_semantic_qa(
    session: AsyncSession,
    *,
    query: str,
    chat_id: int,
    redact_query_in_audit: bool,
    attempt_id: int,
    qa_trace_id: int,
    exclude_chat_message_id: int | None,
    provider=None,
) -> SemanticQaResult:
    """Run one admitted query through embedding and governed hybrid retrieval."""

    config = load_embedding_gateway_config()
    embedding = await embed_texts(
        session,
        inputs=[query],
        config=config,
        ledger_repo=LedgerRepo(),
        provider=provider,
        qa_trace_id=qa_trace_id,
    )
    await SemanticQuotaRepo.attach_embedding_call(
        session,
        attempt_id=attempt_id,
        embedding_llm_call_id=embedding.llm_usage_ledger_id,
    )
    # Preserve both the paid ledger and its attempt link before retrieval.
    await session.commit()
    try:
        retrieval = await hybrid_search(
            session,
            query=query,
            query_embedding=embedding.vectors[0],
            chat_id=chat_id,
            embedding_model=config.model,
            exclude_chat_message_id=exclude_chat_message_id,
        )
        bundle = EvidenceBundle.from_hits(query, chat_id, list(retrieval.hits))
        await QaTraceRepo.update_retrieval_fields(
            session,
            qa_trace_id=qa_trace_id,
            evidence_ids=_semantic_evidence_provenance_ids(bundle),
            abstained=bundle.abstained,
        )
        trace = SemanticRetrievalTrace(
            attempt_id=attempt_id,
            qa_trace_id=qa_trace_id,
            query_hash=hashlib.sha256(query.encode("utf-8")).hexdigest(),
            embedding_model=config.model,
            retrieval_mode="hybrid",
            candidate_ranks=retrieval.candidate_ranks,
            result_source_ids=[
                f"{item.source_type}:{item.card_id if item.source_type == 'card' else item.message_version_id}"
                for item in retrieval.hits
            ],
            fts_latency_ms=retrieval.fts_latency_ms,
            vector_latency_ms=retrieval.vector_latency_ms,
            fusion_latency_ms=retrieval.fusion_latency_ms,
            total_latency_ms=retrieval.total_latency_ms,
        )
        session.add(trace)
        await session.flush()
    except SQLAlchemyError as exc:
        logger.error(
            "semantic_qa_retrieval_failed",
            extra={
                "attempt_id": attempt_id,
                "chat_id": chat_id,
                "error_class": type(exc).__name__,
            },
        )
        await session.rollback()
        raise SemanticRetrievalError(embedding_llm_call_id=embedding.llm_usage_ledger_id) from exc
    return SemanticQaResult(
        bundle=bundle,
        query_redacted=redact_query_in_audit,
        embedding_llm_call_id=embedding.llm_usage_ledger_id,
        embedding_cost_usd=embedding.cost_usd,
        retrieval=retrieval,
    )


__all__ = [
    "QaResult",
    "SemanticQaResult",
    "SemanticRetrievalError",
    "run_qa",
    "run_semantic_qa",
]
