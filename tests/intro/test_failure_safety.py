from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.intro.test_effect_worker import (
    RecordingBot,
    EffectTestDatabase,
    _confirmed_application,
    _effect,
    _worker_with_test_database,
)


@pytest_asyncio.fixture
async def effect_test_db(postgres_engine):
    sessions = async_sessionmaker(bind=postgres_engine, class_=AsyncSession, expire_on_commit=False)
    database = EffectTestDatabase(sessions)
    await database.cleanup()
    try:
        yield database
    finally:
        await database.cleanup()


async def _current_intro(session: AsyncSession, user_id: int, application_id: int | None):
    from bot.db.models import Intro

    intro = Intro(
        user_id=user_id,
        application_id=application_id,
        intro_text="old frozen intro",
        vouched_by_name="@voucher",
    )
    session.add(intro)
    await session.flush()
    return intro


@pytest.mark.asyncio
async def test_refresh_telegram_failure_keeps_old_intro_current(
    app_env, effect_test_db, monkeypatch
) -> None:
    from aiogram.exceptions import TelegramNetworkError

    worker, sessions = _worker_with_test_database(monkeypatch, effect_test_db)
    user_id = effect_test_db.user_id()
    async with sessions.begin() as session:
        base = await _confirmed_application(session, user_id=user_id, flow_kind="admission")
        base.status = "added"
        current = await _current_intro(session, user_id, base.id)
        refresh = await _confirmed_application(
            session, user_id=user_id, flow_kind="refresh", base_id=base.id
        )
        effect = await _effect(session, refresh.id, "refresh_intro")
        current_id, effect_id, base_id = current.id, effect.id, base.id

    await worker.process_intro_effects(RecordingBot([TelegramNetworkError(None, "response lost")]))
    async with sessions() as observer:
        current = await observer.get(type(current), current_id)
        effect = await observer.get(type(effect), effect_id)
        assert current.application_id == base_id
        assert effect.status == "unknown"


@pytest.mark.asyncio
async def test_refresh_without_current_intro_is_stale_before_io_and_marks_delivery_failed(
    app_env, effect_test_db, monkeypatch
) -> None:
    worker, sessions = _worker_with_test_database(monkeypatch, effect_test_db)
    user_id = effect_test_db.user_id()
    async with sessions.begin() as session:
        base = await _confirmed_application(session, user_id=user_id, flow_kind="admission")
        base.status = "added"
        refresh = await _confirmed_application(
            session, user_id=user_id, flow_kind="refresh", base_id=base.id
        )
        effect = await _effect(session, refresh.id, "refresh_intro")
        effect_id, refresh_id = effect.id, refresh.id
    bot = RecordingBot()

    await worker.process_intro_effects(bot)
    async with sessions() as observer:
        effect = await observer.get(type(effect), effect_id)
        refresh = await observer.get(type(refresh), refresh_id)
        assert effect.status == "stale"
        assert refresh.status == "delivery_failed"
    assert bot.calls == []


@pytest.mark.asyncio
async def test_non_member_refresh_is_stale_before_io_without_promotion_or_projection(
    app_env, effect_test_db, monkeypatch
) -> None:
    from bot.db.models import IntroEffectOutbox, User

    worker, sessions = _worker_with_test_database(monkeypatch, effect_test_db)
    user_id = effect_test_db.user_id()
    async with sessions.begin() as session:
        base = await _confirmed_application(session, user_id=user_id, flow_kind="admission")
        base.status = "added"
        current = await _current_intro(session, user_id, base.id)
        refresh = await _confirmed_application(
            session, user_id=user_id, flow_kind="refresh", base_id=base.id
        )
        (await session.get(User, user_id)).is_member = False
        effect = await _effect(session, refresh.id, "refresh_intro")
        current_id, effect_id, refresh_id, base_id = current.id, effect.id, refresh.id, base.id
    bot = RecordingBot()

    await worker.process_intro_effects(bot)

    async with sessions() as observer:
        current = await observer.get(type(current), current_id)
        effect = await observer.get(IntroEffectOutbox, effect_id)
        effects = list(
            (
                await observer.execute(
                    select(IntroEffectOutbox).where(IntroEffectOutbox.application_id == refresh_id)
                )
            ).scalars()
        )
        assert current.application_id == base_id
        assert effect.status == "stale"
        assert [row.effect_kind for row in effects] == ["refresh_intro"]
    assert bot.calls == []


