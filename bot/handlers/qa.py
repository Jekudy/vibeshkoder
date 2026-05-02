from __future__ import annotations

import html
import logging
from datetime import datetime

from aiogram import Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.repos.feature_flag import FeatureFlagRepo
from bot.db.repos.qa_trace import QaTraceRepo
from bot.db.repos.user import UserRepo
from bot.services.evidence import EvidenceBundle
from bot.services.governance import detect_policy
from bot.services.qa import run_qa

logger = logging.getLogger(__name__)

router = Router(name="qa")

QA_FEATURE_FLAG = "memory.qa.enabled"


def _short_chat_id(chat_id: int) -> str:
    chat_id_str = str(chat_id)
    return chat_id_str.removeprefix("-100") if chat_id_str.startswith("-100") else chat_id_str


def _format_date(value: datetime) -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M")


def _safe_headline(snippet: str) -> str:
    escaped = html.escape(snippet, quote=False)
    return escaped.replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>")


def _author_name(user: object | None) -> str:
    if user is None:
        return "—"

    first_name = getattr(user, "first_name", None)
    last_name = getattr(user, "last_name", None)
    username = getattr(user, "username", None)

    if first_name:
        name = str(first_name)
        if last_name:
            name = f"{name} {last_name}"
        return html.escape(name)
    if username:
        return html.escape(f"@{username}")
    return "—"


def _format_response(bundle: EvidenceBundle, users_by_id: dict[int, object]) -> str:
    if bundle.abstained:
        return "Не нашёл подходящих свидетельств в истории чата."

    parts = ["<b>Найденные свидетельства:</b>"]
    short_chat_id = _short_chat_id(bundle.chat_id)
    for item in bundle.items:
        author_name = _author_name(users_by_id.get(item.user_id) if item.user_id else None)
        date_text = _format_date(item.message_date)
        snippet = _safe_headline(item.snippet)
        link = f"https://t.me/c/{short_chat_id}/{item.message_id}"
        parts.append(
            f"<blockquote>{snippet}</blockquote>\n"
            f"<i>— {author_name}, {date_text}</i> · "
            f"<a href=\"{html.escape(link, quote=True)}\">сообщение</a> · "
            f"<code>message_version_id:{item.message_version_id}</code>"
        )
    return "\n\n".join(parts)


async def _write_trace(
    session: AsyncSession,
    *,
    user_tg_id: int,
    chat_id: int,
    query: str,
    evidence_ids: list[int],
    abstained: bool,
    redact_query: bool,
) -> None:
    await QaTraceRepo.create(
        session,
        user_tg_id=user_tg_id,
        chat_id=chat_id,
        query=query,
        evidence_ids=evidence_ids,
        abstained=abstained,
        redact_query=redact_query,
    )


@router.message(Command("recall"))
async def recall_handler(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
) -> None:
    if not await FeatureFlagRepo.get(session, QA_FEATURE_FLAG):
        return

    if message.from_user is None:
        return

    query = (command.args or "").strip()
    policy, _payload = detect_policy(text=query, caption=None)
    redact_query = policy != "normal"

    async def audit_empty(abstained: bool = True) -> None:
        await _write_trace(
            session,
            user_tg_id=message.from_user.id,
            chat_id=message.chat.id,
            query=query,
            evidence_ids=[],
            abstained=abstained,
            redact_query=redact_query,
        )

    if message.chat.id != settings.COMMUNITY_CHAT_ID:
        try:
            await message.reply("Команда /recall работает только в community чате.")
        except TelegramForbiddenError:
            # Bot lacks can_send_messages in this chat (e.g. kicked, restricted).
            # Audit-only path: still record the abstain trace, do not raise.
            logger.info(
                "recall refused: bot lacks send permission",
                extra={
                    "chat_id": message.chat.id,
                    "user_id": getattr(message.from_user, "id", None),
                },
            )
        await audit_empty()
        return

    user = await UserRepo.get(session, message.from_user.id)
    if user is None or not (user.is_member or user.is_admin):
        await message.reply("Доступ только участникам сообщества.")
        await audit_empty()
        return

    if not query:
        await message.reply(
            "Использование: <code>/recall &lt;вопрос&gt;</code>",
            parse_mode="HTML",
        )
        await audit_empty()
        return

    result = await run_qa(
        session,
        query=query,
        chat_id=message.chat.id,
        redact_query_in_audit=redact_query,
    )

    users_by_id: dict[int, object] = {}
    for item in result.bundle.items:
        if item.user_id is None or item.user_id in users_by_id:
            continue
        author = await UserRepo.get(session, item.user_id)
        if author is not None:
            users_by_id[item.user_id] = author

    await message.reply(
        _format_response(result.bundle, users_by_id),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await _write_trace(
        session,
        user_tg_id=message.from_user.id,
        chat_id=message.chat.id,
        query=query,
        evidence_ids=result.bundle.evidence_ids,
        abstained=result.bundle.abstained,
        redact_query=result.query_redacted,
    )
