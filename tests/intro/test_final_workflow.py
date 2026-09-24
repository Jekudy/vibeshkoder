from __future__ import annotations

import asyncio
from html import escape
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.conftest import import_module
from tests.intro.test_contract import PREDKO_ANSWERS


def _joined_user(user_id: int = 901_001) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        username="<applicant>",
        first_name="Applicant <One>",
        last_name=None,
        is_bot=False,
    )


def _join_event() -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(id=-100_123_456_7890),
        bot=SimpleNamespace(
            ban_chat_member=AsyncMock(),
            unban_chat_member=AsyncMock(),
            send_message=AsyncMock(),
        ),
    )


@pytest.mark.asyncio
async def test_legacy_added_application_is_visible_only_for_current_member_duplicate_join(
    app_env, db_session
) -> None:
    from bot.db.models import Application
    from bot.db.repos.application import ApplicationRepo
    from bot.db.repos.user import UserRepo

    user_id = 901_010
    await UserRepo.upsert(db_session, user_id, "legacy", "Legacy", None)
    db_session.add(
        Application(
            user_id=user_id,
            status="added",
            flow_kind=None,
            catalog_version="legacy-v1",
            invite_user_id=user_id,
        )
    )
    await db_session.flush()

    assert await ApplicationRepo.get_active(db_session, user_id) is None
    legacy = await ApplicationRepo.get_active(db_session, user_id, include_added=True)
    assert legacy is not None
    assert legacy.flow_kind is None


@pytest.mark.parametrize(
    ("cas_result", "reread_id", "reread_status", "expected_member", "expected_enqueues"),
    [
        (True, None, None, True, 1),
        (False, 73, "added", True, 0),
        (False, 74, "added", False, 0),
        (False, 73, "pending", False, 0),
    ],
    ids=["cas-winner", "cas-loser-same-added", "cas-loser-other-added", "cas-loser-other-state"],
)
def test_join_cas_has_one_winner_and_rechecks_losers(
    app_env,
    monkeypatch: pytest.MonkeyPatch,
    cas_result: bool,
    reread_id: int | None,
    reread_status: str | None,
    expected_member: bool,
    expected_enqueues: int,
) -> None:
    handler = import_module("bot.handlers.chat_events")
    user = _joined_user()
    app = SimpleNamespace(id=73, status="vouched", invite_user_id=user.id)
    reread = (
        SimpleNamespace(id=reread_id, status=reread_status, invite_user_id=user.id)
        if reread_status is not None
        else None
    )
    get_active = AsyncMock(return_value=app)
    get_application = AsyncMock(return_value=reread)
    set_member = AsyncMock()
    enqueue = AsyncMock()
    monkeypatch.setattr(
        handler.UserRepo,
        "get",
        AsyncMock(return_value=SimpleNamespace(is_member=False, left_at=None)),
    )
    monkeypatch.setattr(handler.UserRepo, "upsert", AsyncMock())
    monkeypatch.setattr(handler.UserRepo, "set_member", set_member)
    monkeypatch.setattr(handler.ApplicationRepo, "get_active", get_active)
    monkeypatch.setattr(handler.ApplicationRepo, "get", get_application)
    monkeypatch.setattr(
        handler.ApplicationRepo, "update_status_if", AsyncMock(return_value=cas_result)
    )
    monkeypatch.setattr(handler.IntroRepo, "get", AsyncMock(return_value=None))
    monkeypatch.setattr(handler.IntroEffectOutboxRepo, "enqueue_once", enqueue)

    session = AsyncMock()
    asyncio.run(handler._handle_join(_join_event(), session, user))

    get_active.assert_awaited_once()
    if cas_result:
        get_application.assert_not_awaited()
    else:
        get_application.assert_awaited_once_with(session, 73)
    assert set_member.await_args_list[-1].kwargs["is_member"] is expected_member
    assert enqueue.await_count == expected_enqueues


