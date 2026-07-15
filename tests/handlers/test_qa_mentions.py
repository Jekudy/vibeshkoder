from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from html.parser import HTMLParser
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tests.conftest import import_module


pytestmark = pytest.mark.usefixtures("app_env")

COMMUNITY_CHAT_ID = -1001234567890

FAKE_OPENAI_KEY = "sk-" + "proj-FAKEOPENAI0123456789"
FAKE_DEEPSEEK_KEY = "sk-" + "FAKEDEEPSEEK0123456789"
FAKE_CLOUDFLARE_TOKEN = "cfat_" + "FAKECLOUDFLARE0123456789"
FAKE_TELEGRAM_TOKEN = "123456789" + ":FAKETELEGRAMBOT_TOKEN_0123456789"
FAKE_GENERIC_KEY = "api_" + "key=FAKE_GENERIC_KEY_0123456789"
FAKE_GENERIC_ASSIGNED_SECRET = "Az9!FAKE_TOKEN_0123456789"
FAKE_LONG_PREFIX_ASSIGNMENT = 'token="' + ("a" * 256) + FAKE_GENERIC_ASSIGNED_SECRET + '"'
FAKE_ESCAPED_INTERNAL_DELIMITER_ASSIGNMENT = (
    r"token=\"prefix\\\" " + FAKE_GENERIC_ASSIGNED_SECRET + r"\""
)
FAKE_QUOTED_PREFIX_OUTPUT = (
    ("x" * 960) + ' token="' + "Az9!FAKE_TOKEN_0123456789" + " extra" + ("z" * 400)
)
FAKE_SECRET_FAMILIES = (
    pytest.param(FAKE_OPENAI_KEY, id="openai-token"),
    pytest.param(FAKE_CLOUDFLARE_TOKEN, id="cloudflare-token"),
    pytest.param(FAKE_TELEGRAM_TOKEN, id="telegram-token"),
    pytest.param(
        "OPENAI_API_" + "KEY = 'Az9!Fake_Named-Secret.012345'",
        id="named-assignment",
    ),
)
FAKE_HIGHLIGHT_SPLIT_SECRETS = (
    pytest.param(
        "<b>token</b>=Az9!FAKE_SECRET_0123456789",
        id="highlight-named-assignment",
    ),
    pytest.param(
        "s<b>k</b>-A1b2FAKEHIGHLIGHT0123456789",
        id="highlight-direct-token",
    ),
    pytest.param(
        "s\x00k-A1b2FAKECONTROL0123456789",
        id="control-direct-token",
    ),
    pytest.param(
        "to\x00ken=Az9!FAKE_CONTROL_0123456789",
        id="control-named-assignment",
    ),
)