@pytest.mark.asyncio
async def test_refresh_without_base_or_intro_is_stale_before_io_and_marks_delivery_failed(
    app_env, effect_test_db, monkeypatch
) -> None:
    worker, sessions = _worker_with_test_database(monkeypatch, effect_test_db)
    async with sessions.begin() as session:
        refresh = await _confirmed_application(
            session, user_id=effect_test_db.user_id(), flow_kind="refresh", base_id=None
        )
        effect = await _effect(session, refresh.id, "refresh_intro")
        effect_id, refresh_id = effect.id, refresh.id
    bot = RecordingBot()

    await worker.process_intro_effects(bot)
    async with sessions() as observer:
        effect = await observer.get(type(effect), effect_id)
        refresh = await observer.get(type(refresh), refresh_id)
        assert (effect.status, refresh.status) == ("stale", "delivery_failed")
    assert bot.calls == []


@pytest.mark.asyncio
async def test_refresh_success_promotes_with_cas_enqueues_one_projection_and_closes_tracking(
    app_env, effect_test_db, monkeypatch
) -> None:
    from bot.db.models import IntroEffectOutbox, IntroRefreshTracking

    worker, sessions = _worker_with_test_database(monkeypatch, effect_test_db)
    user_id = effect_test_db.user_id()
    async with sessions.begin() as session:
        base = await _confirmed_application(session, user_id=user_id, flow_kind="admission")
        base.status = "added"
        current = await _current_intro(session, user_id, base.id)
        refresh = await _confirmed_application(
            session, user_id=user_id, flow_kind="refresh", base_id=base.id
        )
        tracking = IntroRefreshTracking(
            user_id=user_id,
            cycle_started_at=base.created_at,
            reminders_sent=1,
            phase="daily",
            completed=False,
        )
        session.add(tracking)
        effect = await _effect(session, refresh.id, "refresh_intro")
        current_id, tracking_id, effect_id, refresh_id = (
            current.id,
            tracking.id,
            effect.id,
            refresh.id,
        )
        snapshot = refresh.confirmed_intro_html

    await worker.process_intro_effects(
        RecordingBot([SimpleNamespace(message_id=222)]), max_effects=1
    )
    async with sessions() as observer:
        current = await observer.get(type(current), current_id)
        tracking = await observer.get(type(tracking), tracking_id)
        effect = await observer.get(type(effect), effect_id)
        refresh = await observer.get(type(refresh), refresh_id)
        rows = list(
            (
                await observer.execute(
                    select(IntroEffectOutbox).where(IntroEffectOutbox.application_id == refresh_id)
                )
            ).scalars()
        )
        assert current.application_id == refresh_id
        assert current.intro_text == snapshot
        assert refresh.status == "added"
        assert effect.status == "sent"
        assert {(row.effect_kind, row.status) for row in rows} == {
            ("refresh_intro", "sent"),
            ("sheet_projection", "pending"),
        }
        assert tracking.completed is True


@pytest.mark.asyncio
async def test_refresh_cas_mismatch_is_stale_but_preserves_telegram_identity_without_projection(
    app_env, effect_test_db, monkeypatch
) -> None:
    from bot.db.models import IntroEffectOutbox

    worker, sessions = _worker_with_test_database(monkeypatch, effect_test_db)
    user_id = effect_test_db.user_id()
    async with sessions.begin() as session:
        base = await _confirmed_application(session, user_id=user_id, flow_kind="admission")
        base.status = "added"
        newer = await _confirmed_application(session, user_id=user_id, flow_kind="admission")
        newer.status = "added"
        current = await _current_intro(session, user_id, newer.id)
        refresh = await _confirmed_application(
            session, user_id=user_id, flow_kind="refresh", base_id=base.id
        )
        effect = await _effect(session, refresh.id, "refresh_intro")
        current_id, effect_id, refresh_id, newer_id = current.id, effect.id, refresh.id, newer.id

    bot = RecordingBot()
    await worker.process_intro_effects(bot)
    async with sessions() as observer:
        current = await observer.get(type(current), current_id)
        effect = await observer.get(type(effect), effect_id)
        rows = list(
            (
                await observer.execute(
                    select(IntroEffectOutbox).where(IntroEffectOutbox.application_id == refresh_id)
                )
            ).scalars()
        )
        assert current.application_id == newer_id
        assert (effect.status, effect.chat_id, effect.message_id) == ("stale", None, None)
        assert [row.effect_kind for row in rows] == ["refresh_intro"]
        assert (await observer.get(type(refresh), refresh_id)).status == "delivery_failed"
    assert bot.calls == []


