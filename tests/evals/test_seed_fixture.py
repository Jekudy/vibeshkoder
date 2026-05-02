from __future__ import annotations

from tests.evals.conftest import Seed


async def test_golden_recall_seed_fixture_loads(seed: Seed) -> None:
    assert seed.seed_id == "golden_recall_v1"
    assert seed.version == 1
    assert len(seed.expected_id_map) >= 20
