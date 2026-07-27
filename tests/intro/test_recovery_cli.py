from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.intro.test_contract import PREDKO_ANSWERS, PREDKO_BODY


USER_ID = 169_419_687
ADMIN_ID = 149_820_031
LEGACY_APPLICATION_ID = 176
ANSWER_ROW_IDS = (19_231, 19_232, 19_233, 19_234, 19_235, 19_236, 19_237)
SOURCE_CONFIRM_ROW_ID = 19_238
AUTHORIZED_INPUT_SHA256 = "5762b931895dc1837abf75209055a86a1b56e3bcdcd472b3346bf2d01b2b1fd5"


def _input_sha256() -> str:
    return AUTHORIZED_INPUT_SHA256


def _private_text_update(*, user_id: int, text: str) -> dict:
    return {
        "message": {
            "from": {"id": user_id},
            "chat": {"id": user_id, "type": "private"},
            "text": text,
        }
    }


def _confirm_update(*, user_id: int, data: str = "confirm:yes") -> dict:
    return {
        "callback_query": {
            "from": {"id": user_id},
            "data": data,
            "message": {"chat": {"id": user_id, "type": "private"}},
        }
    }


async def _seed_predko_evidence(
    session: AsyncSession,
    *,
    non_private_row_id: int | None = None,
    wrong_answer_owner_row_id: int | None = None,
    answer_text_case: str | None = None,
    wrong_callback_owner: bool = False,
    callback_data: str = "confirm:yes",
    altered_answer_row_id: int | None = None,
    wrong_private_chat_row_id: int | None = None,
    non_private_callback_chat: bool = False,
    wrong_callback_chat_id: bool = False,
) -> None:
    from bot.db.models import TelegramUpdate

    for row_id, (_, answer) in zip(ANSWER_ROW_IDS, PREDKO_ANSWERS, strict=True):
        payload = _private_text_update(user_id=USER_ID, text=answer)
        if row_id == non_private_row_id:
            payload["message"]["chat"]["type"] = "group"
        if row_id == wrong_answer_owner_row_id:
            payload["message"]["from"]["id"] = USER_ID + 1
        if row_id == wrong_private_chat_row_id:
            payload["message"]["chat"]["id"] = USER_ID + 1
        if row_id == altered_answer_row_id:
            payload["message"]["text"] = "altered raw evidence"
        if answer_text_case == "missing" and row_id == ANSWER_ROW_IDS[0]:
            del payload["message"]["text"]
        if answer_text_case == "blank" and row_id == ANSWER_ROW_IDS[0]:
            payload["message"]["text"] = "   "
        if answer_text_case == "non-string" and row_id == ANSWER_ROW_IDS[0]:
            payload["message"]["text"] = 42
        session.add(
            TelegramUpdate(
                id=row_id,
                # These denormalized fields deliberately disagree with the evidence:
                # recovery may read only raw_json.
                update_type="not-the-evidence",
                chat_id=-100_999,
                message_id=99_999,
                raw_json=payload,
            )
        )
    callback_payload = _confirm_update(
        user_id=USER_ID + int(wrong_callback_owner), data=callback_data
    )
    if non_private_callback_chat:
        callback_payload["callback_query"]["message"]["chat"]["type"] = "group"
    if wrong_callback_chat_id:
        callback_payload["callback_query"]["message"]["chat"]["id"] = USER_ID + 1
    session.add(
        TelegramUpdate(
            id=SOURCE_CONFIRM_ROW_ID,
            update_type="not-the-evidence",
            chat_id=-100_999,
            message_id=100_000,
            raw_json=callback_payload,
        )
    )
    await session.flush()


