from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tests.conftest import import_module


pytestmark = pytest.mark.usefixtures("app_env")

COMMUNITY_CHAT_ID = -1001234567890


def _message(*, user_id: int = 1001, text: str = "@bot память") -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(id=COMMUNITY_CHAT_ID, type="supergroup"),
        from_user=SimpleNamespace(
            id=user_id,
            username="member",
            first_name="Member",
            last_name=None,
            is_bot=False,
        ),
        message_id=500,
        text=text,
        caption=None,
        reply=AsyncMock(),
    )


def _question(query: str = "что решили про память?"):
    trigger = import_module("bot.services.qa_trigger")
    return trigger.TriggeredQuestion(
        query=query,
        via_mention=True,
        via_reply=False,
        was_truncated=False,
    )


def _qa_result(*, abstained: bool = False):
    from bot.services.evidence import EvidenceBundle, EvidenceItem
    from bot.services.qa import QaResult

    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    items = ()
    if not abstained:
        items = (
            EvidenceItem(
                message_version_id=101,
                chat_message_id=10,
                chat_id=COMMUNITY_CHAT_ID,
                message_id=77,
                user_id=2002,
                snippet="обсуждали память",
                ts_rank=0.9,
                captured_at=now,
                message_date=now,
            ),
        )
    return QaResult(
        bundle=EvidenceBundle(
            query="память",
            chat_id=COMMUNITY_CHAT_ID,
            items=items,
            abstained=abstained,
            created_at=now,
        ),
        query_redacted=False,
    )


def _flag_get(handler, *, qa: bool = True, llm: bool = True) -> AsyncMock:
    async def get(_session, key: str) -> bool:
        if key == handler.QA_FEATURE_FLAG:
            return qa
        if key == handler.LLM_SYNTHESIS_FEATURE_FLAG:
            return llm
        raise AssertionError(f"unexpected flag: {key}")

    return AsyncMock(side_effect=get)


def _patch_common(handler, monkeypatch, *, member: bool = True) -> None:
    monkeypatch.setattr(handler.UserRepo, "upsert", AsyncMock())
    monkeypatch.setattr(
        handler,
        "persist_message_with_policy",
        AsyncMock(return_value=SimpleNamespace(chat_message=SimpleNamespace(id=8501))),
    )
    monkeypatch.setattr(
        handler.QaTraceRepo,
        "get_by_source_chat_message_id",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        handler.UserRepo,
        "get",
        AsyncMock(
            side_effect=[
                SimpleNamespace(id=1001, is_member=member, is_admin=False),
                SimpleNamespace(
                    id=2002,
                    is_member=True,
                    is_admin=False,
                    first_name="Author",
                    last_name=None,
                    username="author",
                ),
            ]
        ),
    )


async def test_mention_question_runs_bounded_evidence_only_ai_path(monkeypatch) -> None:
    handler = import_module("bot.handlers.qa")
    guardrails = import_module("bot.services.qa_guardrails")
    from bot.services.llm_gateway import AnswerWithCitations

    message = _message()
    session = AsyncMock()
    trace = SimpleNamespace(id=901)
    answer = AnswerWithCitations(
        answer_text="д" * (guardrails.MAX_AI_ANSWER_CHARS + 500),
        citation_ids=(101,),
        cost_usd=Decimal("0.001"),
        cache_hit=False,
        llm_call_id=902,
    )

    _patch_common(handler, monkeypatch)
    monkeypatch.setattr(handler.FeatureFlagRepo, "get", _flag_get(handler))
    run_qa = AsyncMock(return_value=_qa_result())
    synthesize = AsyncMock(return_value=answer)
    monkeypatch.setattr(handler, "run_qa", run_qa)
    monkeypatch.setattr(
        handler,
        "acquire_daily_llm_question_slot",
        AsyncMock(return_value=SimpleNamespace(allowed=True, used=0, limit=2)),
    )
    monkeypatch.setattr(handler.QaTraceRepo, "create", AsyncMock(return_value=trace))
    monkeypatch.setattr(handler.QaTraceRepo, "update_llm_fields", AsyncMock())
    monkeypatch.setattr(
        handler,
        "_load_gateway_config",
        Mock(return_value=SimpleNamespace(provider="openai")),
    )
    monkeypatch.setattr(handler, "_resolve_provider", Mock(return_value=object()))
    monkeypatch.setattr(handler, "synthesize_answer", synthesize)

    await handler.mention_question_handler(
        message,
        _question(),
        session,
    )

    run_qa.assert_awaited_once_with(
        session,
        query="что решили про память?",
        chat_id=COMMUNITY_CHAT_ID,
        redact_query_in_audit=False,
        limit=handler.QA_EVIDENCE_LIMIT,
    )
    synthesize.assert_awaited_once()
    guarded_query = synthesize.call_args.kwargs["query"]
    assert "untrusted" in guarded_query.lower()
    assert "no tools" in guarded_query.lower()
    assert guarded_query.endswith("что решили про память?")
    response = message.reply.call_args.args[0]
    assert len(response) <= guardrails.MAX_AI_ANSWER_CHARS
    assert "[1]" in response
    assert message.reply.call_args.kwargs["disable_web_page_preview"] is True
    session.commit.assert_awaited_once()


