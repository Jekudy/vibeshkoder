"""Issue #406 orchestration and 1–3-call structured pipeline tests."""

from __future__ import annotations

import itertools
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

pytestmark = pytest.mark.usefixtures("app_env")

_users = itertools.count(8_300_000_000)
_messages = itertools.count(830_000)
_chats = itertools.count(8300)


def _chat_id() -> int:
    return -1_000_000_000_000 - next(_chats)


async def _make_message(session, *, chat_id: int, ts: datetime, text: str) -> tuple[int, int, int]:
    from bot.db.models import ChatMessage, MessageVersion
    from bot.db.repos.user import UserRepo

    user_id = next(_users)
    await UserRepo.upsert(
        session,
        telegram_id=user_id,
        username=f"u{user_id}",
        first_name="Женя",
        last_name="Кудрявцев",
    )
    telegram_message_id = next(_messages)
    message = ChatMessage(
        message_id=telegram_message_id,
        chat_id=chat_id,
        user_id=user_id,
        text=text,
        date=ts,
        raw_json={"text": text},
        memory_policy="normal",
        is_redacted=False,
        message_kind="text",
    )
    session.add(message)
    await session.flush()
    version = MessageVersion(
        chat_message_id=message.id,
        version_seq=1,
        text=text,
        normalized_text=text,
        entities_json={"entities": []},
        content_hash=f"digest-run-{telegram_message_id}",
        is_redacted=False,
    )
    session.add(version)
    await session.flush()
    message.current_version_id = version.id
    await session.flush()
    return message.id, version.id, telegram_message_id


def _gateway_config():
    from bot.services.llm_gateway import LLMGatewayConfig

    return LLMGatewayConfig(
        provider="openai",
        model="gpt-5.6-sol",
        daily_ceiling_usd=Decimal("100"),
        monthly_ceiling_usd=Decimal("1000"),
        prompt_template_version="digest-v0.3.0",
    )


class _StructuredProvider:
    def __init__(self, responses: list[dict] | None = None) -> None:
        self.responses = responses or []
        self.calls: list[dict] = []

    async def call_structured(self, **kwargs):
        from bot.services.llm_providers import ProviderResult

        self.calls.append(kwargs)
        if len(self.calls) > len(self.responses):
            raise AssertionError("unexpected digest provider call")
        return ProviderResult(
            answer_text=json.dumps(self.responses[len(self.calls) - 1], ensure_ascii=False),
            citation_ids=(),
            tokens_in=200,
            tokens_out=100,
            request_id=f"digest-{len(self.calls)}",
            raw_latency_ms=1,
        )


def _draft(*, message_version_id: int, publish: bool = True) -> dict:
    if not publish:
        return {
            "publish": False,
            "layout": "none",
            "sections": [],
            "closing": {"text": "", "citations": []},
        }
    token = f"[[mv:{message_version_id}]]"
    return {
        "publish": True,
        "layout": "flat",
        "sections": [
            {
                "heading": "",
                "items": [{"text": "Женя сравнил две версии", "citations": [token]}],
            }
        ],
        "closing": {
            "text": "У версий наконец появился повод познакомиться",
            "citations": [token],
        },
    }


def _verifier(*, item_action="keep", item_reason="ok", closing_action="keep", closing_reason="ok"):
    return {
        "items": [
            {"item_key": "item_1", "verdict": f"{item_action}_{item_reason}"},
            {"item_key": "closing", "verdict": f"{closing_action}_{closing_reason}"},
        ]
    }


async def _context(session, *, digest_type: str = "daily"):
    from bot.services.digest_context import DigestContext, DigestContextMessage

    chat_id = _chat_id()
    start = datetime(2026, 7, 20, 2, tzinfo=timezone.utc)
    message_id, version_id, telegram_message_id = await _make_message(
        session,
        chat_id=chat_id,
        ts=start + timedelta(hours=4),
        text="Сравнил версии 5.6 и 5.7",
    )
    return DigestContext(
        type=digest_type,
        window_start=start,
        window_end=start + timedelta(days=7 if digest_type == "weekly" else 1),
        source_chat_id=chat_id,
        cards=[],
        messages=[
            DigestContextMessage(
                message_version_id=version_id,
                chat_message_id=message_id,
                telegram_message_id=telegram_message_id,
                author_display="Женя Кудрявцев",
                text="Сравнил версии 5.6 и 5.7",
                caption="Таблица сравнения",
                message_kind="photo",
                reply_to_message_id=101,
                message_thread_id=202,
                media_kind="photo",
                media_description="Две колонки с результатами",
                forward_origin_type="user",
                forward_origin_display="Анна Петрова",
                forward_origin_date="2026-07-20T04:00:00+00:00",
                ts=start + timedelta(hours=4),
            )
        ],
    )


