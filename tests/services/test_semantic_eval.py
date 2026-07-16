"""Deterministic acceptance-gate tests for semantic Q&A evaluation."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from typing import Any

import pytest

from bot.services.semantic_eval import (
    SemanticEvalCase,
    SemanticEvalResult,
    evaluate_semantic_run,
    validate_semantic_report,
)


def _message(value: int) -> str:
    return f"message:{value}"


def _case(
    case_id: str,
    category: str,
    expected: tuple[str, ...],
    *,
    forbidden: tuple[str, ...] = (),
    abstain: bool = False,
) -> SemanticEvalCase:
    return SemanticEvalCase(
        case_id=case_id,
        category=category,  # type: ignore[arg-type]
        expected_source_ids=expected,
        forbidden_source_ids=forbidden,
        expected_abstain=abstain,
    )


def _result(
    case_id: str,
    *,
    fts: tuple[str, ...] = (),
    hybrid: tuple[str, ...] = (),
    abstained: bool = False,
    leakage_count: int = 0,
    valid_source_links: int | None = None,
    invalid_source_links: int = 0,
    unsupported_claims: int = 0,
    total_claims: int | None = None,
    retrieval: float = 1.0,
    full: float = 5.0,
) -> SemanticEvalResult:
    return SemanticEvalResult(
        case_id=case_id,
        fts_source_ids=fts,
        hybrid_source_ids=hybrid,
        abstained=abstained,
        leakage_count=leakage_count,
        valid_source_links=(0 if abstained else 1)
        if valid_source_links is None
        else valid_source_links,
        invalid_source_links=invalid_source_links,
        unsupported_claims=unsupported_claims,
        total_claims=(0 if abstained else 1) if total_claims is None else total_claims,
        retrieval_latency_seconds=retrieval,
        full_latency_seconds=full,
    )


def _passing_run() -> tuple[list[SemanticEvalCase], list[SemanticEvalResult]]:
    semantic_ids = tuple(_message(value) for value in range(1, 6))
    cases = [
        _case("semantic", "semantic", semantic_ids),
        _case("exact", "exact", (_message(10),)),
        _case("multi", "multi_source", (_message(20),)),
        _case("no-answer", "no_answer", (), abstain=True),
        _case(
            "privacy",
            "privacy_governance",
            (_message(30),),
            forbidden=(_message(999),),
        ),
    ]
    results = [
        _result("semantic", fts=semantic_ids[:4], hybrid=semantic_ids),
        _result("exact", fts=(_message(10),), hybrid=(_message(10),)),
        _result("multi", fts=(_message(20),), hybrid=(_message(20),)),
        _result("no-answer", abstained=True),
        _result("privacy", fts=(_message(30),), hybrid=(_message(30),)),
    ]
    return cases, results


def test_passing_report_has_every_category_and_separate_privacy_abstentions() -> None:
    cases, results = _passing_run()
    cases.append(
        _case(
            "privacy-abstain",
            "privacy_governance",
            (),
            forbidden=(_message(998),),
            abstain=True,
        )
    )
    results.append(_result("privacy-abstain", abstained=True))

    report, violations = evaluate_semantic_run(cases, results)

    assert report.case_count == 6
    assert report.answerable_case_count == 4
    assert report.no_answer_case_count == 1
    assert report.privacy_expected_abstention_case_count == 1
    assert report.privacy_expected_abstention_failures == 0
    assert report.unexpected_answerable_abstention_count == 0
    assert report.no_answer_abstention_rate == 1.0
    assert report.macro_recall_at_5 == 1.0
    assert report.semantic_hybrid_minus_fts_at_5 == 0.2
    assert violations == ()
    with pytest.raises(FrozenInstanceError):
        report.case_count = 7  # type: ignore[misc]


def test_privacy_leakage_is_computed_and_review_count_must_match() -> None:
    cases, results = _passing_run()
    leaked = replace(
        results[-1],
        hybrid_source_ids=(_message(30), _message(999)),
        leakage_count=1,
    )

    report, violations = evaluate_semantic_run(cases, [*results[:-1], leaked])

    assert report.leakage_count == 1
    assert "leakage_count=1 > 0" in violations
    with pytest.raises(ValueError, match="forbidden-source intersection"):
        evaluate_semantic_run(cases, [*results[:-1], replace(leaked, leakage_count=0)])


def test_privacy_expected_abstention_failure_is_blocking_but_not_no_answer_metric() -> None:
    cases, results = _passing_run()
    cases[-1] = _case("privacy", "privacy_governance", (), forbidden=(_message(999),), abstain=True)
    results[-1] = _result("privacy", hybrid=(_message(30),))

    report, violations = evaluate_semantic_run(cases, results)

    assert report.no_answer_abstention_rate == 1.0
    assert report.privacy_expected_abstention_failures == 1
    assert any(value.startswith("privacy_expected_abstention_failures=") for value in violations)


@pytest.mark.parametrize("case_id", ["semantic", "exact"])
def test_unexpected_answerable_abstention_is_blocking_even_with_perfect_retrieval(
    case_id: str,
) -> None:
    cases, results = _passing_run()
    result_index = next(index for index, result in enumerate(results) if result.case_id == case_id)
    results[result_index] = replace(
        results[result_index],
        abstained=True,
        valid_source_links=0,
        total_claims=0,
    )

    report, violations = evaluate_semantic_run(cases, results)

    assert report.macro_recall_at_5 == 1.0
    assert report.unexpected_answerable_abstention_count == 1
    assert "unexpected_answerable_abstention_count=1 > 0" in violations


@pytest.mark.parametrize(
    ("source_id", "valid"),
    [
        ("message:1", True),
        ("message:0", False),
        ("message:-1", False),
        ("card:123e4567-e89b-42d3-a456-426614174000", True),
        ("card:123E4567-E89B-42D3-A456-426614174000", False),
        ("123", False),
    ],
)
def test_source_ids_are_canonical_strings(source_id: str, valid: bool) -> None:
    kwargs = {
        "case_id": "case",
        "category": "semantic",
        "expected_source_ids": (source_id,),
        "forbidden_source_ids": (),
        "expected_abstain": False,
    }
    if valid:
        assert SemanticEvalCase(**kwargs).expected_source_ids == (source_id,)  # type: ignore[arg-type]
    else:
        with pytest.raises(ValueError, match="canonical"):
            SemanticEvalCase(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            {
                "case_id": "x",
                "category": "privacy_governance",
                "expected_source_ids": (),
                "forbidden_source_ids": (),
                "expected_abstain": True,
            },
            "forbidden",
        ),
        (
            {
                "case_id": "x",
                "category": "exact",
                "expected_source_ids": (_message(1),),
                "forbidden_source_ids": (_message(2),),
                "expected_abstain": False,
            },
            "only privacy",
        ),
        (
            {
                "case_id": "x",
                "category": "privacy_governance",
                "expected_source_ids": (_message(1),),
                "forbidden_source_ids": (_message(1),),
                "expected_abstain": False,
            },
            "disjoint",
        ),
    ],
)
def test_case_privacy_schema_fails_closed(kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        SemanticEvalCase(**kwargs)  # type: ignore[arg-type]


def test_result_and_run_validation_fail_closed() -> None:
    cases, results = _passing_run()
    with pytest.raises(ValueError, match="category"):
        evaluate_semantic_run(cases[:-1], results[:-1])
    with pytest.raises(ValueError, match="match cases exactly"):
        evaluate_semantic_run(cases, results[:-1])
    with pytest.raises(ValueError, match="claim annotations"):
        _result("x", total_claims=0)
    with pytest.raises(ValueError, match="finite"):
        _result("x", retrieval=float("nan"))


def test_sanitized_report_validator_rejects_impossible_metrics() -> None:
    cases, results = _passing_run()
    report, _ = evaluate_semantic_run(cases, results)
    impossible = replace(report, macro_recall_at_5=1.1, unsupported_claims=2, total_claims=1)

    violations = validate_semantic_report(impossible)

    assert any("outside [0,1]" in value for value in violations)
    assert "unsupported_claims > total_claims" in violations


def test_sanitized_report_validator_rejects_unexpected_abstention_overflow() -> None:
    cases, results = _passing_run()
    report, _ = evaluate_semantic_run(cases, results)
    impossible = replace(
        report,
        unexpected_answerable_abstention_count=report.answerable_case_count + 1,
    )

    violations = validate_semantic_report(impossible)

    assert "unexpected_answerable_abstention_count=5 > 0" in violations
    assert "unexpected_answerable_abstention_count > answerable_case_count" in violations