async def _seed_legacy_application(session: AsyncSession) -> None:
    from bot.db.models import Application, Intro, QuestionnaireAnswer, User

    session.add(
        User(
            id=USER_ID,
            username="predko",
            first_name="Сергей",
            last_name=None,
            is_member=True,
        )
    )
    session.add(
        Application(
            id=LEGACY_APPLICATION_ID,
            user_id=USER_ID,
            status="added",
            flow_kind="admission",
            catalog_version="intro-v2",
            confirmed_intro_html="old snapshot must not be recovered",
        )
    )
    session.add(
        QuestionnaireAnswer(
            user_id=USER_ID,
            application_id=LEGACY_APPLICATION_ID,
            field_id="name",
            question_index=0,
            question_text="old question",
            answer_text="old value must not be used",
        )
    )
    session.add(
        Intro(
            user_id=USER_ID,
            application_id=LEGACY_APPLICATION_ID,
            intro_text="old intro must stay current until outbox delivery",
            vouched_by_name="@voucher",
            sheets_row_number=17,
        )
    )
    await session.flush()


async def _seed_operator(session: AsyncSession, *, is_admin: bool = True) -> None:
    from bot.db.models import User

    session.add(User(id=ADMIN_ID, username="admin", first_name="Admin", is_admin=is_admin))
    await session.flush()


def _recovery_kwargs(**overrides: object) -> dict[str, object]:
    return {
        "user_id": USER_ID,
        "answer_row_ids": ANSWER_ROW_IDS,
        "source_confirm_row_id": SOURCE_CONFIRM_ROW_ID,
        "operator_user_id": ADMIN_ID,
        "authorize_operator_remediation": True,
        "expected_input_sha256": _input_sha256(),
        "reason": "issue-484-predko-remediation",
        **overrides,
    }


async def _legacy_state(session: AsyncSession) -> tuple[tuple[object, ...], ...]:
    from bot.db.models import Application, Intro, QuestionnaireAnswer

    application = await session.get(Application, LEGACY_APPLICATION_ID)
    answer = await session.scalar(
        select(QuestionnaireAnswer).where(
            QuestionnaireAnswer.application_id == LEGACY_APPLICATION_ID
        )
    )
    intro = await session.scalar(select(Intro).where(Intro.user_id == USER_ID))
    assert application is not None
    assert answer is not None
    assert intro is not None
    return (
        (
            application.id,
            application.user_id,
            application.status,
            application.flow_kind,
            application.catalog_version,
            application.confirmed_intro_html,
        ),
        (
            answer.id,
            answer.user_id,
            answer.application_id,
            answer.field_id,
            answer.question_index,
            answer.question_text,
            answer.answer_text,
            answer.is_current,
        ),
        (
            intro.id,
            intro.user_id,
            intro.application_id,
            intro.intro_text,
            intro.vouched_by_name,
            intro.sheets_row_number,
        ),
    )


