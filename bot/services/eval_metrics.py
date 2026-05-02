"""Deterministic offline evaluation metrics for Phase 11."""

from __future__ import annotations


def recall_at_k(returned: list[int], expected: list[int], k: int) -> float:
    """Return recall@K for returned message_version_ids.

    The metric is ``|returned[:k] intersection expected| / |expected|``.
    ``expected == []`` is undefined for recall, so this function returns ``0.0``.
    Both inputs are treated as sets for matching, so duplicate ids in either list
    do not double-count a hit or increase the expected denominator.
    """
    _validate_k(k)
    expected_ids = set(expected)
    if not expected_ids:
        return 0.0

    hits = len(set(returned[:k]) & expected_ids)
    return hits / len(expected_ids)


def precision_at_k(returned: list[int], expected: list[int], k: int) -> float:
    """Return precision@K for returned message_version_ids.

    The metric is ``|returned[:k] intersection expected| / k``. Inputs are treated
    as sets for matching, so duplicate ids in either list do not double-count a hit.
    """
    _validate_k(k)
    hits = len(set(returned[:k]) & set(expected))
    return hits / k


def _validate_k(k: int) -> None:
    if k < 1:
        raise ValueError("k must be >= 1")
