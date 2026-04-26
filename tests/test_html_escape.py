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


def test_intro_round_trip_no_double_escape(app_env, monkeypatch) -> None:
    """Stored build_intro_preview output is displayed without double-escape."""
    questionnaire = import_module("bot.handlers.questionnaire")
    handler = import_module("bot.handlers.forward_lookup")
    answers = [SimpleNamespace(question_index=0, answer_text="<b>x</b>")]
    stored_intro_text = questionnaire.build_intro_preview(answers)
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
        first_name="<Alice>",
        username="<alice>",
    )
    chat_message = SimpleNamespace(user_id=222)
    intro = SimpleNamespace(intro_text=stored_intro_text)

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
    assert "&amp;lt;" not in answer_text
    assert "&lt;b&gt;x&lt;/b&gt;" in answer_text
    assert "<b>x</b>" not in answer_text
    assert "Автор сообщения: &lt;Alice&gt; (@&lt;alice&gt;)" in answer_text
    assert "Автор сообщения: <Alice> (@<alice>)" not in answer_text
