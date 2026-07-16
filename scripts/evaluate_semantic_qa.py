"""Build and validate no-content semantic Q&A acceptance reports.

The ``evaluate`` command reads private labels/reviewer observations and writes a
sanitized report bound to the release commit plus the exact frozen dataset and
reviewed-results hashes.  ``validate-report`` is the single CI trust boundary;
workflow YAML deliberately contains no duplicate threshold implementation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Sequence

from bot.services.semantic_eval import (
    SemanticEvalCase,
    SemanticEvalReport,
    SemanticEvalResult,
    evaluate_semantic_run,
    validate_semantic_report,
)


MIN_FROZEN_CASES = 50
REPORT_SCHEMA_VERSION = 3
OBSERVATIONS_SCHEMA_VERSION = 2
REVIEWED_RESULTS_SCHEMA_VERSION = 1
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_RELEASE_SHA_RE = re.compile(r"[0-9a-f]{40}")
_REPORT_KEYS = {
    "schema_version",
    "status",
    "contains_raw_text",
    "release_sha",
    "dataset_sha256",
    "observations_sha256",
    "results_sha256",
    "report",
    "violations",
}
_INTEGER_REPORT_FIELDS = {
    "case_count",
    "answerable_case_count",
    "no_answer_case_count",
    "semantic_case_count",
    "exact_case_count",
    "multi_source_case_count",
    "privacy_governance_case_count",
    "privacy_expected_abstention_case_count",
    "privacy_expected_abstention_failures",
    "unexpected_answerable_abstention_count",
    "leakage_count",
    "invalid_source_links",
    "unsupported_claims",
    "total_claims",
}


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"line {line_number} is not valid JSON") from exc
            if not isinstance(value, dict):
                raise ValueError(f"line {line_number} must be a JSON object")
            values.append(value)
    if not values:
        raise ValueError("input JSONL is empty")
    return values


def _exact_fields(value: dict[str, Any], expected: set[str], *, kind: str) -> None:
    if set(value) != expected:
        raise ValueError(f"{kind} fields do not match the versioned schema")


def _load_cases(path: Path) -> tuple[SemanticEvalCase, ...]:
    cases: list[SemanticEvalCase] = []
    expected = {
        "case_id",
        "category",
        "expected_source_ids",
        "forbidden_source_ids",
        "expected_abstain",
    }
    for value in _load_jsonl(path):
        _exact_fields(value, expected, kind="case")
        source_ids = value["expected_source_ids"]
        forbidden_ids = value["forbidden_source_ids"]
        if not isinstance(source_ids, list) or not isinstance(forbidden_ids, list):
            raise ValueError("case source ids must be JSON arrays")
        cases.append(
            SemanticEvalCase(
                case_id=value["case_id"],
                category=value["category"],
                expected_source_ids=tuple(source_ids),
                forbidden_source_ids=tuple(forbidden_ids),
                expected_abstain=value["expected_abstain"],
            )
        )
    if len(cases) < MIN_FROZEN_CASES:
        raise ValueError(f"frozen evaluation requires at least {MIN_FROZEN_CASES} cases")
    return tuple(cases)


def _validate_cases_match_dataset(dataset_path: Path, cases: tuple[SemanticEvalCase, ...]) -> None:
    """Bind evaluated labels to the exact raw runner input being hashed."""

    dataset_fields = {
        "case_id",
        "category",
        "chat_id",
        "query",
        "expected_source_ids",
        "forbidden_source_ids",
        "expected_abstain",
        "exclude_chat_message_id",
    }
    projected: list[SemanticEvalCase] = []
    for value in _load_jsonl(dataset_path):
        _exact_fields(value, dataset_fields, kind="dataset")
        expected_ids = value["expected_source_ids"]
        forbidden_ids = value["forbidden_source_ids"]
        if not isinstance(expected_ids, list) or not isinstance(forbidden_ids, list):
            raise ValueError("dataset source ids must be JSON arrays")
        projected.append(
            SemanticEvalCase(
                case_id=value["case_id"],
                category=value["category"],
                expected_source_ids=tuple(expected_ids),
                forbidden_source_ids=tuple(forbidden_ids),
                expected_abstain=value["expected_abstain"],
            )
        )
    if tuple(projected) != cases:
        raise ValueError("sanitized cases must exactly match the hashed private dataset")


@dataclass(frozen=True, slots=True)
class _ObjectiveObservation:
    case_id: str
    fts_source_ids: tuple[str, ...]
    hybrid_source_ids: tuple[str, ...]
    abstained: bool
    leakage_count: int
    retrieval_latency_seconds: float
    full_latency_seconds: float


@dataclass(frozen=True, slots=True)
class _ReviewAnnotations:
    case_id: str
    valid_source_links: int
    invalid_source_links: int
    unsupported_claims: int
    total_claims: int


def _load_observations(
    path: Path,
    *,
    expected_release_sha: str,
    expected_dataset_sha256: str,
    expected_case_count: int,
) -> tuple[_ObjectiveObservation, ...]:
    """Load immutable no-content observations emitted by the live runner."""

    rows = _load_jsonl(path)
    header_fields = {
        "record_type",
        "schema_version",
        "contains_raw_text",
        "release_sha",
        "dataset_sha256",
        "case_count",
    }
    header = rows[0]
    _exact_fields(header, header_fields, kind="result header")
    if (
        header["record_type"] != "header"
        or header["schema_version"] != OBSERVATIONS_SCHEMA_VERSION
        or header["contains_raw_text"] is not False
        or header["release_sha"] != expected_release_sha
        or header["dataset_sha256"] != expected_dataset_sha256
        or type(header["case_count"]) is not int
        or header["case_count"] != expected_case_count
    ):
        raise ValueError("observations are not bound to this runner release and dataset")
    expected_observation_fields = {
        "record_type",
        "case_id",
        "fts_source_ids",
        "hybrid_source_ids",
        "abstained",
        "leakage_count",
        "retrieval_latency_seconds",
        "full_latency_seconds",
    }
    observations: list[_ObjectiveObservation] = []
    for value in rows[1:]:
        _exact_fields(value, expected_observation_fields, kind="observation")
        if value["record_type"] != "case_observation":
            raise ValueError("invalid observation record type")
        fts_ids = value["fts_source_ids"]
        hybrid_ids = value["hybrid_source_ids"]
        if not isinstance(fts_ids, list) or not isinstance(hybrid_ids, list):
            raise ValueError("observation source ids must be JSON arrays")
        observations.append(
            _ObjectiveObservation(
                case_id=value["case_id"],
                fts_source_ids=tuple(fts_ids),
                hybrid_source_ids=tuple(hybrid_ids),
                abstained=value["abstained"],
                leakage_count=value["leakage_count"],
                retrieval_latency_seconds=value["retrieval_latency_seconds"],
                full_latency_seconds=value["full_latency_seconds"],
            )
        )
    if len(observations) != expected_case_count:
        raise ValueError("observation count does not match the runner manifest")
    return tuple(observations)


def _load_review_annotations(
    path: Path,
    *,
    expected_release_sha: str,
    expected_dataset_sha256: str,
    expected_observations_sha256: str,
    expected_case_count: int,
) -> tuple[_ReviewAnnotations, ...]:
    """Load only human link/claim annotations; objective fields are forbidden."""

    rows = _load_jsonl(path)
    header_fields = {
        "record_type",
        "schema_version",
        "contains_raw_text",
        "release_sha",
        "dataset_sha256",
        "observations_sha256",
        "case_count",
    }
    header = rows[0]
    _exact_fields(header, header_fields, kind="result header")
    if (
        header["record_type"] != "header"
        or header["schema_version"] != REVIEWED_RESULTS_SCHEMA_VERSION
        or header["contains_raw_text"] is not False
        or header["release_sha"] != expected_release_sha
        or header["dataset_sha256"] != expected_dataset_sha256
        or header["observations_sha256"] != expected_observations_sha256
        or type(header["case_count"]) is not int
        or header["case_count"] != expected_case_count
    ):
        raise ValueError("review annotations are not bound to the exact runner observations")
    expected_annotation_fields = {
        "case_id",
        "valid_source_links",
        "invalid_source_links",
        "unsupported_claims",
        "total_claims",
    }
    annotations: list[_ReviewAnnotations] = []
    for value in rows[1:]:
        _exact_fields(value, expected_annotation_fields, kind="review annotation")
        annotations.append(_ReviewAnnotations(**value))
    if len(annotations) != expected_case_count:
        raise ValueError("review annotation count does not match the runner manifest")
    return tuple(annotations)


def _merge_results(
    cases: tuple[SemanticEvalCase, ...],
    observations: tuple[_ObjectiveObservation, ...],
    annotations: tuple[_ReviewAnnotations, ...],
) -> tuple[SemanticEvalResult, ...]:
    case_ids = {case.case_id for case in cases}
    observations_by_id = {value.case_id: value for value in observations}
    annotations_by_id = {value.case_id: value for value in annotations}
    if (
        len(observations_by_id) != len(observations)
        or len(annotations_by_id) != len(annotations)
        or set(observations_by_id) != case_ids
        or set(annotations_by_id) != case_ids
    ):
        raise ValueError("observations and review annotations must match cases exactly")
    return tuple(
        SemanticEvalResult(
            case_id=case.case_id,
            fts_source_ids=observations_by_id[case.case_id].fts_source_ids,
            hybrid_source_ids=observations_by_id[case.case_id].hybrid_source_ids,
            abstained=observations_by_id[case.case_id].abstained,
            leakage_count=observations_by_id[case.case_id].leakage_count,
            valid_source_links=annotations_by_id[case.case_id].valid_source_links,
            invalid_source_links=annotations_by_id[case.case_id].invalid_source_links,
            unsupported_claims=annotations_by_id[case.case_id].unsupported_claims,
            total_claims=annotations_by_id[case.case_id].total_claims,
            retrieval_latency_seconds=observations_by_id[case.case_id].retrieval_latency_seconds,
            full_latency_seconds=observations_by_id[case.case_id].full_latency_seconds,
        )
        for case in cases
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_private_json(path: Path, payload: dict[str, Any], *, overwrite: bool) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | (os.O_TRUNC if overwrite else os.O_EXCL)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        os.write(
            descriptor,
            (json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n").encode(),
        )
    finally:
        os.close(descriptor)


def _validate_release_sha(value: str) -> str:
    if _RELEASE_SHA_RE.fullmatch(value) is None:
        raise ValueError("release SHA must be exactly 40 lowercase hexadecimal characters")
    return value


def _parse_report_metrics(value: object) -> SemanticEvalReport:
    if not isinstance(value, dict):
        raise ValueError("report metrics must be a JSON object")
    expected = {field.name for field in fields(SemanticEvalReport)}
    if set(value) != expected:
        raise ValueError("report metric fields do not match the versioned schema")
    parsed: dict[str, int | float] = {}
    for name, raw in value.items():
        if name in _INTEGER_REPORT_FIELDS:
            if type(raw) is not int or raw < 0:
                raise ValueError("report integer metrics must be non-negative integers")
            parsed[name] = raw
            continue
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError("report numeric metrics must be finite numbers")
        numeric = float(raw)
        if not math.isfinite(numeric):
            raise ValueError("report numeric metrics must be finite numbers")
        parsed[name] = numeric
    return SemanticEvalReport(**parsed)


def validate_report_payload(
    payload: object,
    *,
    expected_release_sha: str,
) -> dict[str, Any]:
    """Strictly validate one sanitized report and return a CI-safe artifact row."""

    release_sha = _validate_release_sha(expected_release_sha)
    if not isinstance(payload, dict) or set(payload) != _REPORT_KEYS:
        raise ValueError("invalid report schema")
    if (
        payload["schema_version"] != REPORT_SCHEMA_VERSION
        or payload["status"] != "pass"
        or payload["contains_raw_text"] is not False
        or payload["violations"] != []
        or payload["release_sha"] != release_sha
    ):
        raise ValueError("report is not a release-bound no-content pass")
    for hash_name in ("dataset_sha256", "observations_sha256", "results_sha256"):
        hash_value = payload[hash_name]
        if not isinstance(hash_value, str) or _SHA256_RE.fullmatch(hash_value) is None:
            raise ValueError("report file hashes must be lowercase SHA-256 values")
    report = _parse_report_metrics(payload["report"])
    if report.case_count < MIN_FROZEN_CASES:
        raise ValueError("frozen report contains fewer than 50 cases")
    if validate_semantic_report(report):
        raise ValueError("frozen report contains a blocking metric violation")
    return {
        **payload,
        "suite": "private-frozen-semantic-eval",
        "commit_sha": release_sha,
    }


def _append_artifact(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as target:
        target.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")


def _run_evaluate(args: argparse.Namespace) -> int:
    release_sha = _validate_release_sha(args.release_sha)
    dataset_path = Path(args.dataset).expanduser()
    cases_path = Path(args.cases).expanduser()
    observations_path = Path(args.observations).expanduser()
    results_path = Path(args.results).expanduser()
    dataset_sha256 = _sha256_file(dataset_path)
    observations_sha256 = _sha256_file(observations_path)
    cases = _load_cases(cases_path)
    _validate_cases_match_dataset(dataset_path, cases)
    observations = _load_observations(
        observations_path,
        expected_release_sha=release_sha,
        expected_dataset_sha256=dataset_sha256,
        expected_case_count=len(cases),
    )
    annotations = _load_review_annotations(
        results_path,
        expected_release_sha=release_sha,
        expected_dataset_sha256=dataset_sha256,
        expected_observations_sha256=observations_sha256,
        expected_case_count=len(cases),
    )
    results = _merge_results(cases, observations, annotations)
    report, violations = evaluate_semantic_run(cases, results)
    payload = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": "pass" if not violations else "fail",
        "contains_raw_text": False,
        "release_sha": release_sha,
        "dataset_sha256": dataset_sha256,
        "observations_sha256": observations_sha256,
        "results_sha256": _sha256_file(results_path),
        "report": asdict(report),
        "violations": list(violations),
    }
    _write_private_json(
        Path(args.report).expanduser(),
        payload,
        overwrite=args.overwrite,
    )
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
    return 0 if not violations else 1


def _run_validate_report(args: argparse.Namespace) -> int:
    raw_report = os.environ.get("SEMANTIC_EVAL_REPORT_JSON")
    if not raw_report:
        raise ValueError("SEMANTIC_EVAL_REPORT_JSON is required")
    try:
        payload = json.loads(raw_report)
    except json.JSONDecodeError as exc:
        raise ValueError("SEMANTIC_EVAL_REPORT_JSON is not valid JSON") from exc
    artifact = validate_report_payload(
        payload,
        expected_release_sha=args.expected_release_sha,
    )
    _append_artifact(Path(args.artifact), artifact)
    print(
        json.dumps(
            {
                "schema_version": REPORT_SCHEMA_VERSION,
                "status": "pass",
                "suite": "private-frozen-semantic-eval",
                "commit_sha": artifact["commit_sha"],
                "case_count": artifact["report"]["case_count"],
                "contains_raw_text": False,
            },
            sort_keys=True,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scripts.evaluate_semantic_qa")
    commands = parser.add_subparsers(dest="command", required=True)

    evaluate = commands.add_parser("evaluate", help="Build a release-bound sanitized report.")
    evaluate.add_argument("--dataset", required=True, help="Exact private frozen runner input.")
    evaluate.add_argument("--cases", required=True, help="Private frozen labels JSONL.")
    evaluate.add_argument(
        "--observations",
        required=True,
        help="Immutable no-content observations emitted by the live runner.",
    )
    evaluate.add_argument(
        "--results",
        required=True,
        help="Reviewed observations JSONL with the copied runner manifest header.",
    )
    evaluate.add_argument("--release-sha", required=True)
    evaluate.add_argument("--report", required=True, help="No-content gate report JSON.")
    evaluate.add_argument("--overwrite", action="store_true")
    evaluate.set_defaults(func=_run_evaluate)

    validate = commands.add_parser(
        "validate-report",
        help="Fail closed unless SEMANTIC_EVAL_REPORT_JSON is a valid release-bound pass.",
    )
    validate.add_argument("--expected-release-sha", required=True)
    validate.add_argument("--artifact", required=True)
    validate.set_defaults(func=_run_validate_report)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (FileExistsError, FileNotFoundError, PermissionError, TypeError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "schema_version": REPORT_SCHEMA_VERSION,
                    "status": "error",
                    "error_class": type(exc).__name__,
                    "contains_raw_text": False,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
