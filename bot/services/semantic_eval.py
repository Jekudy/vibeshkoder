"""Pure deterministic acceptance metrics for semantic Q&A evaluation runs."""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Literal

SemanticEvalCategory = Literal[
    "semantic",
    "exact",
    "multi_source",
    "no_answer",
    "privacy_governance",
]

MIN_MACRO_RECALL_AT_5 = 0.80
MIN_SEMANTIC_RECALL_DELTA_AT_5 = 0.15
MIN_EXACT_RECALL_DELTA_AT_5 = -0.05
MIN_NO_ANSWER_ABSTENTION_RATE = 0.90
MAX_RETRIEVAL_P95_SECONDS = 2.0
MAX_FULL_P95_SECONDS = 15.0

_CATEGORIES = frozenset({"semantic", "exact", "multi_source", "no_answer", "privacy_governance"})
_MESSAGE_SOURCE_ID_RE = re.compile(r"message:[1-9][0-9]*")
_CARD_SOURCE_ID_RE = re.compile(
    r"card:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


def _validate_case_id(case_id: object) -> None:
    if not isinstance(case_id, str) or not case_id or case_id != case_id.strip():
        raise ValueError("case_id must be a non-empty trimmed string")


def _validate_source_ids(name: str, source_ids: object) -> None:
    if not isinstance(source_ids, tuple):
        raise TypeError(f"{name} must be a tuple")
    for source_id in source_ids:
        if not isinstance(source_id, str):
            raise ValueError(f"{name} must contain canonical string source ids")
        if _MESSAGE_SOURCE_ID_RE.fullmatch(source_id):
            continue
        if _CARD_SOURCE_ID_RE.fullmatch(source_id):
            card_id = source_id.removeprefix("card:")
            if str(uuid.UUID(card_id)) == card_id:
                continue
        raise ValueError(
            f"{name} must use message:<positive-id> or card:<canonical-uuid> source ids"
        )
    if len(source_ids) != len(set(source_ids)):
        raise ValueError(f"{name} must not contain duplicate source ids")


def _validate_non_negative_int(name: str, value: object) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _validate_latency(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite non-negative number")
    if not math.isfinite(float(value)) or value < 0:
        raise ValueError(f"{name} must be a finite non-negative number")


@dataclass(frozen=True, slots=True)
class SemanticEvalCase:
    """Frozen gold label for one semantic Q&A evaluation case."""

    case_id: str
    category: SemanticEvalCategory
    expected_source_ids: tuple[str, ...]
    forbidden_source_ids: tuple[str, ...]
    expected_abstain: bool

    def __post_init__(self) -> None:
        _validate_case_id(self.case_id)
        if self.category not in _CATEGORIES:
            raise ValueError(f"unsupported semantic evaluation category: {self.category!r}")
        _validate_source_ids("expected_source_ids", self.expected_source_ids)
        _validate_source_ids("forbidden_source_ids", self.forbidden_source_ids)
        if set(self.expected_source_ids) & set(self.forbidden_source_ids):
            raise ValueError("expected and forbidden source ids must be disjoint")
        if type(self.expected_abstain) is not bool:
            raise ValueError("expected_abstain must be a boolean")
        if self.category == "no_answer":
            if not self.expected_abstain or self.expected_source_ids:
                raise ValueError(
                    "no_answer cases must expect abstention and have no expected sources"
                )
        elif self.category == "privacy_governance" and self.expected_abstain:
            if self.expected_source_ids:
                raise ValueError(
                    "abstaining privacy_governance cases must have no expected sources"
                )
        elif self.expected_abstain or not self.expected_source_ids:
            raise ValueError(
                "answerable cases must not expect abstention and must have expected sources"
            )
        if self.category == "privacy_governance":
            if not self.forbidden_source_ids:
                raise ValueError("privacy_governance cases must include forbidden source ids")
        elif self.forbidden_source_ids:
            raise ValueError("only privacy_governance cases may include forbidden source ids")


@dataclass(frozen=True, slots=True)
class SemanticEvalResult:
    """Frozen observations for one case, produced outside this pure module."""

    case_id: str
    fts_source_ids: tuple[str, ...]
    hybrid_source_ids: tuple[str, ...]
    abstained: bool
    leakage_count: int
    valid_source_links: int
    invalid_source_links: int
    unsupported_claims: int
    total_claims: int
    retrieval_latency_seconds: float
    full_latency_seconds: float

    def __post_init__(self) -> None:
        _validate_case_id(self.case_id)
        _validate_source_ids("fts_source_ids", self.fts_source_ids)
        _validate_source_ids("hybrid_source_ids", self.hybrid_source_ids)
        if type(self.abstained) is not bool:
            raise ValueError("abstained must be a boolean")
        for name in (
            "leakage_count",
            "valid_source_links",
            "invalid_source_links",
            "unsupported_claims",
            "total_claims",
        ):
            _validate_non_negative_int(name, getattr(self, name))
        if self.unsupported_claims > self.total_claims:
            raise ValueError("unsupported_claims must not exceed total_claims")
        _validate_latency("retrieval_latency_seconds", self.retrieval_latency_seconds)
        _validate_latency("full_latency_seconds", self.full_latency_seconds)
        if self.full_latency_seconds < self.retrieval_latency_seconds:
            raise ValueError("full_latency_seconds must include retrieval_latency_seconds")
        if self.abstained and any(
            (
                self.valid_source_links,
                self.invalid_source_links,
                self.unsupported_claims,
                self.total_claims,
            )
        ):
            raise ValueError("abstained results must not contain source links or claims")
        if not self.abstained and self.total_claims == 0:
            raise ValueError("non-abstained results must include claim annotations")


@dataclass(frozen=True, slots=True)
class SemanticEvalReport:
    """Aggregate #404 gate evidence.

    Recall deltas use a 0..1 fraction: ``0.15`` means 15 percentage points.
    ``invalid_source_links`` also counts each non-abstained answer that has no
    source link at all, so a missing mandatory citation cannot pass the zero gate.
    """

    case_count: int
    answerable_case_count: int
    no_answer_case_count: int
    semantic_case_count: int
    exact_case_count: int
    multi_source_case_count: int
    privacy_governance_case_count: int
    privacy_expected_abstention_case_count: int
    privacy_expected_abstention_failures: int
    unexpected_answerable_abstention_count: int
    macro_recall_at_5: float
    semantic_hybrid_minus_fts_at_5: float
    exact_hybrid_minus_fts_at_5: float
    no_answer_abstention_rate: float
    leakage_count: int
    invalid_source_links: int
    unsupported_claims: int
    total_claims: int
    unsupported_claim_rate: float
    retrieval_p95_seconds: float
    full_p95_seconds: float


def _recall_at_5(returned: tuple[str, ...], expected: tuple[str, ...]) -> Fraction:
    hits = len(set(returned[:5]) & set(expected))
    return Fraction(hits, len(expected))


def _mean(values: Sequence[Fraction]) -> Fraction:
    return sum(values, start=Fraction()) / len(values)


def _nearest_rank_p95(values: Sequence[float]) -> float:
    ordered = sorted(float(value) for value in values)
    rank = (95 * len(ordered) + 99) // 100
    return ordered[rank - 1]


def evaluate_semantic_run(
    cases: Sequence[SemanticEvalCase],
    results: Sequence[SemanticEvalResult],
) -> tuple[SemanticEvalReport, tuple[str, ...]]:
    """Compute #404 metrics and return the immutable report plus gate violations.

    The function performs no I/O and raises immediately for incomplete, duplicate,
    mismatched, or structurally invalid evaluation data.
    """

    if not cases:
        raise ValueError("cases must not be empty")
    if not results:
        raise ValueError("results must not be empty")
    if any(not isinstance(case, SemanticEvalCase) for case in cases):
        raise TypeError("cases must contain only SemanticEvalCase values")
    if any(not isinstance(result, SemanticEvalResult) for result in results):
        raise TypeError("results must contain only SemanticEvalResult values")

    cases_by_id = {case.case_id: case for case in cases}
    if len(cases_by_id) != len(cases):
        raise ValueError("case ids must be unique")
    results_by_id = {result.case_id: result for result in results}
    if len(results_by_id) != len(results):
        raise ValueError("result case ids must be unique")

    case_ids = set(cases_by_id)
    result_ids = set(results_by_id)
    if case_ids != result_ids:
        missing = sorted(case_ids - result_ids)
        unexpected = sorted(result_ids - case_ids)
        raise ValueError(
            f"results must match cases exactly; missing={missing!r}, unexpected={unexpected!r}"
        )

    ordered_pairs = [(case, results_by_id[case.case_id]) for case in cases]
    category_pairs = {
        category: [pair for pair in ordered_pairs if pair[0].category == category]
        for category in _CATEGORIES
    }
    missing_categories = sorted(category for category, pairs in category_pairs.items() if not pairs)
    if missing_categories:
        raise ValueError(
            "frozen evaluation must contain every required category; "
            f"missing={missing_categories!r}"
        )

    answerable = [pair for pair in ordered_pairs if not pair[0].expected_abstain]
    semantic = [pair for pair in answerable if pair[0].category == "semantic"]
    exact = [pair for pair in answerable if pair[0].category == "exact"]
    no_answer = category_pairs["no_answer"]
    privacy = category_pairs["privacy_governance"]
    privacy_expected_abstentions = [pair for pair in privacy if pair[0].expected_abstain]
    if not answerable:
        raise ValueError("at least one answerable case is required")
    if not semantic:
        raise ValueError("at least one answerable semantic case is required")
    if not exact:
        raise ValueError("at least one answerable exact case is required")

    hybrid_answerable = [
        _recall_at_5(result.hybrid_source_ids, case.expected_source_ids)
        for case, result in answerable
    ]
    semantic_hybrid = [
        _recall_at_5(result.hybrid_source_ids, case.expected_source_ids)
        for case, result in semantic
    ]
    semantic_fts = [
        _recall_at_5(result.fts_source_ids, case.expected_source_ids) for case, result in semantic
    ]
    exact_hybrid = [
        _recall_at_5(result.hybrid_source_ids, case.expected_source_ids) for case, result in exact
    ]
    exact_fts = [
        _recall_at_5(result.fts_source_ids, case.expected_source_ids) for case, result in exact
    ]

    macro_recall = _mean(hybrid_answerable)
    semantic_delta = _mean(semantic_hybrid) - _mean(semantic_fts)
    exact_delta = _mean(exact_hybrid) - _mean(exact_fts)
    abstention_rate = Fraction(sum(result.abstained for _, result in no_answer), len(no_answer))
    privacy_expected_abstention_failures = sum(
        not result.abstained for _, result in privacy_expected_abstentions
    )
    unexpected_answerable_abstention_count = sum(result.abstained for _, result in answerable)
    computed_leakage_counts = [
        len(set(result.hybrid_source_ids) & set(case.forbidden_source_ids))
        for case, result in ordered_pairs
    ]
    for (case, result), computed_count in zip(ordered_pairs, computed_leakage_counts, strict=True):
        if result.leakage_count != computed_count:
            raise ValueError(
                "reviewed leakage_count must equal the forbidden-source intersection; "
                f"case_id={case.case_id!r}"
            )
    leakage_count = sum(computed_leakage_counts)
    invalid_source_links = sum(result.invalid_source_links for result in results)
    invalid_source_links += sum(
        not result.abstained and result.valid_source_links == 0 and result.invalid_source_links == 0
        for result in results
    )
    unsupported_claims = sum(result.unsupported_claims for result in results)
    total_claims = sum(result.total_claims for result in results)

    report = SemanticEvalReport(
        case_count=len(cases),
        answerable_case_count=len(answerable),
        no_answer_case_count=len(no_answer),
        semantic_case_count=len(category_pairs["semantic"]),
        exact_case_count=len(category_pairs["exact"]),
        multi_source_case_count=len(category_pairs["multi_source"]),
        privacy_governance_case_count=len(privacy),
        privacy_expected_abstention_case_count=len(privacy_expected_abstentions),
        privacy_expected_abstention_failures=privacy_expected_abstention_failures,
        unexpected_answerable_abstention_count=unexpected_answerable_abstention_count,
        macro_recall_at_5=float(macro_recall),
        semantic_hybrid_minus_fts_at_5=float(semantic_delta),
        exact_hybrid_minus_fts_at_5=float(exact_delta),
        no_answer_abstention_rate=float(abstention_rate),
        leakage_count=leakage_count,
        invalid_source_links=invalid_source_links,
        unsupported_claims=unsupported_claims,
        total_claims=total_claims,
        unsupported_claim_rate=(unsupported_claims / total_claims if total_claims else 0.0),
        retrieval_p95_seconds=_nearest_rank_p95(
            [result.retrieval_latency_seconds for result in results]
        ),
        full_p95_seconds=_nearest_rank_p95([result.full_latency_seconds for result in results]),
    )

    violations = list(validate_semantic_report(report))
    return report, tuple(violations)


def validate_semantic_report(report: SemanticEvalReport) -> tuple[str, ...]:
    """Re-check a sanitized aggregate report at the CI trust boundary."""

    if not isinstance(report, SemanticEvalReport):
        raise TypeError("report must be a SemanticEvalReport")
    violations: list[str] = []
    for name in (
        "macro_recall_at_5",
        "no_answer_abstention_rate",
        "unsupported_claim_rate",
    ):
        value = getattr(report, name)
        if not 0 <= value <= 1:
            violations.append(f"{name}={value:.6f} outside [0,1]")
    for name in (
        "semantic_hybrid_minus_fts_at_5",
        "exact_hybrid_minus_fts_at_5",
    ):
        value = getattr(report, name)
        if not -1 <= value <= 1:
            violations.append(f"{name}={value:.6f} outside [-1,1]")
    if report.retrieval_p95_seconds < 0:
        violations.append("retrieval_p95_seconds < 0")
    if report.full_p95_seconds < report.retrieval_p95_seconds:
        violations.append("full_p95_seconds < retrieval_p95_seconds")
    category_total = (
        report.semantic_case_count
        + report.exact_case_count
        + report.multi_source_case_count
        + report.no_answer_case_count
        + report.privacy_governance_case_count
    )
    if category_total != report.case_count:
        violations.append(f"category_case_count={category_total} != case_count={report.case_count}")
    for name in (
        "semantic_case_count",
        "exact_case_count",
        "multi_source_case_count",
        "no_answer_case_count",
        "privacy_governance_case_count",
    ):
        if getattr(report, name) < 1:
            violations.append(f"{name}=0")
    expected_partition = (
        report.answerable_case_count
        + report.no_answer_case_count
        + report.privacy_expected_abstention_case_count
    )
    if expected_partition != report.case_count:
        violations.append(
            f"expected_partition_case_count={expected_partition} != case_count={report.case_count}"
        )
    if report.privacy_expected_abstention_failures:
        violations.append(
            "privacy_expected_abstention_failures="
            f"{report.privacy_expected_abstention_failures} > 0"
        )
    if report.privacy_expected_abstention_case_count > report.privacy_governance_case_count:
        violations.append("privacy_expected_abstention_case_count > privacy_governance_case_count")
    if report.privacy_expected_abstention_failures > report.privacy_expected_abstention_case_count:
        violations.append(
            "privacy_expected_abstention_failures > privacy_expected_abstention_case_count"
        )
    if report.unexpected_answerable_abstention_count:
        violations.append(
            "unexpected_answerable_abstention_count="
            f"{report.unexpected_answerable_abstention_count} > 0"
        )
    if report.unexpected_answerable_abstention_count > report.answerable_case_count:
        violations.append("unexpected_answerable_abstention_count > answerable_case_count")
    if report.unsupported_claims > report.total_claims:
        violations.append("unsupported_claims > total_claims")
    if report.macro_recall_at_5 < MIN_MACRO_RECALL_AT_5:
        violations.append(
            f"macro_recall_at_5={report.macro_recall_at_5:.6f} < {MIN_MACRO_RECALL_AT_5:.2f}"
        )
    if report.semantic_hybrid_minus_fts_at_5 < MIN_SEMANTIC_RECALL_DELTA_AT_5:
        violations.append(
            "semantic_hybrid_minus_fts_at_5="
            f"{report.semantic_hybrid_minus_fts_at_5:.6f} < "
            f"{MIN_SEMANTIC_RECALL_DELTA_AT_5:.2f}"
        )
    if report.exact_hybrid_minus_fts_at_5 < MIN_EXACT_RECALL_DELTA_AT_5:
        violations.append(
            f"exact_hybrid_minus_fts_at_5={report.exact_hybrid_minus_fts_at_5:.6f} < "
            f"{MIN_EXACT_RECALL_DELTA_AT_5:.2f}"
        )
    if report.no_answer_abstention_rate < MIN_NO_ANSWER_ABSTENTION_RATE:
        violations.append(
            f"no_answer_abstention_rate={report.no_answer_abstention_rate:.6f} < "
            f"{MIN_NO_ANSWER_ABSTENTION_RATE:.2f}"
        )
    if report.leakage_count:
        violations.append(f"leakage_count={report.leakage_count} > 0")
    if report.invalid_source_links:
        violations.append(f"invalid_source_links={report.invalid_source_links} > 0")
    if report.unsupported_claims:
        violations.append(f"unsupported_claims={report.unsupported_claims} > 0")
    if report.unsupported_claim_rate != 0:
        violations.append(f"unsupported_claim_rate={report.unsupported_claim_rate:.6f} != 0")
    if report.retrieval_p95_seconds > MAX_RETRIEVAL_P95_SECONDS:
        violations.append(
            f"retrieval_p95_seconds={report.retrieval_p95_seconds:.6f} > "
            f"{MAX_RETRIEVAL_P95_SECONDS:.1f}"
        )
    if report.full_p95_seconds > MAX_FULL_P95_SECONDS:
        violations.append(
            f"full_p95_seconds={report.full_p95_seconds:.6f} > {MAX_FULL_P95_SECONDS:.1f}"
        )

    return tuple(violations)


__all__ = [
    "MAX_FULL_P95_SECONDS",
    "MAX_RETRIEVAL_P95_SECONDS",
    "MIN_EXACT_RECALL_DELTA_AT_5",
    "MIN_MACRO_RECALL_AT_5",
    "MIN_NO_ANSWER_ABSTENTION_RATE",
    "MIN_SEMANTIC_RECALL_DELTA_AT_5",
    "SemanticEvalCase",
    "SemanticEvalCategory",
    "SemanticEvalReport",
    "SemanticEvalResult",
    "evaluate_semantic_run",
    "validate_semantic_report",
]