async def test_third_daily_question_falls_back_to_deterministic_search(monkeypatch) -> None:
    handler = import_module("bot.handlers.qa")
    message = _message()
    session = AsyncMock()
    synthesize = AsyncMock()
    provider_resolver = Mock()

    _patch_common(handler, monkeypatch)
    monkeypatch.setattr(handler.FeatureFlagRepo, "get", _flag_get(handler))
    monkeypatch.setattr(handler, "run_qa", AsyncMock(return_value=_qa_result()))
    monkeypatch.setattr(
        handler,
        "acquire_daily_llm_question_slot",
        AsyncMock(return_value=SimpleNamespace(allowed=False, used=2, limit=2)),
    )
    trace_create = AsyncMock(return_value=SimpleNamespace(id=903))
    monkeypatch.setattr(handler.QaTraceRepo, "create", trace_create)
    monkeypatch.setattr(handler, "_resolve_provider", provider_resolver)
    monkeypatch.setattr(handler, "synthesize_answer", synthesize)

    await handler.mention_question_handler(message, _question(), session)

    synthesize.assert_not_awaited()
    provider_resolver.assert_not_called()
    response = message.reply.call_args.args[0]
    assert "Лимит — 2 AI-вопроса в день" in response
    assert "Показываю обычный поиск без AI" in response
    assert "<b>Найденные свидетельства:</b>" in response
    assert trace_create.call_args.kwargs["abstained"] is False
    assert trace_create.call_args.kwargs["evidence_ids"] == [101]


async def test_provider_exception_falls_back_without_logging_provider_payload(
    monkeypatch,
    caplog,
) -> None:
    handler = import_module("bot.handlers.qa")
    message = _message()
    session = AsyncMock()
    trace_create = AsyncMock(return_value=SimpleNamespace(id=906))
    update_llm = AsyncMock()
    provider_payload = "sentinel-secret-provider-payload"

    _patch_common(handler, monkeypatch)
    monkeypatch.setattr(handler.FeatureFlagRepo, "get", _flag_get(handler))
    monkeypatch.setattr(handler, "run_qa", AsyncMock(return_value=_qa_result()))
    monkeypatch.setattr(
        handler,
        "acquire_daily_llm_question_slot",
        AsyncMock(return_value=SimpleNamespace(allowed=True, used=0, limit=2)),
    )
    monkeypatch.setattr(handler.QaTraceRepo, "create", trace_create)
    monkeypatch.setattr(handler.QaTraceRepo, "update_llm_fields", update_llm)
    monkeypatch.setattr(
        handler,
        "_load_gateway_config",
        Mock(return_value=SimpleNamespace(provider="deepseek")),
    )
    monkeypatch.setattr(handler, "_resolve_provider", Mock(return_value=object()))
    monkeypatch.setattr(
        handler,
        "synthesize_answer",
        AsyncMock(side_effect=RuntimeError(provider_payload)),
    )
    caplog.set_level("ERROR", logger="bot.handlers.qa")

    await handler.mention_question_handler(message, _question(), session)

    response = message.reply.call_args.args[0]
    assert "<b>Найденные свидетельства:</b>" in response
    assert provider_payload not in caplog.text
    assert "RuntimeError" in caplog.text
    trace_create.assert_awaited_once()
    update_llm.assert_not_awaited()
    session.commit.assert_awaited_once()


async def test_non_member_is_refused_before_search_or_provider(monkeypatch) -> None:
    handler = import_module("bot.handlers.qa")
    message = _message()
    session = AsyncMock()
    run_qa = AsyncMock()
    synthesize = AsyncMock()

    _patch_common(handler, monkeypatch, member=False)
    monkeypatch.setattr(handler.FeatureFlagRepo, "get", _flag_get(handler))
    monkeypatch.setattr(handler, "run_qa", run_qa)
    monkeypatch.setattr(handler, "synthesize_answer", synthesize)
    monkeypatch.setattr(handler.QaTraceRepo, "create", AsyncMock())

    await handler.mention_question_handler(message, _question(), session)

    run_qa.assert_not_awaited()
    synthesize.assert_not_awaited()
    message.reply.assert_awaited_once_with("Доступ только участникам сообщества.")


