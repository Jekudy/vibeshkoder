"""T6-07: card-evidence rendering tests for /recall (both Phase 4 fallback
and Phase 5 synth modes).

These tests run pure renderer functions — no DB, no LLM — so they exercise
the dual-branch logic in ``bot/handlers/qa.py::_format_response`` and
``_format_synthesized_response``. The Phase 4 byte-for-byte preservation guard
lives in ``test_qa_recall_phase4_preserved.py``; this module adds card cases.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from tests.conftest import import_module

pytestmark = pytest.mark.usefixtures("app_env")

COMMUNITY_CHAT_ID = -1001234567890


def _now() -> datetime:
    return datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)


def _message_item(mvid: int, user_id: int = 2002):
    evidence = import_module("bot.services.evidence")
    return evidence.EvidenceItem(
        message_version_id=mvid,
        chat_message_id=50,
        chat_id=COMMUNITY_CHAT_ID,
        message_id=77,
        user_id=user_id,
        snippet="обсуждали <b>память</b>",
        ts_rank=0.8,
        captured_at=_now(),
        message_date=_now(),
    )


def _card_item(
    *,
    card_id: uuid.UUID | None = None,
    mvids: tuple[int, ...] = (42, 43, 44),
    anchor_mvid: int = 42,
    anchor_message_id: int = 99,
    snippet: str = "Карточка про <b>память</b>",
):
    evidence = import_module("bot.services.evidence")
    return evidence.EvidenceItem(
        message_version_id=anchor_mvid,
        chat_message_id=80,
        chat_id=COMMUNITY_CHAT_ID,
        message_id=anchor_message_id,
        user_id=None,
        snippet=snippet,
        ts_rank=0.9,
        captured_at=_now(),
        message_date=_now(),
        source_type="card",
        card_id=card_id or uuid.UUID("11111111-2222-3333-4444-555555555555"),
        card_source_message_version_ids=mvids,
    )


def _bundle(items, *, abstained: bool = False):
    evidence = import_module("bot.services.evidence")
    return evidence.EvidenceBundle(
        query="память",
        chat_id=COMMUNITY_CHAT_ID,
        items=tuple(items),
        abstained=abstained,
        created_at=_now(),
    )


# ─── _format_response card branch ─────────────────────────────────────────────


def test_format_response_pure_card_bundle_renders_card_markers() -> None:
    item = _card_item()
    qa_handler = import_module("bot.handlers.qa")
    text = qa_handler._format_response(_bundle((item,)), users_by_id={})

    assert "Найденные свидетельства" in text
    assert "Карточка" in text  # T6-07 card marker (📋 emoji prefix)
    assert "первоисточник" in text  # anchor source link label
    assert "card_id:11111111-2222-3333-4444-555555555555" in text
    assert "sources:[42, 43, 44]" in text


def test_format_response_pure_card_bundle_links_to_anchor_message() -> None:
    item = _card_item(anchor_message_id=99)
    qa_handler = import_module("bot.handlers.qa")
    text = qa_handler._format_response(_bundle((item,)), users_by_id={})

    # COMMUNITY_CHAT_ID -1001234567890 → short prefix 1234567890
    assert "https://t.me/c/1234567890/99" in text


def test_format_response_mixed_bundle_renders_both_branches_in_order() -> None:
    """Card first, then message; renderer preserves bundle order."""
    bundle = _bundle((_card_item(), _message_item(mvid=500)))
    user = type("U", (), {"first_name": "Author", "last_name": None, "username": None})()
    qa_handler = import_module("bot.handlers.qa")
    text = qa_handler._format_response(bundle, users_by_id={2002: user})

    card_idx = text.index("Карточка")
    msg_idx = text.index("message_version_id:500")
    assert card_idx < msg_idx
    # Both markers appear.
    assert "card_id:" in text
    assert "Author" in text


def test_format_response_pure_message_bundle_does_not_emit_card_markers() -> None:
    """Phase 4 path remains untouched (byte-for-byte guarded elsewhere)."""
    user = type("U", (), {"first_name": "Author", "last_name": None, "username": None})()
    qa_handler = import_module("bot.handlers.qa")
    text = qa_handler._format_response(
        _bundle((_message_item(mvid=500),)),
        users_by_id={2002: user},
    )

    assert "card_id" not in text
    assert "Карточка" not in text
    assert "первоисточник" not in text
    assert "message_version_id:500" in text


def test_format_response_card_with_empty_source_list_renders_safely() -> None:
    """Defensive: empty card_source_message_version_ids must not crash."""
    item = _card_item(mvids=())
    qa_handler = import_module("bot.handlers.qa")
    text = qa_handler._format_response(_bundle((item,)), users_by_id={})

    assert "sources:[]" in text


def test_format_response_card_snippet_html_escapes_unsafe_characters() -> None:
    """The renderer must not allow raw HTML / unsafe characters in the snippet
    to escape the <blockquote> region. _safe_headline preserves <b>/</b> from
    ts_headline but escapes everything else."""
    item = _card_item(snippet="<script>alert(1)</script>")
    qa_handler = import_module("bot.handlers.qa")
    text = qa_handler._format_response(_bundle((item,)), users_by_id={})

    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;" in text


# ─── _format_synthesized_response card branch ─────────────────────────────────


def test_synth_response_pure_card_bundle_emits_card_footer() -> None:
    """Synthesis mode renders cards in the Источники footer."""
    llm_gateway = import_module("bot.services.llm_gateway")
    AnswerWithCitations = llm_gateway.AnswerWithCitations

    answer = AnswerWithCitations(
        answer_text="Ответ",
        citation_ids=(),
        cost_usd=Decimal("0"),
        cache_hit=False,
        llm_call_id=1,
    )
    item = _card_item()
    qa_handler = import_module("bot.handlers.qa")
    text = qa_handler._format_synthesized_response(answer, _bundle((item,)), users_by_id={})

    assert "Источники" in text
    assert "Card" in text or "Карточка" in text  # discriminator label
    assert "[1]" in text
    assert "11111111-2222-3333-4444-555555555555" in text
    assert "(sources: 3)" in text


def test_synth_response_mixed_bundle_numbers_continuously() -> None:
    """Mixed bundle: [1] = card, [2] = message; numbering is continuous."""
    llm_gateway = import_module("bot.services.llm_gateway")
    AnswerWithCitations = llm_gateway.AnswerWithCitations

    answer = AnswerWithCitations(
        answer_text="Ответ",
        citation_ids=(),
        cost_usd=Decimal("0"),
        cache_hit=False,
        llm_call_id=1,
    )
    user = type("U", (), {"first_name": "Author", "last_name": None, "username": None})()
    bundle = _bundle((_card_item(), _message_item(mvid=500)))
    qa_handler = import_module("bot.handlers.qa")
    text = qa_handler._format_synthesized_response(
        answer, bundle, users_by_id={2002: user}
    )

    # [1] is the card; [2] is the message.
    one_idx = text.index("[1]")
    two_idx = text.index("[2]")
    assert one_idx < two_idx
    card_marker_pos = text.index("Card") if "Card" in text else text.index("Карточка")
    assert one_idx < card_marker_pos < two_idx
    assert "Author" in text[two_idx:]


def test_synth_response_pure_message_bundle_does_not_emit_card_marker() -> None:
    """Phase 5 path remains untouched."""
    llm_gateway = import_module("bot.services.llm_gateway")
    AnswerWithCitations = llm_gateway.AnswerWithCitations

    answer = AnswerWithCitations(
        answer_text="Ответ",
        citation_ids=(),
        cost_usd=Decimal("0"),
        cache_hit=False,
        llm_call_id=1,
    )
    user = type("U", (), {"first_name": "Author", "last_name": None, "username": None})()
    qa_handler = import_module("bot.handlers.qa")
    text = qa_handler._format_synthesized_response(
        answer, _bundle((_message_item(mvid=500),)), users_by_id={2002: user}
    )

    assert "Card" not in text
    assert "Карточка" not in text
    assert "card_id" not in text
    assert "[1] " in text  # Phase 5 marker preserved
    assert "Author" in text


def test_synth_response_answer_text_is_html_escaped() -> None:
    """Phase 5 invariant preserved: answer_text gets HTML-escaped."""
    llm_gateway = import_module("bot.services.llm_gateway")
    AnswerWithCitations = llm_gateway.AnswerWithCitations

    answer = AnswerWithCitations(
        answer_text="<script>x</script>",
        citation_ids=(),
        cost_usd=Decimal("0"),
        cache_hit=False,
        llm_call_id=2,
    )
    qa_handler = import_module("bot.handlers.qa")
    text = qa_handler._format_synthesized_response(
        answer, _bundle((_card_item(),)), users_by_id={}
    )

    assert "<script>x</script>" not in text
    assert "&lt;script&gt;" in text


# ─── R-07-07: source truncation at 5 ──────────────────────────────────────────


def test_format_response_card_sources_truncated_at_5() -> None:
    """R-07-07: when card has >5 source mvids, renderer shows first 5 and '+N more'."""
    item = _card_item(mvids=(10, 20, 30, 40, 50, 60, 70))  # 7 sources
    qa_handler = import_module("bot.handlers.qa")
    text = qa_handler._format_response(_bundle((item,)), users_by_id={})

    # First 5 present
    assert "10, 20, 30, 40, 50" in text
    # 6th and 7th NOT in the sources list
    assert "60" not in text
    assert "70" not in text
    # Continuation marker for the 2 remaining
    assert "+2 more" in text


def test_format_response_card_exactly_5_sources_no_truncation() -> None:
    """R-07-07: exactly 5 sources — no '+N more' suffix."""
    item = _card_item(mvids=(10, 20, 30, 40, 50))  # exactly 5
    qa_handler = import_module("bot.handlers.qa")
    text = qa_handler._format_response(_bundle((item,)), users_by_id={})

    assert "10, 20, 30, 40, 50" in text
    assert "+0 more" not in text
    assert "more" not in text
