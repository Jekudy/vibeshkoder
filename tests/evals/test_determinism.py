"""Phase 11 §5.5 — determinism binding test.

Same seed + same query run twice in the same process MUST produce
byte-identical bundle.evidence_ids. Catches non-deterministic orderings
before they corrupt the baseline.

Tests are grouped under a class so they share the class-scoped event loop
that the eval_db_session fixture (also class-scoped) is bound to.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.eval_runner import run_eval_recall

pytestmark = pytest.mark.usefixtures("eval_app_env", "golden_recall_seed")


class TestDeterminism:
    @pytest.mark.parametrize(
        "query",
        [
            "Когда будет воркшоп по Postgres FTS?",
            "Какие правила дизайна зафиксировали для интерфейса?",
            "Где пройдет офлайн-встреча и какой нужен переходник?",
        ],
    )
    async def test_same_seed_same_query_same_evidence_ids(
        self,
        eval_db_session: AsyncSession,
        query: str,
    ) -> None:
        """Two consecutive runs of the same query → identical evidence_ids."""
        bundle_a, _trace_a = await run_eval_recall(
            eval_db_session,
            query=query,
            chat_id=-1001234567890,
        )
        bundle_b, _trace_b = await run_eval_recall(
            eval_db_session,
            query=query,
            chat_id=-1001234567890,
        )

        assert bundle_a.evidence_ids == bundle_b.evidence_ids, (
            f"determinism violation for query={query!r}:\n"
            f"  run A: {bundle_a.evidence_ids}\n"
            f"  run B: {bundle_b.evidence_ids}"
        )
        assert bundle_a.abstained == bundle_b.abstained

    async def test_abstention_is_deterministic(
        self,
        eval_db_session: AsyncSession,
    ) -> None:
        """A query with no evidence must abstain on every run."""
        query = "Что Иван Иванович решил насчёт лотерей в 2050 году?"
        for _ in range(3):
            bundle, _trace = await run_eval_recall(
                eval_db_session,
                query=query,
                chat_id=-1001234567890,
            )
            assert bundle.abstained is True
            assert bundle.items == ()
