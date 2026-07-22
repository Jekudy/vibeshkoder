"""Canonical 05:00 Europe/Moscow digest windows."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
_BOUNDARY = time(hour=5)


def _require_aware(now: datetime | None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ValueError("digest window reference time must be timezone-aware")
    return value.astimezone(MSK)


def current_daily_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Return the active ``[05:00, next 05:00)`` Moscow window in UTC."""
    local = _require_aware(now)
    boundary_date = local.date()
    if local.timetz().replace(tzinfo=None) < _BOUNDARY:
        boundary_date -= timedelta(days=1)
    start_local = datetime.combine(boundary_date, _BOUNDARY, tzinfo=MSK)
    return start_local.astimezone(timezone.utc), (start_local + timedelta(days=1)).astimezone(
        timezone.utc
    )


def completed_daily_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Return the most recently completed Moscow daily window in UTC."""
    active_start, _ = current_daily_window(now)
    return active_start - timedelta(days=1), active_start


def completed_weekly_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Return seven completed daily windows, Monday 05:00 to Monday 05:00."""
    active_start_utc, _ = current_daily_window(now)
    active_start_local = active_start_utc.astimezone(MSK)
    current_week_start = active_start_local - timedelta(days=active_start_local.weekday())
    return (
        (current_week_start - timedelta(days=7)).astimezone(timezone.utc),
        current_week_start.astimezone(timezone.utc),
    )


__all__ = ["MSK", "completed_daily_window", "completed_weekly_window", "current_daily_window"]
