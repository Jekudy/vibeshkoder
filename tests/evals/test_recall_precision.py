"""Phase 11 §5.4 — recall@K / precision@K smoke test against seed_v1.

Establishes the empirical baseline for Phase 4 /recall. Hard thresholds
are NOT asserted in this smoke pass — the test prints metrics so that
T11-W2-04 (baseline freeze) can record them in seed_meta.yaml after a
stable Phase 4 run.

Tests are grouped under a class so they share the class-scoped event loop
that the eval_db_session fixture (also class-scoped) is bound to.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.eval_metrics import precision_at_k, recall_at_k
from bot.services.eval_runner import run_eval_recall
from bot.services.eval_seeds import (
    QueryRow,
    SeedSpec,
    load_seed_spec,
    resolve_expected_ids,
)

pytestmark = pytest.mark.usefixtures("eval_app_env", "golden_recall_seed")

SEED_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "golden_recall" / "seed_v1"
SEED_CHAT_ID = -1001234567890


@pytest.fixture(scope="module")
def seed_spec() -> SeedSpec:
    return load_seed_spec(SEED_DIR, seed_id="golden_recall_v1", version=1)


async def _measure_query(
    session: AsyncSession,
    query: QueryRow,
    seed_local_id_map: dict[str, int],
) -> tuple[list[int], list[int], bool]:
    bundle, _trace = await run_eval_recall(
        session,
        query=query.query,
        chat_id=SEED_CHAT_ID,
    )
    returned = list(bundle.evidence_ids)
    expected = resolve_expected_ids(query, seed_local_id_map) if not query.expected_abstain else []
    return returned, expected, bundle.abstained


class TestRecallPrecision:
    @pytest.mark.parametrize("k", [1, 3, 5])
    async def test_recall_precision_baseline_smoke(
        self,
        eval_db_session: AsyncSession,
        seed_spec: SeedSpec,
        seed_local_id_map: dict[str, int],
        k: int,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Compute recall@K / precision@K across answerable seed queries; print summary."""
        answerable = [q for q in seed_spec.queries if not q.expected_abstain]
        if not answerable:
            pytest.skip("seed has no answerable queries")

        recall_values: list[float] = []
        precision_values: list[float] = []
        per_query: list[tuple[str, float, float]] = []
        for query in answerable:
            returned, expected, abstained = await _measure_query(
                eval_db_session, query, seed_local_id_map
            )
            assert not abstained, f"query {query.query_id} expected non-abstain, got abstained"
            r = recall_at_k(returned, expected, k)
            p = precision_at_k(returned, expected, k)
            recall_values.append(r)
            precision_values.append(p)
            per_query.append((query.query_id, r, p))

        mean_recall = sum(recall_values) / len(recall_values)
        mean_precision = sum(precision_values) / len(precision_values)
        with capsys.disabled():
            print(f"\n[seed_v1] @{k} mean_recall={mean_recall:.3f} mean_precision={mean_precision:.3f}")
            for qid, r, p in per_query:
                print(f"  {qid}: recall={r:.3f} precision={p:.3f}")

        # Smoke-only floors — kept loose until T11-W2-04 freezes baseline.
        assert mean_recall >= 0.0
        assert mean_precision >= 0.0

    async def test_abstain_queries_actually_abstain(
        self,
        eval_db_session: AsyncSession,
        seed_spec: SeedSpec,
    ) -> None:
        """Queries flagged expected_abstain in the seed must produce abstained bundles."""
        abstain_queries = [q for q in seed_spec.queries if q.expected_abstain]
        if not abstain_queries:
            pytest.skip("seed has no expected-abstain queries")

        for query in abstain_queries:
            bundle, _trace = await run_eval_recall(
                eval_db_session,
                query=query.query,
                chat_id=SEED_CHAT_ID,
            )
            assert bundle.abstained is True, (
                f"query {query.query_id!r} expected abstain but recall returned "
                f"{len(bundle.items)} item(s)"
            )
            assert bundle.items == ()
