"""T5-04: /recall handler 4-step ORDER + flag mechanism (contracts.md §6.1).

Tests cover the LLM-synthesis branch of ``bot/handlers/qa.py::recall_handler``:
the four-step ordering, feature-flag gate, kwargs passed to ``synthesize_answer``,
audit-trace shape, HTML escaping, exception fallback, and provider resolution.

All tests mock ``synthesize_answer`` (or inject a fake) via monkeypatch so no
real DB nor LLM SDK is touched. Phase 4 byte-for-byte preservation lives in
``test_qa_recall_phase4_preserved.py``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.conftest import import_module

pytestmark = pytest.mark.usefixtures("app_env")

COMMUNITY_CHAT_ID = -1001234567890


# ─── shared fixtures (mirror tests/handlers/test_qa.py style) ─────────────────


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


def _qa_result(*, abstained: bool, query_redacted: bool = False):
    from bot.services.evidence import EvidenceBundle, EvidenceItem
    from bot.services.qa import QaResult

    now = datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc)
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


def _patch_persist(handler, monkeypatch) -> None:
    from bot.services.message_persistence import PersistResult
    fake_cm = SimpleNamespace(id=1, current_version_id=None)
    monkeypatch.setattr(
        handler,
        "persist_message_with_policy",
        AsyncMock(return_value=PersistResult(
            chat_message=fake_cm, policy="normal", is_offrecord_mark_created=False
        )),
    )
    monkeypatch.setattr(handler.UserRepo, "upsert", AsyncMock())


def _flag_get(
    *,
    qa_enabled: bool = True,
    llm_synthesis_enabled: bool = False,
):
    """Build an AsyncMock-compatible function for ``FeatureFlagRepo.get``.

    Resolves to ``qa_enabled`` for ``memory.qa.enabled`` and to
    ``llm_synthesis_enabled`` for ``memory.qa.llm_synthesis.enabled``. Defaults
    to False for any other key.
    """

    async def _impl(session, flag_key, *args, **kwargs):
        if flag_key == "memory.qa.enabled":
            return qa_enabled
        if flag_key == "memory.qa.llm_synthesis.enabled":
            return llm_synthesis_enabled
        return False

    return _impl


def _fake_trace(trace_id: int = 7777):
    return SimpleNamespace(id=trace_id)


def _answer_with_citations(
    *,
    answer_text: str = "Помню разговор про память.",
    citation_ids: tuple[int, ...] = (500,),
    cost_usd: Decimal = Decimal("0.001234"),
    llm_call_id: int = 4242,
):
    from bot.services.llm_gateway import AnswerWithCitations

    return AnswerWithCitations(
        answer_text=answer_text,
        citation_ids=citation_ids,
        cost_usd=cost_usd,
        cache_hit=False,
        llm_call_id=llm_call_id,
    )


def _abstention(
    *,
    reason: str = "all_filtered",
    cost_usd: Decimal = Decimal("0"),
    llm_call_id: int = 5151,
):
    from bot.services.llm_gateway import Abstention

    return Abstention(
        reason=reason,  # type: ignore[arg-type]
        cost_usd=cost_usd,
        llm_call_id=llm_call_id,
    )


# ─── tests ────────────────────────────────────────────────────────────────────


async def test_step_ordering_create_trace_before_synthesize(monkeypatch) -> None:
    """Step 1 (create QaTrace) MUST complete before Step 2 (synthesize_answer).

    Asserts via a recorder list that ``QaTraceRepo.create`` is observed BEFORE
    the gateway spy. Catches a future refactor that reverts to
    create-trace-after-synthesis.
    """
    handler = import_module("bot.handlers.qa")
    message = _message()
    session = AsyncMock()

    call_order: list[str] = []

    async def trace_create(*args, **kwargs):
        call_order.append("create_trace")
        return _fake_trace()

    async def update_llm(*args, **kwargs):
        call_order.append("update_llm_fields")
        return 1

    async def synth_spy(*args, **kwargs):
        call_order.append("synthesize_answer")
        return _answer_with_citations()

    _patch_persist(handler, monkeypatch)
    monkeypatch.setattr(
        handler.FeatureFlagRepo, "get", _flag_get(llm_synthesis_enabled=True)
    )
    monkeypatch.setattr(
        handler.UserRepo,
        "get",
        AsyncMock(side_effect=[_user(), _user(user_id=2002, first_name="Author")]),
    )
    monkeypatch.setattr(handler.QaTraceRepo, "create", trace_create)
    monkeypatch.setattr(handler.QaTraceRepo, "update_llm_fields", update_llm)
    monkeypatch.setattr(handler, "run_qa", AsyncMock(return_value=_qa_result(abstained=False)))
    monkeypatch.setattr(handler, "synthesize_answer", synth_spy)

    await handler.recall_handler(message, _command("память"), session)

    assert call_order == ["create_trace", "synthesize_answer", "update_llm_fields"], (
        f"4-step ORDER violated: {call_order}"
    )


async def test_flag_off_runs_phase4_path(monkeypatch) -> None:
    """LLM synthesis flag OFF → existing Phase 4 path. No synthesize_answer."""
    handler = import_module("bot.handlers.qa")
    message = _message()
    session = AsyncMock()
    synth = AsyncMock()

    _patch_persist(handler, monkeypatch)
    monkeypatch.setattr(
        handler.FeatureFlagRepo, "get", _flag_get(llm_synthesis_enabled=False)
    )
    monkeypatch.setattr(
        handler.UserRepo,
        "get",
        AsyncMock(side_effect=[_user(), _user(user_id=2002, first_name="Author")]),
    )
    monkeypatch.setattr(handler.QaTraceRepo, "create", AsyncMock())
    monkeypatch.setattr(handler, "run_qa", AsyncMock(return_value=_qa_result(abstained=False)))
    monkeypatch.setattr(handler, "synthesize_answer", synth)

    await handler.recall_handler(message, _command("память"), session)

    synth.assert_not_awaited()
    # Phase 4 reply rendered (Найденные свидетельства marker present).
    response = message.reply.call_args.args[0]
    assert "<b>Найденные свидетельства:</b>" in response


async def test_flag_on_empty_bundle_skips_synthesize(monkeypatch) -> None:
    """Flag ON + abstained bundle → synthesize_answer NOT called. Phase 4 abstention reply."""
    handler = import_module("bot.handlers.qa")
    message = _message()
    session = AsyncMock()
    synth = AsyncMock()

    _patch_persist(handler, monkeypatch)
    monkeypatch.setattr(
        handler.FeatureFlagRepo, "get", _flag_get(llm_synthesis_enabled=True)
    )
    monkeypatch.setattr(handler.UserRepo, "get", AsyncMock(return_value=_user()))
    monkeypatch.setattr(handler.QaTraceRepo, "create", AsyncMock())
    monkeypatch.setattr(handler, "run_qa", AsyncMock(return_value=_qa_result(abstained=True)))
    monkeypatch.setattr(handler, "synthesize_answer", synth)

    await handler.recall_handler(message, _command("ничего"), session)

    synth.assert_not_awaited()
    assert message.reply.call_args.args[0] == "Не нашёл подходящих свидетельств в истории чата."


async def test_synthesize_answer_called_with_all_required_kwargs(monkeypatch) -> None:
    """synthesize_answer called with bundle, query, config, qa_trace_id,
    ledger_repo, cache_repo, provider — all 7 kwargs.
    """
    handler = import_module("bot.handlers.qa")
    message = _message()
    session = AsyncMock()
    captured: dict = {}

    async def synth_spy(s, *, bundle, query, config, qa_trace_id, ledger_repo, cache_repo, provider, **rest):
        captured["session"] = s
        captured["bundle"] = bundle
        captured["query"] = query
        captured["config"] = config
        captured["qa_trace_id"] = qa_trace_id
        captured["ledger_repo"] = ledger_repo
        captured["cache_repo"] = cache_repo
        captured["provider"] = provider
        return _answer_with_citations()

    _patch_persist(handler, monkeypatch)
    monkeypatch.setattr(
        handler.FeatureFlagRepo, "get", _flag_get(llm_synthesis_enabled=True)
    )
    monkeypatch.setattr(
        handler.UserRepo,
        "get",
        AsyncMock(side_effect=[_user(), _user(user_id=2002, first_name="Author")]),
    )
    monkeypatch.setattr(handler.QaTraceRepo, "create", AsyncMock(return_value=_fake_trace(trace_id=999)))
    monkeypatch.setattr(handler.QaTraceRepo, "update_llm_fields", AsyncMock(return_value=1))
    monkeypatch.setattr(handler, "run_qa", AsyncMock(return_value=_qa_result(abstained=False)))
    monkeypatch.setattr(handler, "synthesize_answer", synth_spy)

    await handler.recall_handler(message, _command("память"), session)

    assert captured["session"] is session
    assert captured["query"] == "память"
    assert captured["qa_trace_id"] == 999
    # bundle is the Phase 4 EvidenceBundle (non-abstained, 1 item)
    assert captured["bundle"].evidence_ids == [500]
    # config is an LLMGatewayConfig with v1.0.0 prompt template version
    assert captured["config"].prompt_template_version == "v1.0.0"
    # ledger_repo / cache_repo / provider are concrete repo instances (Protocol-satisfying)
    assert captured["ledger_repo"] is not None
    assert captured["cache_repo"] is not None
    assert captured["provider"] is not None


async def test_answer_with_citations_renders_and_updates_trace(monkeypatch) -> None:
    """AnswerWithCitations result → _format_synthesized_response rendered.
    update_llm_fields called with answer_text + cost_usd + llm_call_id from result.
    """
    handler = import_module("bot.handlers.qa")
    message = _message()
    session = AsyncMock()
    update_spy = AsyncMock(return_value=1)
    answer = _answer_with_citations(
        answer_text="Помню сильный разговор про память.",
        cost_usd=Decimal("0.005555"),
        llm_call_id=8888,
    )

    _patch_persist(handler, monkeypatch)
    monkeypatch.setattr(
        handler.FeatureFlagRepo, "get", _flag_get(llm_synthesis_enabled=True)
    )
    monkeypatch.setattr(
        handler.UserRepo,
        "get",
        AsyncMock(side_effect=[_user(), _user(user_id=2002, first_name="Author")]),
    )
    monkeypatch.setattr(handler.QaTraceRepo, "create", AsyncMock(return_value=_fake_trace(trace_id=42)))
    monkeypatch.setattr(handler.QaTraceRepo, "update_llm_fields", update_spy)
    monkeypatch.setattr(handler, "run_qa", AsyncMock(return_value=_qa_result(abstained=False)))
    monkeypatch.setattr(handler, "synthesize_answer", AsyncMock(return_value=answer))

    await handler.recall_handler(message, _command("память"), session)

    update_spy.assert_awaited_once()
    kwargs = update_spy.await_args.kwargs
    assert kwargs["qa_trace_id"] == 42
    assert kwargs["llm_call_id"] == 8888
    assert kwargs["llm_response_summary"] == "Помню сильный разговор про память."
    assert kwargs["cost_usd"] == Decimal("0.005555")
    assert kwargs["llm_response_redacted"] is False

    response = message.reply.call_args.args[0]
    assert "Помню сильный разговор про память." in response
    assert "<b>Источники:</b>" in response


async def test_abstention_falls_back_to_phase4_reply(monkeypatch) -> None:
    """Abstention result → Phase 4 reply (_format_response) rendered.
    update_llm_fields called with llm_response_summary=None + cost_usd from result.
    """
    handler = import_module("bot.handlers.qa")
    message = _message()
    session = AsyncMock()
    update_spy = AsyncMock(return_value=1)
    abst = _abstention(reason="all_filtered", cost_usd=Decimal("0"), llm_call_id=7373)

    _patch_persist(handler, monkeypatch)
    monkeypatch.setattr(
        handler.FeatureFlagRepo, "get", _flag_get(llm_synthesis_enabled=True)
    )
    monkeypatch.setattr(
        handler.UserRepo,
        "get",
        AsyncMock(side_effect=[_user(), _user(user_id=2002, first_name="Author")]),
    )
    monkeypatch.setattr(handler.QaTraceRepo, "create", AsyncMock(return_value=_fake_trace(trace_id=42)))
    monkeypatch.setattr(handler.QaTraceRepo, "update_llm_fields", update_spy)
    monkeypatch.setattr(handler, "run_qa", AsyncMock(return_value=_qa_result(abstained=False)))
    monkeypatch.setattr(handler, "synthesize_answer", AsyncMock(return_value=abst))

    await handler.recall_handler(message, _command("память"), session)

    update_spy.assert_awaited_once()
    kwargs = update_spy.await_args.kwargs
    assert kwargs["llm_response_summary"] is None
    assert kwargs["cost_usd"] == Decimal("0")
    assert kwargs["llm_call_id"] == 7373

    # Phase 4 fallback reply for the Abstention case.
    response = message.reply.call_args.args[0]
    assert "<b>Найденные свидетельства:</b>" in response


async def test_update_llm_fields_touches_only_phase5_kwargs(monkeypatch) -> None:
    """Step 3 update_llm_fields kwargs MUST be exactly the 4 Phase 5 columns
    (qa_trace_id, llm_call_id, llm_response_summary, llm_response_redacted,
    cost_usd) — never query / evidence_ids / abstained / query_redacted.
    """
    handler = import_module("bot.handlers.qa")
    message = _message()
    session = AsyncMock()
    update_spy = AsyncMock(return_value=1)
    answer = _answer_with_citations()

    _patch_persist(handler, monkeypatch)
    monkeypatch.setattr(
        handler.FeatureFlagRepo, "get", _flag_get(llm_synthesis_enabled=True)
    )
    monkeypatch.setattr(
        handler.UserRepo,
        "get",
        AsyncMock(side_effect=[_user(), _user(user_id=2002, first_name="Author")]),
    )
    monkeypatch.setattr(handler.QaTraceRepo, "create", AsyncMock(return_value=_fake_trace()))
    monkeypatch.setattr(handler.QaTraceRepo, "update_llm_fields", update_spy)
    monkeypatch.setattr(handler, "run_qa", AsyncMock(return_value=_qa_result(abstained=False)))
    monkeypatch.setattr(handler, "synthesize_answer", AsyncMock(return_value=answer))

    await handler.recall_handler(message, _command("память"), session)

    update_spy.assert_awaited_once()
    kwargs = update_spy.await_args.kwargs
    forbidden = {"query", "query_text", "evidence_ids", "abstained", "redact_query", "query_redacted"}
    assert not (forbidden & set(kwargs.keys())), (
        f"update_llm_fields called with forbidden Phase 4 kwarg(s): {forbidden & set(kwargs.keys())}"
    )
    assert set(kwargs.keys()) == {
        "qa_trace_id",
        "llm_call_id",
        "llm_response_summary",
        "llm_response_redacted",
        "cost_usd",
    }


async def test_gateway_config_prompt_template_version_is_v1(monkeypatch) -> None:
    """_load_gateway_config() (called inside the handler) MUST pass
    prompt_template_version='v1.0.0' to synthesize_answer per §12.5.
    """
    handler = import_module("bot.handlers.qa")
    message = _message()
    session = AsyncMock()
    captured: dict = {}

    async def synth_spy(s, *, config, **rest):
        captured["config"] = config
        return _answer_with_citations()

    _patch_persist(handler, monkeypatch)
    monkeypatch.setattr(
        handler.FeatureFlagRepo, "get", _flag_get(llm_synthesis_enabled=True)
    )
    monkeypatch.setattr(
        handler.UserRepo,
        "get",
        AsyncMock(side_effect=[_user(), _user(user_id=2002, first_name="Author")]),
    )
    monkeypatch.setattr(handler.QaTraceRepo, "create", AsyncMock(return_value=_fake_trace()))
    monkeypatch.setattr(handler.QaTraceRepo, "update_llm_fields", AsyncMock(return_value=1))
    monkeypatch.setattr(handler, "run_qa", AsyncMock(return_value=_qa_result(abstained=False)))
    monkeypatch.setattr(handler, "synthesize_answer", synth_spy)

    await handler.recall_handler(message, _command("память"), session)

    assert captured["config"].prompt_template_version == "v1.0.0"


async def test_synthesized_reply_escapes_injected_html(monkeypatch) -> None:
    """answer_text containing <script> MUST be HTML-escaped in the rendered reply."""
    handler = import_module("bot.handlers.qa")
    message = _message()
    session = AsyncMock()
    answer = _answer_with_citations(answer_text="harmless <script>alert('xss')</script> body")

    _patch_persist(handler, monkeypatch)
    monkeypatch.setattr(
        handler.FeatureFlagRepo, "get", _flag_get(llm_synthesis_enabled=True)
    )
    monkeypatch.setattr(
        handler.UserRepo,
        "get",
        AsyncMock(side_effect=[_user(), _user(user_id=2002, first_name="Author")]),
    )
    monkeypatch.setattr(handler.QaTraceRepo, "create", AsyncMock(return_value=_fake_trace()))
    monkeypatch.setattr(handler.QaTraceRepo, "update_llm_fields", AsyncMock(return_value=1))
    monkeypatch.setattr(handler, "run_qa", AsyncMock(return_value=_qa_result(abstained=False)))
    monkeypatch.setattr(handler, "synthesize_answer", AsyncMock(return_value=answer))

    await handler.recall_handler(message, _command("память"), session)

    response = message.reply.call_args.args[0]
    # The raw <script> tag MUST be escaped (no literal opening tag in the rendered reply).
    assert "<script>" not in response
    assert "&lt;script&gt;" in response


async def test_provider_resolve_failure_falls_back_to_phase4(monkeypatch) -> None:
    """Provider instantiation raises ValueError → Phase 4 fallback reply,
    NO update_llm_fields call (no ledger row to point at), bot does NOT crash.
    """
    handler = import_module("bot.handlers.qa")
    message = _message()
    session = AsyncMock()
    update_spy = AsyncMock()
    synth_spy = AsyncMock()

    def boom(name: str):  # noqa: ARG001
        raise ValueError(f"unknown provider: {name}")

    _patch_persist(handler, monkeypatch)
    monkeypatch.setattr(
        handler.FeatureFlagRepo, "get", _flag_get(llm_synthesis_enabled=True)
    )
    monkeypatch.setattr(
        handler.UserRepo,
        "get",
        AsyncMock(side_effect=[_user(), _user(user_id=2002, first_name="Author")]),
    )
    monkeypatch.setattr(handler.QaTraceRepo, "create", AsyncMock(return_value=_fake_trace()))
    monkeypatch.setattr(handler.QaTraceRepo, "update_llm_fields", update_spy)
    monkeypatch.setattr(handler, "run_qa", AsyncMock(return_value=_qa_result(abstained=False)))
    monkeypatch.setattr(handler, "synthesize_answer", synth_spy)
    monkeypatch.setattr(handler, "_resolve_provider", boom)

    # MUST NOT raise.
    await handler.recall_handler(message, _command("память"), session)

    # No synth attempted; no update_llm_fields (trace stays with NULL llm columns).
    synth_spy.assert_not_awaited()
    update_spy.assert_not_awaited()
    # Phase 4 reply rendered as the fallback.
    response = message.reply.call_args.args[0]
    assert "<b>Найденные свидетельства:</b>" in response


async def test_resolve_provider_rejects_unknown_name() -> None:
    """_resolve_provider('unknown') raises ValueError directly."""
    handler = import_module("bot.handlers.qa")
    with pytest.raises(ValueError, match="unknown provider"):
        handler._resolve_provider("unknown")


async def test_resolve_provider_returns_anthropic_or_openai() -> None:
    """_resolve_provider returns concrete provider instances for both known names."""
    handler = import_module("bot.handlers.qa")
    from bot.services.llm_providers.anthropic import AnthropicProvider
    from bot.services.llm_providers.openai import OpenAIProvider

    anth = handler._resolve_provider("anthropic")
    oai = handler._resolve_provider("openai")
    assert isinstance(anth, AnthropicProvider)
    assert isinstance(oai, OpenAIProvider)


async def test_end_to_end_happy_path_with_fake_provider(monkeypatch) -> None:
    """End-to-end with synthesize_answer returning AnswerWithCitations →
    reply contains answer text + <b>Источники:</b> footer.
    """
    handler = import_module("bot.handlers.qa")
    message = _message()
    session = AsyncMock()
    answer = _answer_with_citations(
        answer_text="Ребята обсуждали как сохранить память чата.",
        citation_ids=(500,),
        cost_usd=Decimal("0.000123"),
        llm_call_id=12345,
    )

    _patch_persist(handler, monkeypatch)
    monkeypatch.setattr(
        handler.FeatureFlagRepo, "get", _flag_get(llm_synthesis_enabled=True)
    )
    monkeypatch.setattr(
        handler.UserRepo,
        "get",
        AsyncMock(side_effect=[_user(), _user(user_id=2002, first_name="Author")]),
    )
    monkeypatch.setattr(handler.QaTraceRepo, "create", AsyncMock(return_value=_fake_trace()))
    monkeypatch.setattr(handler.QaTraceRepo, "update_llm_fields", AsyncMock(return_value=1))
    monkeypatch.setattr(handler, "run_qa", AsyncMock(return_value=_qa_result(abstained=False)))
    monkeypatch.setattr(handler, "synthesize_answer", AsyncMock(return_value=answer))

    await handler.recall_handler(message, _command("память"), session)

    message.reply.assert_awaited_once()
    response = message.reply.call_args.args[0]
    assert "Ребята обсуждали как сохранить память чата." in response
    assert "<b>Источники:</b>" in response
    # Footer enumerates bundle.items in bundle order: [1] only one item.
    assert "[1]" in response
    # parse_mode + disable_web_page_preview preserved (Telegram HTML rendering contract).
    assert message.reply.call_args.kwargs["parse_mode"] == "HTML"
    assert message.reply.call_args.kwargs["disable_web_page_preview"] is True


# Unused import guard — silence ruff for the helper we kept for symmetry with test_qa.py
_ = replace
