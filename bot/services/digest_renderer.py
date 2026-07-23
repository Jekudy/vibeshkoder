"""Render validated adaptive digests as one Telegram HTML post."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from html.parser import HTMLParser
from typing import Literal, Mapping
from zoneinfo import ZoneInfo

from bot.html_escape import html_escape

_CITATION_TOKEN_RE = re.compile(r"\[\[mv:[1-9]\d*\]\]")
_SAFE_SOURCE_URL_RE = re.compile(r"https://t\.me/c/[1-9]\d*/[1-9]\d*")
_TELEGRAM_HARD_LIMIT = 4096
WEEKLY_DIGEST_VISIBLE_TARGET = 3600
_DUMMY_SOURCE_URL = "https://t.me/c/1/1"
_MONTHS_GENITIVE = (
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


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


def _period_label(
    *,
    window_start_utc: datetime,
    window_end_utc: datetime | None,
    digest_type: Literal["daily", "weekly"],
    timezone_name: str,
) -> str:
    timezone = ZoneInfo(timezone_name)
    start = window_start_utc.astimezone(timezone).date()
    if digest_type == "daily":
        return f"{start.day} {_MONTHS_GENITIVE[start.month]}"
    if window_end_utc is None:
        raise ValueError("weekly digest requires window_end_utc")
    end = window_end_utc.astimezone(timezone).date() - timedelta(days=1)
    if start.month == end.month:
        return f"{start.day}–{end.day} {_MONTHS_GENITIVE[start.month]}"
    return f"{start.day} {_MONTHS_GENITIVE[start.month]} – {end.day} {_MONTHS_GENITIVE[end.month]}"


def _render_inline(text: str, source_links_by_citation: Mapping[str, str]) -> str:
    output: list[str] = []
    cursor = 0
    for match in _CITATION_TOKEN_RE.finditer(text):
        output.append(html_escape(text[cursor : match.start()]))
        token = match.group(0)
        url = source_links_by_citation.get(token)
        if url is None or _SAFE_SOURCE_URL_RE.fullmatch(url) is None:
            raise ValueError("digest citation has no safe source link")
        output.append(f'[<a href="{url}">↗ источник</a>]')
        cursor = match.end()
    output.append(html_escape(text[cursor:]))
    return "".join(output)


def _build_digest_html(
    body_markdown: str,
    *,
    window_start_utc: datetime,
    source_links_by_citation: Mapping[str, str],
    timezone_name: str = "Europe/Moscow",
    digest_type: Literal["daily", "weekly"] = "daily",
    window_end_utc: datetime | None = None,
    quiet: bool = False,
) -> str:
    title = "Что было в чате — " + _period_label(
        window_start_utc=window_start_utc,
        window_end_utc=window_end_utc,
        digest_type=digest_type,
        timezone_name=timezone_name,
    )
    if quiet:
        if source_links_by_citation:
            raise ValueError("quiet digest cannot contain source links")
        body_lines = [html_escape(body_markdown.strip())] if body_markdown.strip() else []
    else:
        lines = body_markdown.splitlines()
        if not lines or not any(line.startswith("- ") for line in lines):
            raise ValueError("digest requires at least one content item")
        body_tokens = {match.group(0) for match in _CITATION_TOKEN_RE.finditer(body_markdown)}
        if body_tokens != set(source_links_by_citation):
            raise ValueError("digest source links do not match body citations")
        body_lines: list[str] = []
        details: list[str] = []
        closing_seen = False
        previous_was_item = False
        for line in lines:
            if not line:
                body_lines.append("")
                previous_was_item = False
            elif line.startswith("## "):
                if closing_seen or _CITATION_TOKEN_RE.search(line):
                    raise ValueError("digest section heading is invalid")
                body_lines.append(f"<b>{html_escape(line[3:])}</b>")
                previous_was_item = False
            elif line.startswith("- "):
                if closing_seen:
                    raise ValueError("digest item follows closing")
                body_lines.append(f"- {_render_inline(line[2:], source_links_by_citation)}")
                previous_was_item = True
            elif line.startswith("  "):
                if closing_seen or not previous_was_item or _CITATION_TOKEN_RE.search(line):
                    raise ValueError("digest detail is invalid")
                details.append(line[2:])
                previous_was_item = False
            elif line.startswith("— "):
                if closing_seen:
                    raise ValueError("digest has multiple closings")
                closing_seen = True
                if details:
                    body_lines.append(
                        "<blockquote expandable><b>Подробнее</b>\n"
                        + "\n".join(f"- {html_escape(detail)}" for detail in details)
                        + "</blockquote>"
                    )
                body_lines.append(f"<i>— {_render_inline(line[2:], source_links_by_citation)}</i>")
                previous_was_item = False
            else:
                raise ValueError("digest body has an unsupported line")
        if not closing_seen:
            raise ValueError("digest requires a grounded closing")

    return f"{title}\n\n" + "\n".join(body_lines) + "\n\n#дайджест"


def render_digest_html(
    body_markdown: str,
    *,
    window_start_utc: datetime,
    source_links_by_citation: Mapping[str, str],
    timezone_name: str = "Europe/Moscow",
    digest_type: Literal["daily", "weekly"] = "daily",
    window_end_utc: datetime | None = None,
    quiet: bool = False,
) -> str:
    """Build the sole member-facing adaptive digest format."""
    rendered = _build_digest_html(
        body_markdown,
        window_start_utc=window_start_utc,
        source_links_by_citation=source_links_by_citation,
        timezone_name=timezone_name,
        digest_type=digest_type,
        window_end_utc=window_end_utc,
        quiet=quiet,
    )
    if _visible_text_length(rendered) > _TELEGRAM_HARD_LIMIT:
        raise ValueError("rendered digest exceeds Telegram message limit")
    return rendered


def measure_digest_visible_length(
    body_markdown: str,
    *,
    window_start_utc: datetime,
    digest_type: Literal["daily", "weekly"] = "daily",
    window_end_utc: datetime | None = None,
    timezone_name: str = "Europe/Moscow",
) -> int:
    """Measure Telegram-visible characters with source markup excluded."""
    tokens = {match.group(0) for match in _CITATION_TOKEN_RE.finditer(body_markdown)}
    rendered = _build_digest_html(
        body_markdown,
        window_start_utc=window_start_utc,
        source_links_by_citation={token: _DUMMY_SOURCE_URL for token in tokens},
        timezone_name=timezone_name,
        digest_type=digest_type,
        window_end_utc=window_end_utc,
        quiet=False,
    )
    return _visible_text_length(rendered)


__all__ = [
    "WEEKLY_DIGEST_VISIBLE_TARGET",
    "measure_digest_visible_length",
    "render_digest_html",
]