def test_join_with_existing_intro_still_finalizes_admission_without_new_effect(
    app_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = import_module("bot.handlers.chat_events")
    user = _joined_user()
    update_status = AsyncMock(return_value=True)
    enqueue = AsyncMock()
    monkeypatch.setattr(
        handler.UserRepo,
        "get",
        AsyncMock(return_value=SimpleNamespace(is_member=False, left_at=None)),
    )
    monkeypatch.setattr(handler.UserRepo, "upsert", AsyncMock())
    monkeypatch.setattr(handler.UserRepo, "set_member", AsyncMock())
    monkeypatch.setattr(
        handler.ApplicationRepo,
        "get_active",
        AsyncMock(return_value=SimpleNamespace(id=74, status="vouched", invite_user_id=user.id)),
    )
    monkeypatch.setattr(handler.ApplicationRepo, "update_status_if", update_status)
    monkeypatch.setattr(handler.IntroRepo, "get", AsyncMock(return_value=SimpleNamespace()))
    monkeypatch.setattr(handler.IntroEffectOutboxRepo, "enqueue_once", enqueue)

    asyncio.run(handler._handle_join(_join_event(), AsyncMock(), user))

    update_status.assert_awaited_once()
    enqueue.assert_not_awaited()


def test_ready_privacy_block_cas_loser_does_not_create_a_second_invite(
    app_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = import_module("bot.handlers.vouch")
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=901_020),
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )
    app = SimpleNamespace(id=75, user_id=901_020, status="privacy_block")
    create_pending = AsyncMock()
    monkeypatch.setattr(handler.ApplicationRepo, "get", AsyncMock(return_value=app))
    monkeypatch.setattr(handler.ApplicationRepo, "update_status_if", AsyncMock(return_value=False))
    monkeypatch.setattr(handler.InviteOutboxRepo, "create_pending", create_pending)

    asyncio.run(handler.handle_ready(callback, SimpleNamespace(application_id=75), AsyncMock()))

    create_pending.assert_not_awaited()
    callback.message.edit_text.assert_not_awaited()
    callback.answer.assert_awaited_once()


def test_unknown_confirm_callback_action_is_always_answered(app_env) -> None:
    questionnaire = import_module("bot.handlers.questionnaire")
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=901_030),
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )

    asyncio.run(
        questionnaire.handle_confirm(
            callback,
            SimpleNamespace(action="unexpected", application_id=76, digest="digest"),
            AsyncMock(),
            AsyncMock(),
        )
    )

    callback.answer.assert_awaited_once()


