from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from html.parser import HTMLParser
import re
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock

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


@pytest.fixture(autouse=True)
def _mock_semantic_delivery_intent(monkeypatch, app_env):
    """Handler tests isolate orchestration from the real PostgreSQL repo."""

    handler = import_module("bot.handlers.qa")
    marker = AsyncMock()
    monkeypatch.setattr(handler.SemanticQuotaRepo, "mark_delivery_started", marker)
    monkeypatch.setattr(handler.SemanticQuotaRepo, "touch", AsyncMock())
    monkeypatch.setattr(handler, "_question_is_governed", AsyncMock(return_value=True))
    monkeypatch.setattr(handler, "persist_semantic_retrieval_trace", AsyncMock())
    return marker


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
        date=datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc),
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


def _question_bundle():
    from bot.services.evidence import EvidenceBundle, EvidenceItem

    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    return EvidenceBundle(
        query="что решили про память?",
        chat_id=COMMUNITY_CHAT_ID,
        items=(
            EvidenceItem(
                message_version_id=9501,
                chat_message_id=8501,
                chat_id=COMMUNITY_CHAT_ID,
                message_id=500,
                user_id=1001,
                snippet="",
                ts_rank=1.0,
                captured_at=now,
                message_date=now,
            ),
        ),
        abstained=False,
        created_at=now,
    )


def _flag_get(
    handler,
    *,
    qa: bool = True,
    llm: bool = True,
    semantic: bool = False,
) -> AsyncMock:
    async def get(
        _session,
        key: str,
        scope_type: str | None = None,
        scope_id: str | None = None,
    ) -> bool:
        if key == handler.QA_FEATURE_FLAG:
            return qa
        if key == handler.LLM_SYNTHESIS_FEATURE_FLAG:
            return llm
        if key == handler.SEMANTIC_QA_FEATURE_FLAG:
            return semantic if scope_type is None and scope_id is None else False
        raise AssertionError(f"unexpected flag: {key}")

    return AsyncMock(side_effect=get)


def test_five_source_footer_retains_clickable_links_with_worst_case_text() -> None:
    handler = import_module("bot.handlers.qa")
    base = _qa_result(snippet="<>&" * 2_000).bundle
    item = base.items[0]
    bundle = replace(
        base,
        items=tuple(
            replace(
                item,
                message_version_id=101 + index,
                chat_message_id=10 + index,
                message_id=77 + index,
                user_id=2002 + index,
            )
            for index in range(5)
        ),
    )
    users = {
        2002 + index: SimpleNamespace(first_name="А" * 2_000, last_name="Б" * 2_000)
        for index in range(5)
    }

    rendered = handler._format_bounded_mention_response("О" * 10_000, bundle, users)

    _assert_bounded_valid_html(rendered)
    assert rendered.count('<a href="https://t.me/c/1234567890/') == 5


def test_bounded_footer_strips_fts_headline_markers_and_escapes_other_html() -> None:
    handler = import_module("bot.handlers.qa")
    bundle = _qa_result(snippet="<b>тетс</b> <script>").bundle

    rendered = handler._format_bounded_mention_response("", bundle, {})

    assert "тетс" in rendered
    assert "&lt;b&gt;" not in rendered
    assert "&lt;/b&gt;" not in rendered
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    _assert_bounded_valid_html(rendered)


def test_semantic_trace_binding_flattens_card_provenance() -> None:
    qa_service = import_module("bot.services.qa")
    from bot.services.evidence import EvidenceBundle, EvidenceItem

    now = datetime(2026, 7, 16, 12, tzinfo=timezone.utc)
    card = EvidenceItem(
        message_version_id=101,
        chat_message_id=10,
        chat_id=COMMUNITY_CHAT_ID,
        message_id=77,
        user_id=2002,
        snippet="approved card",
        ts_rank=0.9,
        captured_at=now,
        message_date=now,
        source_type="card",
        card_source_message_version_ids=(101, 102, 103),
    )
    message = replace(
        card,
        message_version_id=104,
        source_type="message",
        card_source_message_version_ids=(),
    )
    bundle = EvidenceBundle("q", COMMUNITY_CHAT_ID, (card, message), False, now)

    assert qa_service._semantic_evidence_provenance_ids(bundle) == [101, 102, 103, 104]


async def test_semantic_retrieval_trace_persists_only_governed_result_ranks(
    monkeypatch,
) -> None:
    qa_service = import_module("bot.services.qa")
    session = AsyncMock()
    session.add = Mock()
    update_retrieval = AsyncMock()
    monkeypatch.setattr(qa_service.QaTraceRepo, "update_retrieval_fields", update_retrieval)
    result = SimpleNamespace(
        bundle=_qa_result().bundle,
        embedding_model="text-embedding-3-small",
        retrieval=SimpleNamespace(
            candidate_ranks={
                "message:101": {"vector": 1, "fts": 2},
                "message:999": {"vector": 2},
            },
            fts_latency_ms=2,
            vector_latency_ms=3,
            fusion_latency_ms=1,
            total_latency_ms=6,
        ),
    )

    trace = await qa_service.persist_semantic_retrieval_trace(
        session,
        result=result,
        query="память",
        attempt_id=3017,
        qa_trace_id=5017,
    )

    assert trace.result_source_ids == ["message:101"]
    assert trace.candidate_ranks == {"message:101": {"vector": 1, "fts": 2}}
    update_retrieval.assert_awaited_once_with(
        session,
        qa_trace_id=5017,
        evidence_ids=[101],
        abstained=False,
    )
    session.add.assert_called_once_with(trace)
    session.flush.assert_awaited_once()