class _TelegramHTMLValidator(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        assert self.stack and self.stack.pop() == tag


def _assert_bounded_valid_html(value: str) -> None:
    from bot.services.qa_guardrails import MAX_AI_ANSWER_CHARS

    assert len(value) <= MAX_AI_ANSWER_CHARS
    parser = _TelegramHTMLValidator()
    parser.feed(value)
    parser.close()
    assert parser.stack == []
    assert re.search(r"&(?:#x?[0-9A-Fa-f]*|[A-Za-z]*)$", value) is None


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


def _qa_result(
    *,
    abstained: bool = False,
    snippet: str = "обсуждали память",
    query_redacted: bool = False,
):
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
                snippet=snippet,
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
        query_redacted=query_redacted,
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


@pytest.mark.parametrize("fallback", ["quota", "provider_error", "abstention"])
async def test_every_dynamic_fallback_is_bounded_valid_html_and_secret_safe(
    monkeypatch,
    fallback: str,
) -> None:
    handler = import_module("bot.handlers.qa")
    from bot.services.llm_gateway import Abstention

    message = _message()
    session = AsyncMock()
    unsafe_snippet = "<b>" + ("x&" * 2_000)

    _patch_common(handler, monkeypatch)
    monkeypatch.setattr(handler.FeatureFlagRepo, "get", _flag_get(handler))
    monkeypatch.setattr(
        handler,
        "run_qa",
        AsyncMock(return_value=_qa_result(snippet=unsafe_snippet)),
    )
    monkeypatch.setattr(
        handler,
        "acquire_daily_llm_question_slot",
        AsyncMock(
            return_value=SimpleNamespace(
                allowed=fallback != "quota",
                used=2 if fallback == "quota" else 0,
                limit=2,
            )
        ),
    )
    monkeypatch.setattr(
        handler.QaTraceRepo,
        "create",
        AsyncMock(return_value=SimpleNamespace(id=903)),
    )
    monkeypatch.setattr(handler.QaTraceRepo, "update_llm_fields", AsyncMock())
    monkeypatch.setattr(
        handler,
        "_load_gateway_config",
        Mock(return_value=SimpleNamespace(provider="deepseek")),
    )
    monkeypatch.setattr(handler, "_resolve_provider", Mock(return_value=object()))
    if fallback == "provider_error":
        synth_result: object = RuntimeError("provider unavailable")
        monkeypatch.setattr(
            handler,
            "synthesize_answer",
            AsyncMock(side_effect=synth_result),
        )
    else:
        synth_result = Abstention(
            reason="provider_error",
            cost_usd=Decimal("0"),
            llm_call_id=904,
        )
        monkeypatch.setattr(
            handler,
            "synthesize_answer",
            AsyncMock(return_value=synth_result),
        )

    await handler.mention_question_handler(message, _question(), session)

    response = message.reply.call_args.args[0]
    _assert_bounded_valid_html(response)
    assert "[1]" in response


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


@pytest.mark.parametrize("secret", FAKE_SECRET_FAMILIES)
async def test_sensitive_query_is_persisted_raw_then_refused_before_all_derived_sinks(
    monkeypatch,
    caplog,
    secret: str,
) -> None:
    handler = import_module("bot.handlers.qa")

    original_text = f"@bot вопрос {secret}"
    message = _message(text=original_text)
    question = _question(f"вопрос {secret}")
    session = AsyncMock()
    trace_create = AsyncMock(return_value=SimpleNamespace(id=910))
    run_qa = AsyncMock()
    quota = AsyncMock()
    synthesize = AsyncMock()
    load_config = Mock()
    resolve_provider = Mock()

    _patch_common(handler, monkeypatch)
    monkeypatch.setattr(handler.FeatureFlagRepo, "get", _flag_get(handler))
    monkeypatch.setattr(handler, "run_qa", run_qa)
    monkeypatch.setattr(handler, "acquire_daily_llm_question_slot", quota)
    monkeypatch.setattr(handler.QaTraceRepo, "create", trace_create)
    monkeypatch.setattr(handler, "_load_gateway_config", load_config)
    monkeypatch.setattr(handler, "_resolve_provider", resolve_provider)
    monkeypatch.setattr(handler, "synthesize_answer", synthesize)
    caplog.set_level("DEBUG")

    await handler.mention_question_handler(message, question, session)

    persisted_message = handler.persist_message_with_policy.await_args.args[1]
    assert persisted_message is message
    assert persisted_message.text == original_text

    run_qa.assert_not_awaited()
    quota.assert_not_awaited()
    load_config.assert_not_called()
    resolve_provider.assert_not_called()
    synthesize.assert_not_awaited()
    trace_kwargs = trace_create.await_args.kwargs
    assert trace_kwargs["query"] == handler.SENSITIVE_QA_TRACE_MARKER
    assert trace_kwargs["evidence_ids"] == []
    assert trace_kwargs["abstained"] is True
    assert trace_kwargs["redact_query"] is True
    assert trace_kwargs["source_chat_message_id"] == 8501

    message.reply.assert_awaited_once_with(handler.SENSITIVE_QA_REFUSAL)
    assert secret not in str(trace_kwargs)
    assert secret not in message.reply.await_args.args[0]
    assert secret not in caplog.text
    _assert_bounded_valid_html(message.reply.await_args.args[0])


@pytest.mark.parametrize("content_field", ["text", "caption"])
async def test_sensitive_tail_beyond_trigger_query_limit_is_refused_from_raw_message(
    monkeypatch,
    content_field: str,
) -> None:
    handler = import_module("bot.handlers.qa")
    trigger = import_module("bot.services.qa_trigger")

    secret = FAKE_OPENAI_KEY
    raw_content = "@bot " + ("x" * trigger.MAX_USER_QUERY_CHARS) + f" {secret}"
    message = _message(text=raw_content if content_field == "text" else "")
    message.text = raw_content if content_field == "text" else None
    message.caption = raw_content if content_field == "caption" else None
    question = trigger.extract_triggered_question(
        message,
        expected_chat_id=COMMUNITY_CHAT_ID,
        bot_id=9999,
        bot_username="bot",
    )
    assert question is not None
    assert question.was_truncated is True
    assert secret not in question.query

    session = AsyncMock()
    trace_create = AsyncMock(return_value=SimpleNamespace(id=911))
    run_qa = AsyncMock(side_effect=AssertionError("raw sensitive tail reached search"))
    quota = AsyncMock()
    synthesize = AsyncMock()

    _patch_common(handler, monkeypatch)
    monkeypatch.setattr(handler.FeatureFlagRepo, "get", _flag_get(handler))
    monkeypatch.setattr(handler.QaTraceRepo, "create", trace_create)
    monkeypatch.setattr(handler, "run_qa", run_qa)
    monkeypatch.setattr(handler, "acquire_daily_llm_question_slot", quota)
    monkeypatch.setattr(handler, "synthesize_answer", synthesize)

    await handler.mention_question_handler(message, question, session)

    persisted_message = handler.persist_message_with_policy.await_args.args[1]
    assert getattr(persisted_message, content_field) == raw_content
    run_qa.assert_not_awaited()
    quota.assert_not_awaited()
    synthesize.assert_not_awaited()
    trace_kwargs = trace_create.await_args.kwargs
    assert trace_kwargs["query"] == handler.SENSITIVE_QA_TRACE_MARKER
    assert trace_kwargs["evidence_ids"] == []
    assert trace_kwargs["redact_query"] is True
    message.reply.assert_awaited_once_with(handler.SENSITIVE_QA_REFUSAL)


@pytest.mark.parametrize("secret", (*FAKE_SECRET_FAMILIES, *FAKE_HIGHLIGHT_SPLIT_SECRETS))
async def test_sensitive_evidence_is_refused_before_quota_provider_or_trace_payload(
    monkeypatch,
    caplog,
    secret: str,
) -> None:
    handler = import_module("bot.handlers.qa")

    message = _message()
    session = AsyncMock()
    trace_create = AsyncMock(return_value=SimpleNamespace(id=912))
    quota = AsyncMock()
    synthesize = AsyncMock()
    load_config = Mock()
    resolve_provider = Mock()

    _patch_common(handler, monkeypatch)
    monkeypatch.setattr(handler.FeatureFlagRepo, "get", _flag_get(handler))
    monkeypatch.setattr(
        handler,
        "run_qa",
        AsyncMock(return_value=_qa_result(snippet=f"evidence {secret}")),
    )
    monkeypatch.setattr(handler, "acquire_daily_llm_question_slot", quota)
    monkeypatch.setattr(handler.QaTraceRepo, "create", trace_create)
    monkeypatch.setattr(handler, "_load_gateway_config", load_config)
    monkeypatch.setattr(handler, "_resolve_provider", resolve_provider)
    monkeypatch.setattr(handler, "synthesize_answer", synthesize)
    caplog.set_level("DEBUG")

    await handler.mention_question_handler(message, _question(), session)

    quota.assert_not_awaited()
    load_config.assert_not_called()
    resolve_provider.assert_not_called()
    synthesize.assert_not_awaited()
    trace_kwargs = trace_create.await_args.kwargs
    assert trace_kwargs["query"] == handler.SENSITIVE_QA_TRACE_MARKER
    assert trace_kwargs["evidence_ids"] == []
    assert trace_kwargs["abstained"] is True
    assert trace_kwargs["redact_query"] is True
    assert trace_kwargs["source_chat_message_id"] == 8501
    message.reply.assert_awaited_once_with(handler.SENSITIVE_QA_REFUSAL)
    assert secret not in str(trace_kwargs)
    assert secret not in message.reply.await_args.args[0]
    assert secret not in caplog.text


@pytest.mark.parametrize(
    ("field_name", "secret"),
    [
        ("first_name", FAKE_OPENAI_KEY),
        ("last_name", FAKE_CLOUDFLARE_TOKEN),
        ("username", FAKE_TELEGRAM_TOKEN),
    ],
)
async def test_sensitive_author_metadata_is_refused_before_quota_or_rendering(
    monkeypatch,
    caplog,
    field_name: str,
    secret: str,
) -> None:
    handler = import_module("bot.handlers.qa")

    author_fields: dict[str, object | None] = {
        "id": 2002,
        "is_member": True,
        "is_admin": False,
        "first_name": "Author",
        "last_name": None,
        "username": "author",
    }
    author_fields[field_name] = secret
    if field_name == "username":
        author_fields["first_name"] = None

    message = _message()
    session = AsyncMock()
    trace_create = AsyncMock(return_value=SimpleNamespace(id=915))
    quota = AsyncMock(return_value=SimpleNamespace(allowed=False, used=2, limit=2))
    synthesize = AsyncMock()

    _patch_common(handler, monkeypatch)
    monkeypatch.setattr(handler.FeatureFlagRepo, "get", _flag_get(handler))
    monkeypatch.setattr(
        handler.UserRepo,
        "get",
        AsyncMock(
            side_effect=[
                SimpleNamespace(id=1001, is_member=True, is_admin=False),
                SimpleNamespace(**author_fields),
            ]
        ),
    )
    monkeypatch.setattr(handler, "run_qa", AsyncMock(return_value=_qa_result()))
    monkeypatch.setattr(handler, "acquire_daily_llm_question_slot", quota)
    monkeypatch.setattr(handler.QaTraceRepo, "create", trace_create)
    monkeypatch.setattr(handler, "synthesize_answer", synthesize)
    caplog.set_level("DEBUG")

    await handler.mention_question_handler(message, _question(), session)

    quota.assert_not_awaited()
    synthesize.assert_not_awaited()
    trace_kwargs = trace_create.await_args.kwargs
    assert trace_kwargs["query"] == handler.SENSITIVE_QA_TRACE_MARKER
    assert trace_kwargs["evidence_ids"] == []
    assert trace_kwargs["redact_query"] is True
    message.reply.assert_awaited_once_with(handler.SENSITIVE_QA_REFUSAL)
    assert secret not in str(trace_kwargs)
    assert secret not in message.reply.await_args.args[0]
    assert secret not in caplog.text


@pytest.mark.parametrize("reason", ["sensitive_input", "sensitive_output"])
async def test_sensitive_gateway_abstention_maps_to_fixed_refusal_and_redacted_trace(
    monkeypatch,
    reason: str,
) -> None:
    handler = import_module("bot.handlers.qa")
    from bot.services.llm_gateway import Abstention

    message = _message()
    session = AsyncMock()
    trace_update = AsyncMock()

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
        AsyncMock(return_value=SimpleNamespace(id=913)),
    )
    monkeypatch.setattr(handler.QaTraceRepo, "update_llm_fields", trace_update)
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
            return_value=Abstention(
                reason=reason,
                cost_usd=Decimal("0.0042"),
                llm_call_id=914,
            )
        ),
    )

    await handler.mention_question_handler(message, _question(), session)

    update_kwargs = trace_update.await_args.kwargs
    assert update_kwargs["llm_call_id"] == 914
    assert update_kwargs["llm_response_summary"] is None
    assert update_kwargs["llm_response_redacted"] is True
    assert update_kwargs["cost_usd"] == Decimal("0.0042")
    message.reply.assert_awaited_once_with(
        handler.SENSITIVE_QA_REFUSAL,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@pytest.mark.parametrize(
    ("provider_answer", "forbidden_value"),
    (
        pytest.param(
            "s\x00k-A1b2FAKECONTROL0123456789",
            "sk-A1b2FAKECONTROL0123456789",
            id="control-split",
        ),
        pytest.param(
            FAKE_QUOTED_PREFIX_OUTPUT,
            'token="' + "Az9!FAKE_TOKEN_0123456789",
            id="quoted-prefix-before-truncation",
        ),
        pytest.param(
            FAKE_LONG_PREFIX_ASSIGNMENT,
            FAKE_GENERIC_ASSIGNED_SECRET,
            id="long-low-class-prefix",
        ),
        pytest.param(
            FAKE_ESCAPED_INTERNAL_DELIMITER_ASSIGNMENT,
            FAKE_GENERIC_ASSIGNED_SECRET,
            id="escaped-internal-delimiter",
        ),
    ),
)
async def test_transform_sensitive_provider_answer_never_reaches_trace_summary(
    monkeypatch,
    provider_answer: str,
    forbidden_value: str,
) -> None:
    handler = import_module("bot.handlers.qa")
    from bot.services.llm_gateway import AnswerWithCitations

    message = _message()
    session = AsyncMock()
    trace_update = AsyncMock()

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
        AsyncMock(return_value=SimpleNamespace(id=916)),
    )
    monkeypatch.setattr(handler.QaTraceRepo, "update_llm_fields", trace_update)
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
                answer_text=provider_answer,
                citation_ids=(101,),
                cost_usd=Decimal("0.0042"),
                cache_hit=False,
                llm_call_id=917,
            )
        ),
    )

    await handler.mention_question_handler(message, _question(), session)

    update_kwargs = trace_update.await_args.kwargs
    assert update_kwargs["llm_response_summary"] is None
    assert update_kwargs["llm_response_redacted"] is True
    assert provider_answer not in str(update_kwargs)
    assert forbidden_value not in str(update_kwargs)
    message.reply.assert_awaited_once_with(
        handler.SENSITIVE_QA_REFUSAL,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    assert forbidden_value not in message.reply.await_args.args[0]


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
    _assert_bounded_valid_html(message.reply.await_args.args[0])


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
    _assert_bounded_valid_html(message.reply.await_args.args[0])


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
    _assert_bounded_valid_html(message.reply.await_args.args[0])


async def test_disabled_llm_path_is_bounded_without_search_or_provider(monkeypatch) -> None:
    handler = import_module("bot.handlers.qa")
    message = _message()
    session = AsyncMock()
    run_qa = AsyncMock()
    synthesize = AsyncMock()

    _patch_common(handler, monkeypatch)
    monkeypatch.setattr(
        handler.FeatureFlagRepo,
        "get",
        _flag_get(handler, llm=False),
    )
    monkeypatch.setattr(handler, "run_qa", run_qa)
    monkeypatch.setattr(handler, "synthesize_answer", synthesize)
    monkeypatch.setattr(handler.QaTraceRepo, "create", AsyncMock())

    await handler.mention_question_handler(message, _question(), session)

    run_qa.assert_not_awaited()
    synthesize.assert_not_awaited()
    assert "недоступен" in message.reply.await_args.args[0]
    _assert_bounded_valid_html(message.reply.await_args.args[0])


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
                llm_response_summary=f"Уже сохранённый ответ {FAKE_OPENAI_KEY} " + ("x&" * 2_000),
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
    assert message.reply.await_args.args[0] == handler.SENSITIVE_QA_REFUSAL
    assert "Уже сохранённый ответ" not in message.reply.call_args.args[0]
    assert FAKE_OPENAI_KEY not in message.reply.await_args.args[0]
    _assert_bounded_valid_html(message.reply.await_args.args[0])


async def test_redelivered_sensitive_query_is_refused_without_duplicate_trace(
    monkeypatch,
) -> None:
    handler = import_module("bot.handlers.qa")
    message = _message(text=f"@bot вопрос {FAKE_OPENAI_KEY}")
    session = AsyncMock()
    trace_create = AsyncMock()
    run_qa = AsyncMock()
    quota = AsyncMock()
    synthesize = AsyncMock()

    _patch_common(handler, monkeypatch)
    monkeypatch.setattr(handler.FeatureFlagRepo, "get", _flag_get(handler))
    monkeypatch.setattr(
        handler.QaTraceRepo,
        "get_by_source_chat_message_id",
        AsyncMock(
            return_value=SimpleNamespace(
                id=9902,
                llm_response_summary="previous safe answer",
            )
        ),
    )
    monkeypatch.setattr(handler.QaTraceRepo, "create", trace_create)
    monkeypatch.setattr(handler, "run_qa", run_qa)
    monkeypatch.setattr(handler, "acquire_daily_llm_question_slot", quota)
    monkeypatch.setattr(handler, "synthesize_answer", synthesize)

    await handler.mention_question_handler(
        message,
        _question(f"вопрос {FAKE_OPENAI_KEY}"),
        session,
    )

    message.reply.assert_awaited_once_with(handler.SENSITIVE_QA_REFUSAL)
    trace_create.assert_not_awaited()
    run_qa.assert_not_awaited()
    quota.assert_not_awaited()
    synthesize.assert_not_awaited()