async def _append_context_message(session, context, *, text: str) -> int:
    from bot.services.digest_context import DigestContextMessage

    message_id, version_id, telegram_message_id = await _make_message(
        session,
        chat_id=context.source_chat_id,
        ts=context.window_start + timedelta(hours=5 + len(context.messages)),
        text=text,
    )
    context.messages.append(
        DigestContextMessage(
            message_version_id=version_id,
            chat_message_id=message_id,
            telegram_message_id=telegram_message_id,
            author_display="Женя Кудрявцев",
            text=text,
            caption=None,
            message_kind="text",
            reply_to_message_id=None,
            message_thread_id=None,
            media_kind=None,
            media_description=None,
            forward_origin_type=None,
            forward_origin_display=None,
            forward_origin_date=None,
            ts=context.window_start + timedelta(hours=5 + len(context.messages)),
        )
    )
    return version_id


def _draft_at_weekly_visible_length(*, message_version_id: int, target: int) -> dict:
    from bot.services.digest_renderer import measure_digest_visible_length

    token = f"[[mv:{message_version_id}]]"
    closing = "Закрыли"
    baseline = f"-  {token}\n\n— {closing} {token}"
    baseline_length = measure_digest_visible_length(
        baseline,
        window_start_utc=datetime(2026, 7, 20, 2, tzinfo=timezone.utc),
        window_end_utc=datetime(2026, 7, 27, 2, tzinfo=timezone.utc),
        digest_type="weekly",
    )
    draft = _draft(message_version_id=message_version_id)
    draft["sections"][0]["items"][0]["text"] = "x" * (target - baseline_length)
    draft["closing"] = {"text": closing, "citations": [token]}
    return draft


async def test_empty_window_skips_before_gold_or_provider(db_session) -> None:
    from bot.services.digest_context import DigestContext
    from bot.services.llm_gateway import DigestEmptyWindowError, synthesize_digest

    context = DigestContext(
        type="daily",
        window_start=datetime.now(timezone.utc) - timedelta(days=1),
        window_end=datetime.now(timezone.utc),
        source_chat_id=_chat_id(),
        cards=[],
        messages=[],
    )
    provider = _StructuredProvider()
    with pytest.raises(DigestEmptyWindowError):
        await synthesize_digest(
            db_session,
            context=context,
            config=_gateway_config(),
            ledger_repo=object(),
            provider=provider,
        )
    assert provider.calls == []


async def test_semantically_quiet_window_uses_exactly_one_call(db_session) -> None:
    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from bot.services.llm_gateway import synthesize_digest

    context = await _context(db_session)
    provider = _StructuredProvider([_draft(message_version_id=1, publish=False)])
    result = await synthesize_digest(
        db_session,
        context=context,
        config=_gateway_config(),
        ledger_repo=LedgerRepo(),
        provider=provider,
    )
    assert result.publish is False
    assert result.body_markdown is None
    assert len(provider.calls) == 1


async def test_clean_digest_uses_two_calls_and_preserves_full_input_metadata(db_session) -> None:
    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from bot.services.llm_gateway import synthesize_digest

    context = await _context(db_session)
    version_id = context.messages[0].message_version_id
    provider = _StructuredProvider([_draft(message_version_id=version_id), _verifier()])
    result = await synthesize_digest(
        db_session,
        context=context,
        config=_gateway_config(),
        ledger_repo=LedgerRepo(),
        provider=provider,
    )
    assert result.publish is True
    assert result.body_markdown is not None and "— " in result.body_markdown
    assert len(provider.calls) == 2
    assert all(call["model"] == "gpt-5.6-sol" for call in provider.calls)
    assert all(call["reasoning_effort"] == "medium" for call in provider.calls)
    draft_input = json.loads(provider.calls[0]["input_text"])
    assert draft_input["window"]["start"] == "2026-07-20T05:00:00+03:00"
    message = draft_input["messages"][0]
    assert message["author_display"] == "Женя Кудрявцев"
    assert message["reply_to_message_id"] == 101
    assert message["message_thread_id"] == 202
    assert message["media_description"] == "Две колонки с результатами"
    assert message["forward_origin"] == {
        "type": "user",
        "display": "Анна Петрова",
        "date": "2026-07-20T04:00:00+00:00",
    }


