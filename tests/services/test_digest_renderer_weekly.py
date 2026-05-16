"""Tests for FHR HIGH-5 — Phase 8 / §5.I weekly renderer extension.

Behaviour delta from Phase 7 baseline:

- ``render_digest_html`` accepts a new ``digest_type: Literal['daily','weekly']``
  kwarg. Default ``'daily'`` keeps the Phase 7 output byte-for-byte preserved.
- When ``digest_type='weekly'``:
  1. Section headers ``## Раздел: <name>`` (per ``digest_weekly_v0_1_0``
     prompt module) are bolded as ``<b>Раздел: <name></b>``. Without the
     extension, the renderer html-escapes them and admins / community see
     literal ``## Раздел: Объявления`` text in the published digest.
  2. The footer switches from the daily single-day format to
     "Еженедельный дайджест за {ws} – {we}." using a week-range label.

PHASE8_PLAN.md §5.I — T8-06 sub-component.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.usefixtures("app_env")


# ── 1. Weekly: section headers bolded ────────────────────────────────────────


def test_renderer_weekly_section_headers_bolded():
    """``## Раздел: Объявления`` must become ``<b>Раздел: Объявления</b>``
    when ``digest_type='weekly'`` — NOT a literal ``## Раздел: …`` line in
    the output."""
    from bot.services.digest_renderer import render_digest_html

    body = (
        "TL;DR недельный итог.\n"
        "\n"
        "## Раздел: Объявления\n"
        "- Анонс собрания пятница\n"
        "\n"
        "## Раздел: Обсуждения\n"
        "- Дискуссия про X\n"
    )
    # window_start = Mon 2026-05-11 00:00 MSK = Sun 2026-05-10 21:00 UTC
    ws = datetime(2026, 5, 10, 21, 0, 0, tzinfo=timezone.utc)
    we = datetime(2026, 5, 17, 21, 0, 0, tzinfo=timezone.utc)
    out = render_digest_html(
        body, window_start_utc=ws, window_end_utc=we, digest_type="weekly"
    )
    assert "<b>Раздел: Объявления</b>" in out
    assert "<b>Раздел: Обсуждения</b>" in out
    # And NOT the literal ## header (admins must not see raw markdown).
    assert "## Раздел:" not in out
    # And NOT escaped markdown (e.g. "## " escaped to "##" is still wrong UX).
    assert "##" not in out


# ── 2. Weekly: footer shows week range ────────────────────────────────────


def test_renderer_weekly_footer_shows_week_range():
    """Weekly footer says
    'Еженедельный дайджест за DD.MM.YYYY – DD.MM.YYYY' instead of the daily
    'Дайджест за DD.MM.YYYY' single-day format."""
    from bot.services.digest_renderer import render_digest_html

    body = "TL;DR.\n\n## Раздел: Прочее\n- One bullet\n"
    # ISO week: Mon 2026-05-11 00:00 MSK..Mon 2026-05-18 00:00 MSK
    ws = datetime(2026, 5, 10, 21, 0, 0, tzinfo=timezone.utc)
    we = datetime(2026, 5, 17, 21, 0, 0, tzinfo=timezone.utc)
    out = render_digest_html(
        body, window_start_utc=ws, window_end_utc=we, digest_type="weekly"
    )
    # Spec §5.I step 2 — display range is ws..(we - 1s) so the inclusive
    # range reads as 11.05.2026 – 17.05.2026 (Sun is the last day of the
    # week, not Mon of the next week).
    assert "Еженедельный дайджест за 11.05.2026 – 17.05.2026" in out
    assert "/digest_history" in out
    # Single-day daily footer text MUST be absent.
    assert "Дайджест за 11.05.2026." not in out


# ── 3. Daily: Phase 7 byte-for-byte preserved ─────────────────────────────


def test_renderer_daily_unchanged_default_signature():
    """Default ``digest_type='daily'`` (and the old single-arg call) preserves
    the Phase 7 daily output byte-for-byte. The renderer extension MUST NOT
    change the daily path."""
    from bot.services.digest_renderer import render_digest_html

    body = "TL;DR заголовок.\n\n- Один пункт **bold** *italic*\n"
    ws = datetime(2026, 5, 14, 21, 0, 0, tzinfo=timezone.utc)  # 15.05.2026 MSK

    # Old Phase 7 call (no digest_type kwarg) — must still work and produce
    # the daily footer.
    out_old = render_digest_html(body, window_start_utc=ws)
    assert "Дайджест за 15.05.2026" in out_old
    assert "Еженедельный" not in out_old
    assert "<b>bold</b>" in out_old
    assert "<i>italic</i>" in out_old

    # Explicit digest_type='daily' must match byte-for-byte.
    out_explicit = render_digest_html(
        body, window_start_utc=ws, digest_type="daily"
    )
    assert out_old == out_explicit


def test_renderer_daily_does_not_bold_section_headers():
    """Regression guard: even if a daily body happens to contain
    ``## Раздел: …`` text, daily mode MUST html-escape it as plain
    text (Phase 7 baseline — no Markdown header recognition for daily).

    Daily prompt template does NOT emit section headers; this test confirms
    the renderer doesn't accidentally apply the weekly transformation to
    the daily path.
    """
    from bot.services.digest_renderer import render_digest_html

    body = "TL;DR.\n\n## Раздел: Прочее\n- One bullet\n"
    ws = datetime(2026, 5, 14, 21, 0, 0, tzinfo=timezone.utc)
    out = render_digest_html(body, window_start_utc=ws)
    # Daily: section headers are NOT bolded. The bare ## is html-escape
    # safe (no special chars) so the literal text passes through.
    assert "<b>Раздел: Прочее</b>" not in out


# ── 4. Tag balance preserved with weekly section headers ─────────────────


def test_renderer_weekly_keeps_tag_balance_after_section_bolding():
    """Section bolding regex MUST close every ``<b>`` it opens on the same
    line (PHASE8_PLAN.md §5.I step 4). After conversion, the tag-balance
    assertion must pass — no fallback to plain text."""
    from bot.services.digest_renderer import render_digest_html

    body = (
        "TL;DR.\n\n"
        "## Раздел: Знания и ресурсы\n"
        "- Книга **Foo** [[mv:1]]\n"
        "\n"
        "## Раздел: Прочее\n"
        "- Misc *thing*\n"
    )
    ws = datetime(2026, 5, 10, 21, 0, 0, tzinfo=timezone.utc)
    we = datetime(2026, 5, 17, 21, 0, 0, tzinfo=timezone.utc)
    out = render_digest_html(
        body, window_start_utc=ws, window_end_utc=we, digest_type="weekly"
    )
    # If tag balance failed, fallback marker appears. It must NOT.
    assert "tag-balance fallback applied" not in out
    # Both section heads, bold, italic all converted cleanly.
    assert out.count("<b>") == out.count("</b>")
    assert out.count("<i>") == out.count("</i>")
