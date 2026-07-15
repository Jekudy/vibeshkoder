from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.conftest import import_module


pytestmark = pytest.mark.usefixtures("app_env")

COMMUNITY_CHAT_ID = -1001234567890
BOT_ID = 777
BOT_USERNAME = "VibeShkoderBot"


def _message(
    text: str | None,
    *,
    chat_id: int = COMMUNITY_CHAT_ID,
    user_id: int = 1001,
    user_is_bot: bool = False,
    reply_user_id: int | None = None,
    reply_user_is_bot: bool = True,
) -> SimpleNamespace:
    reply = None
    if reply_user_id is not None:
        reply = SimpleNamespace(
            from_user=SimpleNamespace(id=reply_user_id, is_bot=reply_user_is_bot)
        )
    return SimpleNamespace(
        text=text,
        caption=None,
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(id=user_id, is_bot=user_is_bot),
        reply_to_message=reply,
    )


def test_mention_triggers_case_insensitively_and_is_removed() -> None:
    trigger = import_module("bot.services.qa_trigger")

    result = trigger.extract_triggered_question(
        _message("  @VIBESHKODERBOT,   что мы решили про дайджест?  "),
        expected_chat_id=COMMUNITY_CHAT_ID,
        bot_id=BOT_ID,
        bot_username=BOT_USERNAME,
    )

    assert result is not None
    assert result.via_mention is True
    assert result.via_reply is False
    assert result.query == "что мы решили про дайджест?"


def test_reply_to_this_bot_triggers_without_mention() -> None:
    trigger = import_module("bot.services.qa_trigger")

    result = trigger.extract_triggered_question(
        _message("А где первоисточник?", reply_user_id=BOT_ID),
        expected_chat_id=COMMUNITY_CHAT_ID,
        bot_id=BOT_ID,
        bot_username=BOT_USERNAME,
    )

    assert result is not None
    assert result.via_reply is True
    assert result.via_mention is False
    assert result.query == "А где первоисточник?"


@pytest.mark.parametrize(
    "message",
    [
        _message("обычное сообщение"),
        _message("@VibeShkoderBot вопрос", chat_id=-100999),
        _message("@VibeShkoderBot вопрос", user_is_bot=True),
        _message("вопрос другому боту", reply_user_id=999),
        _message("вопрос человеку", reply_user_id=BOT_ID, reply_user_is_bot=False),
        _message("/recall @VibeShkoderBot память"),
        _message(None, reply_user_id=BOT_ID),
    ],
)
def test_non_eligible_messages_do_not_trigger(message: SimpleNamespace) -> None:
    trigger = import_module("bot.services.qa_trigger")

    assert (
        trigger.extract_triggered_question(
            message,
            expected_chat_id=COMMUNITY_CHAT_ID,
            bot_id=BOT_ID,
            bot_username=BOT_USERNAME,
        )
        is None
    )


def test_query_is_unicode_normalized_whitespace_collapsed_and_limited() -> None:
    trigger = import_module("bot.services.qa_trigger")
    decomposed = "е\u0308"
    payload = f"@{BOT_USERNAME}  {decomposed}\n\t" + ("д" * 500)

    result = trigger.extract_triggered_question(
        _message(payload),
        expected_chat_id=COMMUNITY_CHAT_ID,
        bot_id=BOT_ID,
        bot_username=BOT_USERNAME,
    )

    assert result is not None
    assert "\n" not in result.query
    assert "\t" not in result.query
    assert "ё" in result.query
    assert len(result.query) == trigger.MAX_USER_QUERY_CHARS
    assert result.was_truncated is True
