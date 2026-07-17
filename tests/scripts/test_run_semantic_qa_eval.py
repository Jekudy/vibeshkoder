from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock
import hashlib
import json
import stat
import threading
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
    privacy_class_groups = iter(
        (
            ["cross_chat", "stale_version"],
            ["bot_authored"],
            ["forgotten"],
            ["redacted"],
            ["non_normal"],
        )
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
                "privacy_classes": next(privacy_class_groups)
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

    rows = _rows()
    rows[-5]["privacy_classes"] = ["cross_chat"]
    _write(path, rows)
    with pytest.raises(ValueError, match="missing=.*stale_version"):
        runner.load_private_cases(path, smoke=False)


@pytest.mark.asyncio
async def test_missing_privacy_coverage_stops_before_provider_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = tmp_path / "input.jsonl"
    rows = _rows()
    rows[-5]["privacy_classes"] = ["cross_chat"]
    _write(input_path, rows)
    run_case = AsyncMock()
    monkeypatch.setattr(runner, "_run_case", run_case)
    args = runner.build_parser().parse_args(
        [
            "--input",
            str(input_path),
            "--cases-output",
            str(tmp_path / "cases.jsonl"),
            "--observations-output",
            str(tmp_path / "observations.jsonl"),
            "--review-output",
            str(tmp_path / "review.jsonl"),
            "--release-sha",
            RELEASE_SHA,
        ]
    )

    with pytest.raises(ValueError, match="missing=.*stale_version"):
        await runner.run_private_evaluation(args)
    run_case.assert_not_awaited()


@pytest.mark.parametrize(
    "overlap",
    ["input-output", "output-output", "input-hardlink", "output-hardlink", "symlink"],
)
@pytest.mark.asyncio
async def test_overlapping_eval_paths_fail_before_truncate_or_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overlap: str,
) -> None:
    input_path = tmp_path / "input.jsonl"
    cases_path = tmp_path / "cases.jsonl"
    observations_path = tmp_path / "observations.jsonl"
    review_path = tmp_path / "review.jsonl"
    _write(input_path, _rows())
    original_input = input_path.read_bytes()
    original_output: bytes | None = None
    if overlap == "input-output":
        cases_path = input_path
    elif overlap == "output-output":
        observations_path = cases_path
    elif overlap == "input-hardlink":
        cases_path.hardlink_to(input_path)
    elif overlap == "output-hardlink":
        cases_path.write_bytes(b"existing private artifact\n")
        observations_path.hardlink_to(cases_path)
        original_output = cases_path.read_bytes()
    else:
        cases_path.symlink_to(input_path)
    run_case = AsyncMock()
    monkeypatch.setattr(runner, "_run_case", run_case)
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
            "--overwrite",
        ]
    )

    expected_error = "symbolic links" if overlap == "symlink" else "paths must be distinct"
    with pytest.raises(ValueError, match=expected_error):
        await runner.run_private_evaluation(args)

    assert input_path.read_bytes() == original_input
    if original_output is not None:
        assert cases_path.read_bytes() == original_output
    run_case.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_hardlink_substitution_is_detected_by_open_fds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = tmp_path / "input.jsonl"
    cases_path = tmp_path / "cases.jsonl"
    observations_path = tmp_path / "observations.jsonl"
    review_path = tmp_path / "review.jsonl"
    _write(input_path, _rows())
    original_input = input_path.read_bytes()
    original_mode = stat.S_IMODE(input_path.stat().st_mode)
    open_window = threading.Event()
    substitution_done = threading.Event()
    original_open = runner._open_private_text

    def synchronized_open(path: Path, *, overwrite: bool):  # type: ignore[no-untyped-def]
        if path == cases_path:
            open_window.set()
            assert substitution_done.wait(timeout=5)
        return original_open(path, overwrite=overwrite)

    def substitute_output_with_input_hardlink() -> None:
        assert open_window.wait(timeout=5)
        cases_path.hardlink_to(input_path)
        substitution_done.set()

    monkeypatch.setattr(runner, "_open_private_text", synchronized_open)
    run_case = AsyncMock()
    monkeypatch.setattr(runner, "_run_case", run_case)
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
            "--overwrite",
        ]
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        attacker = executor.submit(substitute_output_with_input_hardlink)
        with pytest.raises(ValueError, match="paths must be distinct"):
            await runner.run_private_evaluation(args)
        attacker.result(timeout=5)

    assert input_path.read_bytes() == original_input
    assert stat.S_IMODE(input_path.stat().st_mode) == original_mode
    run_case.assert_not_awaited()