def _forbid_telegram(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    from aiogram import Bot

    send_message = AsyncMock()
    monkeypatch.setattr(Bot, "send_message", send_message)
    return send_message


def _session_factory(session: MagicMock):
    @asynccontextmanager
    async def factory():
        yield session

    return factory


@pytest.mark.asyncio
@pytest.mark.parametrize("operator_is_admin", [True, False], ids=["db-admin", "env-admin"])
async def test_raw_recovery_creates_new_confirmed_refresh_and_standard_outbox_only(
    app_env, db_session, monkeypatch: pytest.MonkeyPatch, operator_is_admin: bool
) -> None:
    from bot.cli import recover_intro_from_raw
    from bot.db.models import IntroEffectOutbox, QuestionnaireAnswer, User

    await _seed_legacy_application(db_session)
    await _seed_operator(db_session, is_admin=operator_is_admin)
    await _seed_predko_evidence(db_session)
    operator = await db_session.get(User, ADMIN_ID)
    assert operator is not None and operator.is_admin is operator_is_admin
    legacy_before = await _legacy_state(db_session)
    send_message = _forbid_telegram(monkeypatch)

    recovered = await recover_intro_from_raw(db_session, **_recovery_kwargs())

    assert recovered.id != LEGACY_APPLICATION_ID
    assert (
        recovered.user_id,
        recovered.flow_kind,
        recovered.base_application_id,
        recovered.status,
        recovered.catalog_version,
        recovered.confirmed_intro_html,
    ) == (USER_ID, "refresh", LEGACY_APPLICATION_ID, "confirmed", "intro-v2", PREDKO_BODY)
    answers = list(
        (
            await db_session.execute(
                select(QuestionnaireAnswer)
                .where(QuestionnaireAnswer.application_id == recovered.id)
                .order_by(QuestionnaireAnswer.question_index)
            )
        ).scalars()
    )
    assert [(answer.field_id, answer.answer_text) for answer in answers] == PREDKO_ANSWERS
    effects = list(
        (
            await db_session.execute(select(IntroEffectOutbox).order_by(IntroEffectOutbox.id))
        ).scalars()
    )
    assert [
        (
            effect.application_id,
            effect.effect_kind,
            effect.status,
            effect.chat_id,
            effect.message_id,
        )
        for effect in effects
    ] == [(recovered.id, "refresh_intro", "pending", None, None)]
    assert await _legacy_state(db_session) == legacy_before
    send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "wrong-row-order",
        "wrong-row-count",
        "non-private-answer",
        "wrong-answer-owner",
        "missing-answer-text",
        "blank-answer-text",
        "non-string-answer-text",
        "wrong-private-chat-id",
        "wrong-callback-owner",
        "wrong-callback-data",
        "non-private-callback-chat",
        "wrong-callback-chat-id",
        "missing-authorize-flag",
        "configured-admin-without-user-row",
        "non-admin-operator",
        "wrong-input-hash",
        "altered-raw-answer",
        "active-refresh",
        "reason-501",
    ],
)
async def test_raw_recovery_fails_closed_without_partial_application_or_effect(
    app_env, db_session, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from bot.cli import IntroRawRecoveryError, recover_intro_from_raw
    from bot.db.models import Application, IntroEffectOutbox, User

    await _seed_legacy_application(db_session)
    await _seed_predko_evidence(
        db_session,
        non_private_row_id=ANSWER_ROW_IDS[2] if case == "non-private-answer" else None,
        wrong_answer_owner_row_id=(ANSWER_ROW_IDS[2] if case == "wrong-answer-owner" else None),
        answer_text_case={
            "missing-answer-text": "missing",
            "non-string-answer-text": "non-string",
            "blank-answer-text": "blank",
        }.get(case),
        wrong_private_chat_row_id=(ANSWER_ROW_IDS[2] if case == "wrong-private-chat-id" else None),
        wrong_callback_owner=case == "wrong-callback-owner",
        callback_data="confirm:no" if case == "wrong-callback-data" else "confirm:yes",
        altered_answer_row_id=ANSWER_ROW_IDS[0] if case == "altered-raw-answer" else None,
        non_private_callback_chat=case == "non-private-callback-chat",
        wrong_callback_chat_id=case == "wrong-callback-chat-id",
    )
    legacy_before = await _legacy_state(db_session)
    send_message = _forbid_telegram(monkeypatch)
    kwargs = _recovery_kwargs()
    if case == "wrong-row-order":
        kwargs["answer_row_ids"] = tuple(reversed(ANSWER_ROW_IDS))
    elif case == "wrong-row-count":
        kwargs["answer_row_ids"] = ANSWER_ROW_IDS[:-1]
    elif case == "missing-authorize-flag":
        kwargs["authorize_operator_remediation"] = False
    elif case == "non-admin-operator":
        db_session.add(User(id=ADMIN_ID + 1, username="operator", first_name="Operator"))
        await db_session.flush()
        kwargs["operator_user_id"] = ADMIN_ID + 1
    elif case == "wrong-input-hash":
        kwargs["expected_input_sha256"] = "0" * 64
    elif case in {"blank-answer-text", "altered-raw-answer"}:
        # Caller supplies a matching hash for the altered raw input: structure,
        # not merely the caller's hash, must reject a non-authorized answer.
        kwargs["expected_input_sha256"] = hashlib.sha256(
            "\n".join(
                ("   " if case == "blank-answer-text" else "altered raw evidence")
                if row_id == ANSWER_ROW_IDS[0]
                else answer
                for row_id, (_, answer) in zip(ANSWER_ROW_IDS, PREDKO_ANSWERS, strict=True)
            ).encode("utf-8")
        ).hexdigest()
    elif case == "active-refresh":
        from bot.db.models import Application

        db_session.add(
            Application(
                user_id=USER_ID,
                status="filling",
                flow_kind="refresh",
                base_application_id=LEGACY_APPLICATION_ID,
                catalog_version="intro-v2",
            )
        )
        await db_session.flush()
    elif case == "reason-501":
        kwargs["reason"] = f" {'x' * 501} "

    with pytest.raises(IntroRawRecoveryError):
        await recover_intro_from_raw(db_session, **kwargs)

    applications = list(
        (
            await db_session.execute(
                select(Application).where(Application.user_id == USER_ID).order_by(Application.id)
            )
        ).scalars()
    )
    expected_ids = [LEGACY_APPLICATION_ID]
    if case == "active-refresh":
        expected_ids.append(
            next(
                application.id
                for application in applications
                if application.id != LEGACY_APPLICATION_ID
            )
        )
    assert [application.id for application in applications] == sorted(expected_ids)
    assert await db_session.scalar(select(IntroEffectOutbox.id)) is None
    assert await _legacy_state(db_session) == legacy_before
    send_message.assert_not_awaited()


def _cli_argv(*, include_authorization: bool = True) -> list[str]:
    argv = [
        "intro_recover_raw",
        "--user-id",
        str(USER_ID),
        "--answer-row-ids",
        ",".join(map(str, ANSWER_ROW_IDS)),
        "--source-confirm-row-id",
        str(SOURCE_CONFIRM_ROW_ID),
        "--operator-user-id",
        str(ADMIN_ID),
        "--expected-input-sha256",
        _input_sha256(),
        "--reason",
        "issue-484-predko-remediation",
    ]
    if include_authorization:
        argv.append("--authorize-operator-remediation")
    return argv


def test_recovery_cli_parses_audited_arguments_commits_once_without_network(
    app_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bot.cli as cli
    import bot.db.engine as db_engine

    session = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    recover = AsyncMock(return_value=SimpleNamespace(id=777))
    send_message = _forbid_telegram(monkeypatch)
    monkeypatch.setattr(db_engine, "async_session", _session_factory(session))
    monkeypatch.setattr(cli, "recover_intro_from_raw", recover)

    assert cli.main(_cli_argv()) == 0
    recover.assert_awaited_once_with(
        session,
        user_id=USER_ID,
        answer_row_ids=ANSWER_ROW_IDS,
        source_confirm_row_id=SOURCE_CONFIRM_ROW_ID,
        operator_user_id=ADMIN_ID,
        authorize_operator_remediation=True,
        expected_input_sha256=_input_sha256(),
        reason="issue-484-predko-remediation",
    )
    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()
    send_message.assert_not_awaited()


def test_recovery_cli_rolls_back_and_returns_nonzero_on_recovery_error(
    app_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bot.cli as cli
    import bot.db.engine as db_engine
    from bot.cli import IntroRawRecoveryError

    session = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    recover = AsyncMock(side_effect=IntroRawRecoveryError("invalid raw evidence"))
    send_message = _forbid_telegram(monkeypatch)
    monkeypatch.setattr(db_engine, "async_session", _session_factory(session))
    monkeypatch.setattr(cli, "recover_intro_from_raw", recover)

    assert cli.main(_cli_argv()) != 0
    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()
    send_message.assert_not_awaited()


def test_recovery_cli_refuses_to_parse_without_explicit_authorization_flag(
    app_env, capsys: pytest.CaptureFixture[str]
) -> None:
    from bot.cli import main

    with pytest.raises(SystemExit) as caught:
        main(_cli_argv(include_authorization=False))

    assert caught.value.code == 2
    assert "--authorize-operator-remediation" in capsys.readouterr().err
