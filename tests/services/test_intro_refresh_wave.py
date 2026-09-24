from __future__ import annotations

from datetime import datetime, timezone

from bot.services.intro_refresh_wave import (
    calendar_months_before,
    split_expandable_template,
    wave_from_token,
    wave_started_at,
    wave_token,
)


def test_shared_wave_dates_and_five_calendar_month_cutoffs() -> None:
    march = wave_started_at(datetime(2026, 3, 1, 12, tzinfo=timezone.utc))
    september = wave_started_at(datetime(2026, 9, 1, 7, tzinfo=timezone.utc))

    assert march == datetime(2026, 3, 1, 7, tzinfo=timezone.utc)
    assert september == datetime(2026, 9, 1, 7, tzinfo=timezone.utc)
    assert calendar_months_before(march, 5) == datetime(2025, 10, 1, 7, tzinfo=timezone.utc)
    assert calendar_months_before(september, 5) == datetime(2026, 4, 1, 7, tzinfo=timezone.utc)
    assert wave_started_at(datetime(2026, 9, 2, 7, tzinfo=timezone.utc)) is None


def test_calendar_months_before_clamps_to_the_real_month_end() -> None:
    assert calendar_months_before(datetime(2024, 3, 31, tzinfo=timezone.utc), 1) == datetime(
        2024, 2, 29, tzinfo=timezone.utc
    )


def test_wave_token_round_trip() -> None:
    wave = datetime(2026, 9, 1, 7, tzinfo=timezone.utc)
    assert wave_from_token(wave_token(wave)) == wave


def test_expandable_intro_keeps_short_markup_and_splits_long_legacy_text() -> None:
    template = "<b>Заголовок</b>\n<blockquote expandable>{intro_text}</blockquote>"

    assert split_expandable_template(template, "<b>Имя:</b> Катя") == [
        template.format(intro_text="<b>Имя:</b> Катя")
    ]

    parts = split_expandable_template(template, ("<b>Строка:</b> " + "x" * 200 + "\n") * 30)
    assert len(parts) > 1
    assert all(len(part) <= 4096 for part in parts)
    assert all("<blockquote expandable>" in part for part in parts)
