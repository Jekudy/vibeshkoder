from __future__ import annotations

from html import escape


def html_escape(value: str | None) -> str:
    return escape(value or "")
