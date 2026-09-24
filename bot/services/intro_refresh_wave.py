from __future__ import annotations

from calendar import monthrange
from datetime import datetime, timezone
from html import escape
from html.parser import HTMLParser
from zoneinfo import ZoneInfo


MOSCOW = ZoneInfo("Europe/Moscow")
WAVE_MONTHS = (3, 9)


class _PlainTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def wave_started_at(now: datetime) -> datetime | None:
    """Return this shared wave's canonical UTC timestamp on a wave date."""
    local = now.astimezone(MOSCOW)
    if local.day != 1 or local.month not in WAVE_MONTHS:
        return None
    return local.replace(hour=10, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def calendar_months_before(value: datetime, months: int) -> datetime:
    month_index = value.year * 12 + value.month - 1 - months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    day = min(value.day, monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def wave_token(value: datetime) -> str:
    return value.astimezone(MOSCOW).strftime("%Y%m%d")


def wave_from_token(value: str) -> datetime:
    local = datetime.strptime(value, "%Y%m%d").replace(tzinfo=MOSCOW, hour=10)
    if local.day != 1 or local.month not in WAVE_MONTHS:
        raise ValueError("Unknown intro refresh wave")
    return local.astimezone(timezone.utc)


def split_expandable_template(template: str, intro_html: str, limit: int = 4096) -> list[str]:
    """Split a folded intro only when Telegram cannot carry it in one message."""
    rendered = template.format(intro_text=intro_html)
    if len(rendered) <= limit:
        return [rendered]

    opening = "<blockquote expandable>"
    closing = "</blockquote>"
    before, after = template.split("{intro_text}")
    if not before.endswith(opening) or not after.startswith(closing):
        raise ValueError("Template must wrap intro_text in an expandable blockquote")
    header = before[: -len(opening)]
    footer = after[len(closing) :]
    chunk_limit = limit - len(opening) - len(closing) - max(len(header), len(footer))
    if chunk_limit <= 0:
        raise ValueError("Template leaves no room for intro text")

    parser = _PlainTextParser()
    parser.feed(intro_html)
    parser.close()

    chunks: list[str] = []
    current = ""
    for line in "".join(parser.parts).splitlines(keepends=True) or [""]:
        while len(line) > chunk_limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:chunk_limit])
            line = line[chunk_limit:]
        if current and len(current) + len(line) > chunk_limit:
            chunks.append(current)
            current = ""
        current += line
    if current or not chunks:
        chunks.append(current)

    return [
        f"{header if index == 0 else ''}{opening}{escape(chunk, quote=True)}{closing}"
        f"{footer if index == len(chunks) - 1 else ''}"
        for index, chunk in enumerate(chunks)
    ]
