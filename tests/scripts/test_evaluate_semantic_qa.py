from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

import pytest
import yaml

from scripts.evaluate_semantic_qa import (
    OBSERVATIONS_SCHEMA_VERSION,
    REVIEWED_RESULTS_SCHEMA_VERSION,
    main,
    validate_report_payload,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
EVAL_WORKFLOW = PROJECT_ROOT / ".github/workflows/evals.yml"
RELEASE_SHA = "a" * 40


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _write_observations(
    path: Path,
    dataset_path: Path,
    rows: list[dict[str, object]],
    *,
    release_sha: str = RELEASE_SHA,
) -> None:
    _write_jsonl(
        path,
        [
            {
                "record_type": "header",
                "schema_version": OBSERVATIONS_SCHEMA_VERSION,
                "contains_raw_text": False,
                "release_sha": release_sha,
                "dataset_sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
                "case_count": len(rows),
            },
            *[
                {
                    "record_type": "case_observation",
                    "case_id": row["case_id"],
                    "fts_source_ids": row["fts_source_ids"],
                    "hybrid_source_ids": row["hybrid_source_ids"],
                    "abstained": row["abstained"],
                    "leakage_count": row["leakage_count"],
                    "retrieval_latency_seconds": row["retrieval_latency_seconds"],
                    "full_latency_seconds": row["full_latency_seconds"],
                }
                for row in rows
            ],
        ],
    )


def _write_results(
    path: Path,
    dataset_path: Path,
    observations_path: Path,
    rows: list[dict[str, object]],
    *,
    release_sha: str = RELEASE_SHA,
) -> None:
    _write_jsonl(
        path,
        [
            {
                "record_type": "header",
                "schema_version": REVIEWED_RESULTS_SCHEMA_VERSION,
                "contains_raw_text": False,
                "release_sha": release_sha,
                "dataset_sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
                "observations_sha256": hashlib.sha256(observations_path.read_bytes()).hexdigest(),
                "case_count": len(rows),
            },
            *[
                {
                    "case_id": row["case_id"],
                    "valid_source_links": row["valid_source_links"],
                    "invalid_source_links": row["invalid_source_links"],
                    "unsupported_claims": row["unsupported_claims"],
                    "total_claims": row["total_claims"],
                }
                for row in rows
            ],
        ],
    )


def _passing_rows(count: int = 50) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    categories = (
        ["semantic"] * 15
        + ["exact"] * 10
        + ["multi_source"] * 10
        + ["no_answer"] * 10
        + ["privacy_governance"] * 5
    )
    cases: list[dict[str, object]] = []
    results: list[dict[str, object]] = []
    for index, category in enumerate(categories[:count], start=1):
        abstain = category == "no_answer"
        expected = [] if abstain else [f"message:{index}"]
        forbidden = [f"message:{1000 + index}"] if category == "privacy_governance" else []
        cases.append(
            {
                "case_id": f"case-{index:03d}",
                "category": category,
                "expected_source_ids": expected,
                "forbidden_source_ids": forbidden,
                "expected_abstain": abstain,
            }
        )
        results.append(
            {
                "case_id": f"case-{index:03d}",
                "fts_source_ids": [] if category == "semantic" else expected,
                "hybrid_source_ids": expected,
                "abstained": abstain,
                "leakage_count": 0,
                "valid_source_links": 0 if abstain else 1,
                "invalid_source_links": 0,
                "unsupported_claims": 0,
                "total_claims": 0 if abstain else 1,
                "retrieval_latency_seconds": 1.0,
                "full_latency_seconds": 5.0,
            }
        )
    return cases, results


def _dataset_rows(cases: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            **case,
            "chat_id": -1001234567890,
            "query": f"private query {case['case_id']}",
            "exclude_chat_message_id": None,
        }
        for case in cases
    ]


