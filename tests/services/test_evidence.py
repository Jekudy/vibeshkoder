import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bot.services.evidence import EvidenceBundle, EvidenceItem
from bot.services.search import SearchHit


def _make_hit(idx: int) -> SearchHit:
    timestamp = datetime(2026, 1, 1, 12, idx, tzinfo=timezone.utc)
    return SearchHit(
        message_version_id=100 + idx,
        chat_message_id=200 + idx,
        chat_id=-100123,
        message_id=300 + idx,
        user_id=42,
        snippet=f"<b>match</b>{idx}",
        ts_rank=0.5 - idx * 0.01,
        captured_at=timestamp,
        message_date=timestamp,
    )


def test_empty_hits_abstains() -> None:
    bundle = EvidenceBundle.from_hits("hello", -100123, [])

    assert bundle.abstained is True
    assert bundle.items == ()
    assert bundle.evidence_ids == []


def test_three_hits_preserve_order() -> None:
    hits = [_make_hit(idx) for idx in range(3)]
    bundle = EvidenceBundle.from_hits("hello", -100123, hits)

    assert bundle.abstained is False
    assert len(bundle.items) == 3
    assert bundle.evidence_ids == [100, 101, 102]
    assert [item.message_id for item in bundle.items] == [300, 301, 302]


def test_evidence_contract_is_frozen() -> None:
    item = EvidenceItem(
        message_version_id=1,
        chat_message_id=2,
        chat_id=-100123,
        message_id=3,
        user_id=None,
        snippet="match",
        ts_rank=0.5,
        captured_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        message_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    bundle = EvidenceBundle.from_hits("x", 1, [])

    with pytest.raises(FrozenInstanceError):
        item.snippet = "mutated"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        bundle.query = "mutated"  # type: ignore[misc]


def test_to_dict_round_trips_through_json() -> None:
    bundle = EvidenceBundle.from_hits("hello", -100123, [_make_hit(0)])

    decoded = json.loads(json.dumps(bundle.to_dict()))

    assert decoded["abstained"] is False
    assert decoded["items"][0]["message_version_id"] == 100
    assert decoded["items"][0]["captured_at"] == "2026-01-01T12:00:00+00:00"


def test_snapshot_matches_fixture() -> None:
    fixture_path = Path("tests/fixtures/evidence_bundle_v1.json")
    expected = json.loads(fixture_path.read_text(encoding="utf-8"))
    bundle = EvidenceBundle.from_hits(
        "hello",
        -100123,
        [_make_hit(idx) for idx in range(2)],
    )
    actual = bundle.to_dict()
    actual.pop("created_at")

    assert actual == expected


def test_evidence_ids_preserve_search_rank_order() -> None:
    hits = [_make_hit(2), _make_hit(0), _make_hit(1)]
    bundle = EvidenceBundle.from_hits("hello", -100123, hits)

    assert bundle.evidence_ids == [102, 100, 101]