@pytest.mark.asyncio
async def test_refresh_pointer_flip_during_telegram_send_is_stale_with_identity_and_no_projection(
    app_env, effect_test_db, monkeypatch
) -> None:
    from bot.db.models import Intro, IntroEffectOutbox

    worker, sessions = _worker_with_test_database(monkeypatch, effect_test_db)
    user_id = effect_test_db.user_id()
    async with sessions.begin() as session:
        base = await _confirmed_application(session, user_id=user_id, flow_kind="admission")
        base.status = "added"
        current = await _current_intro(session, user_id, base.id)
        refresh = await _confirmed_application(
            session, user_id=user_id, flow_kind="refresh", base_id=base.id
        )
        newer = await _confirmed_application(session, user_id=user_id, flow_kind="admission")
        newer.status = "added"
        effect = await _effect(session, refresh.id, "refresh_intro")
        current_id, refresh_id, effect_id, newer_id = current.id, refresh.id, effect.id, newer.id

    class PointerFlippingBot(RecordingBot):
        async def send_message(self, **kwargs):
            response = await super().send_message(**kwargs)
            async with sessions.begin() as session:
                pointer = await session.get(Intro, current_id, with_for_update=True)
                pointer.application_id = newer_id
            return response

    await worker.process_intro_effects(PointerFlippingBot([SimpleNamespace(message_id=992)]))
    async with sessions() as observer:
        current = await observer.get(type(current), current_id)
        effect = await observer.get(IntroEffectOutbox, effect_id)
        effects = list(
            (
                await observer.execute(
                    select(IntroEffectOutbox).where(IntroEffectOutbox.application_id == refresh_id)
                )
            ).scalars()
        )
        assert current.application_id == newer_id
        assert (effect.status, effect.chat_id, effect.message_id) == (
            "stale",
            -100_123_456_7890,
            992,
        )
        assert [row.effect_kind for row in effects] == ["refresh_intro"]


@pytest.mark.asyncio
async def test_refresh_membership_flip_during_telegram_send_is_stale_with_identity_and_no_projection(
    app_env, effect_test_db, monkeypatch
) -> None:
    from bot.db.models import IntroEffectOutbox, User

    worker, sessions = _worker_with_test_database(monkeypatch, effect_test_db)
    user_id = effect_test_db.user_id()
    async with sessions.begin() as session:
        base = await _confirmed_application(session, user_id=user_id, flow_kind="admission")
        base.status = "added"
        current = await _current_intro(session, user_id, base.id)
        refresh = await _confirmed_application(
            session, user_id=user_id, flow_kind="refresh", base_id=base.id
        )
        effect = await _effect(session, refresh.id, "refresh_intro")
        current_id, effect_id, refresh_id, base_id = current.id, effect.id, refresh.id, base.id

    class MembershipFlippingBot(RecordingBot):
        async def send_message(self, **kwargs):
            response = await super().send_message(**kwargs)
            async with sessions.begin() as session:
                (await session.get(User, user_id, with_for_update=True)).is_member = False
            return response

    await worker.process_intro_effects(MembershipFlippingBot([SimpleNamespace(message_id=993)]))

    async with sessions() as observer:
        current = await observer.get(type(current), current_id)
        effect = await observer.get(IntroEffectOutbox, effect_id)
        effects = list(
            (
                await observer.execute(
                    select(IntroEffectOutbox).where(IntroEffectOutbox.application_id == refresh_id)
                )
            ).scalars()
        )
        assert current.application_id == base_id
        assert (effect.status, effect.chat_id, effect.message_id) == (
            "stale",
            -100_123_456_7890,
            993,
        )
        assert [row.effect_kind for row in effects] == ["refresh_intro"]


@pytest.mark.asyncio
async def test_member_without_intro_and_legacy_null_base_are_distinct_valid_promotions(
    app_env, effect_test_db, monkeypatch
) -> None:
    worker, sessions = _worker_with_test_database(monkeypatch, effect_test_db)
    member_id, legacy_owner = effect_test_db.user_id(), effect_test_db.user_id()
    async with sessions.begin() as session:
        member = await _confirmed_application(session, user_id=member_id, flow_kind="refresh")
        member_effect = await _effect(session, member.id, "member_intro")
        await _confirmed_application(session, user_id=legacy_owner, flow_kind="admission")
        legacy = await _current_intro(session, legacy_owner, None)
        refresh = await _confirmed_application(session, user_id=legacy_owner, flow_kind="refresh")
        refresh_effect = await _effect(session, refresh.id, "refresh_intro")
        legacy_id, member_effect_id, refresh_effect_id, refresh_id = (
            legacy.id,
            member_effect.id,
            refresh_effect.id,
            refresh.id,
        )
    bot = RecordingBot([SimpleNamespace(message_id=444), SimpleNamespace(message_id=445)])

    await worker.process_intro_effects(bot)
    async with sessions() as observer:
        legacy = await observer.get(type(legacy), legacy_id)
        member_effect = await observer.get(type(member_effect), member_effect_id)
        refresh_effect = await observer.get(type(refresh_effect), refresh_effect_id)
        assert member_effect.status == "sent"
        assert legacy.application_id == refresh_id
        assert refresh_effect.status == "sent"
    assert "Обновлённое интро" not in bot.calls[0]["text"]
    assert "Обновлённое интро" in bot.calls[1]["text"]


