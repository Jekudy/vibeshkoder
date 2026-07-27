from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


class _Session:
    def __init__(self) -> None:
        self.commit = AsyncMock()
        self.rollback = AsyncMock()


class _SessionContext(AbstractAsyncContextManager[_Session]):
    def __init__(self, session: _Session) -> None:
        self.session = session

    async def __aenter__(self) -> _Session:
        return self.session

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        return None


@pytest.mark.parametrize(
    ("argv", "expected_kwargs"),
    [
        (
            [
                "intro_effect_reconcile",
                "--effect-id",
                "17",
                "--action",
                "record-sent",
                "--chat-id",
                "-1001234567890",
                "--message-id",
                "615",
                "--operator-user-id",
                "149820031",
                "--reason",
                "found in chat history",
            ],
            {
                "effect_id": 17,
                "action": "record-sent",
                "chat_id": -1001234567890,
                "message_id": 615,
                "evidence_sha256": None,
                "operator_user_id": 149820031,
                "reason": "found in chat history",
            },
        ),
        (
            [
                "intro_effect_reconcile",
                "--effect-id",
                "18",
                "--action",
                "retry-absent",
                "--evidence-sha256",
                "a" * 64,
                "--operator-user-id",
                "149820031",
                "--reason",
                "checked chat history",
            ],
            {
                "effect_id": 18,
                "action": "retry-absent",
                "chat_id": None,
                "message_id": None,
                "evidence_sha256": "a" * 64,
                "operator_user_id": 149820031,
                "reason": "checked chat history",
            },
        ),
    ],
)
def test_intro_effect_reconcile_main_passes_typed_args_and_commits_once(
    app_env: None,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    expected_kwargs: dict[str, object],
) -> None:
    from aiogram import Bot
    from bot import cli
    import bot.db.engine as db_engine
    from bot.services import intro_effect_worker

    session = _Session()
    reconcile = AsyncMock(return_value=SimpleNamespace(id=expected_kwargs["effect_id"]))
    send_message = AsyncMock()
    monkeypatch.setattr(db_engine, "async_session", lambda: _SessionContext(session))
    monkeypatch.setattr(intro_effect_worker, "reconcile_intro_effect", reconcile)
    monkeypatch.setattr(Bot, "send_message", send_message)

    assert cli.main(argv) == 0
    reconcile.assert_awaited_once_with(session, **expected_kwargs)
    session.commit.assert_awaited_once_with()
    session.rollback.assert_not_awaited()
    send_message.assert_not_awaited()


@pytest.mark.parametrize(
    ("argv", "expected_error"),
    [
        (
            [
                "intro_effect_reconcile",
                "--effect-id",
                "17",
                "--action",
                "record-sent",
                "--chat-id",
                "-1001234567890",
                "--operator-user-id",
                "149820031",
                "--reason",
                "found in chat history",
            ],
            "--message-id",
        ),
        (
            [
                "intro_effect_reconcile",
                "--effect-id",
                "18",
                "--action",
                "retry-absent",
                "--operator-user-id",
                "149820031",
                "--reason",
                "checked chat history",
            ],
            "--evidence-sha256",
        ),
        (
            [
                "intro_effect_reconcile",
                "--effect-id",
                "18",
                "--action",
                "retry-absent",
                "--evidence-sha256",
                "a" * 64,
                "--operator-user-id",
                "149820031",
            ],
            "--reason",
        ),
        (
            [
                "intro_effect_reconcile",
                "--effect-id",
                "18",
                "--action",
                "send-now",
                "--operator-user-id",
                "149820031",
                "--reason",
                "forbidden direct send",
            ],
            "invalid choice: 'send-now'",
        ),
    ],
)
def test_intro_effect_reconcile_parser_rejects_missing_contract_fields_or_action(
    argv: list[str], expected_error: str, capsys: pytest.CaptureFixture[str]
) -> None:
    from bot.cli import main

    with pytest.raises(SystemExit) as raised:
        main(argv)

    assert raised.value.code == 2
    stderr = capsys.readouterr().err
    assert expected_error in stderr
    assert "invalid choice: 'intro_effect_reconcile'" not in stderr


def test_intro_effect_reconcile_rolls_back_and_returns_nonzero_on_domain_error(
    app_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aiogram import Bot
    from bot import cli
    import bot.db.engine as db_engine
    from bot.services import intro_effect_worker
    from bot.services.intro_effect_worker import IntroEffectReconcileError

    session = _Session()
    reconcile = AsyncMock(
        side_effect=IntroEffectReconcileError("only unknown effects can be reconciled")
    )
    send_message = AsyncMock()
    monkeypatch.setattr(db_engine, "async_session", lambda: _SessionContext(session))
    monkeypatch.setattr(intro_effect_worker, "reconcile_intro_effect", reconcile)
    monkeypatch.setattr(Bot, "send_message", send_message)

    assert (
        cli.main(
            [
                "intro_effect_reconcile",
                "--effect-id",
                "17",
                "--action",
                "record-sent",
                "--chat-id",
                "-1001234567890",
                "--message-id",
                "615",
                "--operator-user-id",
                "149820031",
                "--reason",
                "found in chat history",
            ]
        )
        == 1
    )
    reconcile.assert_awaited_once()
    session.commit.assert_not_awaited()
    session.rollback.assert_awaited_once_with()
    send_message.assert_not_awaited()
