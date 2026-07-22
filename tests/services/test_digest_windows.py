from datetime import datetime, timedelta, timezone

import pytest

from bot.services.digest_windows import (
    completed_daily_window,
    completed_weekly_window,
    current_daily_window,
)


@pytest.mark.parametrize(
    ("now", "current_start", "completed_start", "completed_end"),
    [
        (
            datetime(2026, 7, 21, 1, 59, tzinfo=timezone.utc),  # 04:59 MSK
            datetime(2026, 7, 20, 2, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 19, 2, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 20, 2, 0, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 7, 21, 2, 0, tzinfo=timezone.utc),  # 05:00 MSK
            datetime(2026, 7, 21, 2, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 20, 2, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 21, 2, 0, tzinfo=timezone.utc),
        ),
    ],
)
def test_daily_windows_change_exactly_at_05_msk(
    now: datetime,
    current_start: datetime,
    completed_start: datetime,
    completed_end: datetime,
) -> None:
    assert current_daily_window(now)[0] == current_start
    assert completed_daily_window(now) == (completed_start, completed_end)


@pytest.mark.parametrize(
    ("now", "expected_start", "expected_end"),
    [
        (
            datetime(2026, 7, 23, 1, 59, tzinfo=timezone.utc),  # Thu 04:59 MSK
            datetime(2026, 7, 9, 2, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 16, 2, 0, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 7, 23, 2, 0, tzinfo=timezone.utc),  # Thu 05:00 MSK
            datetime(2026, 7, 16, 2, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 23, 2, 0, tzinfo=timezone.utc),
        ),
    ],
)
def test_weekly_window_changes_exactly_at_thursday_05_msk(
    now: datetime,
    expected_start: datetime,
    expected_end: datetime,
) -> None:
    assert completed_weekly_window(now) == (expected_start, expected_end)
    assert expected_end - expected_start == timedelta(days=7)


def test_window_helpers_reject_naive_reference_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        current_daily_window(datetime(2026, 7, 21, 5, 0))
