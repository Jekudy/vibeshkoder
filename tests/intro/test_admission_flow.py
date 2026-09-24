from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.intro.test_contract import PREDKO_ANSWERS, PREDKO_BODY
from tests.intro.test_effect_worker import (
    EffectTestDatabase,
    RecordingBot,
    _confirmed_application,
    _effect,
    _worker_with_test_database,
)


class AdmissionEffectTestDatabase(EffectTestDatabase):
    async def cleanup(self) -> None:
        if not self.user_ids:
            return
        from bot.db.models import (
            Application,
            Intro,
            IntroEffectOutbox,
            IntroRefreshTracking,
            InviteOutbox,
            QuestionnaireAnswer,
            User,
            VouchLog,
        )

        async with self.sessions.begin() as session:
            application_ids = select(Application.id).where(Application.user_id.in_(self.user_ids))
            await session.execute(
                delete(IntroEffectOutbox).where(
                    IntroEffectOutbox.application_id.in_(application_ids)
                )
            )
            await session.execute(
                delete(InviteOutbox).where(InviteOutbox.application_id.in_(application_ids))
            )
            await session.execute(
                delete(VouchLog).where(
                    VouchLog.application_id.in_(application_ids)
                    | VouchLog.voucher_id.in_(self.user_ids)
                    | VouchLog.vouchee_id.in_(self.user_ids)
                )
            )
            await session.execute(
                delete(IntroRefreshTracking).where(IntroRefreshTracking.user_id.in_(self.user_ids))
            )
            await session.execute(delete(Intro).where(Intro.user_id.in_(self.user_ids)))
            await session.execute(
                delete(QuestionnaireAnswer).where(
                    QuestionnaireAnswer.application_id.in_(application_ids)
                )
            )
            await session.execute(delete(Application).where(Application.user_id.in_(self.user_ids)))
            await session.execute(delete(User).where(User.id.in_(self.user_ids)))


@pytest_asyncio.fixture
async def admission_test_db(postgres_engine):
    sessions = async_sessionmaker(bind=postgres_engine, class_=AsyncSession, expire_on_commit=False)
    database = AdmissionEffectTestDatabase(sessions)
    await database.cleanup()
    try:
        yield database
    finally:
        await database.cleanup()


async def _add_answers(session: AsyncSession, *, application_id: int, user_id: int) -> None:
    from bot.db.models import QuestionnaireAnswer
    from bot.services.intro_contract import get_intro_catalog

    for index, (field, answer) in enumerate(PREDKO_ANSWERS):
        session.add(
            QuestionnaireAnswer(
                user_id=user_id,
                application_id=application_id,
                field_id=field,
                question_index=index,
                question_text=get_intro_catalog("intro-v2")[index].question,
                answer_text=answer,
            )
        )
    await session.flush()


@pytest.mark.asyncio
async def test_frozen_admission_flows_from_candidate_through_vouch_and_join_once(
    app_env, admission_test_db, monkeypatch
) -> None:
    from bot.db.models import Application, Intro, IntroEffectOutbox, InviteOutbox, User, VouchLog
    from bot.handlers.chat_events import _handle_join
    from bot.handlers.vouch import handle_vouch
    from bot.keyboards.inline import VouchCallback

    worker, sessions = _worker_with_test_database(monkeypatch, admission_test_db)
    applicant_id, voucher_id = admission_test_db.user_id(), admission_test_db.user_id()
    async with sessions.begin() as session:
        application = await _confirmed_application(
            session, user_id=applicant_id, flow_kind="admission"
        )
        application.confirmed_intro_html = PREDKO_BODY
        applicant = await session.get(User, applicant_id)
        assert applicant is not None
        applicant.is_member = False
        session.add(
            User(
                id=voucher_id,
                username="voucher",
                first_name="Voucher",
                last_name=None,
                is_member=True,
            )
        )
        await _add_answers(session, application_id=application.id, user_id=applicant_id)
        candidate = await _effect(session, application.id, "candidate_card")
        application_id, candidate_id = application.id, candidate.id

    candidate_bot = RecordingBot([SimpleNamespace(message_id=801)])
    await worker.process_intro_effects(candidate_bot, max_effects=1)
    async with sessions() as observer:
        application = await observer.get(Application, application_id)
        candidate = await observer.get(IntroEffectOutbox, candidate_id)
        assert application is not None
        assert candidate is not None
        assert (application.status, candidate.status) == ("pending", "sent")
    assert PREDKO_BODY in candidate_bot.calls[0]["text"]

    vouch_bot = SimpleNamespace(send_message=AsyncMock())
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=voucher_id),
        message=None,
        bot=vouch_bot,
        answer=AsyncMock(),
    )
    async with sessions.begin() as session:
        await handle_vouch(callback, VouchCallback(application_id=application_id), session)
        await handle_vouch(callback, VouchCallback(application_id=application_id), session)
    async with sessions() as observer:
        vouches = list(
            (
                await observer.execute(
                    select(VouchLog).where(VouchLog.application_id == application_id)
                )
            ).scalars()
        )
        invites = list(
            (
                await observer.execute(
                    select(InviteOutbox).where(InviteOutbox.application_id == application_id)
                )
            ).scalars()
        )
        assert len(vouches) == 1
        assert len(invites) == 1
    assert vouch_bot.send_message.await_count == 1

    join_bot = SimpleNamespace(
        ban_chat_member=AsyncMock(),
        unban_chat_member=AsyncMock(),
        send_message=AsyncMock(),
    )
    event = SimpleNamespace(chat=SimpleNamespace(id=-100_123_456_7890), bot=join_bot)
    joined_user = SimpleNamespace(
        id=applicant_id,
        username="effect_user",
        first_name="Effect",
        last_name=None,
        is_bot=False,
    )
    async with sessions.begin() as session:
        await _handle_join(event, session, joined_user)
        await _handle_join(event, session, joined_user)
    async with sessions() as observer:
        rows = list(
            (
                await observer.execute(
                    select(IntroEffectOutbox)
                    .where(IntroEffectOutbox.application_id == application_id)
                    .order_by(IntroEffectOutbox.id)
                )
            ).scalars()
        )
        assert [(row.effect_kind, row.status) for row in rows] == [
            ("candidate_card", "sent"),
            ("admission_intro", "pending"),
        ]
        assert await observer.scalar(select(Intro).where(Intro.user_id == applicant_id)) is None
    join_bot.ban_chat_member.assert_not_awaited()
    join_bot.unban_chat_member.assert_not_awaited()
    join_bot.send_message.assert_not_awaited()

    intro_bot = RecordingBot([SimpleNamespace(message_id=802)])
    await worker.process_intro_effects(intro_bot, max_effects=1)
    async with sessions() as observer:
        effect = await observer.scalar(
            select(IntroEffectOutbox).where(
                IntroEffectOutbox.application_id == application_id,
                IntroEffectOutbox.effect_kind == "admission_intro",
            )
        )
        intro = await observer.scalar(select(Intro).where(Intro.user_id == applicant_id))
        assert effect is not None
        assert intro is not None
        assert effect.status == "sent"
        assert (intro.application_id, intro.intro_text) == (application_id, PREDKO_BODY)
    assert PREDKO_BODY in intro_bot.calls[0]["text"]


