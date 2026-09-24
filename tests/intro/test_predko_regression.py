from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.intro.test_contract import PREDKO_ANSWERS, PREDKO_BODY
from tests.intro.test_effect_worker import (
    CHAT_ID,
    EffectTestDatabase,
    RecordingBot,
    _confirmed_application,
    _effect,
    _worker_with_test_database,
)
from tests.intro.test_failure_safety import _current_intro


USER_ID = 169_419_687
LEGACY_APPLICATION_ID = 176


@pytest_asyncio.fixture
async def predko_test_db(postgres_engine):
    sessions = async_sessionmaker(bind=postgres_engine, class_=AsyncSession, expire_on_commit=False)
    database = EffectTestDatabase(sessions)
    await database.cleanup()
    try:
        yield database
    finally:
        await database.cleanup()


async def _add_answers(session: AsyncSession, *, user_id: int, application_id: int) -> None:
    from bot.db.models import QuestionnaireAnswer
    from bot.services.intro_contract import get_intro_catalog

    catalog = get_intro_catalog("intro-v2")
    for index, ((field, answer), definition) in enumerate(
        zip(PREDKO_ANSWERS, catalog, strict=True)
    ):
        session.add(
            QuestionnaireAnswer(
                user_id=user_id,
                application_id=application_id,
                field_id=field,
                question_index=index,
                question_text=definition.question,
                answer_text=answer,
            )
        )
    await session.flush()


async def _seed_poisoned_application_176(session: AsyncSession, *, user_id: int):
    from bot.db.models import Application, User

    session.add(
        User(
            id=user_id,
            username="poisoned176",
            first_name="Poisoned",
            last_name=None,
            is_member=True,
        )
    )
    application = Application(
        id=LEGACY_APPLICATION_ID,
        user_id=user_id,
        status="added",
        flow_kind="admission",
        catalog_version="intro-v2",
        confirmed_intro_html="poisoned application 176 snapshot",
    )
    session.add(application)
    await session.flush()
    await _add_answers(session, user_id=user_id, application_id=application.id)
    intro = await _current_intro(session, user_id, application.id)
    intro.intro_text = "poisoned application 176 intro"
    await session.flush()
    return application, intro


async def _predko_case(session: AsyncSession, *, poison_user_id: int):
    poisoned_application, poisoned_intro = await _seed_poisoned_application_176(
        session,
        user_id=poison_user_id,
    )
    old = await _confirmed_application(session, user_id=USER_ID, flow_kind="admission")
    old.status = "added"
    old.confirmed_intro_html = "old London / generic referral snapshot"
    await _add_answers(session, user_id=USER_ID, application_id=old.id)
    current = await _current_intro(session, USER_ID, old.id)
    fresh = await _confirmed_application(
        session,
        user_id=USER_ID,
        flow_kind="refresh",
        base_id=old.id,
    )
    fresh.confirmed_intro_html = PREDKO_BODY
    await _add_answers(session, user_id=USER_ID, application_id=fresh.id)
    effect = await _effect(session, fresh.id, "refresh_intro")
    return poisoned_application, poisoned_intro, old, current, fresh, effect


def _scalar_state(instance) -> tuple[tuple[str, object], ...]:
    return tuple(
        (column.key, getattr(instance, column.key)) for column in inspect(instance).mapper.columns
    )


async def _poisoned_state(session: AsyncSession, *, user_id: int):
    from bot.db.models import Application, Intro, QuestionnaireAnswer

    application = await session.get(Application, LEGACY_APPLICATION_ID)
    intro = await session.scalar(select(Intro).where(Intro.user_id == user_id))
    answers = list(
        (
            await session.execute(
                select(QuestionnaireAnswer)
                .where(QuestionnaireAnswer.application_id == LEGACY_APPLICATION_ID)
                .order_by(QuestionnaireAnswer.question_index)
            )
        ).scalars()
    )
    assert application is not None
    assert intro is not None
    return (
        _scalar_state(application),
        tuple(_scalar_state(answer) for answer in answers),
        _scalar_state(intro),
    )