async def test_fixable_factual_error_uses_exactly_three_calls(db_session) -> None:
    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from bot.services.llm_gateway import synthesize_digest

    context = await _context(db_session)
    version_id = context.messages[0].message_version_id
    provider = _StructuredProvider(
        [
            _draft(message_version_id=version_id),
            _verifier(item_action="fix", item_reason="number"),
            {"items": [{"item_key": "item_1", "text": "Женя сравнил версии 5.6 и 5.7"}]},
        ]
    )
    result = await synthesize_digest(
        db_session,
        context=context,
        config=_gateway_config(),
        ledger_repo=LedgerRepo(),
        provider=provider,
    )
    assert len(provider.calls) == 3
    assert provider.calls[2]["schema_name"] == "digest_apply_fixes"
    assert set(json.loads(provider.calls[2]["input_text"])) == {"items"}
    assert result.body_markdown is not None and "5.6 и 5.7" in result.body_markdown


async def test_weekly_clean_within_budget_uses_exactly_two_calls(db_session) -> None:
    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from bot.services.llm_gateway import synthesize_digest

    context = await _context(db_session, digest_type="weekly")
    version_id = context.messages[0].message_version_id
    provider = _StructuredProvider([_draft(message_version_id=version_id), _verifier()])

    result = await synthesize_digest(
        db_session,
        context=context,
        config=_gateway_config(),
        ledger_repo=LedgerRepo(),
        provider=provider,
        type="weekly",
    )

    assert result.publish is True
    assert [call["schema_name"] for call in provider.calls] == [
        "digest_draft",
        "digest_factual_verifier",
    ]


async def test_weekly_factual_fix_uses_full_finalizer_call(db_session) -> None:
    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from bot.services.llm_gateway import synthesize_digest

    context = await _context(db_session, digest_type="weekly")
    version_id = context.messages[0].message_version_id
    token = f"[[mv:{version_id}]]"
    finalized = _draft(message_version_id=version_id)
    finalized["sections"][0]["items"][0]["text"] = "Женя сравнил версии 5.6 и 5.7"
    provider = _StructuredProvider(
        [
            _draft(message_version_id=version_id),
            _verifier(item_action="fix", item_reason="number"),
            finalized,
        ]
    )

    result = await synthesize_digest(
        db_session,
        context=context,
        config=_gateway_config(),
        ledger_repo=LedgerRepo(),
        provider=provider,
        type="weekly",
    )

    assert len(provider.calls) == 3
    assert provider.calls[2]["schema_name"] == "digest_weekly_finalizer"
    finalizer_input = json.loads(provider.calls[2]["input_text"])
    assert finalizer_input["visible_character_target"] == 3600
    assert finalizer_input["verifier_decisions"][0]["action"] == "fix"
    assert set(finalizer_input["cited_evidence"]) == {token}
    assert result.body_markdown is not None and "5.6 и 5.7" in result.body_markdown


async def test_weekly_over_target_with_all_keep_uses_finalizer(db_session) -> None:
    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from bot.services.llm_gateway import synthesize_digest

    context = await _context(db_session, digest_type="weekly")
    version_id = context.messages[0].message_version_id
    provider = _StructuredProvider(
        [
            _draft_at_weekly_visible_length(message_version_id=version_id, target=3601),
            _verifier(),
            _draft(message_version_id=version_id),
        ]
    )

    result = await synthesize_digest(
        db_session,
        context=context,
        config=_gateway_config(),
        ledger_repo=LedgerRepo(),
        provider=provider,
        type="weekly",
    )

    assert result.publish is True
    assert len(provider.calls) == 3
    assert provider.calls[2]["schema_name"] == "digest_weekly_finalizer"


@pytest.mark.parametrize(("visible_length", "should_fail"), [(3600, False), (3601, True)])
async def test_weekly_finalizer_visible_target_is_fail_closed(
    db_session, visible_length, should_fail
) -> None:
    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from bot.services.llm_gateway import DigestCitationValidationError, synthesize_digest

    context = await _context(db_session, digest_type="weekly")
    version_id = context.messages[0].message_version_id
    provider = _StructuredProvider(
        [
            _draft(message_version_id=version_id),
            _verifier(item_action="fix", item_reason="number"),
            _draft_at_weekly_visible_length(
                message_version_id=version_id, target=visible_length
            ),
        ]
    )

    call = synthesize_digest(
        db_session,
        context=context,
        config=_gateway_config(),
        ledger_repo=LedgerRepo(),
        provider=provider,
        type="weekly",
    )
    if should_fail:
        with pytest.raises(DigestCitationValidationError, match="visible target"):
            await call
    else:
        result = await call
        assert result.publish is True
    assert len(provider.calls) == 3


