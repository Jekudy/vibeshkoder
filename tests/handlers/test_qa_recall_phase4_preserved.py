"""T5-04: Phase 4 deterministic reply preservation tests (contracts.md §6.2).

When ``memory.qa.llm_synthesis.enabled = False``, the /recall handler MUST
produce reply text identical to Phase 4 behavior (the path landed in T4-04 /
PR #162).  Migration 081 legitimately extended the audit trace create shape
with ``source_chat_message_id``; the tests pin that current schema separately.

The Phase 4 expected strings below were captured from
``bot/handlers/qa.py::_format_response`` on commit ``71d6eff`` (T5-04c —
the cascade slice; identical for Phase 4 reply formatting since T4-04).
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.conftest import import_module

pytestmark = pytest.mark.usefixtures("app_env")

COMMUNITY_CHAT_ID = -1001234567890

# Phase 4 fixture timestamp — chosen so the formatted date is deterministic
# under the host TZ. We use a UTC datetime + astimezone() which depends on
# the host's local TZ; therefore the assertions below match on substrings
# that are TZ-invariant (the snippet, author, HTML markers, link, and
# message_version_id) rather than a fixed wallclock string.
FIXTURE_NOW = datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc)


def _message(
    *,
    chat_id: int = COMMUNITY_CHAT_ID,
    chat_type: str = "supergroup",
    user_id: int = 1001,
    message_id: int = 500,
) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type=chat_type),
        from_user=SimpleNamespace(
            id=user_id,
            username="testuser",
            first_name="Test",
            last_name=None,
        ),
        message_id=message_id,
        reply=AsyncMock(),
    )


def _command(args: str | None) -> SimpleNamespace:
    return SimpleNamespace(args=args)


def _user(
    *,
    user_id: int = 1001,
    is_member: bool = True,
    is_admin: bool = False,
    first_name: str = "Member",
    username: str | None = "member",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        is_member=is_member,
        is_admin=is_admin,
        first_name=first_name,
        last_name=None,
        username=username,
    )


def _qa_result(*, abstained: bool):
    from bot.services.evidence import EvidenceBundle, EvidenceItem
    from bot.services.qa import QaResult

    items: tuple[EvidenceItem, ...] = ()
    if not abstained:
        items = (
            EvidenceItem(
                message_version_id=500,
                chat_message_id=50,
                chat_id=COMMUNITY_CHAT_ID,
                message_id=77,
                user_id=2002,
                snippet="обсуждали <b>память</b>",
                ts_rank=0.8,
                captured_at=FIXTURE_NOW,
                message_date=FIXTURE_NOW,
            ),
        )
    bundle = EvidenceBundle(
        query="память",
        chat_id=COMMUNITY_CHAT_ID,
        items=items,
        abstained=abstained,
        created_at=FIXTURE_NOW,
    )
    return QaResult(bundle=bundle, query_redacted=False)


def _patch_persist(handler, monkeypatch) -> None:
    from bot.services.message_persistence import PersistResult

    fake_cm = SimpleNamespace(id=1, current_version_id=None)
    monkeypatch.setattr(
        handler,
        "persist_message_with_policy",
        AsyncMock(
            return_value=PersistResult(
                chat_message=fake_cm, policy="normal", is_offrecord_mark_created=False
            )
        ),
    )
    monkeypatch.setattr(handler.UserRepo, "upsert", AsyncMock())


def _flag_off_for_llm():
    """FeatureFlagRepo.get fake: QA_FEATURE_FLAG=True, LLM_SYNTHESIS=False."""

    async def _impl(session, flag_key, *args, **kwargs):
        if flag_key == "memory.qa.enabled":
            return True
        if flag_key == "memory.qa.llm_synthesis.enabled":
            return False
        return False

    return _impl


# ─── Phase 4 byte-for-byte tests ──────────────────────────────────────────────


async def test_abstention_reply_byte_identical_when_llm_flag_off(monkeypatch) -> None:
    """Phase 4 abstention message text MUST be the exact Phase 4 string."""
    handler = import_module("bot.handlers.qa")
    message = _message()
    session = AsyncMock()

    _patch_persist(handler, monkeypatch)
    monkeypatch.setattr(handler.FeatureFlagRepo, "get", _flag_off_for_llm())
    monkeypatch.setattr(handler.UserRepo, "get", AsyncMock(return_value=_user()))
    monkeypatch.setattr(handler.QaTraceRepo, "create", AsyncMock())
    monkeypatch.setattr(handler, "run_qa", AsyncMock(return_value=_qa_result(abstained=True)))
    # Synthesize MUST NOT be invoked here; assertion below would fail loudly.
    monkeypatch.setattr(handler, "synthesize_answer", AsyncMock())

    await handler.recall_handler(message, _command("nothing"), session)

    message.reply.assert_awaited_once()
    # Phase 4 verbatim string from _format_response when bundle.abstained.
    assert message.reply.call_args.args[0] == "Не нашёл подходящих свидетельств в истории чата."
    # No LLM synthesis attempted.
    handler.synthesize_answer.assert_not_awaited()


async def test_non_empty_reply_byte_identical_when_llm_flag_off(monkeypatch) -> None:
    """Phase 4 non-empty evidence reply MUST match a hardcoded snapshot byte-for-byte.

    Codex H1 finding: the previous version built ``expected`` by calling
    ``_format_response`` itself, which meant internal drift inside
    ``_format_response`` would NOT be caught — both sides would drift together.

    This version uses a HARDCODED snapshot captured 2026-05-13 per Codex H1 finding.
    Do not update unless you have verified the change is intentional and re-blessed
    by the Phase 11 binding suite (tests/evals/test_leakage.py, test_citations.py).

    ``_format_date`` is patched to a fixed value so the snapshot is TZ-invariant.
    """
    handler = import_module("bot.handlers.qa")
    message = _message()
    session = AsyncMock()

    qa_result = _qa_result(abstained=False)

    # Patch _format_date to a fixed string so the snapshot is TZ-independent.
    monkeypatch.setattr(handler, "_format_date", lambda _dt: "2026-04-30 12:00")

    # Phase 4 hardcoded snapshot — captured 2026-05-13.
    # Inputs: chat_id=-1001234567890 → short "1234567890", message_id=77,
    #         message_version_id=500, snippet='обсуждали <b>память</b>',
    #         author first_name="Author" (no last_name), date patched to "2026-04-30 12:00".
    PHASE4_SNAPSHOT = (
        "<b>Найденные свидетельства:</b>\n\n"
        "<blockquote>обсуждали <b>память</b></blockquote>\n"
        "<i>— Author, 2026-04-30 12:00</i> · "
        '<a href="https://t.me/c/1234567890/77">сообщение</a> · '
        "<code>message_version_id:500</code>"
    )

    _patch_persist(handler, monkeypatch)
    monkeypatch.setattr(handler.FeatureFlagRepo, "get", _flag_off_for_llm())
    monkeypatch.setattr(
        handler.UserRepo,
        "get",
        AsyncMock(side_effect=[_user(), _user(user_id=2002, first_name="Author")]),
    )
    monkeypatch.setattr(handler.QaTraceRepo, "create", AsyncMock())
    monkeypatch.setattr(handler, "run_qa", AsyncMock(return_value=qa_result))
    monkeypatch.setattr(handler, "synthesize_answer", AsyncMock())

    await handler.recall_handler(message, _command("память"), session)

    message.reply.assert_awaited_once()
    actual = message.reply.call_args.args[0]
    assert actual == PHASE4_SNAPSHOT, (
        "Phase 4 byte-identity drift detected!\n"
        "If _format_response changed intentionally, capture a new snapshot here\n"
        "and re-run the Phase 11 binding suite to confirm no leakage regression.\n"
        f"Expected:\n{PHASE4_SNAPSHOT!r}\n\nGot:\n{actual!r}"
    )
    # And the synthesizer was not called.
    handler.synthesize_answer.assert_not_awaited()


async def test_audit_trace_includes_source_message_field_when_llm_flag_off(monkeypatch) -> None:
    """The deterministic path writes the current post-migration trace shape.

    The flag-OFF path calls ``QaTraceRepo.create`` (via ``_write_trace``) with
    the original audit fields plus migration 081's optional source message id.
    LLM result columns are still populated only through ``update_llm_fields``.
    """
    handler = import_module("bot.handlers.qa")
    message = _message(user_id=4242)
    session = AsyncMock()
    trace_create = AsyncMock()

    _patch_persist(handler, monkeypatch)
    monkeypatch.setattr(handler.FeatureFlagRepo, "get", _flag_off_for_llm())
    monkeypatch.setattr(
        handler.UserRepo,
        "get",
        AsyncMock(side_effect=[_user(user_id=4242), _user(user_id=2002, first_name="Author")]),
    )
    monkeypatch.setattr(handler.QaTraceRepo, "create", trace_create)
    monkeypatch.setattr(handler, "run_qa", AsyncMock(return_value=_qa_result(abstained=False)))
    monkeypatch.setattr(handler, "synthesize_answer", AsyncMock())

    await handler.recall_handler(message, _command("память"), session)

    trace_create.assert_awaited_once()
    kwargs = trace_create.await_args.kwargs
    # Current create shape — no LLM result fields.
    assert set(kwargs.keys()) == {
        "user_tg_id",
        "chat_id",
        "query",
        "evidence_ids",
        "abstained",
        "redact_query",
        "source_chat_message_id",
    }
    assert kwargs["user_tg_id"] == 4242
    assert kwargs["chat_id"] == COMMUNITY_CHAT_ID
    assert kwargs["query"] == "память"
    assert kwargs["evidence_ids"] == [500]
    assert kwargs["abstained"] is False
    assert kwargs["redact_query"] is False
    assert kwargs["source_chat_message_id"] is None


async def test_reply_kwargs_byte_identical_when_llm_flag_off(monkeypatch) -> None:
    """message.reply MUST be called with parse_mode='HTML' AND
    disable_web_page_preview=True for the non-empty evidence path.
    """
    handler = import_module("bot.handlers.qa")
    message = _message()
    session = AsyncMock()

    _patch_persist(handler, monkeypatch)
    monkeypatch.setattr(handler.FeatureFlagRepo, "get", _flag_off_for_llm())
    monkeypatch.setattr(
        handler.UserRepo,
        "get",
        AsyncMock(side_effect=[_user(), _user(user_id=2002, first_name="Author")]),
    )
    monkeypatch.setattr(handler.QaTraceRepo, "create", AsyncMock())
    monkeypatch.setattr(handler, "run_qa", AsyncMock(return_value=_qa_result(abstained=False)))
    monkeypatch.setattr(handler, "synthesize_answer", AsyncMock())

    await handler.recall_handler(message, _command("память"), session)

    message.reply.assert_awaited_once()
    assert message.reply.call_args.kwargs == {
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }


def _qa_result_multi() -> object:
    """Two-item pure-message bundle for the multi-item snapshot test (Codex M3)."""
    from bot.services.evidence import EvidenceBundle, EvidenceItem
    from bot.services.qa import QaResult

    items: tuple[EvidenceItem, ...] = (
        EvidenceItem(
            message_version_id=500,
            chat_message_id=50,
            chat_id=COMMUNITY_CHAT_ID,
            message_id=77,
            user_id=2002,
            snippet="обсуждали <b>память</b>",
            ts_rank=0.8,
            captured_at=FIXTURE_NOW,
            message_date=FIXTURE_NOW,
        ),
        EvidenceItem(
            message_version_id=501,
            chat_message_id=51,
            chat_id=COMMUNITY_CHAT_ID,
            message_id=88,
            user_id=3003,
            snippet="другой <b>контекст</b>",
            ts_rank=0.6,
            captured_at=FIXTURE_NOW,
            message_date=FIXTURE_NOW,
        ),
    )
    bundle = EvidenceBundle(
        query="память",
        chat_id=COMMUNITY_CHAT_ID,
        items=items,
        abstained=False,
        created_at=FIXTURE_NOW,
    )
    return QaResult(bundle=bundle, query_redacted=False)


async def test_multi_item_reply_snapshot_when_llm_flag_off(monkeypatch) -> None:
    """Phase 4 two-item bundle MUST produce the hardcoded separator+order snapshot (Codex M3).

    The single-item snapshot test (test_non_empty_reply_byte_identical_when_llm_flag_off)
    catches per-item rendering drift but cannot catch multi-item separator/ordering drift
    (e.g. swapping '\\n\\n' to '\\n', changing item order, dropping a separator).
    This test adds a SECOND hardcoded snapshot for a 2-item bundle.

    Do not update unless the change is intentional and re-blessed by the Phase 11 binding
    suite. If _format_response's separator or loop changes, BOTH snapshots must be updated.
    """
    handler = import_module("bot.handlers.qa")
    message = _message()
    session = AsyncMock()

    qa_result = _qa_result_multi()

    # Patch _format_date to a fixed string so the snapshot is TZ-independent.
    monkeypatch.setattr(handler, "_format_date", lambda _dt: "2026-04-30 12:00")

    # Phase 4 hardcoded snapshot for a 2-item bundle — captured 2026-05-13 (Codex M3).
    # Inputs: chat_id=-1001234567890 → short "1234567890"
    # Item 1: message_id=77, message_version_id=500, snippet='обсуждали <b>память</b>',
    #         author first_name="Author"
    # Item 2: message_id=88, message_version_id=501, snippet='другой <b>контекст</b>',
    #         author first_name="Second"
    # Separator between items: \n\n (double newline, from "\n\n".join(parts)).
    PHASE4_MULTI_SNAPSHOT = (
        "<b>Найденные свидетельства:</b>\n\n"
        "<blockquote>обсуждали <b>память</b></blockquote>\n"
        "<i>— Author, 2026-04-30 12:00</i> · "
        '<a href="https://t.me/c/1234567890/77">сообщение</a> · '
        "<code>message_version_id:500</code>\n\n"
        "<blockquote>другой <b>контекст</b></blockquote>\n"
        "<i>— Second, 2026-04-30 12:00</i> · "
        '<a href="https://t.me/c/1234567890/88">сообщение</a> · '
        "<code>message_version_id:501</code>"
    )

    _patch_persist(handler, monkeypatch)
    monkeypatch.setattr(handler.FeatureFlagRepo, "get", _flag_off_for_llm())
    monkeypatch.setattr(
        handler.UserRepo,
        "get",
        AsyncMock(
            side_effect=[
                _user(),  # calling user
                _user(user_id=2002, first_name="Author"),
                _user(user_id=3003, first_name="Second"),
            ]
        ),
    )
    monkeypatch.setattr(handler.QaTraceRepo, "create", AsyncMock())
    monkeypatch.setattr(handler, "run_qa", AsyncMock(return_value=qa_result))
    monkeypatch.setattr(handler, "synthesize_answer", AsyncMock())

    await handler.recall_handler(message, _command("память"), session)

    message.reply.assert_awaited_once()
    actual = message.reply.call_args.args[0]
    assert actual == PHASE4_MULTI_SNAPSHOT, (
        "Phase 4 multi-item byte-identity drift detected!\n"
        "If _format_response separator/order changed intentionally, capture a new snapshot\n"
        "and re-run the Phase 11 binding suite to confirm no leakage regression.\n"
        f"Expected:\n{PHASE4_MULTI_SNAPSHOT!r}\n\nGot:\n{actual!r}"
    )
