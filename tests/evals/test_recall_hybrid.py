"""Phase 11 — hybrid retrieval (FTS + pgvector, RRF) measurement on seed_v1.

Extends the golden-recall eval (``test_recall_precision.py``, FTS-only) to the
semantic hybrid path shipped with semantic Q&A: the seed corpus is backfilled
into ``semantic_retrieval_units`` and every answerable query runs through
``run_eval_recall_hybrid`` — the same ``hybrid_search`` RRF fusion that
production ``run_semantic_qa`` uses.

Embeddings come from a deterministic feature-hashing provider by default, so
the suite stays offline and passes the ``_llm_guard`` no-network binding. For a
local measurement with real embeddings set ``EVAL_SEMANTIC_PROVIDER=openai``
plus ``OPENAI_API_KEY`` and leave ``EVAL_HARNESS_ENABLED`` unset — the override
is ignored whenever the harness flag is on, so CI can never reach the network.

Committed-fixture note: unlike the FTS eval, this module cannot run inside the
outer-transaction ``eval_db_session`` pattern. ``backfill_semantic_index``
acquires the per-message ``chat_msg:`` advisory locks through a dedicated
NullPool lock connection (``hold_session_advisory_locks``); the same locks are
already held by the outer transaction that persisted the seed, so a joined
session deadlocks against itself. The seed is therefore persisted and indexed
on a real engine session with true commits and removed in fixture teardown —
the same committed-fixture pattern as ``test_semantic_index_postgres.py``.

Abstention semantics differ from the FTS path by design: the governed vector
branch has no similarity floor, so retrieval returns nearest candidates even
for no-answer queries and abstention is decided at the synthesis layer. This
suite therefore gates retrieval quality (frozen recall floors + governance of
returned ids), not abstain behaviour.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import yaml

from bot.services.eval_metrics import precision_at_k, recall_at_k
from bot.services.eval_runner import run_eval_recall_hybrid
from bot.services.eval_seeds import (
    QueryRow,
    SeedSpec,
    load_seed_spec,
    resolve_expected_ids,
)
from bot.services.llm_gateway import EmbeddingGatewayConfig
from bot.services.llm_providers.openai_embeddings import EmbeddingResult
from bot.services.semantic_index import backfill_semantic_index
from tests.evals.conftest import _load_jsonl, _persist_seed_message

pytestmark = [
    pytest.mark.usefixtures("eval_app_env"),
    pytest.mark.asyncio(loop_scope="class"),
]

SEED_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "golden_recall" / "seed_v1"
SEED_META = SEED_DIR / "seed_meta.yaml"
CHAT_HISTORY_PATH = SEED_DIR / "chat_history.jsonl"
SEED_CHAT_ID = -1001234567890
EMBEDDING_DIMENSIONS = 1536
EMBEDDING_MODEL = "text-embedding-3-small"

EMBEDDING_CONFIG = EmbeddingGatewayConfig(
    model=EMBEDDING_MODEL,
    dimensions=EMBEDDING_DIMENSIONS,
    daily_ceiling_usd=Decimal("100"),
    monthly_ceiling_usd=Decimal("1000"),
)

_TOKEN_RE = re.compile(r"[0-9a-zA-Zа-яА-ЯёЁ]+")


def _hash_embedding(text: str) -> tuple[float, ...]:
    """Feature-hashing bag-of-words embedding — deterministic and offline.

    Every whitespace/punctuation-delimited token contributes to one dimension
    via sha256; tokens longer than 5 chars also hash a 5-char prefix so simple
    Russian morphology variants ("комнату"/"комнате") share a slot. Not a
    semantic model — just enough signal to exercise the vector branch and RRF
    fusion on the frozen seed without a network call.
    """
    vector = [0.0] * EMBEDDING_DIMENSIONS
    for token in _TOKEN_RE.findall(text.lower()):
        slots = (token, token[:5]) if len(token) > 5 else (token,)
        for slot in slots:
            digest = hashlib.sha256(slot.encode("utf-8")).digest()
            vector[int.from_bytes(digest[:4], "big") % EMBEDDING_DIMENSIONS] += 1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return tuple(value / norm for value in vector)


class _DeterministicEmbeddingProvider:
    """Provider-protocol shim producing ``_hash_embedding`` vectors."""

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, *, inputs: Any, model: str, dimensions: int) -> EmbeddingResult:
        self.calls += 1
        values = tuple(inputs)
        return EmbeddingResult(
            vectors=tuple(_hash_embedding(value) for value in values),
            tokens_in=sum(len(value) for value in values),
            request_id=f"eval-hash-{self.calls}",
            raw_latency_ms=0,
        )


def _build_provider() -> Any:
    """Real OpenAI embeddings only on explicit local opt-in; never under the harness."""
    if (
        os.environ.get("EVAL_SEMANTIC_PROVIDER") == "openai"
        and os.environ.get("OPENAI_API_KEY")
        and not os.environ.get("EVAL_HARNESS_ENABLED")
    ):
        from bot.services.llm_providers.openai_embeddings import OpenAIEmbeddingsProvider

        return OpenAIEmbeddingsProvider()
    return _DeterministicEmbeddingProvider()


@pytest.fixture(scope="module")
def seed_spec() -> SeedSpec:
    return load_seed_spec(SEED_DIR, seed_id="golden_recall_v1", version=1)


@pytest.fixture(scope="module")
def baseline_thresholds() -> dict[str, float]:
    with SEED_META.open("r", encoding="utf-8") as fh:
        meta = yaml.safe_load(fh) or {}
    thresholds = meta.get("baseline_thresholds")
    if not isinstance(thresholds, dict):
        pytest.fail(f"{SEED_META}: missing or invalid baseline_thresholds map")
    return {k: float(v) for k, v in thresholds.items()}


@pytest_asyncio.fixture(scope="class", loop_scope="class")
async def semantic_seed(eval_postgres_engine: Any) -> Any:
    """Persist seed_v1 for real, run the governed semantic backfill, clean up."""
    from bot.db.models import (
        ChatMessage,
        LlmUsageLedger,
        SemanticIndexRun,
        SemanticRetrievalUnit,
        User,
    )

    factory = async_sessionmaker(
        eval_postgres_engine, class_=AsyncSession, expire_on_commit=False
    )
    provider = _build_provider()
    rows = _load_jsonl(CHAT_HISTORY_PATH)
    user_ids = {int(row["user_id_local"]) for row in rows}
    id_map: dict[str, int] = {}
    run_id_floor = 0
    report = None
    try:
        async with factory() as session:
            for row in rows:
                id_map[str(row["seed_local_id"])] = await _persist_seed_message(
                    session, row
                )
            await session.commit()
            run_id_floor = int(
                await session.scalar(select(func.max(SemanticIndexRun.id))) or 0
            )
            report = await backfill_semantic_index(
                session,
                config=EMBEDDING_CONFIG,
                provider=provider,
                chat_id=SEED_CHAT_ID,
            )
            assert report.failed == 0, f"semantic backfill failed: {report.reason_counts}"
            assert report.indexed > 0, "semantic backfill indexed nothing"
        yield SimpleNamespace(provider=provider, report=report, id_map=id_map)
    finally:
        async with factory() as cleanup:
            ledger_ids = {
                int(value)
                for value in (
                    await cleanup.execute(
                        select(SemanticRetrievalUnit.llm_usage_ledger_id).where(
                            SemanticRetrievalUnit.chat_id == SEED_CHAT_ID
                        )
                    )
                ).scalars()
                if value is not None
            }
            await cleanup.execute(
                delete(SemanticRetrievalUnit).where(
                    SemanticRetrievalUnit.chat_id == SEED_CHAT_ID
                )
            )
            # Runs have no chat_id; remove every run created after the floor
            # captured before backfill so a failed run row is cleaned too.
            await cleanup.execute(
                delete(SemanticIndexRun).where(SemanticIndexRun.id > run_id_floor)
            )
            await cleanup.execute(
                delete(ChatMessage).where(ChatMessage.chat_id == SEED_CHAT_ID)
            )
            if ledger_ids:
                await cleanup.execute(
                    delete(LlmUsageLedger).where(LlmUsageLedger.id.in_(ledger_ids))
                )
            if user_ids:
                await cleanup.execute(delete(User).where(User.id.in_(user_ids)))
            await cleanup.commit()


async def _measure_hybrid_query(
    session: AsyncSession,
    provider: Any,
    query: QueryRow,
    seed_local_id_map: dict[str, int],
) -> tuple[list[int], list[int], bool, Any]:
    embedding = await provider.embed(
        inputs=[query.query],
        model=EMBEDDING_MODEL,
        dimensions=EMBEDDING_DIMENSIONS,
    )
    bundle, retrieval = await run_eval_recall_hybrid(
        session,
        query=query.query,
        query_embedding=embedding.vectors[0],
        chat_id=SEED_CHAT_ID,
        embedding_model=EMBEDDING_MODEL,
    )
    returned = list(bundle.evidence_ids)
    expected = resolve_expected_ids(query, seed_local_id_map) if not query.expected_abstain else []
    return returned, expected, bundle.abstained, retrieval


class TestHybridRecall:
    @pytest.mark.parametrize("k", [1, 3, 5])
    async def test_hybrid_recall_precision_parity(
        self,
        eval_db_session: AsyncSession,
        semantic_seed: SimpleNamespace,
        seed_spec: SeedSpec,
        baseline_thresholds: dict[str, float],
        k: int,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Hybrid retrieval must not drop below the frozen FTS floors."""
        answerable = [q for q in seed_spec.queries if not q.expected_abstain]
        if not answerable:
            pytest.skip("seed has no answerable queries")

        known_mvids = set(semantic_seed.id_map.values())
        recall_values: list[float] = []
        precision_values: list[float] = []
        per_query: list[tuple[str, float, float]] = []
        abstain_count = 0
        for query in answerable:
            returned, expected, abstained, retrieval = await _measure_hybrid_query(
                eval_db_session, semantic_seed.provider, query, semantic_seed.id_map
            )
            vector_ranks = [
                ranks["vector"]
                for ranks in retrieval.candidate_ranks.values()
                if "vector" in ranks
            ]
            assert vector_ranks, (
                f"query {query.query_id!r}: vector branch contributed no "
                "candidates — suite would stay green with a dead vector_search"
            )
            unknown = set(returned) - known_mvids
            assert not unknown, (
                f"query {query.query_id!r} returned evidence outside the governed "
                f"seed scope: {sorted(unknown)}"
            )
            if abstained:
                abstain_count += 1
            r = recall_at_k(returned, expected, k)
            p = precision_at_k(returned, expected, k)
            recall_values.append(r)
            precision_values.append(p)
            per_query.append((query.query_id, r, p))

        mean_recall = sum(recall_values) / len(recall_values)
        mean_precision = sum(precision_values) / len(precision_values)
        with capsys.disabled():
            print(
                f"\n[seed_v1 hybrid] @{k} mean_recall={mean_recall:.3f} "
                f"mean_precision={mean_precision:.3f} "
                f"abstained={abstain_count}/{len(answerable)}"
            )
            for qid, r, p in per_query:
                print(f"  {qid}: recall={r:.3f} precision={p:.3f}")

        recall_floor = baseline_thresholds[f"recall_at_{k}_min"]
        abstain_max = baseline_thresholds.get("abstain_rate_max", 1.0)
        observed_abstain_rate = abstain_count / len(answerable)
        assert mean_recall >= recall_floor, (
            f"hybrid mean_recall@{k}={mean_recall:.3f} below frozen floor {recall_floor:.3f}"
        )
        assert observed_abstain_rate <= abstain_max, (
            f"hybrid abstain rate {observed_abstain_rate:.3f} above ceiling {abstain_max:.3f}"
        )
        assert semantic_seed.provider.calls >= len(answerable), (
            "embedding provider was not invoked for every answerable query"
        )

    async def test_hybrid_abstain_query_stays_in_scope(
        self,
        eval_db_session: AsyncSession,
        semantic_seed: SimpleNamespace,
        seed_spec: SeedSpec,
    ) -> None:
        """The no-answer query may return candidates; every one must be governed."""
        abstain_queries = [q for q in seed_spec.queries if q.expected_abstain]
        if not abstain_queries:
            pytest.skip("seed has no expected-abstain queries")

        known_mvids = set(semantic_seed.id_map.values())
        for query in abstain_queries:
            returned, _expected, _abstained, _retrieval = await _measure_hybrid_query(
                eval_db_session, semantic_seed.provider, query, semantic_seed.id_map
            )
            unknown = set(returned) - known_mvids
            assert not unknown, (
                f"abstain query {query.query_id!r} leaked evidence outside the "
                f"governed seed scope: {sorted(unknown)}"
            )