async def test_weekly_finalizer_rejects_new_context_citation(db_session) -> None:
    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from bot.services.llm_gateway import DigestCitationValidationError, synthesize_digest

    context = await _context(db_session, digest_type="weekly")
    original_id = context.messages[0].message_version_id
    extra_id = await _append_context_message(db_session, context, text="Вторая тема")
    provider = _StructuredProvider(
        [
            _draft(message_version_id=original_id),
            _verifier(item_action="fix", item_reason="fact"),
            _draft(message_version_id=extra_id),
        ]
    )

    with pytest.raises(DigestCitationValidationError, match="unknown citation"):
        await synthesize_digest(
            db_session,
            context=context,
            config=_gateway_config(),
            ledger_repo=LedgerRepo(),
            provider=provider,
            type="weekly",
        )
    assert len(provider.calls) == 3
    finalizer_input = json.loads(provider.calls[2]["input_text"])
    assert set(finalizer_input["cited_evidence"]) == {f"[[mv:{original_id}]]"}


async def test_weekly_finalizer_can_merge_items_and_preserve_source_rows(db_session) -> None:
    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from bot.services.llm_gateway import synthesize_digest

    context = await _context(db_session, digest_type="weekly")
    first_id = context.messages[0].message_version_id
    second_id = await _append_context_message(db_session, context, text="Вторая тема")
    first_token = f"[[mv:{first_id}]]"
    second_token = f"[[mv:{second_id}]]"
    original = {
        "publish": True,
        "layout": "flat",
        "sections": [
            {
                "heading": "",
                "items": [
                    {"text": "Первая тема", "citations": [first_token]},
                    {"text": "Вторая тема", "citations": [second_token]},
                ],
            }
        ],
        "closing": {"text": "Итог", "citations": [first_token]},
    }
    finalized = {
        "publish": True,
        "layout": "flat",
        "sections": [
            {
                "heading": "",
                "items": [
                    {
                        "text": "Объединили темы",
                        "citations": [first_token, second_token],
                    }
                ],
            }
        ],
        "closing": {"text": "Итог", "citations": [first_token]},
    }
    verifier = {
        "items": [
            {"item_key": "item_1", "verdict": "fix_fact"},
            {"item_key": "item_2", "verdict": "keep_ok"},
            {"item_key": "closing", "verdict": "keep_ok"},
        ]
    }
    provider = _StructuredProvider([original, verifier, finalized])

    result = await synthesize_digest(
        db_session,
        context=context,
        config=_gateway_config(),
        ledger_repo=LedgerRepo(),
        provider=provider,
        type="weekly",
    )

    assert result.body_markdown is not None
    assert sum(line.startswith("- ") for line in result.body_markdown.splitlines()) == 1
    assert result.citations == [
        {"kind": "message_version", "id": first_id, "position": 0},
        {"kind": "message_version", "id": second_id, "position": 0},
        {"kind": "message_version", "id": first_id, "position": 1},
    ]


@pytest.mark.parametrize("digest_type", ["daily", "weekly"])
async def test_unfixable_citation_mismatch_blocks_after_two_calls(
    db_session, digest_type
) -> None:
    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from bot.services.llm_gateway import DigestCitationValidationError, synthesize_digest

    context = await _context(db_session, digest_type=digest_type)
    version_id = context.messages[0].message_version_id
    provider = _StructuredProvider(
        [_draft(message_version_id=version_id), _verifier(item_action="block", item_reason="citations")]
    )
    with pytest.raises(DigestCitationValidationError, match="blocked"):
        await synthesize_digest(
            db_session,
            context=context,
            config=_gateway_config(),
            ledger_repo=LedgerRepo(),
            provider=provider,
            type=digest_type,
        )
    assert len(provider.calls) == 2


def test_digest_gateway_config_is_isolated_from_shared_llm(monkeypatch) -> None:
    from bot.services.llm_gateway import load_digest_gateway_config

    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4-flash")
    config = load_digest_gateway_config(digest_type="daily")
    assert config.provider == "openai"
    assert config.model == "gpt-5.6-sol"


def test_load_digest_config_ignores_removed_truncation_knobs(monkeypatch) -> None:
    from bot.services.digests import load_digest_config

    monkeypatch.setenv("DIGEST_RAW_MESSAGE_TOP_N", "1")
    monkeypatch.setenv("DIGEST_TOKEN_BUDGET_INPUT", "1")
    config = load_digest_config()
    assert not hasattr(config, "raw_message_top_n")
    assert not hasattr(config, "token_budget_input")
