from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

import pytest
from aiogram.types import (
    Chat,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberUpdated,
    Message,
    Update,
    User,
)
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


pytestmark = pytest.mark.usefixtures("app_env")


def _update_id(offset: int) -> int:
    return 8_500_000_000_000 + int(uuid.uuid4().hex[:8], 16) * 10 + offset


def _edited_update(update_id: int, secret: str) -> Update:
    return Update(
        update_id=update_id,
        edited_message=Message(
            message_id=701,
            date=datetime.now(timezone.utc),
            edit_date=int(datetime.now(timezone.utc).timestamp()),
            chat=Chat(id=-1001234567890, type="supergroup", title="community"),
            from_user=User(id=4201, is_bot=False, first_name="Member"),
            text=f"edited {secret}",
        ),
    )


def _service_update(update_id: int) -> Update:
    actor = User(id=4202, is_bot=False, first_name="Admin")
    bot_user = User(id=4203, is_bot=True, first_name="Shkoder")
    return Update(
        update_id=update_id,
        my_chat_member=ChatMemberUpdated(
            chat=Chat(id=-1001234567890, type="supergroup", title="community"),
            from_user=actor,
            date=datetime.now(timezone.utc),
            old_chat_member=ChatMemberLeft(user=bot_user),
            new_chat_member=ChatMemberMember(user=bot_user),
        ),
    )


def _bot_message_update(update_id: int, secret: str) -> Update:
    return Update(
        update_id=update_id,
        message=Message(
            message_id=702,
            date=datetime.now(timezone.utc),
            chat=Chat(id=-1001234567890, type="supergroup", title="community"),
            from_user=User(id=4203, is_bot=True, first_name="Shkoder"),
            text=f"bot-output {secret}",
        ),
    )


async def test_all_raw_update_shapes_survive_downstream_rollback(
    postgres_engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The source archive commits before dispatch, independently of handler outcome."""

    from bot.db.models import FeatureFlag, TelegramUpdate
    from bot.db.repos.feature_flag import FeatureFlagRepo
    from bot.middlewares import db_session as db_session_module
    from bot.middlewares.raw_update_persistence import RawUpdatePersistenceMiddleware
    from bot.services.ingestion import RAW_ARCHIVE_FLAG

    factory = async_sessionmaker(
        postgres_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    monkeypatch.setattr(db_session_module, "async_session", factory)
    secret = "raw-durability-secret-sentinel"
    update_ids = [_update_id(offset) for offset in range(3)]
    updates = [
        _edited_update(update_ids[0], secret),
        _service_update(update_ids[1]),
        _bot_message_update(update_ids[2], secret),
    ]
    expected_types = ["edited_message", "my_chat_member", "message"]

    async with factory() as setup:
        previous_flag = await setup.scalar(
            select(FeatureFlag).where(
                FeatureFlag.flag_key == RAW_ARCHIVE_FLAG,
                FeatureFlag.scope_type.is_(None),
                FeatureFlag.scope_id.is_(None),
            )
        )
        previous_state = (
            None
            if previous_flag is None
            else (
                previous_flag.enabled,
                previous_flag.config_json,
                previous_flag.updated_by,
            )
        )
        await FeatureFlagRepo.set_enabled(setup, RAW_ARCHIVE_FLAG, enabled=True)
        await setup.commit()

    caplog.set_level(logging.ERROR)
    try:
        for update, expected_type in zip(updates, expected_types, strict=True):
            downstream_seen: list[int] = []

            async def downstream(event, data):
                raw_update = data["raw_update"]
                downstream_seen.append(raw_update.id)
                async with factory() as visible_before_dispatch:
                    persisted = await visible_before_dispatch.scalar(
                        select(TelegramUpdate).where(TelegramUpdate.update_id == event.update_id)
                    )
                    assert persisted is not None
                    assert persisted.update_type == expected_type
                raise RuntimeError(secret)

            raw_middleware = RawUpdatePersistenceMiddleware()

            async def raw_then_downstream(event, data):
                return await raw_middleware(downstream, event, data)

            with pytest.raises(RuntimeError, match=secret):
                await db_session_module.DbSessionMiddleware()(
                    raw_then_downstream,
                    update,
                    {},
                )

            assert len(downstream_seen) == 1

        async with factory() as verify:
            rows = (
                (
                    await verify.execute(
                        select(TelegramUpdate)
                        .where(TelegramUpdate.update_id.in_(update_ids))
                        .order_by(TelegramUpdate.update_id)
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 3
        assert {row.update_type for row in rows} == set(expected_types)
        assert secret not in caplog.text
        assert all(record.exc_info is None for record in caplog.records)
    finally:
        async with factory() as cleanup:
            await cleanup.execute(
                delete(TelegramUpdate).where(TelegramUpdate.update_id.in_(update_ids))
            )
            if previous_state is None:
                await cleanup.execute(
                    delete(FeatureFlag).where(
                        FeatureFlag.flag_key == RAW_ARCHIVE_FLAG,
                        FeatureFlag.scope_type.is_(None),
                        FeatureFlag.scope_id.is_(None),
                    )
                )
            else:
                enabled, config_json, updated_by = previous_state
                await FeatureFlagRepo.set_enabled(
                    cleanup,
                    RAW_ARCHIVE_FLAG,
                    enabled=enabled,
                    config_json=config_json,
                    updated_by=updated_by,
                )
            await cleanup.commit()