@pytest.mark.asyncio
async def test_input_path_substitution_after_open_cannot_change_cases_or_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = tmp_path / "input.jsonl"
    detached_path = tmp_path / "detached-input.jsonl"
    cases_path = tmp_path / "cases.jsonl"
    observations_path = tmp_path / "observations.jsonl"
    review_path = tmp_path / "review.jsonl"
    original_row = _rows()[0]
    replacement_row = dict(original_row)
    replacement_row["query"] = "replacement query"
    _write(input_path, [original_row])
    original_bytes = input_path.read_bytes()
    original_load = runner._load_jsonl

    def substitute_after_fd_open(source):  # type: ignore[no-untyped-def]
        input_path.rename(detached_path)
        _write(input_path, [replacement_row])
        return original_load(source)

    async def fake_run_case(case: runner.PrivateEvalCase) -> dict[str, object]:
        assert case.query == original_row["query"]
        return {
            "record_type": "case_review",
            "case_id": case.label.case_id,
            "query": case.query,
            "answer": None,
            "fts_source_ids": [],
            "hybrid_source_ids": [],
            "objective_leakage_count": 0,
            "reviewed_result": {
                "abstained": True,
                "retrieval_latency_seconds": 0.1,
                "full_latency_seconds": 0.2,
            },
        }

    monkeypatch.setattr(runner, "_load_jsonl", substitute_after_fd_open)
    run_case = AsyncMock(side_effect=fake_run_case)
    monkeypatch.setattr(runner, "_run_case", run_case)
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
            "--smoke",
        ]
    )

    assert await runner.run_private_evaluation(args) == 0

    header = json.loads(observations_path.read_text(encoding="utf-8").splitlines()[0])
    assert header["dataset_sha256"] == hashlib.sha256(original_bytes).hexdigest()
    run_case.assert_awaited_once()


@pytest.mark.asyncio
async def test_observations_hash_stays_bound_to_open_output_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_path = tmp_path / "input.jsonl"
    cases_path = tmp_path / "cases.jsonl"
    observations_path = tmp_path / "observations.jsonl"
    detached_observations_path = tmp_path / "detached-observations.jsonl"
    review_path = tmp_path / "review.jsonl"
    _write(input_path, [_rows()[0]])

    async def fake_run_case(case: runner.PrivateEvalCase) -> dict[str, object]:
        observations_path.rename(detached_observations_path)
        observations_path.write_text("substituted pathname\n", encoding="utf-8")
        return {
            "record_type": "case_review",
            "case_id": case.label.case_id,
            "query": case.query,
            "answer": None,
            "fts_source_ids": [],
            "hybrid_source_ids": [],
            "objective_leakage_count": 0,
            "reviewed_result": {
                "abstained": True,
                "retrieval_latency_seconds": 0.1,
                "full_latency_seconds": 0.2,
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
            "--smoke",
        ]
    )

    assert await runner.run_private_evaluation(args) == 0

    report = json.loads(capsys.readouterr().out)
    detached_bytes = detached_observations_path.read_bytes()
    assert report["observations_sha256"] == hashlib.sha256(detached_bytes).hexdigest()
    assert (
        report["observations_sha256"] != hashlib.sha256(observations_path.read_bytes()).hexdigest()
    )


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
            privacy_classes=("redacted",),
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
