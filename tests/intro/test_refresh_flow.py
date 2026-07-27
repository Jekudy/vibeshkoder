from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.intro.test_contract import PREDKO_ANSWERS
from tests.intro.test_effect_worker import (
    EffectTestDatabase,
    RecordingBot,
    _confirmed_application,
    _effect,
    _worker_with_test_database,
)
from tests.intro.test_failure_safety import _current_intro


@pytest_asyncio.fixture
async def refresh_test_db(postgres_engine):
    sessions = async_sessionmaker(bind=postgres_engine, class_=AsyncSession, expire_on_commit=False)
    database = EffectTestDatabase(sessions)
    await database.cleanup()
    try:
        yield database
    finally:
        await database.cleanup()


@pytest.mark.asyncio
async def test_refresh_promotes_s2_and_completes_tracking_only_after_telegram_success(
    app_env, refresh_test_db, monkeypatch
) -> None:
    from bot.db.models import Application, Intro, IntroEffectOutbox, IntroRefreshTracking

    worker, sessions = _worker_with_test_database(monkeypatch, refresh_test_db)
    user_id = refresh_test_db.user_id()
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
        RecordingBot([SimpleNamespace(message_id=901)]), max_effects=1
    )
    async with sessions() as observer:
        current = await observer.get(Intro, current_id)
        tracking = await observer.get(IntroRefreshTracking, tracking_id)
        effect = await observer.get(IntroEffectOutbox, effect_id)
        refresh = await observer.get(Application, refresh_id)
        rows = list(
            (
                await observer.execute(
                    select(IntroEffectOutbox).where(IntroEffectOutbox.application_id == refresh_id)
                )
            ).scalars()
        )
        assert current is not None
        assert tracking is not None
        assert effect is not None
        assert refresh is not None
        assert (current.application_id, current.intro_text) == (refresh_id, snapshot)
        assert (refresh.status, effect.status, tracking.completed) == ("added", "sent", True)
        assert {(row.effect_kind, row.status) for row in rows} == {
            ("refresh_intro", "sent"),
            ("sheet_projection", "pending"),
        }


@pytest.mark.asyncio
async def test_failed_refresh_keeps_s1_and_tracking_open(
    app_env, refresh_test_db, monkeypatch
) -> None:
    from bot.db.models import Intro, IntroEffectOutbox, IntroRefreshTracking

    worker, sessions = _worker_with_test_database(monkeypatch, refresh_test_db)
    user_id = refresh_test_db.user_id()
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
        current_id, tracking_id, effect_id, base_id = (
            current.id,
            tracking.id,
            effect.id,
            base.id,
        )

    from aiogram.exceptions import TelegramNetworkError

    await worker.process_intro_effects(
        RecordingBot([TelegramNetworkError(None, "response lost")]), max_effects=1
    )
    async with sessions() as observer:
        current = await observer.get(Intro, current_id)
        tracking = await observer.get(IntroRefreshTracking, tracking_id)
        effect = await observer.get(IntroEffectOutbox, effect_id)
        assert current is not None
        assert tracking is not None
        assert effect is not None
        assert (current.application_id, effect.status, tracking.completed) == (
            base_id,
            "unknown",
            False,
        )


@pytest.mark.asyncio
async def test_stale_refresh_preserves_current_s1_and_tracking(
    app_env, refresh_test_db, monkeypatch
) -> None:
    from bot.db.models import Intro, IntroEffectOutbox, IntroRefreshTracking

    worker, sessions = _worker_with_test_database(monkeypatch, refresh_test_db)
    user_id = refresh_test_db.user_id()
    async with sessions.begin() as session:
        base = await _confirmed_application(session, user_id=user_id, flow_kind="admission")
        base.status = "added"
        current_s1 = await _confirmed_application(session, user_id=user_id, flow_kind="admission")
        current_s1.status = "added"
        current = await _current_intro(session, user_id, current_s1.id)
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
        current_id, tracking_id, effect_id, current_s1_id = (
            current.id,
            tracking.id,
            effect.id,
            current_s1.id,
        )

    await worker.process_intro_effects(
        RecordingBot([SimpleNamespace(message_id=903)]), max_effects=1
    )
    async with sessions() as observer:
        current = await observer.get(Intro, current_id)
        tracking = await observer.get(IntroRefreshTracking, tracking_id)
        effect = await observer.get(IntroEffectOutbox, effect_id)
        assert current is not None
        assert tracking is not None
        assert effect is not None
        assert (current.application_id, effect.status, tracking.completed) == (
            current_s1_id,
            "stale",
            False,
        )


@pytest.mark.asyncio
async def test_member_without_intro_confirms_one_member_effect_and_publishes_intro(
    app_env, refresh_test_db, monkeypatch
) -> None:
    from bot.db.models import Application, Intro, IntroEffectOutbox, User
    from bot.db.repos.application import ApplicationRepo
    from bot.db.repos.questionnaire import QuestionnaireRepo
    from bot.services.intro_contract import get_intro_catalog, intro_digest, render_intro_html
    from bot.services.intro_workflow import confirm_application

    worker, sessions = _worker_with_test_database(monkeypatch, refresh_test_db)
    user_id = refresh_test_db.user_id()
    async with sessions.begin() as session:
        session.add(
            User(
                id=user_id,
                username="member_without_intro",
                first_name="Member",
                last_name=None,
                is_member=True,
            )
        )
        application = await ApplicationRepo.create(
            session,
            user_id=user_id,
            flow_kind="refresh",
            base_application_id=None,
            catalog_version="intro-v2",
        )
        catalog = get_intro_catalog("intro-v2")
        for index, (field_id, answer_text) in enumerate(PREDKO_ANSWERS):
            await QuestionnaireRepo.save_answer(
                session,
                user_id=user_id,
                application_id=application.id,
                field_id=field_id,
                question_index=index,
                question_text=catalog[index].question,
                answer_text=answer_text,
            )
        snapshot = render_intro_html(PREDKO_ANSWERS, catalog_version="intro-v2")
        await confirm_application(
            session,
            user_id=user_id,
            application_id=application.id,
            digest=intro_digest(snapshot),
        )
        await confirm_application(
            session,
            user_id=user_id,
            application_id=application.id,
            digest=intro_digest(snapshot),
        )
        application_id = application.id

    async with sessions() as observer:
        rows = list(
            (
                await observer.execute(
                    select(IntroEffectOutbox).where(
                        IntroEffectOutbox.application_id == application_id
                    )
                )
            ).scalars()
        )
        assert [(row.effect_kind, row.status) for row in rows] == [("member_intro", "pending")]

    bot = RecordingBot([SimpleNamespace(message_id=902)])
    await worker.process_intro_effects(bot, max_effects=1)
    async with sessions() as observer:
        application = await observer.get(Application, application_id)
        intro = await observer.scalar(select(Intro).where(Intro.user_id == user_id))
        assert application is not None
        assert intro is not None
        assert (application.status, intro.application_id, intro.intro_text) == (
            "added",
            application_id,
            snapshot,
        )
    assert "Обновлённое интро" not in bot.calls[0]["text"]
