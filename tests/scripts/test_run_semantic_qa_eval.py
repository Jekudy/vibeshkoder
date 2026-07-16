from __future__ import annotations

from unittest.mock import AsyncMock
import json
import stat
from pathlib import Path

import pytest

from scripts import run_semantic_qa_eval as runner


RELEASE_SHA = "a" * 40


def _secret_query() -> str:
    """Build a detector fixture without storing a credential-shaped literal."""

    return "".join(("OPENAI_API_KEY=", "sk-", "proj-", "abcdefghijklmnop", "qrstuvwxyz012345"))


def _rows(count: int = 50) -> list[dict[str, object]]:
    categories = (
        ["semantic"] * 15
        + ["exact"] * 10
        + ["multi_source"] * 10
        + ["no_answer"] * 10
        + ["privacy_governance"] * 5
    )
    rows: list[dict[str, object]] = []
    for index, category in enumerate(categories[:count], start=1):
        abstain = category == "no_answer"
        rows.append(
            {
                "case_id": f"case-{index:03d}",
                "category": category,
                "chat_id": -1001234567890,
                "query": f"private query {index}",
                "expected_source_ids": [] if abstain else [f"message:{index}"],
                "forbidden_source_ids": [f"message:{1000 + index}"]
                if category == "privacy_governance"
                else [],
                "expected_abstain": abstain,
                "exclude_chat_message_id": None,
            }
        )
    return rows


def _write(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_input_is_fully_validated_before_provider_work(tmp_path: Path) -> None:
    path = tmp_path / "input.jsonl"
    rows = _rows()
    rows[-1]["forbidden_source_ids"] = []
    _write(path, rows)

    with pytest.raises(ValueError, match="forbidden"):
        runner.load_private_cases(path, smoke=False)

    _write(path, _rows(count=49))
    with pytest.raises(ValueError, match="at least 50"):
        runner.load_private_cases(path, smoke=False)


def test_smoke_mode_is_explicit_and_requires_exactly_one_case(tmp_path: Path) -> None:
    path = tmp_path / "smoke.jsonl"
    row = _rows()[0]
    _write(path, [row])

    cases = runner.load_private_cases(path, smoke=True)

    assert len(cases) == 1
    with pytest.raises(ValueError, match="at least 50"):
        runner.load_private_cases(path, smoke=False)


@pytest.mark.parametrize(
    "unsafe_query",
    [
        "x" * 146,
        pytest.param(_secret_query(), id="secret-like"),
    ],
)
def test_private_input_rejects_queries_outside_production_guard_before_run(
    tmp_path: Path,
    unsafe_query: str,
) -> None:
    path = tmp_path / "unsafe.jsonl"
    row = _rows()[0]
    row["query"] = unsafe_query
    _write(path, [row])

    with pytest.raises(ValueError):
        runner.load_private_cases(path, smoke=True)


@pytest.mark.asyncio
async def test_run_case_rechecks_query_before_embedding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bot.services import llm_gateway
    from bot.services.semantic_eval import SemanticEvalCase

    embed = AsyncMock()
    monkeypatch.setattr(llm_gateway, "embed_texts", embed)
    case = runner.PrivateEvalCase(
        label=SemanticEvalCase(
            case_id="unsafe-direct-case",
            category="privacy_governance",
            expected_source_ids=(),
            forbidden_source_ids=("message:1",),
            expected_abstain=True,
        ),
        chat_id=-1001234567890,
        query=_secret_query(),
        exclude_chat_message_id=None,
    )

    with pytest.raises(ValueError):
        await runner._run_case(case)
    embed.assert_not_awaited()


@pytest.mark.asyncio
async def test_runner_outputs_private_0600_files_and_never_logs_raw_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_path = tmp_path / "input.jsonl"
    cases_path = tmp_path / "cases.jsonl"
    observations_path = tmp_path / "observations.jsonl"
    review_path = tmp_path / "review.jsonl"
    rows = _rows()
    rows[0]["query"] = "PRIVATE-QUERY-SENTINEL"
    _write(input_path, rows)

    async def fake_run_case(case: runner.PrivateEvalCase) -> dict[str, object]:
        return {
            "record_type": "case_review",
            "case_id": case.label.case_id,
            "query": case.query,
            "answer": "PRIVATE-ANSWER-SENTINEL",
            "fts_source_ids": [],
            "hybrid_source_ids": list(case.label.expected_source_ids),
            "objective_leakage_count": 0,
            "reviewed_result": {
                "abstained": False,
                "retrieval_latency_seconds": 1.0,
                "full_latency_seconds": 2.0,
            },
        }

    monkeypatch.setattr(runner, "_run_case", fake_run_case)
    args = runner.build_parser().parse_args(
        [
            "--input",
            str(input_path),
            "--cases-output",
            str(cases_path),
            "--observations-output",
            str(observations_path),
            "--review-output",
            str(review_path),
            "--release-sha",
            RELEASE_SHA,
        ]
    )

    assert await runner.run_private_evaluation(args) == 0
    captured = capsys.readouterr()
    assert "PRIVATE-QUERY-SENTINEL" not in captured.out + captured.err
    assert "PRIVATE-ANSWER-SENTINEL" not in captured.out + captured.err
    assert stat.S_IMODE(cases_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(observations_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(review_path.stat().st_mode) == 0o600
    assert "PRIVATE-QUERY-SENTINEL" not in cases_path.read_text(encoding="utf-8")
    observations_text = observations_path.read_text(encoding="utf-8")
    assert "PRIVATE-QUERY-SENTINEL" not in observations_text
    observations_header = json.loads(observations_text.splitlines()[0])
    assert observations_header["contains_raw_text"] is False
    assert observations_header["release_sha"] == RELEASE_SHA
    review_text = review_path.read_text(encoding="utf-8")
    assert "PRIVATE-QUERY-SENTINEL" in review_text
    assert "PRIVATE-ANSWER-SENTINEL" in review_text
    header = json.loads(review_text.splitlines()[0])
    assert header["contains_raw_text"] is True
    assert header["release_sha"] == RELEASE_SHA


def test_source_key_and_branch_ranking_are_canonical() -> None:
    ranks = {
        "message:2": {"fts": 2, "vector": 1},
        "message:1": {"fts": 1},
        "card:123e4567-e89b-42d3-a456-426614174000": {"vector": 2},
    }

    assert runner._ranked_branch_keys(ranks, "fts") == ["message:1", "message:2"]
    assert runner._ranked_branch_keys(ranks, "vector") == [
        "message:2",
        "card:123e4567-e89b-42d3-a456-426614174000",
    ]