@pytest.mark.asyncio
async def test_sheet_projection_failure_does_not_undo_promoted_intro(
    app_env, effect_test_db, monkeypatch
) -> None:
    from bot.db.models import IntroEffectOutbox
    from bot.services.sheets import SheetProjectionError

    worker, sessions = _worker_with_test_database(monkeypatch, effect_test_db)
    user_id = effect_test_db.user_id()
    async with sessions.begin() as session:
        base = await _confirmed_application(session, user_id=user_id, flow_kind="admission")
        base.status = "added"
        current = await _current_intro(session, user_id, base.id)
        refresh = await _confirmed_application(
            session, user_id=user_id, flow_kind="refresh", base_id=base.id
        )
        await _effect(session, refresh.id, "refresh_intro")
        current_id, refresh_id = current.id, refresh.id

    await worker.process_intro_effects(
        RecordingBot([SimpleNamespace(message_id=555)]), max_effects=1
    )
    project_sheet = AsyncMock(side_effect=SheetProjectionError("sheet down"))
    await worker.process_intro_effects(RecordingBot(), max_effects=1, project_sheet=project_sheet)
    async with sessions() as observer:
        current = await observer.get(type(current), current_id)
        projection = (
            await observer.execute(
                select(IntroEffectOutbox).where(
                    IntroEffectOutbox.application_id == refresh_id,
                    IntroEffectOutbox.effect_kind == "sheet_projection",
                )
            )
        ).scalar_one()
        assert current.application_id == refresh_id
        assert (projection.status, projection.attempt_count) == ("pending", 1)
    project_sheet.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_sheet_effect_with_existing_old_pointer_never_calls_projector(
    app_env, effect_test_db, monkeypatch
) -> None:
    worker, sessions = _worker_with_test_database(monkeypatch, effect_test_db)
    user_id = effect_test_db.user_id()
    async with sessions.begin() as session:
        application = await _confirmed_application(session, user_id=user_id, flow_kind="admission")
        await _current_intro(session, user_id, application.id)
        effect = await _effect(session, application.id, "sheet_projection")
        effect_id = effect.id
    project_sheet = AsyncMock()

    await worker.process_intro_effects(RecordingBot(), project_sheet=project_sheet)
    async with sessions() as observer:
        assert (await observer.get(type(effect), effect_id)).status == "stale"
    project_sheet.assert_not_awaited()


@pytest.mark.asyncio
async def test_unexpected_sheet_projector_error_reraises_and_leaves_claim_processing(
    app_env, effect_test_db, monkeypatch
) -> None:
    worker, sessions = _worker_with_test_database(monkeypatch, effect_test_db)
    user_id = effect_test_db.user_id()
    async with sessions.begin() as session:
        application = await _confirmed_application(session, user_id=user_id, flow_kind="admission")
        application.status = "added"
        await _current_intro(session, user_id, application.id)
        effect = await _effect(session, application.id, "sheet_projection")
        effect_id = effect.id
    project_sheet = AsyncMock(side_effect=RuntimeError("unexpected sheet failure"))

    with pytest.raises(RuntimeError, match="unexpected sheet failure"):
        await worker.process_intro_effects(RecordingBot(), project_sheet=project_sheet)
    async with sessions() as observer:
        assert (await observer.get(type(effect), effect_id)).status == "processing"


@pytest.mark.asyncio
async def test_sheet_cancellation_reraises_and_returns_effect_to_pending(
    app_env, effect_test_db, monkeypatch
) -> None:
    worker, sessions = _worker_with_test_database(monkeypatch, effect_test_db)
    user_id = effect_test_db.user_id()
    async with sessions.begin() as session:
        application = await _confirmed_application(session, user_id=user_id, flow_kind="admission")
        application.status = "added"
        await _current_intro(session, user_id, application.id)
        effect = await _effect(session, application.id, "sheet_projection")
        effect_id = effect.id
    project_sheet = AsyncMock(side_effect=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await worker.process_intro_effects(RecordingBot(), project_sheet=project_sheet)
    async with sessions() as observer:
        assert (await observer.get(type(effect), effect_id)).status == "pending"
