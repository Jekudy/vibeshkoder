from datetime import datetime, timezone

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


def test_weekly_window_is_seven_contiguous_daily_windows() -> None:
    assert completed_weekly_window(datetime(2026, 7, 20, 2, 0, tzinfo=timezone.utc)) == (
        datetime(2026, 7, 13, 2, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 20, 2, 0, tzinfo=timezone.utc),
    )


def test_window_helpers_reject_naive_reference_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        current_daily_window(datetime(2026, 7, 21, 5, 0))
