"""Offline evaluation runner for the production recall service path."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import QaTrace
from bot.services.evidence import EvidenceBundle
from bot.services.qa import run_qa
from bot.services.semantic_index import HybridSearchResult, hybrid_search


async def run_eval_recall(
    session: AsyncSession,
    *,
    query: str,
    chat_id: int,
    redact_query_in_audit: bool = False,
) -> tuple[EvidenceBundle, QaTrace | None]:
    """Call the same recall service used by production and expose its bundle."""
    result = await run_qa(
        session,
        query=query,
        chat_id=chat_id,
        redact_query_in_audit=redact_query_in_audit,
    )
    return result.bundle, None


async def run_eval_recall_hybrid(
    session: AsyncSession,
    *,
    query: str,
    query_embedding: Sequence[float],
    chat_id: int,
    embedding_model: str,
) -> tuple[EvidenceBundle, HybridSearchResult]:
    """Call the production governed hybrid retrieval used by semantic Q&A.

    Unlike ``run_eval_recall`` (FTS-only), this exercises the same
    ``hybrid_search`` RRF fusion that ``run_semantic_qa`` uses in production.
    The caller supplies the query embedding so the eval can choose between a
    deterministic offline provider and the real embedding provider.
    """
    retrieval = await hybrid_search(
        session,
        query=query,
        query_embedding=query_embedding,
        chat_id=chat_id,
        embedding_model=embedding_model,
    )
    bundle = EvidenceBundle.from_hits(query, chat_id, list(retrieval.hits))
    return bundle, retrieval