@pytest.mark.asyncio
async def test_historical_added_intro_cannot_admit_a_non_member_without_new_vouch(
    app_env, admission_test_db
) -> None:
    from bot.db.models import Intro, IntroEffectOutbox, User
    from bot.handlers.chat_events import _handle_join

    sessions = admission_test_db.sessions
    user_id = admission_test_db.user_id()
    async with sessions.begin() as session:
        application = await _confirmed_application(session, user_id=user_id, flow_kind="admission")
        application.status = "added"
        application.invite_user_id = user_id
        user = await session.get(User, user_id)
        assert user is not None
        user.is_member = False
        session.add(
            Intro(
                user_id=user_id,
                application_id=application.id,
                intro_text=PREDKO_BODY,
                vouched_by_name="@voucher",
            )
        )
        application_id = application.id

    join_bot = SimpleNamespace(
        ban_chat_member=AsyncMock(),
        unban_chat_member=AsyncMock(),
        send_message=AsyncMock(),
    )
    event = SimpleNamespace(chat=SimpleNamespace(id=-100_123_456_7890), bot=join_bot)
    joined_user = SimpleNamespace(
        id=user_id,
        username="effect_user",
        first_name="Effect",
        last_name=None,
        is_bot=False,
    )
    async with sessions.begin() as session:
        await _handle_join(event, session, joined_user)

    async with sessions() as observer:
        user = await observer.get(User, user_id)
        effects = list(
            (
                await observer.execute(
                    select(IntroEffectOutbox).where(
                        IntroEffectOutbox.application_id == application_id
                    )
                )
            ).scalars()
        )
        assert user is not None
        assert user.is_member is False
        assert effects == []
    join_bot.ban_chat_member.assert_awaited_once_with(-100_123_456_7890, user_id)
    join_bot.unban_chat_member.assert_awaited_once_with(-100_123_456_7890, user_id)
    assert any(
        call.kwargs["chat_id"] == user_id and "/start" in call.kwargs["text"]
        for call in join_bot.send_message.await_args_list
    )


@pytest.mark.asyncio
async def test_join_cas_loser_rereads_concurrent_winner_instead_of_kicking(
    app_env, admission_test_db, monkeypatch
) -> None:
    from bot.db.models import Application, User
    from bot.db.repos.application import ApplicationRepo
    from bot.handlers.chat_events import _handle_join

    sessions = admission_test_db.sessions
    user_id = admission_test_db.user_id()
    async with sessions.begin() as session:
        application = await _confirmed_application(session, user_id=user_id, flow_kind="admission")
        application.status = "vouched"
        application.invite_user_id = user_id
        user = await session.get(User, user_id)
        assert user is not None
        user.is_member = False
        application_id = application.id

    original_update_status_if = ApplicationRepo.update_status_if

    async def concurrent_winner_then_loser(session, app_id, expected_from, new_status, **fields):
        async with sessions.begin() as winner:
            assert await original_update_status_if(
                winner,
                app_id,
                expected_from=expected_from,
                new_status=new_status,
                **fields,
            )
        return False

    monkeypatch.setattr(ApplicationRepo, "update_status_if", concurrent_winner_then_loser)
    join_bot = SimpleNamespace(
        ban_chat_member=AsyncMock(),
        unban_chat_member=AsyncMock(),
        send_message=AsyncMock(),
    )
    event = SimpleNamespace(chat=SimpleNamespace(id=-100_123_456_7890), bot=join_bot)
    joined_user = SimpleNamespace(
        id=user_id,
        username="effect_user",
        first_name="Effect",
        last_name=None,
        is_bot=False,
    )

    async with sessions.begin() as session:
        await _handle_join(event, session, joined_user)

    async with sessions() as observer:
        application = await observer.get(Application, application_id)
        user = await observer.get(User, user_id)
        assert application is not None
        assert user is not None
        assert application.status == "added"
        assert user.is_member is True
    join_bot.ban_chat_member.assert_not_awaited()
    join_bot.unban_chat_member.assert_not_awaited()
