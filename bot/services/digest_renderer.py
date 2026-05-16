"""HTML rendering for daily + weekly digests — T7-05 baseline + T8-06 §5.I.

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

Phase 8 / §5.I extension (T8-06 sub-component, FHR HIGH-5):
- New ``digest_type: Literal['daily','weekly']`` kwarg. Default ``'daily'``
  preserves Phase 7 byte-for-byte.
- ``digest_type='weekly'``:
  - Recognizes ``## Раздел: <name>`` Markdown headers and bolds them as
    ``<b>Раздел: <name></b>`` (per the weekly prompt module's
    ``SECTION_NAME_ALLOWLIST`` contract).
  - Switches the footer to "Еженедельный дайджест за {ws} – {we}." using
    the inclusive week range (we - 1 second).
- ``digest_type='daily'`` keeps the Phase 7 single-day footer and does NOT
  apply the section-header bolding (daily prompt template does not emit
  section headers; the regression-guard test in
  ``test_digest_renderer_weekly.py`` documents this).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from bot.html_escape import html_escape

_CITATION_TOKEN_RE = re.compile(r"\[\[(?:cs|mv|card):[^\]]+\]\]")
_MAX_BODY_CHARS_BEFORE_HTML = 3800  # leaves 296 chars for HTML overhead + footer
_TELEGRAM_HARD_LIMIT = 4096
_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
# Phase 8 §5.I — weekly section header pattern. Applied AFTER html_escape so
# the input is the literal escaped text "## Раздел: <name>"; we wrap the
# title in <b>…</b>. Single-line guarantee: regex ends at $ (MULTILINE) so
# no multi-line <b> spans are introduced; tag-balance assertion still passes.
_WEEKLY_SECTION_HEADER_RE = re.compile(
    r"^##\s+Раздел:\s+(.+)$", flags=re.MULTILINE
)


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
    digest_type: Literal["daily", "weekly"] = "daily",
    window_end_utc: datetime | None = None,
) -> str:
    """Render a digest body for Telegram HTML output.

    Steps:
    1. Strip citation tokens.
    2. Truncate plain Markdown.
    3. html_escape (covers LLM-emitted `<`, `>`, `&`).
    4. Convert minimal MD to HTML.
    4b. (weekly only) Bold ``## Раздел: <name>`` section headers — Phase 8
       §5.I. The substitution runs AFTER ``html_escape`` so the input pattern
       matches the escaped text; the wrapped ``<b>…</b>`` is on a single line
       (regex anchored to ``$`` with MULTILINE) so the tag-balance assertion
       in step 6 still passes.
    5. Append footer with date(s) + admin breadcrumb.
       - daily  → "Дайджест за DD.MM.YYYY."
       - weekly → "Еженедельный дайджест за DD.MM.YYYY – DD.MM.YYYY."
         using the inclusive week range (``window_end_utc - 1s``).
    6. Tag-balance assertion; strip formatting on imbalance.
    7. Hard-cap to Telegram limit.

    The output is a single string ready for ``bot.send_message(parse_mode='HTML')``.

    Args:
        body_markdown: LLM-produced body (with citation tokens stripped here).
        window_start_utc: inclusive lower bound of the digest window (UTC).
        timezone_name: target timezone for footer formatting (default MSK).
        digest_type: ``'daily'`` (Phase 7 baseline) or ``'weekly'`` (Phase 8
            §5.I extension). Default ``'daily'`` keeps the call signature
            backward-compatible.
        window_end_utc: required when ``digest_type='weekly'`` — the
            exclusive upper bound of the week. The inclusive label is
            ``window_end_utc - 1 second`` so the range reads as
            Mon..Sun rather than Mon..Mon. Optional for daily; ignored.
    """
    stripped = _strip_citation_tokens(body_markdown).strip()
    truncated = _truncate_plain_markdown(stripped, max_chars=_MAX_BODY_CHARS_BEFORE_HTML)
    escaped = html_escape(truncated)
    converted = _convert_minimal_markdown(escaped)

    # §5.I step 4b — weekly section header bolding. Daily path is byte-for-
    # byte unchanged (no substitution applied).
    if digest_type == "weekly":
        converted = _WEEKLY_SECTION_HEADER_RE.sub(
            r"<b>Раздел: \1</b>", converted
        )

    # Footer in target timezone.
    tz = ZoneInfo(timezone_name)
    window_local = window_start_utc.astimezone(tz)
    if digest_type == "weekly":
        if window_end_utc is None:
            # Defensive: weekly mode requires the end window. Fall back to
            # the daily footer so we never raise from the publisher path;
            # log via the renderer call-site if this ever fires.
            footer = (
                "\n\n<i>Дайджест за "
                f"{window_local.strftime('%d.%m.%Y')}. "
                "Полный список источников: /digest_history</i>"
            )
        else:
            # Inclusive end label: subtract 1s so Mon..Mon-exclusive reads as
            # Mon..Sun.
            end_inclusive_local = (
                window_end_utc - timedelta(seconds=1)
            ).astimezone(tz)
            footer = (
                "\n\n<i>Еженедельный дайджест за "
                f"{window_local.strftime('%d.%m.%Y')} – "
                f"{end_inclusive_local.strftime('%d.%m.%Y')}. "
                "Полный список источников: /digest_history</i>"
            )
    else:
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
