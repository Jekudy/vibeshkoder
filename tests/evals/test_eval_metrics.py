from __future__ import annotations

import pytest

from bot.services.eval_metrics import precision_at_k, recall_at_k


@pytest.mark.parametrize("k", [1, 3, 5])
def test_full_overlap_returns_one(k: int) -> None:
    expected = list(range(1, k + 1))
    returned = [*expected, 100, 101]

    assert recall_at_k(returned, expected, k) == 1.0
    assert precision_at_k(returned, expected, k) == 1.0


@pytest.mark.parametrize("k", [1, 3, 5])
def test_zero_overlap_returns_zero(k: int) -> None:
    returned = list(range(1, k + 1))
    expected = [100, 101]

    assert recall_at_k(returned, expected, k) == 0.0
    assert precision_at_k(returned, expected, k) == 0.0


@pytest.mark.parametrize(
    ("k", "expected_recall", "expected_precision"),
    [
        (1, 0.25, 1.0),
        (3, 0.5, 2 / 3),
        (5, 0.5, 0.4),
    ],
)
def test_partial_overlap(k: int, expected_recall: float, expected_precision: float) -> None:
    returned = [1, 2, 9, 10, 11]
    expected = [1, 2, 3, 4]

    assert recall_at_k(returned, expected, k) == expected_recall
    assert precision_at_k(returned, expected, k) == expected_precision


def test_empty_expected_boundary() -> None:
    assert recall_at_k([1, 2, 3], [], 3) == 0.0
    assert precision_at_k([1, 2, 3], [], 3) == 0.0


def test_empty_returned_boundary() -> None:
    assert recall_at_k([], [1, 2], 3) == 0.0
    assert precision_at_k([], [1, 2], 3) == 0.0


def test_k_greater_than_returned_length_uses_available_results() -> None:
    assert recall_at_k([1, 2], [1, 2, 3, 4], 5) == 0.5
    assert precision_at_k([1, 2], [1, 2, 3, 4], 5) == 0.4


@pytest.mark.parametrize("k", [0, -1])
def test_k_less_than_one_raises_value_error(k: int) -> None:
    with pytest.raises(ValueError, match="k must be >= 1"):
        recall_at_k([1], [1], k)

    with pytest.raises(ValueError, match="k must be >= 1"):
        precision_at_k([1], [1], k)


def test_duplicate_returned_ids_count_each_expected_element_once() -> None:
    returned = [1, 1, 2]
    expected = [1, 2]

    assert recall_at_k(returned, expected, 3) == 1.0
    assert precision_at_k(returned, expected, 3) == 2 / 3


def test_duplicate_expected_ids_do_not_increase_recall_denominator() -> None:
    returned = [1, 2]
    expected = [1, 1, 2]

    assert recall_at_k(returned, expected, 2) == 1.0
    assert precision_at_k(returned, expected, 2) == 1.0
