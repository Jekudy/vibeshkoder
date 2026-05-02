from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.conftest import import_module

pytestmark = pytest.mark.usefixtures("app_env")

COMMUNITY_CHAT_ID = -1001234567890


def _message(
    *,
    chat_id: int = COMMUNITY_CHAT_ID,
    chat_type: str = "supergroup",
    user_id: int = 1001,
) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type=chat_type),
        from_user=SimpleNamespace(id=user_id),
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


def _qa_result(*, abstained: bool, query_redacted: bool = False):
    from bot.services.evidence import EvidenceBundle, EvidenceItem
    from bot.services.qa import QaResult

    now = datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc)
    items = ()
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
                captured_at=now,
                message_date=now,
            ),
        )
    bundle = EvidenceBundle(
        query="память",
        chat_id=COMMUNITY_CHAT_ID,
        items=items,
        abstained=abstained,
        created_at=now,
    )
    return QaResult(bundle=bundle, query_redacted=query_redacted)


async def test_flag_off_silent_return(monkeypatch) -> None:
    handler = import_module("bot.handlers.qa")
    message = _message()
    session = AsyncMock()
    trace_create = AsyncMock()
    run_qa = AsyncMock()

    monkeypatch.setattr(handler.FeatureFlagRepo, "get", AsyncMock(return_value=False))
    monkeypatch.setattr(handler.QaTraceRepo, "create", trace_create)
    monkeypatch.setattr(handler, "run_qa", run_qa)

    await handler.recall_handler(message, _command("память"), session)

    message.reply.assert_not_awaited()
    run_qa.assert_not_awaited()
    trace_create.assert_not_awaited()


async def test_dm_invocation_refuses_and_audits(monkeypatch) -> None:
    handler = import_module("bot.handlers.qa")
    message = _message(chat_id=1001, chat_type="private")
    session = AsyncMock()
    trace_create = AsyncMock()

    monkeypatch.setattr(handler.FeatureFlagRepo, "get", AsyncMock(return_value=True))
    monkeypatch.setattr(handler.QaTraceRepo, "create", trace_create)
    monkeypatch.setattr(handler, "run_qa", AsyncMock())

    await handler.recall_handler(message, _command("память"), session)

    message.reply.assert_awaited_once_with(
        "Команда /recall работает только в community чате."
    )
    trace_create.assert_awaited_once()
    assert trace_create.call_args.kwargs["query"] == "память"
    assert trace_create.call_args.kwargs["evidence_ids"] == []
    assert trace_create.call_args.kwargs["abstained"] is True


async def test_non_member_refuses_and_audits(monkeypatch) -> None:
    handler = import_module("bot.handlers.qa")
    message = _message()
    session = AsyncMock()
    trace_create = AsyncMock()

    monkeypatch.setattr(handler.FeatureFlagRepo, "get", AsyncMock(return_value=True))
    monkeypatch.setattr(
        handler.UserRepo,
        "get",
        AsyncMock(return_value=_user(is_member=False, is_admin=False)),
    )
    monkeypatch.setattr(handler.QaTraceRepo, "create", trace_create)
    monkeypatch.setattr(handler, "run_qa", AsyncMock())

    await handler.recall_handler(message, _command("память"), session)

    message.reply.assert_awaited_once_with("Доступ только участникам сообщества.")
    trace_create.assert_awaited_once()
    assert trace_create.call_args.kwargs["query"] == "память"
    assert trace_create.call_args.kwargs["abstained"] is True


async def test_empty_query_usage_hint_and_audits(monkeypatch) -> None:
    handler = import_module("bot.handlers.qa")
    message = _message()
    session = AsyncMock()
    trace_create = AsyncMock()

    monkeypatch.setattr(handler.FeatureFlagRepo, "get", AsyncMock(return_value=True))
    monkeypatch.setattr(handler.UserRepo, "get", AsyncMock(return_value=_user()))
    monkeypatch.setattr(handler.QaTraceRepo, "create", trace_create)
    monkeypatch.setattr(handler, "run_qa", AsyncMock())

    await handler.recall_handler(message, _command("   "), session)

    message.reply.assert_awaited_once_with(
        "Использование: <code>/recall &lt;вопрос&gt;</code>",
        parse_mode="HTML",
    )
    trace_create.assert_awaited_once()
    assert trace_create.call_args.kwargs["query"] == ""
    assert trace_create.call_args.kwargs["abstained"] is True


async def test_member_with_results_renders_response(monkeypatch) -> None:
    handler = import_module("bot.handlers.qa")
    message = _message()
    session = AsyncMock()
    trace_create = AsyncMock()
    run_qa = AsyncMock(return_value=_qa_result(abstained=False))

    monkeypatch.setattr(handler.FeatureFlagRepo, "get", AsyncMock(return_value=True))
    monkeypatch.setattr(
        handler.UserRepo,
        "get",
        AsyncMock(side_effect=[_user(), _user(user_id=2002, first_name="Author")]),
    )
    monkeypatch.setattr(handler.QaTraceRepo, "create", trace_create)
    monkeypatch.setattr(handler, "run_qa", run_qa)

    await handler.recall_handler(message, _command("память"), session)

    run_qa.assert_awaited_once()
    assert run_qa.call_args.kwargs["redact_query_in_audit"] is False
    message.reply.assert_awaited_once()
    response = message.reply.call_args.args[0]
    assert "<b>Найденные свидетельства:</b>" in response
    assert "обсуждали <b>память</b>" in response
    assert "Author" in response
    assert "https://t.me/c/1234567890/77" in response
    assert "message_version_id:500" in response
    assert message.reply.call_args.kwargs["parse_mode"] == "HTML"
    trace_create.assert_awaited_once()
    assert trace_create.call_args.kwargs["evidence_ids"] == [500]
    assert trace_create.call_args.kwargs["abstained"] is False


