from __future__ import annotations

from datetime import datetime, timezone
from html.parser import HTMLParser

import pytest

from bot.services.digest_renderer import render_digest_html


_WINDOW_START = datetime(2026, 5, 15, 2, tzinfo=timezone.utc)
_SOURCE_LINK = "https://t.me/c/123/1"


class _VisibleTextCounter(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.length = 0

    def handle_data(self, data: str) -> None:
        self.length += len(data)


def _visible_text_length(rendered_html: str) -> int:
    counter = _VisibleTextCounter()
    counter.feed(rendered_html)
    counter.close()
    return counter.length


def _body_at_visible_length(target: int, *, filler: str = "x") -> str:
    body = "- [[mv:1]]\n\n— Закрыли [[mv:1]]"
    baseline = render_digest_html(
        body,
        window_start_utc=_WINDOW_START,
        source_links_by_citation={"[[mv:1]]": _SOURCE_LINK},
    )
    return f"- {filler * (target - _visible_text_length(baseline))}[[mv:1]]\n\n— Закрыли [[mv:1]]"


def test_renderer_accepts_exactly_4096_visible_characters() -> None:
    rendered = render_digest_html(
        _body_at_visible_length(4096),
        window_start_utc=_WINDOW_START,
        source_links_by_citation={"[[mv:1]]": _SOURCE_LINK},
    )

    assert _visible_text_length(rendered) == 4096
    assert len(rendered) > 4096


def test_renderer_rejects_4097_visible_characters() -> None:
    with pytest.raises(ValueError, match="Telegram message limit"):
        render_digest_html(
            _body_at_visible_length(4097),
            window_start_utc=_WINDOW_START,
            source_links_by_citation={"[[mv:1]]": _SOURCE_LINK},
        )


def test_renderer_counts_decoded_charrefs_as_one_visible_character() -> None:
    rendered = render_digest_html(
        _body_at_visible_length(4096, filler="&"),
        window_start_utc=_WINDOW_START,
        source_links_by_citation={"[[mv:1]]": _SOURCE_LINK},
    )

    assert "&amp;" in rendered
    assert _visible_text_length(rendered) == 4096
    assert len(rendered) > 4096


def test_renderer_excludes_link_markup_and_href_from_visible_limit() -> None:
    source_link = f"https://t.me/c/{'1' * 1000}/{'2' * 1000}"
    body = _body_at_visible_length(4096)
    rendered = render_digest_html(
        body,
        window_start_utc=_WINDOW_START,
        source_links_by_citation={"[[mv:1]]": source_link},
    )

    assert _visible_text_length(rendered) == 4096
    assert source_link in rendered
    assert len(rendered) > 4096
