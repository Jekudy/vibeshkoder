from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tests.conftest import import_module


def _application_answers() -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            application_id=444, question_index=0, field_id="name", answer_text="<b>x</b>"
        ),
        SimpleNamespace(
            application_id=444, question_index=1, field_id="location", answer_text="UK & EU"
        ),
        SimpleNamespace(
            application_id=444, question_index=2, field_id="referral", answer_text="@nick"
        ),
        SimpleNamespace(
            application_id=444,
            question_index=3,
            field_id="experience",
            answer_text='"<script>"',
        ),
        SimpleNamespace(
            application_id=444,
            question_index=4,
            field_id="projects",
            answer_text="A 'quote'",
        ),
        SimpleNamespace(
            application_id=444, question_index=5, field_id="hardest", answer_text="5 > 3"
        ),
        SimpleNamespace(application_id=444, question_index=6, field_id="goals", answer_text="R&D"),
    ]


def _expected_intro() -> str:
    return "\n".join(
        [
            "👤 Имя: &lt;b&gt;x&lt;/b&gt;",
            "📍 Основная локация: UK &amp; EU",
            "🔗 От кого узнал о чате: @nick",
            "💡 Опыт с вайб-кодингом: &quot;&lt;script&gt;&quot;",
            "🚀 Проекты и автоматизации: A &#x27;quote&#x27;",
            "🏋️ Самое сложное: 5 &gt; 3",
            "🎯 Цели: R&amp;D",
        ]
    )


def test_intro_preview_uses_complete_application_scoped_intro_v2_renderer(app_env) -> None:
    questionnaire = import_module("bot.handlers.questionnaire")
    answers = _application_answers()

    intro_text = questionnaire.build_intro_preview(answers)

    assert intro_text == _expected_intro()


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
    """A frozen application snapshot is displayed without double escaping."""
    questionnaire = import_module("bot.handlers.questionnaire")
    handler = import_module("bot.handlers.forward_lookup")
    stored_intro_text = questionnaire.build_intro_preview(_application_answers())
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
    intro = SimpleNamespace(intro_text=stored_intro_text, application_id=444)

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
    assert _expected_intro() in answer_text
    assert "&amp;lt;" not in answer_text
    assert "&lt;b&gt;x&lt;/b&gt;" in answer_text
    assert "<b>x</b>" not in answer_text
    assert "Автор сообщения: &lt;Alice&gt; (@&lt;alice&gt;)" in answer_text
    assert "Автор сообщения: <Alice> (@<alice>)" not in answer_text
