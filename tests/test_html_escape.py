from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tests.conftest import import_module


def test_intro_preview_escapes_html_in_answers(app_env) -> None:
    questionnaire = import_module("bot.handlers.questionnaire")
    answers = [SimpleNamespace(question_index=0, answer_text="<b>x</b>")]

    intro_text = questionnaire.build_intro_preview(answers)

    assert "&lt;b&gt;x&lt;/b&gt;" in intro_text
    assert "<b>x</b>" not in intro_text


def test_admin_nudge_escapes_html_in_username(app_env) -> None:
    scheduler = import_module("bot.services.scheduler")

    message = scheduler.format_admin_nudge(
        name="Alice",
        username="<script>",
        app_id=42,
    )

    assert "@&lt;script&gt;" in message
    assert "@<script>" not in message


def test_forward_intro_escapes_html_in_intro_text(app_env, monkeypatch) -> None:
    handler = import_module("bot.handlers.forward_lookup")
    session = AsyncMock()
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=111),
        text="stored community message",
        answer=AsyncMock(),
    )
    requester = SimpleNamespace(
        id=111,
        is_member=True,
        is_admin=False,
        first_name="Requester",
        username="requester",
    )
    author = SimpleNamespace(
        id=222,
        is_member=True,
        is_admin=False,
        first_name="Alice",
        username="alice",
    )
    chat_message = SimpleNamespace(user_id=222)
    intro = SimpleNamespace(intro_text='<a href="https://example.com">click</a>')

    monkeypatch.setattr(handler.UserRepo, "get", AsyncMock(side_effect=[requester, author]))
    monkeypatch.setattr(
        handler.MessageRepo,
        "find_by_exact_text",
        AsyncMock(return_value=chat_message),
    )
    monkeypatch.setattr(handler.IntroRepo, "get", AsyncMock(return_value=intro))

    asyncio.run(handler.handle_forwarded_message(message, session))

    message.answer.assert_awaited_once()
    answer_text = message.answer.await_args.args[0]
    assert "&lt;a href=&quot;https://example.com&quot;&gt;click&lt;/a&gt;" in answer_text
    assert '<a href="https://example.com">click</a>' not in answer_text