async def test_member_no_results_abstains(monkeypatch) -> None:
    handler = import_module("bot.handlers.qa")
    message = _message()
    session = AsyncMock()
    trace_create = AsyncMock()

    monkeypatch.setattr(handler.FeatureFlagRepo, "get", AsyncMock(return_value=True))
    monkeypatch.setattr(handler.UserRepo, "get", AsyncMock(return_value=_user()))
    monkeypatch.setattr(handler.QaTraceRepo, "create", trace_create)
    monkeypatch.setattr(handler, "run_qa", AsyncMock(return_value=_qa_result(abstained=True)))

    await handler.recall_handler(message, _command("ничего"), session)

    message.reply.assert_awaited_once()
    assert message.reply.call_args.args[0] == "Не нашёл подходящих свидетельств в истории чата."
    assert message.reply.call_args.kwargs["parse_mode"] == "HTML"
    trace_create.assert_awaited_once()
    assert trace_create.call_args.kwargs["evidence_ids"] == []
    assert trace_create.call_args.kwargs["abstained"] is True


async def test_offrecord_query_not_echoed_and_audit_redacted(monkeypatch) -> None:
    handler = import_module("bot.handlers.qa")
    message = _message()
    session = AsyncMock()
    trace_create = AsyncMock()
    run_qa = AsyncMock(return_value=_qa_result(abstained=True, query_redacted=True))
    query = "секретный запрос #offrecord"

    monkeypatch.setattr(handler.FeatureFlagRepo, "get", AsyncMock(return_value=True))
    monkeypatch.setattr(handler.UserRepo, "get", AsyncMock(return_value=_user()))
    monkeypatch.setattr(handler.QaTraceRepo, "create", trace_create)
    monkeypatch.setattr(handler, "run_qa", run_qa)

    await handler.recall_handler(message, _command(query), session)

    assert run_qa.call_args.kwargs["redact_query_in_audit"] is True
    response = message.reply.call_args.args[0]
    assert query not in response
    trace_create.assert_awaited_once()
    assert trace_create.call_args.kwargs["query"] == query
    assert trace_create.call_args.kwargs["redact_query"] is True


async def test_audit_row_written_once_for_processed_invocation(monkeypatch) -> None:
    handler = import_module("bot.handlers.qa")
    message = _message(user_id=3030)
    session = AsyncMock()
    trace_create = AsyncMock()

    monkeypatch.setattr(handler.FeatureFlagRepo, "get", AsyncMock(return_value=True))
    monkeypatch.setattr(handler.UserRepo, "get", AsyncMock(return_value=_user(user_id=3030)))
    monkeypatch.setattr(handler.QaTraceRepo, "create", trace_create)
    monkeypatch.setattr(handler, "run_qa", AsyncMock(return_value=_qa_result(abstained=True)))

    await handler.recall_handler(message, _command("история"), session)

    trace_create.assert_awaited_once()
    assert trace_create.call_args.args == (session,)
    assert trace_create.call_args.kwargs == {
        "user_tg_id": 3030,
        "chat_id": COMMUNITY_CHAT_ID,
        "query": "история",
        "evidence_ids": [],
        "abstained": True,
        "redact_query": False,
    }


# ─── §3.4 asymmetric /recall refusal tests ────────────────────────────────


async def test_recall_in_non_community_group_replies_and_audits(monkeypatch) -> None:
    """§3.4: supergroup with chat.id != COMMUNITY_CHAT_ID → reply sent + qa_traces abstain."""
    handler = import_module("bot.handlers.qa")
    # Non-community supergroup (not private, not community)
    message = _message(chat_id=-9999999999, chat_type="supergroup")
    session = AsyncMock()
    trace_create = AsyncMock()

    monkeypatch.setattr(handler.FeatureFlagRepo, "get", AsyncMock(return_value=True))
    monkeypatch.setattr(handler.QaTraceRepo, "create", trace_create)
    monkeypatch.setattr(handler, "run_qa", AsyncMock())

    await handler.recall_handler(message, _command("что-то"), session)

    # New behavior: reply is always sent for non-community chats (not just private).
    message.reply.assert_awaited_once_with(
        "Команда /recall работает только в community чате."
    )
    trace_create.assert_awaited_once()
    assert trace_create.call_args.kwargs["abstained"] is True


async def test_recall_in_non_community_group_handles_forbidden(monkeypatch) -> None:
    """§3.4: if bot lacks send_messages permission, TelegramForbiddenError is caught;
    qa_traces audit row still created, no exception escapes."""
    from aiogram.exceptions import TelegramForbiddenError

    handler = import_module("bot.handlers.qa")
    message = _message(chat_id=-9999999998, chat_type="supergroup")
    # Simulate bot lacking can_send_messages.
    message.reply = AsyncMock(side_effect=TelegramForbiddenError(method=None, message="Forbidden: bot was kicked"))
    session = AsyncMock()
    trace_create = AsyncMock()

    monkeypatch.setattr(handler.FeatureFlagRepo, "get", AsyncMock(return_value=True))
    monkeypatch.setattr(handler.QaTraceRepo, "create", trace_create)
    monkeypatch.setattr(handler, "run_qa", AsyncMock())

    # Must NOT raise — TelegramForbiddenError is caught internally.
    await handler.recall_handler(message, _command("тест"), session)

    # Audit row still created.
    trace_create.assert_awaited_once()
    assert trace_create.call_args.kwargs["abstained"] is True