def _evaluate(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    cases, results = _passing_rows()
    dataset_path = tmp_path / "private-dataset.jsonl"
    cases_path = tmp_path / "cases.jsonl"
    observations_path = tmp_path / "observations.jsonl"
    results_path = tmp_path / "results.jsonl"
    report_path = tmp_path / "report.json"
    _write_jsonl(dataset_path, _dataset_rows(cases))
    _write_jsonl(cases_path, cases)
    _write_observations(observations_path, dataset_path, results)
    _write_results(results_path, dataset_path, observations_path, results)
    assert (
        main(
            [
                "evaluate",
                "--dataset",
                str(dataset_path),
                "--cases",
                str(cases_path),
                "--observations",
                str(observations_path),
                "--results",
                str(results_path),
                "--release-sha",
                RELEASE_SHA,
                "--report",
                str(report_path),
            ]
        )
        == 0
    )
    return dataset_path, cases_path, observations_path, results_path, report_path


def test_private_fifty_case_report_is_hashed_release_bound_and_mode_0600(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset_path, _, observations_path, results_path, report_path = _evaluate(tmp_path)

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["status"] == "pass"
    assert payload["release_sha"] == RELEASE_SHA
    assert payload["dataset_sha256"] == hashlib.sha256(dataset_path.read_bytes()).hexdigest()
    assert (
        payload["observations_sha256"] == hashlib.sha256(observations_path.read_bytes()).hexdigest()
    )
    assert payload["results_sha256"] == hashlib.sha256(results_path.read_bytes()).hexdigest()
    assert payload["report"]["case_count"] == 50
    assert payload["report"]["privacy_governance_case_count"] == 5
    assert payload["contains_raw_text"] is False
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600
    assert "private query case-001" not in capsys.readouterr().out


def test_validator_binds_exact_release_sha_and_workflow_calls_it(tmp_path: Path) -> None:
    _, _, _, _, report_path = _evaluate(tmp_path)
    payload = json.loads(report_path.read_text(encoding="utf-8"))

    artifact = validate_report_payload(payload, expected_release_sha=RELEASE_SHA)

    assert artifact["commit_sha"] == RELEASE_SHA
    assert artifact["suite"] == "private-frozen-semantic-eval"
    with pytest.raises(ValueError, match="release-bound"):
        validate_report_payload(payload, expected_release_sha="b" * 40)

    workflow_text = EVAL_WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.safe_load(workflow_text)
    assert set(workflow[True]) == {"workflow_dispatch"}
    assert "evals-gate" not in workflow["jobs"]
    assert "python -m scripts.evaluate_semantic_qa validate-report" in workflow_text
    assert "MIN_MACRO_RECALL_AT_5" not in workflow_text


def test_validator_rejects_fractional_unexpected_abstention_count(tmp_path: Path) -> None:
    _, _, _, _, report_path = _evaluate(tmp_path)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["report"]["unexpected_answerable_abstention_count"] = 0.5

    with pytest.raises(ValueError, match="non-negative integer"):
        validate_report_payload(payload, expected_release_sha=RELEASE_SHA)


def test_validate_report_command_fails_closed_without_or_with_wrong_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_path = tmp_path / "artifact.jsonl"
    monkeypatch.delenv("SEMANTIC_EVAL_REPORT_JSON", raising=False)
    assert (
        main(
            [
                "validate-report",
                "--expected-release-sha",
                RELEASE_SHA,
                "--artifact",
                str(artifact_path),
            ]
        )
        == 2
    )
    monkeypatch.setenv("SEMANTIC_EVAL_REPORT_JSON", "{}")
    assert (
        main(
            [
                "validate-report",
                "--expected-release-sha",
                RELEASE_SHA,
                "--artifact",
                str(artifact_path),
            ]
        )
        == 2
    )
    assert not artifact_path.exists()


def test_validate_report_command_appends_only_sanitized_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, _, _, report_path = _evaluate(tmp_path)
    monkeypatch.setenv("SEMANTIC_EVAL_REPORT_JSON", report_path.read_text(encoding="utf-8"))
    artifact_path = tmp_path / "artifact.jsonl"

    exit_code = main(
        [
            "validate-report",
            "--expected-release-sha",
            RELEASE_SHA,
            "--artifact",
            str(artifact_path),
        ]
    )

    assert exit_code == 0
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert artifact["commit_sha"] == RELEASE_SHA
    assert artifact["contains_raw_text"] is False


def test_gate_rejects_fewer_than_fifty_cases_without_report(tmp_path: Path) -> None:
    cases, results = _passing_rows(count=49)
    dataset_path = tmp_path / "dataset.jsonl"
    cases_path = tmp_path / "cases.jsonl"
    observations_path = tmp_path / "observations.jsonl"
    results_path = tmp_path / "results.jsonl"
    report_path = tmp_path / "report.json"
    _write_jsonl(dataset_path, _dataset_rows(cases))
    _write_jsonl(cases_path, cases)
    _write_observations(observations_path, dataset_path, results)
    _write_results(results_path, dataset_path, observations_path, results)

    assert (
        main(
            [
                "evaluate",
                "--dataset",
                str(dataset_path),
                "--cases",
                str(cases_path),
                "--observations",
                str(observations_path),
                "--results",
                str(results_path),
                "--release-sha",
                RELEASE_SHA,
                "--report",
                str(report_path),
            ]
        )
        == 2
    )
    assert not report_path.exists()


def test_objective_privacy_leakage_cannot_be_overridden_by_reviewer(tmp_path: Path) -> None:
    cases, results = _passing_rows()
    dataset_path = tmp_path / "dataset.jsonl"
    cases_path = tmp_path / "cases.jsonl"
    observations_path = tmp_path / "observations.jsonl"
    results_path = tmp_path / "results.jsonl"
    report_path = tmp_path / "report.json"
    _write_jsonl(dataset_path, _dataset_rows(cases))
    _write_jsonl(cases_path, cases)
    _write_observations(observations_path, dataset_path, results)
    _write_results(results_path, dataset_path, observations_path, results)
    result_rows = [json.loads(line) for line in results_path.read_text().splitlines()]
    result_rows[-1]["hybrid_source_ids"] = cases[-1]["forbidden_source_ids"]
    result_rows[-1]["leakage_count"] = 0
    _write_jsonl(results_path, result_rows)

    assert (
        main(
            [
                "evaluate",
                "--dataset",
                str(dataset_path),
                "--cases",
                str(cases_path),
                "--observations",
                str(observations_path),
                "--results",
                str(results_path),
                "--release-sha",
                RELEASE_SHA,
                "--report",
                str(report_path),
            ]
        )
        == 2
    )
    assert not report_path.exists()


def test_gate_rejects_results_from_another_release(tmp_path: Path) -> None:
    cases, results = _passing_rows()
    dataset_path = tmp_path / "dataset.jsonl"
    cases_path = tmp_path / "cases.jsonl"
    observations_path = tmp_path / "observations.jsonl"
    results_path = tmp_path / "results.jsonl"
    report_path = tmp_path / "report.json"
    _write_jsonl(dataset_path, _dataset_rows(cases))
    _write_jsonl(cases_path, cases)
    _write_observations(observations_path, dataset_path, results)
    _write_results(
        results_path,
        dataset_path,
        observations_path,
        results,
        release_sha="b" * 40,
    )

    assert (
        main(
            [
                "evaluate",
                "--dataset",
                str(dataset_path),
                "--cases",
                str(cases_path),
                "--observations",
                str(observations_path),
                "--results",
                str(results_path),
                "--release-sha",
                RELEASE_SHA,
                "--report",
                str(report_path),
            ]
        )
        == 2
    )
    assert not report_path.exists()


def test_gate_rejects_results_from_changed_dataset(tmp_path: Path) -> None:
    cases, results = _passing_rows()
    dataset_path = tmp_path / "dataset.jsonl"
    cases_path = tmp_path / "cases.jsonl"
    observations_path = tmp_path / "observations.jsonl"
    results_path = tmp_path / "results.jsonl"
    report_path = tmp_path / "report.json"
    dataset_rows = _dataset_rows(cases)
    _write_jsonl(dataset_path, dataset_rows)
    _write_jsonl(cases_path, cases)
    _write_observations(observations_path, dataset_path, results)
    _write_results(results_path, dataset_path, observations_path, results)
    dataset_rows[0]["query"] = "changed after runner execution"
    _write_jsonl(dataset_path, dataset_rows)

    assert (
        main(
            [
                "evaluate",
                "--dataset",
                str(dataset_path),
                "--cases",
                str(cases_path),
                "--observations",
                str(observations_path),
                "--results",
                str(results_path),
                "--release-sha",
                RELEASE_SHA,
                "--report",
                str(report_path),
            ]
        )
        == 2
    )
    assert not report_path.exists()