def _patch_common(handler, monkeypatch, *, member: bool = True) -> None:
    monkeypatch.setattr(handler.UserRepo, "upsert", AsyncMock())
    monkeypatch.setattr(
        handler,
        "persist_message_with_policy",
        AsyncMock(
            return_value=SimpleNamespace(
                chat_message=SimpleNamespace(id=8501, current_version_id=9501)
            )
        ),
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
        exclude_chat_message_id=8501,
        human_only=True,
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
    assert "сохранённый ответ повторно не показывается" in message.reply.await_args.args[0]
    assert FAKE_OPENAI_KEY not in message.reply.await_args.args[0]
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


async def test_redelivered_semantic_answer_never_replays_stored_content(monkeypatch) -> None:
    handler = import_module("bot.handlers.qa")
    message = _message()
    session = AsyncMock()
    rendered = (
        'Ответ [1]\n\n<b>Источники:</b>\n[1] <a href="https://t.me/c/1234567890/77">источник</a>'
    )
    _patch_common(handler, monkeypatch)
    monkeypatch.setattr(handler.FeatureFlagRepo, "get", _flag_get(handler, semantic=True))
    monkeypatch.setattr(
        handler.QaTraceRepo,
        "get_by_source_chat_message_id",
        AsyncMock(
            return_value=SimpleNamespace(
                llm_response_summary=f"{handler._SEMANTIC_REPLAY_HTML_PREFIX}{rendered}"
            )
        ),
    )
    semantic = AsyncMock()
    monkeypatch.setattr(handler, "_semantic_mention_question", semantic)

    await handler.mention_question_handler(message, _question(), session)

    semantic.assert_not_awaited()
    assert rendered not in message.reply.await_args.args[0]
    assert "сохранённый ответ повторно не показывается" in message.reply.await_args.args[0]


async def test_semantic_feature_routes_before_legacy_retrieval(monkeypatch) -> None:
    handler = import_module("bot.handlers.qa")
    message = _message()
    session = AsyncMock()
    semantic_handler = AsyncMock()
    legacy = AsyncMock()

    _patch_common(handler, monkeypatch)
    monkeypatch.setattr(
        handler.FeatureFlagRepo,
        "get",
        _flag_get(handler, semantic=True),
    )
    monkeypatch.setattr(handler, "_semantic_mention_question", semantic_handler)
    monkeypatch.setattr(handler, "run_qa", legacy)

    await handler.mention_question_handler(message, _question(), session)

    semantic_handler.assert_awaited_once_with(
        message=message,
        session=session,
        sender_id=1001,
        persisted_chat_message_id=8501,
        question_bundle=ANY,
        query="что решили про память?",
        query_redacted=False,
    )
    legacy.assert_not_awaited()


async def test_semantic_question_forget_gate_blocks_attempt_before_provider(monkeypatch) -> None:
    handler = import_module("bot.handlers.qa")
    message = _message()
    session = AsyncMock()
    semantic = AsyncMock()
    trace_create = AsyncMock()
    finalize = AsyncMock()
    monkeypatch.setattr(
        handler.SemanticQuotaRepo,
        "reserve",
        AsyncMock(return_value=SimpleNamespace(allowed=True, replayed=False, attempt_id=3003)),
    )
    monkeypatch.setattr(handler, "_question_is_governed", AsyncMock(return_value=False))
    monkeypatch.setattr(handler.QaTraceRepo, "create", trace_create)
    monkeypatch.setattr(handler.SemanticQuotaRepo, "finalize", finalize)
    monkeypatch.setattr(handler, "run_semantic_qa", semantic)

    await handler._semantic_mention_question(
        message=message,
        session=session,
        sender_id=1001,
        persisted_chat_message_id=8501,
        question_bundle=_question_bundle(),
        query="что решили про память?",
        query_redacted=False,
    )

    trace_create.assert_not_awaited()
    semantic.assert_not_awaited()
    assert finalize.await_args.kwargs["outcome"] == "technical_failure"
    assert "удалён из памяти" in message.reply.await_args.args[0]
    assert "лимит не списан" in message.reply.await_args.args[0]


async def test_semantic_question_invalidation_after_embedding_blocks_synthesis(monkeypatch) -> None:
    handler = import_module("bot.handlers.qa")
    message = _message()
    session = AsyncMock()
    semantic_result = _qa_result()
    synthesize = AsyncMock()
    finalize = AsyncMock()

    monkeypatch.setattr(
        handler.SemanticQuotaRepo,
        "reserve",
        AsyncMock(return_value=SimpleNamespace(allowed=True, replayed=False, attempt_id=3016)),
    )
    monkeypatch.setattr(
        handler,
        "_question_is_governed",
        AsyncMock(side_effect=[True, False]),
    )
    monkeypatch.setattr(
        handler.QaTraceRepo,
        "create",
        AsyncMock(return_value=SimpleNamespace(id=5016)),
    )
    monkeypatch.setattr(handler.SemanticQuotaRepo, "attach_trace", AsyncMock())
    monkeypatch.setattr(
        handler,
        "run_semantic_qa",
        AsyncMock(
            return_value=SimpleNamespace(
                bundle=semantic_result.bundle,
                embedding_llm_call_id=4016,
            )
        ),
    )
    monkeypatch.setattr(
        handler,
        "_load_gateway_config",
        Mock(return_value=SimpleNamespace(provider="deepseek", model="deepseek-v4-flash")),
    )
    monkeypatch.setattr(handler, "_resolve_provider", Mock(return_value=object()))
    monkeypatch.setattr(handler, "synthesize_answer", synthesize)
    monkeypatch.setattr(handler.SemanticQuotaRepo, "finalize", finalize)

    await handler._semantic_mention_question(
        message=message,
        session=session,
        sender_id=1001,
        persisted_chat_message_id=8501,
        question_bundle=_question_bundle(),
        query="что решили про память?",
        query_redacted=False,
    )

    synthesize.assert_not_awaited()
    assert finalize.await_args.kwargs["outcome"] == "technical_failure"
    assert finalize.await_args.kwargs["embedding_llm_call_id"] == 4016
    assert "удалён из памяти" in message.reply.await_args.args[0]


async def test_semantic_user_scoped_flag_routes_when_global_is_off(monkeypatch) -> None:
    handler = import_module("bot.handlers.qa")
    message = _message(user_id=1001)
    session = AsyncMock()
    semantic_handler = AsyncMock()
    legacy = AsyncMock()

    async def flag_get(_session, key, scope_type=None, scope_id=None):
        if key in (handler.QA_FEATURE_FLAG, handler.LLM_SYNTHESIS_FEATURE_FLAG):
            return True
        if key == handler.SEMANTIC_QA_FEATURE_FLAG:
            return scope_type == "user" and scope_id == "1001"
        raise AssertionError(key)

    _patch_common(handler, monkeypatch)
    monkeypatch.setattr(handler.FeatureFlagRepo, "get", AsyncMock(side_effect=flag_get))
    monkeypatch.setattr(handler, "_semantic_mention_question", semantic_handler)
    monkeypatch.setattr(handler, "run_qa", legacy)

    await handler.mention_question_handler(message, _question(), session)

    semantic_handler.assert_awaited_once()
    legacy.assert_not_awaited()
    scoped_call = handler.FeatureFlagRepo.get.await_args_list[-1]
    assert scoped_call.kwargs == {"scope_type": "user", "scope_id": "1001"}


async def test_semantic_user_scoped_flag_does_not_enable_other_user(monkeypatch) -> None:
    handler = import_module("bot.handlers.qa")
    message = _message(user_id=1002)
    session = AsyncMock()
    semantic_handler = AsyncMock()
    lexical = AsyncMock(return_value=_qa_result(abstained=True))

    async def flag_get(_session, key, scope_type=None, scope_id=None):
        if key in (handler.QA_FEATURE_FLAG, handler.LLM_SYNTHESIS_FEATURE_FLAG):
            return True
        if key == handler.SEMANTIC_QA_FEATURE_FLAG:
            return scope_type == "user" and scope_id == "1001"
        raise AssertionError(key)

    _patch_common(handler, monkeypatch)
    monkeypatch.setattr(handler.FeatureFlagRepo, "get", AsyncMock(side_effect=flag_get))
    monkeypatch.setattr(handler, "_semantic_mention_question", semantic_handler)
    monkeypatch.setattr(handler, "run_qa", lexical)
    monkeypatch.setattr(handler, "_write_trace", AsyncMock())

    await handler.mention_question_handler(message, _question(), session)

    semantic_handler.assert_not_awaited()
    lexical.assert_awaited_once()
    assert "Не нашёл достаточно" in message.reply.await_args.args[0]


async def test_semantic_third_question_calls_only_ordinary_search(monkeypatch) -> None:
    handler = import_module("bot.handlers.qa")
    message = _message()
    session = AsyncMock()
    semantic = AsyncMock()
    synthesize = AsyncMock()
    ordinary = _qa_result()

    monkeypatch.setattr(
        handler.SemanticQuotaRepo,
        "reserve",
        AsyncMock(
            return_value=SimpleNamespace(
                allowed=False,
                attempt_id=3003,
                used=2,
                limit=2,
            )
        ),
    )
    monkeypatch.setattr(handler, "run_semantic_qa", semantic)
    monkeypatch.setattr(handler, "synthesize_answer", synthesize)
    monkeypatch.setattr(
        handler,
        "_ordinary_search_result",
        AsyncMock(return_value=ordinary),
    )
    monkeypatch.setattr(
        handler,
        "_bundle_users",
        AsyncMock(return_value=({}, False)),
    )
    monkeypatch.setattr(handler, "_write_trace", AsyncMock())
    reply_ordinary = AsyncMock()
    monkeypatch.setattr(
        handler,
        "_reply_with_governed_ordinary_search",
        reply_ordinary,
    )

    await handler._semantic_mention_question(
        message=message,
        session=session,
        sender_id=1001,
        persisted_chat_message_id=8501,
        question_bundle=_question_bundle(),
        query="что решили про память?",
        query_redacted=False,
    )

    semantic.assert_not_awaited()
    synthesize.assert_not_awaited()
    reply_ordinary.assert_awaited_once()
    assert "Embedding и AI-синтез не вызывались" in reply_ordinary.await_args.kwargs["notice"]


async def test_semantic_empty_retrieval_consumes_abstention_slot(monkeypatch) -> None:
    handler = import_module("bot.handlers.qa")
    message = _message()
    session = AsyncMock()
    empty = _qa_result(abstained=True)
    finalize = AsyncMock()

    monkeypatch.setattr(
        handler.SemanticQuotaRepo,
        "reserve",
        AsyncMock(
            return_value=SimpleNamespace(
                allowed=True,
                attempt_id=3004,
                used=0,
                limit=2,
            )
        ),
    )
    monkeypatch.setattr(
        handler,
        "run_semantic_qa",
        AsyncMock(
            return_value=SimpleNamespace(
                bundle=empty.bundle,
                embedding_llm_call_id=4004,
            )
        ),
    )
    monkeypatch.setattr(
        handler.QaTraceRepo,
        "create",
        AsyncMock(return_value=SimpleNamespace(id=5004)),
    )
    monkeypatch.setattr(handler.SemanticQuotaRepo, "attach_trace", AsyncMock())
    monkeypatch.setattr(handler.SemanticQuotaRepo, "finalize", finalize)

    await handler._semantic_mention_question(
        message=message,
        session=session,
        sender_id=1001,
        persisted_chat_message_id=8501,
        question_bundle=_question_bundle(),
        query="нет ответа",
        query_redacted=False,
    )

    assert finalize.await_args.kwargs["outcome"] == "abstained"
    assert finalize.await_args.kwargs["embedding_llm_call_id"] == 4004
    assert "Не нашёл достаточно" in message.reply.await_args.args[0]


async def test_semantic_trace_is_committed_before_embedding_dispatch(monkeypatch) -> None:
    handler = import_module("bot.handlers.qa")
    message = _message()
    session = AsyncMock()
    events: list[str] = []

    async def commit() -> None:
        events.append("commit")

    async def create(*_args, **_kwargs):
        events.append("trace")
        return SimpleNamespace(id=5015)

    async def attach(*_args, **_kwargs) -> None:
        events.append("attach")

    async def run(*_args, **_kwargs):
        events.append("embedding")
        return SimpleNamespace(
            bundle=_qa_result(abstained=True).bundle,
            embedding_llm_call_id=4015,
        )

    session.commit.side_effect = commit
    monkeypatch.setattr(
        handler.SemanticQuotaRepo,
        "reserve",
        AsyncMock(
            return_value=SimpleNamespace(
                allowed=True,
                replayed=False,
                attempt_id=3015,
                limit=2,
            )
        ),
    )
    monkeypatch.setattr(handler.QaTraceRepo, "create", create)
    monkeypatch.setattr(handler.SemanticQuotaRepo, "attach_trace", attach)
    monkeypatch.setattr(handler.SemanticQuotaRepo, "finalize", AsyncMock())
    monkeypatch.setattr(handler, "run_semantic_qa", run)

    await handler._semantic_mention_question(
        message=message,
        session=session,
        sender_id=1001,
        persisted_chat_message_id=8501,
        question_bundle=_question_bundle(),
        query="порядок",
        query_redacted=False,
    )

    assert events[:5] == ["commit", "trace", "attach", "commit", "embedding"]


@pytest.mark.parametrize(
    ("failure_phase", "expected_trace_id", "expected_events"),
    [
        pytest.param(
            "pre_trace",
            None,
            ("commit", "released", "commit", "fallback"),
            id="pre-trace",
        ),
        pytest.param(
            "post_trace",
            5017,
            ("commit", "commit", "released", "commit", "fallback"),
            id="post-trace",
        ),
    ],
)
async def test_semantic_database_failure_releases_before_ordinary_fallback(
    monkeypatch,
    failure_phase: str,
    expected_trace_id: int | None,
    expected_events: tuple[str, ...],
) -> None:
    handler = import_module("bot.handlers.qa")
    from sqlalchemy.exc import SQLAlchemyError

    message = _message()
    session = AsyncMock()
    events: list[str] = []

    @asynccontextmanager
    async def locked(*_args, **_kwargs):
        yield

    async def commit() -> None:
        events.append("commit")

    async def record_finalize(*_args, **_kwargs) -> None:
        events.append("released")

    async def fallback(*_args, **_kwargs) -> None:
        events.append("fallback")

    governed = AsyncMock(return_value=True)
    run_semantic = AsyncMock()
    if failure_phase == "pre_trace":
        governed.side_effect = SQLAlchemyError("question governance read failed")
    else:
        run_semantic.side_effect = SQLAlchemyError("embedding audit write failed")

    session.commit.side_effect = commit
    monkeypatch.setattr(
        handler.SemanticQuotaRepo,
        "reserve",
        AsyncMock(
            return_value=SimpleNamespace(
                allowed=True,
                replayed=False,
                attempt_id=3016,
                limit=2,
            )
        ),
    )
    monkeypatch.setattr(handler, "hold_evidence_delivery_locks", locked)
    monkeypatch.setattr(handler, "_question_is_governed", governed)
    create_trace = AsyncMock(return_value=SimpleNamespace(id=5017))
    monkeypatch.setattr(
        handler.QaTraceRepo,
        "create",
        create_trace,
    )
    monkeypatch.setattr(handler.SemanticQuotaRepo, "attach_trace", AsyncMock())
    finalize = AsyncMock(side_effect=record_finalize)
    monkeypatch.setattr(handler.SemanticQuotaRepo, "finalize", finalize)
    monkeypatch.setattr(handler, "run_semantic_qa", run_semantic)
    monkeypatch.setattr(handler, "_reply_after_technical_release", fallback)

    await handler._semantic_mention_question(
        message=message,
        session=session,
        sender_id=1001,
        persisted_chat_message_id=8501,
        question_bundle=_question_bundle(),
        query=f"ошибка {failure_phase}",
        query_redacted=False,
    )

    assert tuple(events) == expected_events
    assert finalize.await_args.kwargs == {
        "attempt_id": 3016,
        "outcome": "technical_failure",
        "qa_trace_id": expected_trace_id,
    }
    if failure_phase == "pre_trace":
        create_trace.assert_not_awaited()
        run_semantic.assert_not_awaited()
    else:
        create_trace.assert_awaited_once()
        run_semantic.assert_awaited_once()


async def test_semantic_embedding_failure_releases_slot(monkeypatch) -> None:
    handler = import_module("bot.handlers.qa")
    from bot.services.llm_providers import ProviderTransientError

    message = _message()
    session = AsyncMock()
    failure = ProviderTransientError("timeout", message="provider timeout")
    failure.llm_usage_ledger_id = 4005
    finalize = AsyncMock()

    monkeypatch.setattr(
        handler.SemanticQuotaRepo,
        "reserve",
        AsyncMock(
            return_value=SimpleNamespace(
                allowed=True,
                attempt_id=3005,
                used=0,
                limit=2,
            )
        ),
    )
    monkeypatch.setattr(handler, "run_semantic_qa", AsyncMock(side_effect=failure))
    monkeypatch.setattr(
        handler,
        "_ordinary_search_result",
        AsyncMock(return_value=_qa_result()),
    )
    monkeypatch.setattr(
        handler.QaTraceRepo,
        "create",
        AsyncMock(return_value=SimpleNamespace(id=5005)),
    )
    monkeypatch.setattr(handler.SemanticQuotaRepo, "attach_trace", AsyncMock())
    monkeypatch.setattr(handler.SemanticQuotaRepo, "finalize", finalize)
    monkeypatch.setattr(
        handler,
        "_bundle_users",
        AsyncMock(return_value=({}, False)),
    )
    monkeypatch.setattr(
        handler,
        "_reply_with_governed_ordinary_search",
        AsyncMock(),
    )

    await handler._semantic_mention_question(
        message=message,
        session=session,
        sender_id=1001,
        persisted_chat_message_id=8501,
        question_bundle=_question_bundle(),
        query="техническая ошибка",
        query_redacted=False,
    )

    assert finalize.await_args.kwargs["outcome"] == "technical_failure"
    assert finalize.await_args.kwargs["embedding_llm_call_id"] == 4005


async def test_semantic_answer_has_valid_link_and_consumes_slot(monkeypatch) -> None:
    handler = import_module("bot.handlers.qa")
    from bot.services.llm_gateway import AnswerWithCitations

    message = _message()
    session = AsyncMock()
    semantic_result = _qa_result()
    finalize = AsyncMock()
    author = SimpleNamespace(first_name="Author", last_name=None, username="author")
    lock_depth = 0
    lock_entries = 0

    @asynccontextmanager
    async def sequential_lock_scope(*_args, **_kwargs):
        nonlocal lock_depth, lock_entries
        lock_depth += 1
        lock_entries += 1
        assert lock_depth == 1, "semantic governance locks must never be nested"
        try:
            yield
        finally:
            lock_depth -= 1

    monkeypatch.setattr(
        handler.SemanticQuotaRepo,
        "reserve",
        AsyncMock(
            return_value=SimpleNamespace(
                allowed=True,
                attempt_id=3006,
                used=0,
                limit=2,
            )
        ),
    )
    monkeypatch.setattr(
        handler,
        "run_semantic_qa",
        AsyncMock(
            return_value=SimpleNamespace(
                bundle=semantic_result.bundle,
                embedding_llm_call_id=4006,
            )
        ),
    )
    monkeypatch.setattr(
        handler,
        "_bundle_users",
        AsyncMock(return_value=({2002: author}, False)),
    )
    monkeypatch.setattr(
        handler.QaTraceRepo,
        "create",
        AsyncMock(return_value=SimpleNamespace(id=5006)),
    )
    monkeypatch.setattr(handler.SemanticQuotaRepo, "attach_trace", AsyncMock())
    monkeypatch.setattr(handler.QaTraceRepo, "update_llm_fields", AsyncMock())
    monkeypatch.setattr(
        handler,
        "filter_surviving_evidence",
        AsyncMock(return_value=semantic_result.bundle),
    )
    monkeypatch.setattr(handler.SemanticQuotaRepo, "finalize", finalize)
    monkeypatch.setattr(handler, "hold_evidence_delivery_locks", sequential_lock_scope)
    monkeypatch.setattr(
        handler,
        "_load_gateway_config",
        Mock(
            return_value=SimpleNamespace(
                provider="deepseek",
                model="deepseek-v4-flash",
            )
        ),
    )
    monkeypatch.setattr(handler, "_resolve_provider", Mock(return_value=object()))
    monkeypatch.setattr(
        handler,
        "synthesize_answer",
        AsyncMock(
            return_value=AnswerWithCitations(
                answer_text="Решение подтверждено [[mv:101]]",
                citation_ids=(101,),
                cost_usd=Decimal("0.001"),
                cache_hit=False,
                llm_call_id=6006,
                surviving_evidence_ids=(101,),
            )
        ),
    )

    await handler._semantic_mention_question(
        message=message,
        session=session,
        sender_id=1001,
        persisted_chat_message_id=8501,
        question_bundle=_question_bundle(),
        query="что решили",
        query_redacted=False,
    )

    assert finalize.await_args.kwargs["outcome"] == "answered"
    assert handler.synthesize_answer.await_args.kwargs["max_evidence_items"] == 5
    assert handler.synthesize_answer.await_args.kwargs["durable_placeholder"] is True
    assert handler.synthesize_answer.await_args.kwargs["revalidate_after_provider"] is True
    assert handler.synthesize_answer.await_args.kwargs["cache_enabled"] is False
    assert lock_entries == 5
    assert lock_depth == 0
    handler.SemanticQuotaRepo.attach_trace.assert_awaited_once_with(
        session,
        attempt_id=3006,
        qa_trace_id=5006,
    )
    reply = message.reply.await_args.args[0]
    assert "Решение подтверждено [1]" in reply
    assert 'href="https://t.me/c/1234567890/77"' in reply
    assert "[[mv:" not in reply
    stored_summary = handler.QaTraceRepo.update_llm_fields.await_args.kwargs["llm_response_summary"]
    assert stored_summary.startswith(handler._SEMANTIC_REPLAY_HTML_PREFIX)
    assert 'href="https://t.me/c/1234567890/77"' in stored_summary


async def test_semantic_reserved_replay_never_dispatches_providers(monkeypatch) -> None:
    handler = import_module("bot.handlers.qa")
    message = _message()
    session = AsyncMock()
    semantic = AsyncMock()
    synthesis = AsyncMock()

    monkeypatch.setattr(
        handler.SemanticQuotaRepo,
        "reserve",
        AsyncMock(
            return_value=SimpleNamespace(
                allowed=False,
                replayed=True,
                status="reserved",
                outcome=None,
                attempt_id=3010,
                limit=2,
            )
        ),
    )
    monkeypatch.setattr(
        handler.QaTraceRepo,
        "get_by_source_chat_message_id",
        AsyncMock(return_value=SimpleNamespace(llm_response_summary=None)),
    )
    monkeypatch.setattr(handler, "run_semantic_qa", semantic)
    monkeypatch.setattr(handler, "synthesize_answer", synthesis)

    await handler._semantic_mention_question(
        message=message,
        session=session,
        sender_id=1001,
        persisted_chat_message_id=8501,
        question_bundle=_question_bundle(),
        query="повтор",
        query_redacted=False,
    )

    semantic.assert_not_awaited()
    synthesis.assert_not_awaited()
    assert "уже обрабатывается" in message.reply.await_args.args[0]


async def test_semantic_answer_replay_never_emits_stored_content(monkeypatch) -> None:
    handler = import_module("bot.handlers.qa")
    message = _message()
    session = AsyncMock()
    rendered = (
        'Готово [1]\n\n<b>Источники:</b>\n[1] <a href="https://t.me/c/1234567890/77">источник</a>'
    )

    monkeypatch.setattr(
        handler.SemanticQuotaRepo,
        "reserve",
        AsyncMock(
            return_value=SimpleNamespace(
                allowed=False,
                replayed=True,
                status="consumed",
                outcome="answered",
                attempt_id=3011,
                limit=2,
            )
        ),
    )
    monkeypatch.setattr(
        handler.QaTraceRepo,
        "get_by_source_chat_message_id",
        AsyncMock(
            return_value=SimpleNamespace(
                llm_response_summary=f"{handler._SEMANTIC_REPLAY_HTML_PREFIX}{rendered}"
            )
        ),
    )
    semantic = AsyncMock()
    synthesis = AsyncMock()
    monkeypatch.setattr(handler, "run_semantic_qa", semantic)
    monkeypatch.setattr(handler, "synthesize_answer", synthesis)

    await handler._semantic_mention_question(
        message=message,
        session=session,
        sender_id=1001,
        persisted_chat_message_id=8501,
        question_bundle=_question_bundle(),
        query="повтор",
        query_redacted=False,
    )

    semantic.assert_not_awaited()
    synthesis.assert_not_awaited()
    assert rendered not in message.reply.await_args.args[0]
    assert "провайдеры повторно не вызывались" in message.reply.await_args.args[0]


@pytest.mark.parametrize(
    ("reason", "expected_outcome"),
    [("all_filtered", "abstained"), ("budget_exceeded", "technical_failure")],
)
async def test_semantic_abstention_classifies_quota_outcome(
    monkeypatch,
    reason: str,
    expected_outcome: str,
) -> None:
    handler = import_module("bot.handlers.qa")
    from bot.services.llm_gateway import Abstention

    message = _message()
    session = AsyncMock()
    finalize = AsyncMock()
    monkeypatch.setattr(
        handler.SemanticQuotaRepo,
        "reserve",
        AsyncMock(
            return_value=SimpleNamespace(
                allowed=True,
                replayed=False,
                attempt_id=3012,
                limit=2,
            )
        ),
    )
    monkeypatch.setattr(
        handler.QaTraceRepo,
        "create",
        AsyncMock(return_value=SimpleNamespace(id=5012)),
    )
    monkeypatch.setattr(handler.QaTraceRepo, "update_llm_fields", AsyncMock())
    monkeypatch.setattr(handler.SemanticQuotaRepo, "attach_trace", AsyncMock())
    monkeypatch.setattr(handler.SemanticQuotaRepo, "finalize", finalize)
    monkeypatch.setattr(
        handler,
        "filter_surviving_evidence",
        AsyncMock(return_value=_qa_result().bundle),
    )
    monkeypatch.setattr(
        handler,
        "run_semantic_qa",
        AsyncMock(
            return_value=SimpleNamespace(
                bundle=_qa_result().bundle,
                embedding_llm_call_id=4012,
            )
        ),
    )
    monkeypatch.setattr(
        handler,
        "_load_gateway_config",
        Mock(return_value=SimpleNamespace(provider="deepseek", model="deepseek-v4-flash")),
    )
    monkeypatch.setattr(handler, "_resolve_provider", Mock(return_value=object()))
    monkeypatch.setattr(
        handler,
        "synthesize_answer",
        AsyncMock(
            return_value=Abstention(
                reason=reason,
                cost_usd=Decimal("0"),
                llm_call_id=6012,
            )
        ),
    )
    monkeypatch.setattr(
        handler,
        "_reply_after_technical_release",
        AsyncMock(),
    )

    await handler._semantic_mention_question(
        message=message,
        session=session,
        sender_id=1001,
        persisted_chat_message_id=8501,
        question_bundle=_question_bundle(),
        query="классификация",
        query_redacted=False,
    )

    assert finalize.await_args.kwargs["outcome"] == expected_outcome


async def test_semantic_retrieval_trace_revalidation_change_abstains_without_leak(
    monkeypatch,
) -> None:
    handler = import_module("bot.handlers.qa")
    from bot.services.llm_gateway import AnswerWithCitations

    message = _message()
    session = AsyncMock()
    original = _qa_result(snippet="PRIVATE-FORGOTTEN-SNIPPET")
    empty = _qa_result(abstained=True)
    finalize = AsyncMock()
    monkeypatch.setattr(
        handler.SemanticQuotaRepo,
        "reserve",
        AsyncMock(
            return_value=SimpleNamespace(
                allowed=True,
                replayed=False,
                attempt_id=3013,
                limit=2,
            )
        ),
    )
    monkeypatch.setattr(
        handler.QaTraceRepo,
        "create",
        AsyncMock(return_value=SimpleNamespace(id=5013)),
    )
    monkeypatch.setattr(handler.QaTraceRepo, "update_retrieval_fields", AsyncMock())
    monkeypatch.setattr(handler.QaTraceRepo, "update_llm_fields", AsyncMock())
    monkeypatch.setattr(handler.SemanticQuotaRepo, "attach_trace", AsyncMock())
    monkeypatch.setattr(handler.SemanticQuotaRepo, "finalize", finalize)
    monkeypatch.setattr(
        handler,
        "run_semantic_qa",
        AsyncMock(
            return_value=SimpleNamespace(
                bundle=original.bundle,
                embedding_llm_call_id=4013,
            )
        ),
    )
    monkeypatch.setattr(
        handler,
        "_load_gateway_config",
        Mock(return_value=SimpleNamespace(provider="deepseek", model="deepseek-v4-flash")),
    )
    monkeypatch.setattr(handler, "_resolve_provider", Mock(return_value=object()))
    monkeypatch.setattr(
        handler,
        "synthesize_answer",
        AsyncMock(
            return_value=AnswerWithCitations(
                answer_text="Ответ [[mv:101]]",
                citation_ids=(101,),
                cost_usd=Decimal("0.001"),
                cache_hit=False,
                llm_call_id=6013,
                surviving_evidence_ids=(101,),
            )
        ),
    )
    monkeypatch.setattr(
        handler,
        "filter_surviving_evidence",
        AsyncMock(return_value=empty.bundle),
    )

    await handler._semantic_mention_question(
        message=message,
        session=session,
        sender_id=1001,
        persisted_chat_message_id=8501,
        question_bundle=_question_bundle(),
        query="гонка forget",
        query_redacted=False,
    )

    assert finalize.await_args.kwargs["outcome"] == "abstained"
    handler.persist_semantic_retrieval_trace.assert_not_awaited()
    handler.synthesize_answer.assert_not_awaited()
    reply = message.reply.await_args.args[0]
    assert "PRIVATE-FORGOTTEN-SNIPPET" not in reply
    assert "Не нашёл достаточно" in reply


async def test_semantic_technical_release_precedes_failing_ordinary_fallback(
    monkeypatch,
) -> None:
    handler = import_module("bot.handlers.qa")
    from sqlalchemy.exc import SQLAlchemyError
    from bot.services.llm_providers import ProviderTransientError

    message = _message()
    session = AsyncMock()
    events: list[str] = []
    failure = ProviderTransientError("timeout", message="provider timeout")
    failure.llm_usage_ledger_id = 4014

    async def finalize(*_args, **_kwargs) -> None:
        events.append("released")

    async def fallback(*_args, **_kwargs):
        events.append("fallback")
        raise SQLAlchemyError("ordinary search failed")

    monkeypatch.setattr(
        handler.SemanticQuotaRepo,
        "reserve",
        AsyncMock(
            return_value=SimpleNamespace(
                allowed=True,
                replayed=False,
                attempt_id=3014,
                limit=2,
            )
        ),
    )
    monkeypatch.setattr(
        handler.QaTraceRepo,
        "create",
        AsyncMock(return_value=SimpleNamespace(id=5014)),
    )
    monkeypatch.setattr(handler.SemanticQuotaRepo, "attach_trace", AsyncMock())
    monkeypatch.setattr(handler.SemanticQuotaRepo, "finalize", finalize)
    monkeypatch.setattr(handler, "run_semantic_qa", AsyncMock(side_effect=failure))
    monkeypatch.setattr(handler, "_ordinary_search_result", fallback)

    await handler._semantic_mention_question(
        message=message,
        session=session,
        sender_id=1001,
        persisted_chat_message_id=8501,
        question_bundle=_question_bundle(),
        query="ошибка fallback",
        query_redacted=False,
    )

    assert events == ["released", "fallback"]
    assert "Лимит не списан" in message.reply.await_args.args[0]


async def test_semantic_quota_consumes_only_after_successful_delivery(monkeypatch) -> None:
    handler = import_module("bot.handlers.qa")
    session = AsyncMock()
    message = _message()
    events: list[str] = []

    async def reply(*_args, **_kwargs) -> None:
        events.append("delivered")

    async def finalize(*_args, **kwargs) -> None:
        events.append(f"finalized:{kwargs['outcome']}")

    async def mark_delivery(*_args, **_kwargs) -> None:
        events.append("delivery-intent")

    monkeypatch.setattr(handler, "_reply_to_mention", reply)
    monkeypatch.setattr(handler.SemanticQuotaRepo, "mark_delivery_started", mark_delivery)
    monkeypatch.setattr(handler.SemanticQuotaRepo, "finalize", finalize)

    await handler._deliver_and_consume_semantic_attempt(
        message,
        session,
        attempt_id=31,
        outcome="answered",
        qa_trace_id=41,
        embedding_llm_call_id=51,
        synthesis_llm_call_id=61,
        reply_text="answer",
    )

    assert events == ["delivery-intent", "delivered", "finalized:answered"]


async def test_post_delivery_commit_failure_keeps_durable_delivery_intent(monkeypatch) -> None:
    from sqlalchemy.exc import SQLAlchemyError

    handler = import_module("bot.handlers.qa")
    session = AsyncMock()
    message = _message()
    commits = 0

    async def commit() -> None:
        nonlocal commits
        commits += 1
        if commits == 3:
            raise SQLAlchemyError("post-delivery commit failed")

    intent = AsyncMock()
    finalize = AsyncMock()
    reply = AsyncMock()
    session.commit.side_effect = commit
    monkeypatch.setattr(handler.SemanticQuotaRepo, "mark_delivery_started", intent)
    monkeypatch.setattr(handler.SemanticQuotaRepo, "finalize", finalize)
    monkeypatch.setattr(handler, "_reply_to_mention", reply)

    with pytest.raises(SQLAlchemyError, match="post-delivery"):
        await handler._deliver_and_consume_semantic_attempt(
            message,
            session,
            attempt_id=33,
            outcome="answered",
            qa_trace_id=43,
            embedding_llm_call_id=53,
            synthesis_llm_call_id=63,
            reply_text="delivered answer",
        )

    intent.assert_awaited_once()
    reply.assert_awaited_once()
    finalize.assert_awaited_once()
    assert commits == 3


async def test_pre_delivery_intent_commit_failure_releases_slot(monkeypatch) -> None:
    from sqlalchemy.exc import SQLAlchemyError

    handler = import_module("bot.handlers.qa")
    session = AsyncMock()
    message = _message()
    commits = 0

    async def commit() -> None:
        nonlocal commits
        commits += 1
        if commits == 2:
            raise SQLAlchemyError("intent commit failed")

    intent = AsyncMock()
    finalize = AsyncMock()
    reply = AsyncMock()
    clear = AsyncMock()
    session.commit.side_effect = commit
    monkeypatch.setattr(handler.SemanticQuotaRepo, "mark_delivery_started", intent)
    monkeypatch.setattr(handler.SemanticQuotaRepo, "finalize", finalize)
    monkeypatch.setattr(handler.QaTraceRepo, "clear_undelivered_llm_summary", clear)
    monkeypatch.setattr(handler, "_reply_to_mention", reply)

    await handler._deliver_and_consume_semantic_attempt(
        message,
        session,
        attempt_id=36,
        outcome="answered",
        qa_trace_id=46,
        embedding_llm_call_id=56,
        synthesis_llm_call_id=66,
        reply_text="must not be sent",
        clear_summary_on_failure=True,
    )

    intent.assert_awaited_once()
    clear.assert_awaited_once_with(session, qa_trace_id=46)
    assert finalize.await_args.kwargs == {
        "attempt_id": 36,
        "outcome": "technical_failure",
        "qa_trace_id": 46,
        "embedding_llm_call_id": 56,
        "synthesis_llm_call_id": 66,
    }
    assert reply.await_count == 1
    assert "лимит не списан" in reply.await_args.args[1]
    assert commits == 3


async def test_delivery_revalidation_database_failure_releases_slot(monkeypatch) -> None:
    from sqlalchemy.exc import SQLAlchemyError

    handler = import_module("bot.handlers.qa")
    session = AsyncMock()
    message = _message()
    bundle = _qa_result().bundle

    @asynccontextmanager
    async def locked(*_args, **_kwargs):
        yield

    finalize = AsyncMock()
    intent = AsyncMock()
    reply = AsyncMock()
    monkeypatch.setattr(handler, "hold_evidence_delivery_locks", locked)
    monkeypatch.setattr(
        handler,
        "filter_surviving_evidence",
        AsyncMock(side_effect=SQLAlchemyError("revalidation failed")),
    )
    monkeypatch.setattr(handler.SemanticQuotaRepo, "mark_delivery_started", intent)
    monkeypatch.setattr(handler.SemanticQuotaRepo, "finalize", finalize)
    monkeypatch.setattr(handler, "_reply_to_mention", reply)

    await handler._deliver_and_consume_semantic_attempt(
        message,
        session,
        attempt_id=37,
        outcome="answered",
        qa_trace_id=47,
        embedding_llm_call_id=57,
        synthesis_llm_call_id=67,
        reply_text="must not be sent",
        delivery_bundle=bundle,
        expected_evidence_ids=tuple(bundle.evidence_ids),
    )

    intent.assert_not_awaited()
    assert finalize.await_args.kwargs["outcome"] == "technical_failure"
    assert finalize.await_args.kwargs["embedding_llm_call_id"] == 57
    assert finalize.await_args.kwargs["synthesis_llm_call_id"] == 67
    assert "лимит не списан" in reply.await_args.args[1]


async def test_final_delivery_gate_blocks_invalidated_evidence(monkeypatch) -> None:
    handler = import_module("bot.handlers.qa")
    session = AsyncMock()
    message = _message()
    original = _qa_result().bundle
    empty = _qa_result(abstained=True).bundle

    @asynccontextmanager
    async def unlocked(*_args, **_kwargs):
        yield

    invalidate = AsyncMock()
    clear = AsyncMock()
    reply = AsyncMock()
    monkeypatch.setattr(handler, "hold_evidence_delivery_locks", unlocked)
    monkeypatch.setattr(handler, "filter_surviving_evidence", AsyncMock(return_value=empty))
    monkeypatch.setattr(handler.SynthesisCacheRepo, "invalidate_by_citation", invalidate)
    monkeypatch.setattr(handler.QaTraceRepo, "clear_undelivered_llm_summary", clear)
    monkeypatch.setattr(handler, "_reply_to_mention", reply)

    with pytest.raises(handler.EvidenceInvalidatedBeforeDelivery):
        await handler._deliver_and_consume_semantic_attempt(
            message,
            session,
            attempt_id=34,
            outcome="answered",
            qa_trace_id=44,
            embedding_llm_call_id=54,
            synthesis_llm_call_id=64,
            reply_text="must not leak",
            clear_summary_on_failure=True,
            delivery_bundle=original,
            expected_evidence_ids=tuple(original.evidence_ids),
        )

    invalidate.assert_awaited()
    clear.assert_awaited_once_with(session, qa_trace_id=44)
    reply.assert_not_awaited()


async def test_generic_semantic_delivery_revalidates_required_question(monkeypatch) -> None:
    handler = import_module("bot.handlers.qa")
    session = AsyncMock()
    message = _message()
    finalize = AsyncMock()
    monkeypatch.setattr(handler, "_question_is_governed", AsyncMock(return_value=False))
    monkeypatch.setattr(handler.SemanticQuotaRepo, "finalize", finalize)

    await handler._deliver_and_consume_semantic_attempt(
        message,
        session,
        attempt_id=39,
        outcome="abstained",
        qa_trace_id=49,
        embedding_llm_call_id=59,
        synthesis_llm_call_id=None,
        reply_text="must not be delivered",
        required_governed_bundle=_question_bundle(),
    )

    handler.SemanticQuotaRepo.mark_delivery_started.assert_not_awaited()
    assert finalize.await_args.kwargs["outcome"] == "technical_failure"
    assert "must not be delivered" not in message.reply.await_args.args[0]
    assert "удалён из памяти" in message.reply.await_args.args[0]


async def test_semantic_ambiguous_delivery_failure_consumes_and_preserves_summary(
    monkeypatch,
) -> None:
    handler = import_module("bot.handlers.qa")
    from aiogram.exceptions import TelegramNetworkError

    session = AsyncMock()
    message = _message()
    finalize = AsyncMock()
    clear = AsyncMock()
    failure = TelegramNetworkError(method=object(), message="network down")
    monkeypatch.setattr(handler, "_reply_to_mention", AsyncMock(side_effect=failure))
    monkeypatch.setattr(handler.SemanticQuotaRepo, "finalize", finalize)
    monkeypatch.setattr(handler.QaTraceRepo, "clear_undelivered_llm_summary", clear)

    with pytest.raises(TelegramNetworkError):
        await handler._deliver_and_consume_semantic_attempt(
            message,
            session,
            attempt_id=32,
            outcome="answered",
            qa_trace_id=42,
            embedding_llm_call_id=52,
            synthesis_llm_call_id=62,
            reply_text="undelivered answer",
            clear_summary_on_failure=True,
        )

    clear.assert_not_awaited()
    assert finalize.await_args.kwargs["outcome"] == "answered"


async def test_semantic_definitive_delivery_rejection_releases_and_clears_summary(
    monkeypatch,
) -> None:
    handler = import_module("bot.handlers.qa")
    from aiogram.exceptions import TelegramForbiddenError

    session = AsyncMock()
    message = _message()
    finalize = AsyncMock()
    clear = AsyncMock()
    failure = TelegramForbiddenError(method=object(), message="bot blocked")
    monkeypatch.setattr(handler, "_reply_to_mention", AsyncMock(side_effect=failure))
    monkeypatch.setattr(handler.SemanticQuotaRepo, "finalize", finalize)
    monkeypatch.setattr(handler.QaTraceRepo, "clear_undelivered_llm_summary", clear)

    with pytest.raises(TelegramForbiddenError):
        await handler._deliver_and_consume_semantic_attempt(
            message,
            session,
            attempt_id=35,
            outcome="answered",
            qa_trace_id=45,
            embedding_llm_call_id=55,
            synthesis_llm_call_id=65,
            reply_text="definitively rejected answer",
            clear_summary_on_failure=True,
        )

    clear.assert_awaited_once_with(session, qa_trace_id=45)
    assert finalize.await_args.kwargs["outcome"] == "technical_failure"


async def test_ordinary_delivery_gate_refuses_changed_evidence(monkeypatch) -> None:
    handler = import_module("bot.handlers.qa")
    session = AsyncMock()
    message = _message()
    original = _qa_result().bundle
    empty = _qa_result(abstained=True).bundle

    @asynccontextmanager
    async def locked(*_args, **_kwargs):
        yield

    reply = AsyncMock()
    monkeypatch.setattr(handler, "hold_evidence_delivery_locks", locked)
    monkeypatch.setattr(handler, "filter_surviving_evidence", AsyncMock(return_value=empty))
    monkeypatch.setattr(handler, "_reply_to_mention", reply)

    await handler._reply_with_governed_ordinary_search(
        message,
        session,
        bundle=original,
        notice="Обычный поиск",
    )

    assert "Источники изменились" in reply.await_args.args[1]
    assert "evidence" not in reply.await_args.args[1]