@pytest.mark.asyncio
async def test_predko_timeout_keeps_old_pointer_and_leaves_poisoned_application_176_unchanged(
    app_env, predko_test_db, monkeypatch
) -> None:
    predko_test_db.user_ids.append(USER_ID)
    worker, sessions = _worker_with_test_database(monkeypatch, predko_test_db)
    poison_user_id = predko_test_db.user_id()
    async with sessions.begin() as session:
        poisoned, poisoned_intro, old, current, fresh, effect = await _predko_case(
            session,
            poison_user_id=poison_user_id,
        )
        poisoned_id = poisoned.id
        old_id, current_id, fresh_id, effect_id = old.id, current.id, fresh.id, effect.id
    async with sessions() as observer:
        poisoned_before = await _poisoned_state(observer, user_id=poison_user_id)

    from aiogram.exceptions import TelegramNetworkError

    await worker.process_intro_effects(
        RecordingBot([TelegramNetworkError(None, "telegram response lost")])
    )

    async with sessions() as observer:
        from bot.db.models import Application, Intro, IntroEffectOutbox

        current = await observer.get(Intro, current_id)
        fresh = await observer.get(Application, fresh_id)
        effect = await observer.get(IntroEffectOutbox, effect_id)
        poisoned_after = await _poisoned_state(observer, user_id=poison_user_id)

        assert poisoned_id == LEGACY_APPLICATION_ID
        assert current.application_id == old_id
        assert fresh.confirmed_intro_html == PREDKO_BODY
        assert (effect.status, effect.chat_id, effect.message_id) == ("unknown", None, None)
        assert poisoned_after == poisoned_before


@pytest.mark.asyncio
async def test_predko_success_publishes_exact_user_block_and_leaves_application_176_unchanged(
    app_env, predko_test_db, monkeypatch
) -> None:
    from bot.db.models import IntroRefreshTracking
    from bot.services.intro_contract import render_intro_html

    predko_test_db.user_ids.append(USER_ID)
    worker, sessions = _worker_with_test_database(monkeypatch, predko_test_db)
    poison_user_id = predko_test_db.user_id()
    async with sessions.begin() as session:
        _, _, _, current, fresh, effect = await _predko_case(session, poison_user_id=poison_user_id)
        tracking = IntroRefreshTracking(
            user_id=USER_ID,
            cycle_started_at=fresh.created_at,
            reminders_sent=1,
            phase="daily",
            completed=False,
        )
        session.add(tracking)
        current_id, fresh_id, effect_id = current.id, fresh.id, effect.id
    async with sessions() as observer:
        poisoned_before = await _poisoned_state(observer, user_id=poison_user_id)
    bot = RecordingBot([SimpleNamespace(message_id=48_500)])

    assert render_intro_html(PREDKO_ANSWERS, catalog_version="intro-v2") == PREDKO_BODY
    await worker.process_intro_effects(bot)

    async with sessions() as observer:
        from bot.db.models import Application, Intro, IntroEffectOutbox

        current = await observer.get(Intro, current_id)
        fresh = await observer.get(Application, fresh_id)
        effect = await observer.get(IntroEffectOutbox, effect_id)
        tracking = await observer.get(IntroRefreshTracking, tracking.id)
        projection = await observer.scalar(
            select(IntroEffectOutbox).where(
                IntroEffectOutbox.application_id == fresh_id,
                IntroEffectOutbox.effect_kind == "sheet_projection",
            )
        )
        poisoned_after = await _poisoned_state(observer, user_id=poison_user_id)

        assert current.application_id == fresh_id
        assert current.intro_text == PREDKO_BODY
        assert fresh.confirmed_intro_html == PREDKO_BODY
        assert (effect.status, effect.chat_id, effect.message_id) == ("sent", CHAT_ID, 48_500)
        assert tracking.completed is True
        assert projection is not None and projection.status == "pending"
        assert poisoned_after == poisoned_before

    header, user_block = (
        bot.calls[0]["text"][: -len(PREDKO_BODY)],
        bot.calls[0]["text"][-len(PREDKO_BODY) :],
    )
    assert "Обновлённое интро" in header
    assert user_block == PREDKO_BODY
