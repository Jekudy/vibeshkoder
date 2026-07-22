from datetime import datetime, timezone

import pytest

from bot.services.digest_renderer import render_digest_html


def test_renderer_produces_plain_navigation_header_and_safe_source_link() -> None:
    rendered = render_digest_html(
        "- Обсуждали выбор хостинга. [[mv:42]]\n\n— Хостинг выдержал обсуждение. [[mv:42]]",
        window_start_utc=datetime(2026, 7, 19, 2, tzinfo=timezone.utc),
        source_links_by_citation={"[[mv:42]]": "https://t.me/c/123456789/42"},
    )

    assert rendered == (
        "Что было в чате — 19 июля\n\n"
        '- Обсуждали выбор хостинга. [<a href="https://t.me/c/123456789/42">↗ источник</a>]\n\n'
        '<i>— Хостинг выдержал обсуждение. [<a href="https://t.me/c/123456789/42">↗ источник</a>]</i>\n\n'
        "#дайджест"
    )
    assert "<b>" not in rendered


def test_renderer_requires_one_safe_link_for_each_item() -> None:
    with pytest.raises(ValueError, match="source link"):
        render_digest_html(
            "- Обсуждали выбор хостинга. [[mv:42]]\n\n— Финал. [[mv:42]]",
            window_start_utc=datetime(2026, 7, 19, 2, tzinfo=timezone.utc),
            source_links_by_citation={"[[mv:42]]": "https://example.com/not-telegram"},
        )


def test_renderer_uses_inclusive_weekly_range() -> None:
    rendered = render_digest_html(
        "- Сравнивали варианты оплаты. [[mv:8]]\n\n— Неделя сошлась. [[mv:8]]",
        window_start_utc=datetime(2026, 7, 13, 2, tzinfo=timezone.utc),
        window_end_utc=datetime(2026, 7, 20, 2, tzinfo=timezone.utc),
        digest_type="weekly",
        source_links_by_citation={"[[mv:8]]": "https://t.me/c/123456789/8"},
    )

    assert rendered.startswith("Что было в чате — 13–19 июля\n\n")