async def test_no_evidence_abstains_without_consuming_quota(monkeypatch) -> None:
    handler = import_module("bot.handlers.qa")
    message = _message()
    session = AsyncMock()
    quota = AsyncMock()
    synthesize = AsyncMock()

    _patch_common(handler, monkeypatch)
    monkeypatch.setattr(handler.FeatureFlagRepo, "get", _flag_get(handler))
    monkeypatch.setattr(handler, "run_qa", AsyncMock(return_value=_qa_result(abstained=True)))
    monkeypatch.setattr(handler, "acquire_daily_llm_question_slot", quota)
    monkeypatch.setattr(handler, "synthesize_answer", synthesize)
    monkeypatch.setattr(handler.QaTraceRepo, "create", AsyncMock())

    await handler.mention_question_handler(message, _question(), session)

    quota.assert_not_awaited()
    synthesize.assert_not_awaited()
    assert "Не нашёл" in message.reply.call_args.args[0]


async def test_empty_trigger_query_prompts_without_search(monkeypatch) -> None:
    handler = import_module("bot.handlers.qa")
    message = _message(text="@bot")
    session = AsyncMock()
    run_qa = AsyncMock()

    _patch_common(handler, monkeypatch)
    monkeypatch.setattr(handler.FeatureFlagRepo, "get", _flag_get(handler))
    monkeypatch.setattr(handler, "run_qa", run_qa)
    monkeypatch.setattr(handler.QaTraceRepo, "create", AsyncMock())

    await handler.mention_question_handler(message, _question(""), session)

    run_qa.assert_not_awaited()
    assert "вопрос" in message.reply.call_args.args[0].lower()


def test_qa_handler_config_and_resolver_support_deepseek(monkeypatch) -> None:
    handler = import_module("bot.handlers.qa")
    from bot.services.llm_providers.deepseek import DeepSeekProvider

    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.delenv("LLM_MODEL", raising=False)

    config = handler._load_gateway_config()

    assert config.provider == "deepseek"
    assert config.model == "deepseek-v4-flash"
    assert config.prompt_template_version == "v1.1.0"
    assert isinstance(handler._resolve_provider("deepseek"), DeepSeekProvider)


async def test_paid_ledger_is_committed_before_outbound_reply(monkeypatch) -> None:
    handler = import_module("bot.handlers.qa")
    from bot.services.llm_gateway import AnswerWithCitations

    message = _message()
    order: list[str] = []

    async def commit() -> None:
        order.append("commit")

    async def reply(*args, **kwargs) -> None:
        order.append("reply")
        raise RuntimeError("telegram unavailable")

    session = AsyncMock()
    session.commit.side_effect = commit
    message.reply.side_effect = reply
    _patch_common(handler, monkeypatch)
    monkeypatch.setattr(handler.FeatureFlagRepo, "get", _flag_get(handler))
    monkeypatch.setattr(handler, "run_qa", AsyncMock(return_value=_qa_result()))
    monkeypatch.setattr(
        handler,
        "acquire_daily_llm_question_slot",
        AsyncMock(return_value=SimpleNamespace(allowed=True, used=0, limit=2)),
    )
    monkeypatch.setattr(
        handler.QaTraceRepo,
        "create",
        AsyncMock(return_value=SimpleNamespace(id=904)),
    )
    monkeypatch.setattr(handler.QaTraceRepo, "update_llm_fields", AsyncMock())
    monkeypatch.setattr(
        handler,
        "_load_gateway_config",
        Mock(return_value=SimpleNamespace(provider="deepseek")),
    )
    monkeypatch.setattr(handler, "_resolve_provider", Mock(return_value=object()))
    monkeypatch.setattr(
        handler,
        "synthesize_answer",
        AsyncMock(
            return_value=AnswerWithCitations(
                answer_text="Ответ",
                citation_ids=(101,),
                cost_usd=Decimal("0.001"),
                cache_hit=False,
                llm_call_id=905,
            )
        ),
    )

    with pytest.raises(RuntimeError, match="telegram unavailable"):
        await handler.mention_question_handler(message, _question(), session)

    assert order == ["commit", "reply"]


async def test_redelivered_question_does_not_consume_second_llm_slot(monkeypatch) -> None:
    handler = import_module("bot.handlers.qa")
    message = _message()
    session = AsyncMock()
    quota = AsyncMock()
    synthesize = AsyncMock()

    _patch_common(handler, monkeypatch)
    monkeypatch.setattr(handler.FeatureFlagRepo, "get", _flag_get(handler))
    monkeypatch.setattr(
        handler.QaTraceRepo,
        "get_by_source_chat_message_id",
        AsyncMock(
            return_value=SimpleNamespace(
                id=9901,
                llm_response_summary="Уже сохранённый ответ",
            )
        ),
    )
    run_qa = AsyncMock()
    monkeypatch.setattr(handler, "run_qa", run_qa)
    monkeypatch.setattr(handler, "acquire_daily_llm_question_slot", quota)
    monkeypatch.setattr(handler, "synthesize_answer", synthesize)

    await handler.mention_question_handler(message, _question(), session)

    run_qa.assert_not_awaited()
    quota.assert_not_awaited()
    synthesize.assert_not_awaited()
    assert "Уже сохранённый ответ" in message.reply.call_args.args[0]
