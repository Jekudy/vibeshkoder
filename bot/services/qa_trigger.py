"""Trigger detection for conversational memory questions.

The public Q&A surface deliberately has no command.  A message is eligible only
when a human in the configured community chat either mentions this bot by its
exact username or replies to a message authored by this bot.  Keeping this logic
in an aiogram filter is important: non-triggering messages must continue to the
lower-priority ``chat_messages`` router instead of being swallowed by a catch-all
Q&A handler.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from aiogram import Bot
from aiogram.filters import Filter
from aiogram.types import Message


# The downstream Phase 5 gateway has a 256-character hard limit.  We reserve
# room there for the evidence-only instruction wrapper added by qa_guardrails.
MAX_USER_QUERY_CHARS = 145


@dataclass(frozen=True, slots=True)
class TriggeredQuestion:
    query: str
    via_mention: bool
    via_reply: bool
    was_truncated: bool


def _message_text(message: object) -> str | None:
    text = getattr(message, "text", None)
    if isinstance(text, str) and text:
        return text
    caption = getattr(message, "caption", None)
    if isinstance(caption, str) and caption:
        return caption
    return None


def _mention_pattern(bot_username: str | None) -> re.Pattern[str] | None:
    if not bot_username:
        return None
    username = bot_username.removeprefix("@").strip()
    if not username:
        return None
    # Telegram usernames contain ASCII letters, digits, and underscores.
    # Explicit boundaries prevent @ShkoderBotFake from triggering ShkoderBot.
    return re.compile(
        rf"(?<![A-Za-z0-9_])@{re.escape(username)}(?![A-Za-z0-9_])",
        flags=re.IGNORECASE,
    )


def _normalise_query(value: str) -> tuple[str, bool]:
    value = unicodedata.normalize("NFKC", value)
    # Remove non-whitespace control/format characters.  Newlines and tabs are
    # then folded by split/join together with ordinary repeated whitespace.
    value = "".join(
        char for char in value if not unicodedata.category(char).startswith("C") or char.isspace()
    )
    value = " ".join(value.split()).strip(" ,:;—–-")
    was_truncated = len(value) > MAX_USER_QUERY_CHARS
    if was_truncated:
        value = value[:MAX_USER_QUERY_CHARS].rstrip()
    return value, was_truncated


def extract_triggered_question(
    message: object,
    *,
    expected_chat_id: int,
    bot_id: int,
    bot_username: str | None,
) -> TriggeredQuestion | None:
    """Return a bounded question only for an eligible conversational trigger."""

    chat = getattr(message, "chat", None)
    sender = getattr(message, "from_user", None)
    if (
        chat is None
        or getattr(chat, "id", None) != expected_chat_id
        or sender is None
        or bool(getattr(sender, "is_bot", False))
    ):
        return None

    text = _message_text(message)
    if text is None:
        return None
    if text.lstrip().startswith("/"):
        # Commands are intentionally not a public Q&A trigger.
        return None

    pattern = _mention_pattern(bot_username)
    via_mention = pattern is not None and pattern.search(text) is not None

    reply = getattr(message, "reply_to_message", None)
    reply_sender = getattr(reply, "from_user", None) if reply is not None else None
    via_reply = bool(
        reply_sender is not None
        and getattr(reply_sender, "id", None) == bot_id
        and bool(getattr(reply_sender, "is_bot", False))
    )
    if not (via_mention or via_reply):
        return None

    without_trigger = pattern.sub(" ", text) if via_mention and pattern else text
    query, was_truncated = _normalise_query(without_trigger)
    return TriggeredQuestion(
        query=query,
        via_mention=via_mention,
        via_reply=via_reply,
        was_truncated=was_truncated,
    )


class ShkoderQuestionFilter(Filter):
    """Aiogram filter that injects ``qa_question`` into handler data."""

    def __init__(self, expected_chat_id: int) -> None:
        self._expected_chat_id = expected_chat_id

    async def __call__(self, message: Message, bot: Bot) -> bool | dict[str, Any]:
        me = await bot.me()
        question = extract_triggered_question(
            message,
            expected_chat_id=self._expected_chat_id,
            bot_id=me.id,
            bot_username=me.username,
        )
        if question is None:
            return False
        return {"qa_question": question}


__all__ = [
    "MAX_USER_QUERY_CHARS",
    "ShkoderQuestionFilter",
    "TriggeredQuestion",
    "extract_triggered_question",
]
