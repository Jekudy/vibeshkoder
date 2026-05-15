"""HTML rendering for daily digests — T7-05.

Converts the LLM-produced Markdown body (with `[[cs:UUID]]` / `[[mv:INT]]`
citation tokens) into Telegram HTML for the publisher. Citation tokens are
STRIPPED from the public output per ratified decision Q6 — operators see
citation audit details via `/digest_preview` admin handler instead.

Key invariants (per PHASE7_PLAN.md §5.G):
- Truncate plain Markdown BEFORE escape+convert (avoid mid-tag cuts).
- Strip citation tokens early.
- Escape via stdlib `html.escape` (defense vs LLM-emitted angle-brackets).
- Convert minimal Markdown (`**bold**`, `*italic*`, bullet prefixes).
- Append footer with date + admin breadcrumb.
- Tag-balance assertion: if `<b>` / `<i>` open/close counts diverge after
  conversion, strip ALL formatting and fall back to plain-escaped text
  (better dumb-but-valid than malformed-and-silently-dropped).
"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from bot.html_escape import html_escape

_CITATION_TOKEN_RE = re.compile(r"\[\[(?:cs|mv|card):[^\]]+\]\]")
_MAX_BODY_CHARS_BEFORE_HTML = 3800  # leaves 296 chars for HTML overhead + footer
_TELEGRAM_HARD_LIMIT = 4096
_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")


def _strip_citation_tokens(body: str) -> str:
    """Remove all citation tokens from the body."""
    return _CITATION_TOKEN_RE.sub("", body)


def _truncate_plain_markdown(body: str, *, max_chars: int) -> str:
    """Truncate ``body`` at the last paragraph boundary before ``max_chars``.

    Operates on plain Markdown BEFORE escape+convert so we never cut inside
    an open `<b>` / `<i>` tag.
    """
    if len(body) <= max_chars:
        return body
    cut = body.rfind("\n\n", 0, max_chars)
    if cut <= 0:
        cut = body.rfind("\n", 0, max_chars)
    if cut <= 0:
        cut = max_chars
    return body[:cut].rstrip() + "\n\n..."


def _convert_minimal_markdown(escaped: str) -> str:
    """Convert escaped text into Telegram HTML.

    Only handles `**bold**` → `<b>...</b>`, `*italic*` → `<i>...</i>`,
    and `- ` / `• ` bullet prefixes (already-present `•` is preserved).
    """
    # Bold first so `*` doesn't get swallowed by italic.
    out = _BOLD_RE.sub(r"<b>\1</b>", escaped)
    out = _ITALIC_RE.sub(r"<i>\1</i>", out)

    # Normalize bullet prefix at line start (Markdown `- ` → bullet character).
    out_lines = []
    for line in out.splitlines():
        if line.startswith("- "):
            out_lines.append("• " + line[2:])
        else:
            out_lines.append(line)
    return "\n".join(out_lines)


def _tag_balance_ok(html: str) -> bool:
    open_b = html.count("<b>")
    close_b = html.count("</b>")
    open_i = html.count("<i>")
    close_i = html.count("</i>")
    return open_b == close_b and open_i == close_i


def _strip_all_formatting(html: str) -> str:
    """Last-resort: strip ALL <b>/<i> tags. Returns valid plain-HTML."""
    html = html.replace("<b>", "").replace("</b>", "")
    html = html.replace("<i>", "").replace("</i>", "")
    return html


def render_digest_html(
    body_markdown: str,
    *,
    window_start_utc: datetime,
    timezone_name: str = "Europe/Moscow",
) -> str:
    """Render a digest body for Telegram HTML output.

    Steps:
    1. Strip citation tokens.
    2. Truncate plain Markdown.
    3. html_escape (covers LLM-emitted `<`, `>`, `&`).
    4. Convert minimal MD to HTML.
    5. Append footer with date + admin breadcrumb.
    6. Tag-balance assertion; strip formatting on imbalance.
    7. Hard-cap to Telegram limit.

    The output is a single string ready for `bot.send_message(parse_mode='HTML')`.
    """
    stripped = _strip_citation_tokens(body_markdown).strip()
    truncated = _truncate_plain_markdown(stripped, max_chars=_MAX_BODY_CHARS_BEFORE_HTML)
    escaped = html_escape(truncated)
    converted = _convert_minimal_markdown(escaped)

    # Footer in target timezone.
    tz = ZoneInfo(timezone_name)
    window_local = window_start_utc.astimezone(tz)
    footer = (
        "\n\n<i>Дайджест за "
        f"{window_local.strftime('%d.%m.%Y')}. "
        "Полный список источников: /digest_history</i>"
    )

    body_with_footer = converted + footer

    if not _tag_balance_ok(body_with_footer):
        body_with_footer = _strip_all_formatting(body_with_footer)
        body_with_footer = body_with_footer + "\n\n[!] tag-balance fallback applied"

    if len(body_with_footer) > _TELEGRAM_HARD_LIMIT:
        body_with_footer = body_with_footer[: _TELEGRAM_HARD_LIMIT - 3] + "..."

    return body_with_footer


__all__ = ["render_digest_html"]
