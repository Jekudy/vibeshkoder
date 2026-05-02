from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

SEED_CHAT_ID = -1001234567890
L5_CHAT_ID = 1001
OTHER_CHAT_ID = 1002

_OFFRECORD_MARKER = "#" + "off" + "record"
_NOMEM_MARKER = "#" + "no" + "mem"
_OFFRECORD_POLICY = "off" + "record"
_NOMEM_POLICY = "no" + "mem"


@dataclass(frozen=True)
class PersistedMessage:
    chat_message_id: int
    version_id: int
    chat_id: int
    message_id: int
    user_id: int


@pytest_asyncio.fixture()
async def leakage_session(eval_db_session: AsyncSession) -> AsyncIterator[AsyncSession]:
    await _clear_leakage_tables(eval_db_session)
    yield eval_db_session
    await _clear_leakage_tables(eval_db_session)


async def _clear_leakage_tables(session: AsyncSession) -> None:
    await session.execute(
        text(
            """
            TRUNCATE TABLE
                qa_traces,
                offrecord_marks,
                message_versions,
                chat_messages,
                forget_events
            RESTART IDENTITY CASCADE
            """
        )
    )
    await session.flush()


def _model_classes() -> tuple[Any, Any]:
    models = importlib.import_module("bot.db.models")
    return models.ChatMessage, models.MessageVersion


def _make_message(
    *,
    message_id: int,
    chat_id: int,
    user_id: int,
    text_value: str,
) -> SimpleNamespace:
    raw_json = {
        "message_id": message_id,
        "chat": {"id": chat_id, "type": "supergroup"},
        "from": {"id": user_id},
        "date": datetime.now(timezone.utc).isoformat(),
        "text": text_value,
    }

    def model_dump(*, mode: str = "json", exclude_none: bool = True) -> dict[str, Any]:
        _ = (mode, exclude_none)
        return raw_json

    return SimpleNamespace(
        message_id=message_id,
        chat=SimpleNamespace(id=chat_id, type="supergroup"),
        from_user=SimpleNamespace(
            id=user_id,
            username=f"leakage_user_{user_id}",
            first_name="Leakage",
            last_name=None,
        ),
        text=text_value,
        caption=None,
        date=datetime.now(timezone.utc),
        model_dump=model_dump,
        reply_to_message=None,
        message_thread_id=None,
        photo=None,
        video=None,
        voice=None,
        audio=None,
        document=None,
        sticker=None,
        animation=None,
        video_note=None,
        location=None,
        contact=None,
        poll=None,
        dice=None,
        forward_origin=None,
        new_chat_members=None,
        left_chat_member=None,
        pinned_message=None,
        entities=None,
        caption_entities=None,
    )


async def _fetch_persisted(
    session: AsyncSession,
    *,
    chat_id: int,
    message_id: int,
) -> PersistedMessage:
    ChatMessage, MessageVersion = _model_classes()
    chat_message = await session.scalar(
        select(ChatMessage).where(
            ChatMessage.chat_id == chat_id,
            ChatMessage.message_id == message_id,
        )
    )
    if chat_message is None:
        raise AssertionError(f"message was not persisted: {chat_id}:{message_id}")
    if chat_message.current_version_id is None:
        raise AssertionError(f"current_version_id missing: {chat_id}:{message_id}")

    version = await session.scalar(
        select(MessageVersion).where(MessageVersion.id == chat_message.current_version_id)
    )
    if version is None:
        raise AssertionError(f"message version missing: {chat_message.current_version_id}")

    return PersistedMessage(
        chat_message_id=int(chat_message.id),
        version_id=int(version.id),
        chat_id=int(chat_message.chat_id),
        message_id=int(chat_message.message_id),
        user_id=int(chat_message.user_id),
    )


async def _persist_via_handler(
    session: AsyncSession,
    *,
    message_id: int,
    user_id: int,
    text_value: str,
) -> PersistedMessage:
    chat_messages_handler = importlib.import_module("bot.handlers.chat_messages")
    message = _make_message(
        message_id=message_id,
        chat_id=SEED_CHAT_ID,
        user_id=user_id,
        text_value=text_value,
    )
    await chat_messages_handler.save_chat_message(message, session)
    return await _fetch_persisted(session, chat_id=SEED_CHAT_ID, message_id=message_id)