def test_start_handles_questionnaire_race_with_a_user_message(
    app_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = import_module("bot.handlers.start")
    message = SimpleNamespace(from_user=_joined_user(901_040), answer=AsyncMock())
    monkeypatch.setattr(
        handler.UserRepo, "upsert", AsyncMock(return_value=SimpleNamespace(is_member=False))
    )
    monkeypatch.setattr(
        handler.ApplicationRepo,
        "get_active",
        AsyncMock(return_value=SimpleNamespace(id=77, status="filling")),
    )
    monkeypatch.setattr(
        handler, "next_field_id", AsyncMock(side_effect=handler.IntroWorkflowError("race"))
    )

    asyncio.run(handler.cmd_start(message, AsyncMock(), AsyncMock()))

    message.answer.assert_awaited_once_with("Анкета устарела. Запусти /start ещё раз.")


def _answers_with_goals(goals: str) -> list[tuple[str, str]]:
    return [(field, goals if field == "goals" else answer) for field, answer in PREDKO_ANSWERS]


def test_renderer_rejects_blank_answers_and_flattens_newlines(app_env) -> None:
    from bot.services.intro_contract import IntroContractError, render_intro_html

    with pytest.raises(IntroContractError):
        render_intro_html(_answers_with_goals("  "), catalog_version="intro-v2")

    body = render_intro_html(
        _answers_with_goals("line one\r\nline two\rline three"), catalog_version="intro-v2"
    )
    assert "line one line two line three" in body
    assert "\r" not in body


def test_renderer_limit_keeps_worst_case_admission_payload_within_telegram_limit(app_env) -> None:
    from bot.services.intro_contract import IntroContractError, render_intro_html
    from bot.services.intro_effect_worker import ClaimedEffect, _telegram_payload

    telegram_limit = 4_096
    first_name = '"' * 64
    voucher_name = '"' * 64
    username = "u" * 32
    header = (
        f"🎉 Новый участник: {escape(first_name)} (@{escape(username)})\n"
        f"Поручился: {escape(voucher_name)}\n\n"
    )
    safe_limit = telegram_limit - len(header)
    fixed_length = len(render_intro_html(_answers_with_goals("x"), catalog_version="intro-v2"))
    within_limit = "x" * (safe_limit - fixed_length + 1)
    snapshot = render_intro_html(_answers_with_goals(within_limit), catalog_version="intro-v2")
    payload = _telegram_payload(
        ClaimedEffect(
            effect_id=1,
            attempt_count=1,
            application_id=78,
            effect_kind="admission_intro",
            user_id=901_060,
            confirmed_intro_html=snapshot,
            first_name=first_name,
            answers_by_field_id={},
            username=username,
            voucher_name=voucher_name,
        )
    )
    assert len(payload["text"]) <= telegram_limit
    with pytest.raises(IntroContractError):
        render_intro_html(_answers_with_goals(within_limit + "x"), catalog_version="intro-v2")


@pytest.mark.asyncio
async def test_oversized_final_answer_is_not_persisted_and_can_be_replaced(
    app_env, db_session
) -> None:
    from bot.db.repos.application import ApplicationRepo
    from bot.db.repos.questionnaire import QuestionnaireRepo
    from bot.db.repos.user import UserRepo
    from bot.services.intro_workflow import IntroWorkflowError, write_answer

    user_id = 901_050
    await UserRepo.upsert(db_session, user_id, "answer", "Answer", None)
    application = await ApplicationRepo.create(
        db_session,
        user_id=user_id,
        flow_kind="admission",
        base_application_id=None,
        catalog_version="intro-v2",
    )
    for field, answer in PREDKO_ANSWERS[:-1]:
        await write_answer(
            db_session,
            user_id=user_id,
            application_id=application.id,
            field_id=field,
            answer_text=answer,
        )
    with pytest.raises(IntroWorkflowError):
        await write_answer(
            db_session,
            user_id=user_id,
            application_id=application.id,
            field_id="goals",
            answer_text="x" * 4_001,
        )
    assert not any(
        answer.field_id == "goals"
        for answer in await QuestionnaireRepo.get_by_application(
            db_session, application_id=application.id
        )
    )
    await write_answer(
        db_session,
        user_id=user_id,
        application_id=application.id,
        field_id="goals",
        answer_text="brief goal",
    )
    answers = await QuestionnaireRepo.get_by_application(db_session, application_id=application.id)
    assert (
        next(answer.answer_text for answer in answers if answer.field_id == "goals") == "brief goal"
    )


@pytest.mark.parametrize(
    ("effect_kind", "expected"),
    [
        (
            "candidate_card",
            "📋 Новая анкета от Applicant &lt;One&gt; (@&lt;applicant&gt;)\n\n&lt;b&gt;Safe&lt;/b&gt;",
        ),
        (
            "admission_intro",
            "🎉 Новый участник: Applicant &lt;One&gt; (@&lt;applicant&gt;)\n"
            "Поручился: @voucher&lt;one&gt;\n\n&lt;b&gt;Safe&lt;/b&gt;",
        ),
    ],
)
def test_effect_payload_preserves_exact_escaped_applicant_and_voucher(
    app_env, effect_kind: str, expected: str
) -> None:
    from bot.services.intro_effect_worker import ClaimedEffect, _telegram_payload

    payload = _telegram_payload(
        ClaimedEffect(
            effect_id=1,
            attempt_count=1,
            application_id=78,
            effect_kind=effect_kind,
            user_id=901_060,
            confirmed_intro_html="&lt;b&gt;Safe&lt;/b&gt;",
            answers_by_field_id={},
            username="<applicant>",
            first_name="Applicant <One>",
            voucher_name="@voucher<one>",
        )
    )

    assert payload["text"] == expected