async def _persist_via_service(
    session: AsyncSession,
    *,
    message_id: int,
    chat_id: int,
    user_id: int,
    text_value: str,
) -> PersistedMessage:
    user_repo = importlib.import_module("bot.db.repos.user")
    message_persistence = importlib.import_module("bot.services.message_persistence")

    await user_repo.UserRepo.upsert(
        session,
        telegram_id=user_id,
        username=f"leakage_user_{user_id}",
        first_name="Leakage",
        last_name=None,
    )
    message = _make_message(
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text_value=text_value,
    )
    await message_persistence.persist_message_with_policy(session, message, source="live")
    return await _fetch_persisted(session, chat_id=chat_id, message_id=message_id)


async def _create_case(
    session: AsyncSession,
    case_id: str,
) -> tuple[int, str, set[int]]:
    ChatMessage, MessageVersion = _model_classes()
    if case_id == "L1":
        body = f"{_OFFRECORD_MARKER} приватная арматура люкс"
        created = await _persist_via_handler(
            session,
            message_id=11_001,
            user_id=91_001,
            text_value=body,
        )
        chat_message = await session.get(ChatMessage, created.chat_message_id)
        version = await session.get(MessageVersion, created.version_id)
        assert chat_message is not None
        assert version is not None
        assert chat_message.memory_policy == _OFFRECORD_POLICY
        assert chat_message.is_redacted is True
        assert chat_message.text is None
        assert version.is_redacted is True
        assert version.normalized_text is None
        return SEED_CHAT_ID, "арматура", {created.version_id}

    if case_id == "L2":
        body = f"{_NOMEM_MARKER} скрытая дельта люкс"
        created = await _persist_via_handler(
            session,
            message_id=11_002,
            user_id=91_002,
            text_value=body,
        )
        chat_message = await session.get(ChatMessage, created.chat_message_id)
        assert chat_message is not None
        assert chat_message.memory_policy == _NOMEM_POLICY
        assert chat_message.is_redacted is False
        return SEED_CHAT_ID, "дельта", {created.version_id}

    if case_id == "L3":
        forget_event_repo = importlib.import_module("bot.db.repos.forget_event")

        created = await _persist_via_service(
            session,
            chat_id=SEED_CHAT_ID,
            message_id=11_003,
            user_id=91_003,
            text_value="удаляемая сигма люкс",
        )
        event = await forget_event_repo.ForgetEventRepo.create(
            session,
            target_type="message",
            target_id=str(created.message_id),
            actor_user_id=None,
            authorized_by="system",
            tombstone_key=f"message:{created.chat_id}:{created.message_id}",
        )
        await forget_event_repo.ForgetEventRepo.mark_status(session, event.id, status="processing")
        await forget_event_repo.ForgetEventRepo.mark_status(session, event.id, status="completed")
        return SEED_CHAT_ID, "сигма", {created.version_id}

    if case_id == "L4":
        created = await _persist_via_service(
            session,
            chat_id=SEED_CHAT_ID,
            message_id=11_004,
            user_id=91_004,
            text_value="закрытая омега люкс",
        )
        chat_message = await session.get(ChatMessage, created.chat_message_id)
        if chat_message is None:
            raise AssertionError("redaction target missing")
        chat_message.is_redacted = True
        await session.flush()
        return SEED_CHAT_ID, "омега", {created.version_id}

    if case_id == "L5":
        await _persist_via_service(
            session,
            chat_id=L5_CHAT_ID,
            message_id=11_005,
            user_id=91_005,
            text_value="общая каппа люкс",
        )
        other = await _persist_via_service(
            session,
            chat_id=OTHER_CHAT_ID,
            message_id=11_006,
            user_id=91_006,
            text_value="общая каппа люкс",
        )
        return L5_CHAT_ID, "каппа", {other.version_id}

    raise AssertionError(f"unknown case id: {case_id}")


@pytest.mark.parametrize("case_id", ["L1", "L2", "L3", "L4", "L5"])
async def test_recall_governance_leakage(
    eval_app_env: None,
    leakage_session: AsyncSession,
    case_id: str,
) -> None:
    _ = eval_app_env
    eval_runner = importlib.import_module("bot.services.eval_runner")
    chat_id, query, blocked_ids = await _create_case(leakage_session, case_id)

    bundle, trace = await eval_runner.run_eval_recall(
        leakage_session,
        query=query,
        chat_id=chat_id,
    )

    assert trace is None
    assert set(bundle.evidence_ids).isdisjoint(blocked_ids)
    if case_id == "L5":
        assert bundle.items
        assert all(item.chat_id == L5_CHAT_ID for item in bundle.items)
