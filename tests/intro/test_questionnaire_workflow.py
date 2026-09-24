from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import import_module
from tests.intro.test_contract import PREDKO_ANSWERS


USER_ID = 710_001
OTHER_USER_ID = 710_002


async def _seed_user(session: AsyncSession, user_id: int, *, is_member: bool = False) -> None:
    from bot.db.repos.user import UserRepo

    await UserRepo.upsert(
        session,
        telegram_id=user_id,
        username=f"user{user_id}",
        first_name="Test",
        last_name=None,
    )
    await UserRepo.set_member(session, user_id, is_member)


async def _new_application(
    session: AsyncSession,
    user_id: int,
    *,
    flow_kind: str,
    base_application_id: int | None = None,
):
    from bot.db.repos.application import ApplicationRepo

    return await ApplicationRepo.create(
        session,
        user_id=user_id,
        flow_kind=flow_kind,
        base_application_id=base_application_id,
        catalog_version="intro-v2",
    )


async def _write_answers(
    session: AsyncSession,
    application_id: int,
    user_id: int,
    answers=PREDKO_ANSWERS,
) -> None:
    from bot.services.intro_workflow import write_answer

    for field_id, answer_text in answers:
        await write_answer(
            session,
            user_id=user_id,
            application_id=application_id,
            field_id=field_id,
            answer_text=answer_text,
        )


async def _complete_draft(
    session: AsyncSession,
    application_id: int,
    user_id: int,
    answers=PREDKO_ANSWERS,
) -> None:
    from bot.db.repos.questionnaire import QuestionnaireRepo

    answered = {
        answer.field_id
        for answer in await QuestionnaireRepo.get_by_application(
            session, application_id=application_id
        )
        if answer.is_current
    }
    await _write_answers(
        session,
        application_id,
        user_id,
        [(field_id, answer_text) for field_id, answer_text in answers if field_id not in answered],
    )


async def _snapshot_body(session: AsyncSession, application_id: int) -> str:
    from bot.db.repos.questionnaire import QuestionnaireRepo
    from bot.services.intro_contract import render_intro_html

    answers = await QuestionnaireRepo.get_by_application(session, application_id=application_id)
    return render_intro_html(
        [(answer.field_id, answer.answer_text) for answer in answers if answer.is_current],
        catalog_version="intro-v2",
    )


async def _snapshot_digest(session: AsyncSession, application_id: int) -> str:
    from bot.services.intro_contract import intro_digest

    return intro_digest(await _snapshot_body(session, application_id))


async def _outbox_rows(session: AsyncSession, application_id: int):
    from bot.db.models import IntroEffectOutbox

    result = await session.execute(
        select(IntroEffectOutbox).where(IntroEffectOutbox.application_id == application_id)
    )
    return list(result.scalars())


async def _make_completed_application(
    session: AsyncSession,
    *,
    user_id: int,
    referral: str,
):
    from bot.db.repos.questionnaire import QuestionnaireRepo
    from bot.services.intro_contract import get_intro_catalog

    application = await _new_application(session, user_id, flow_kind="admission")
    for index, field in enumerate(get_intro_catalog("intro-v2")):
        answer_text = (
            referral if field.field_id == "referral" else dict(PREDKO_ANSWERS)[field.field_id]
        )
        await QuestionnaireRepo.save_answer(
            session,
            user_id=user_id,
            application_id=application.id,
            field_id=field.field_id,
            question_index=index,
            question_text=field.question,
            answer_text=answer_text,
        )
    application.confirmed_intro_html = await _snapshot_body(session, application.id)
    application.status = "added"
    await session.flush()
    return application


async def _cleanup_concurrent_user(postgres_engine, user_id: int) -> None:
    from bot.db.models import Application, Intro, IntroEffectOutbox, QuestionnaireAnswer, User

    Session = async_sessionmaker(bind=postgres_engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session, session.begin():
        application_ids = select(Application.id).where(Application.user_id == user_id)
        await session.execute(
            delete(IntroEffectOutbox).where(IntroEffectOutbox.application_id.in_(application_ids))
        )
        await session.execute(
            delete(QuestionnaireAnswer).where(QuestionnaireAnswer.user_id == user_id)
        )
        await session.execute(delete(Intro).where(Intro.user_id == user_id))
        await session.execute(delete(Application).where(Application.user_id == user_id))
        await session.execute(delete(User).where(User.id == user_id))


@pytest.mark.asyncio
async def test_confirm_freezes_exact_snapshot_and_enqueues_one_pending_candidate_effect(
    app_env, db_session
) -> None:
    from bot.services.intro_workflow import confirm_application

    await _seed_user(db_session, USER_ID)
    application = await _new_application(db_session, USER_ID, flow_kind="admission")
    await _write_answers(db_session, application.id, USER_ID)
    expected_body = await _snapshot_body(db_session, application.id)

    await confirm_application(
        db_session,
        user_id=USER_ID,
        application_id=application.id,
        digest=await _snapshot_digest(db_session, application.id),
    )
    await db_session.refresh(application)
    effects = await _outbox_rows(db_session, application.id)

    assert application.status == "confirmed"
    assert application.confirmed_intro_html == expected_body
    assert [(effect.effect_kind, effect.status) for effect in effects] == [
        ("candidate_card", "pending")
    ]


@pytest.mark.asyncio
async def test_confirm_ignores_quarantined_historical_answers(app_env, db_session) -> None:
    from bot.db.models import QuestionnaireAnswer
    from bot.db.repos.questionnaire import QuestionnaireRepo
    from bot.services.intro_workflow import confirm_application

    await _seed_user(db_session, USER_ID)
    application = await _new_application(db_session, USER_ID, flow_kind="admission")
    db_session.add(
        QuestionnaireAnswer(
            user_id=USER_ID,
            application_id=application.id,
            question_index=0,
            question_text="Historical question",
            answer_text="Historical answer",
            field_id=None,
            is_current=False,
        )
    )
    await _write_answers(db_session, application.id, USER_ID)
    assert [
        (answer.field_id, answer.answer_text)
        for answer in await QuestionnaireRepo.get_by_application(
            db_session, application_id=application.id
        )
    ] == list(PREDKO_ANSWERS)
    expected_body = await _snapshot_body(db_session, application.id)

    await confirm_application(
        db_session,
        user_id=USER_ID,
        application_id=application.id,
        digest=await _snapshot_digest(db_session, application.id),
    )
    await db_session.refresh(application)

    assert application.confirmed_intro_html == expected_body


@pytest.mark.asyncio
async def test_reset_preserves_quarantined_answer_and_deletes_current_answers(
    app_env, db_session
) -> None:
    from bot.db.models import QuestionnaireAnswer
    from bot.db.repos.questionnaire import QuestionnaireRepo
    from bot.services.intro_contract import get_intro_catalog
    from bot.services.intro_workflow import reset_draft

    await _seed_user(db_session, USER_ID)
    application = await _new_application(db_session, USER_ID, flow_kind="admission")
    historical = QuestionnaireAnswer(
        user_id=USER_ID,
        application_id=application.id,
        question_index=0,
        question_text="Historical question",
        answer_text="Historical answer",
        field_id=None,
        is_current=False,
    )
    db_session.add(historical)
    for index, field in enumerate(get_intro_catalog("intro-v2")):
        await QuestionnaireRepo.save_answer(
            db_session,
            user_id=USER_ID,
            application_id=application.id,
            field_id=field.field_id,
            question_index=index,
            question_text=field.question,
            answer_text=(
                "От участника чата"
                if field.field_id == "referral"
                else dict(PREDKO_ANSWERS)[field.field_id]
            ),
        )
    digest = await _snapshot_digest(db_session, application.id)

    await reset_draft(db_session, user_id=USER_ID, application_id=application.id, digest=digest)

    await db_session.refresh(historical)
    assert (historical.question_index, historical.question_text, historical.answer_text) == (
        0,
        "Historical question",
        "Historical answer",
    )
    assert historical.field_id is None
    assert historical.is_current is False
    assert (
        await QuestionnaireRepo.get_by_application(db_session, application_id=application.id) == []
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["wrong-owner", "non-filling", "incomplete", "stale-digest"])
async def test_invalid_confirm_rejects_without_effect(app_env, db_session, case: str) -> None:
    from bot.services.intro_workflow import IntroWorkflowError, confirm_application

    await _seed_user(db_session, USER_ID)
    await _seed_user(db_session, OTHER_USER_ID)
    application = await _new_application(db_session, USER_ID, flow_kind="admission")
    if case != "incomplete":
        await _write_answers(db_session, application.id, USER_ID)
    if case == "non-filling":
        application.confirmed_intro_html = await _snapshot_body(db_session, application.id)
        application.status = "pending"
        await db_session.flush()

    owner_id = OTHER_USER_ID if case == "wrong-owner" else USER_ID
    digest = (
        "stale-digest"
        if case in {"incomplete", "stale-digest"}
        else await _snapshot_digest(db_session, application.id)
    )

    with pytest.raises(IntroWorkflowError):
        await confirm_application(
            db_session,
            user_id=owner_id,
            application_id=application.id,
            digest=digest,
        )

    assert await _outbox_rows(db_session, application.id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "vouched", "added", "rejected", "privacy_block"])
async def test_terminal_application_cannot_confirm_again(app_env, db_session, status: str) -> None:
    from bot.services.intro_workflow import IntroWorkflowError, confirm_application

    await _seed_user(db_session, USER_ID)
    application = await _new_application(db_session, USER_ID, flow_kind="admission")
    await _write_answers(db_session, application.id, USER_ID)
    snapshot = await _snapshot_body(db_session, application.id)
    application.confirmed_intro_html = snapshot
    application.status = status
    await db_session.flush()

    with pytest.raises(IntroWorkflowError):
        await confirm_application(
            db_session,
            user_id=USER_ID,
            application_id=application.id,
            digest=await _snapshot_digest(db_session, application.id),
        )

    assert application.confirmed_intro_html == snapshot
    assert await _outbox_rows(db_session, application.id) == []


@pytest.mark.asyncio
async def test_repeat_same_confirm_is_idempotent_with_one_database_effect(
    app_env, db_session
) -> None:
    from bot.services.intro_workflow import confirm_application

    await _seed_user(db_session, USER_ID)
    application = await _new_application(db_session, USER_ID, flow_kind="admission")
    await _write_answers(db_session, application.id, USER_ID)
    digest = await _snapshot_digest(db_session, application.id)

    await confirm_application(
        db_session, user_id=USER_ID, application_id=application.id, digest=digest
    )
    await confirm_application(
        db_session, user_id=USER_ID, application_id=application.id, digest=digest
    )

    effects = await _outbox_rows(db_session, application.id)
    assert len(effects) == 1
    assert effects[0].effect_kind == "candidate_card"


@pytest.mark.asyncio
async def test_confirmed_application_rejects_wrong_digest_without_duplicate_effect(
    app_env, db_session
) -> None:
    from bot.services.intro_workflow import IntroWorkflowError, confirm_application

    await _seed_user(db_session, USER_ID)
    application = await _new_application(db_session, USER_ID, flow_kind="admission")
    await _write_answers(db_session, application.id, USER_ID)
    digest = await _snapshot_digest(db_session, application.id)
    await confirm_application(
        db_session, user_id=USER_ID, application_id=application.id, digest=digest
    )

    with pytest.raises(IntroWorkflowError):
        await confirm_application(
            db_session,
            user_id=USER_ID,
            application_id=application.id,
            digest="wrong-digest",
        )

    assert [
        (effect.effect_kind, effect.status)
        for effect in await _outbox_rows(db_session, application.id)
    ] == [("candidate_card", "pending")]


@pytest.mark.asyncio
async def test_answer_write_after_confirmation_is_rejected(app_env, db_session) -> None:
    from bot.services.intro_workflow import IntroWorkflowError, confirm_application, write_answer

    await _seed_user(db_session, USER_ID)
    application = await _new_application(db_session, USER_ID, flow_kind="admission")
    await _write_answers(db_session, application.id, USER_ID)
    await confirm_application(
        db_session,
        user_id=USER_ID,
        application_id=application.id,
        digest=await _snapshot_digest(db_session, application.id),
    )

    with pytest.raises(IntroWorkflowError):
        await write_answer(
            db_session,
            user_id=USER_ID,
            application_id=application.id,
            field_id="name",
            answer_text="Другой Сергей",
        )


@pytest.mark.asyncio
async def test_generic_referral_refresh_has_no_prefill_resumes_and_accepts_refinement(
    app_env, db_session
) -> None:
    from bot.db.models import Intro
    from bot.db.repos.questionnaire import QuestionnaireRepo
    from bot.services.intro_workflow import start_or_resume_refresh, write_answer

    await _seed_user(db_session, USER_ID, is_member=True)
    current = await _make_completed_application(
        db_session, user_id=USER_ID, referral="От участника чата"
    )
    db_session.add(
        Intro(
            user_id=USER_ID,
            application_id=current.id,
            intro_text="old current intro",
            vouched_by_name="Vouch",
        )
    )
    await db_session.flush()

    first = await start_or_resume_refresh(db_session, user_id=USER_ID)
    second = await start_or_resume_refresh(db_session, user_id=USER_ID)
    assert first.id == second.id
    assert first.base_application_id == current.id
    assert await QuestionnaireRepo.get_by_application(db_session, application_id=first.id) == []

    await write_answer(
        db_session, user_id=USER_ID, application_id=first.id, field_id="name", answer_text="Сергей"
    )
    await write_answer(
        db_session, user_id=USER_ID, application_id=first.id, field_id="location", answer_text="UK"
    )
    next_field = await write_answer(
        db_session,
        user_id=USER_ID,
        application_id=first.id,
        field_id="referral",
        answer_text="t.me/New_Name",
    )
    answers = await QuestionnaireRepo.get_by_application(db_session, application_id=first.id)

    assert next_field == "experience"
    assert [(answer.field_id, answer.answer_text) for answer in answers] == [
        ("name", "Сергей"),
        ("location", "UK"),
        ("referral", "@new_name"),
    ]


@pytest.mark.asyncio
async def test_new_admission_rejects_generic_referral_without_saving_it(
    app_env, db_session
) -> None:
    from bot.db.repos.questionnaire import QuestionnaireRepo
    from bot.services.intro_workflow import InvalidReferralAnswer, next_field_id, write_answer

    await _seed_user(db_session, USER_ID)
    application = await _new_application(db_session, USER_ID, flow_kind="admission")
    await write_answer(
        db_session,
        user_id=USER_ID,
        application_id=application.id,
        field_id="name",
        answer_text="Сергей",
    )
    await write_answer(
        db_session,
        user_id=USER_ID,
        application_id=application.id,
        field_id="location",
        answer_text="UK",
    )

    with pytest.raises(InvalidReferralAnswer):
        await write_answer(
            db_session,
            user_id=USER_ID,
            application_id=application.id,
            field_id="referral",
            answer_text="От участника чата",
        )

    assert (
        await next_field_id(db_session, user_id=USER_ID, application_id=application.id)
        == "referral"
    )
    assert [
        answer.field_id
        for answer in await QuestionnaireRepo.get_by_application(
            db_session, application_id=application.id
        )
    ] == ["name", "location"]


@pytest.mark.asyncio
async def test_current_intro_pointer_not_latest_application_decides_refresh_base_and_referral(
    app_env, db_session
) -> None:
    from bot.db.models import Intro
    from bot.db.repos.questionnaire import QuestionnaireRepo
    from bot.services.intro_workflow import start_or_resume_refresh

    await _seed_user(db_session, USER_ID, is_member=True)
    older = await _make_completed_application(db_session, user_id=USER_ID, referral="@older_name")
    newer = await _make_completed_application(db_session, user_id=USER_ID, referral="@newer_name")
    db_session.add(
        Intro(
            user_id=USER_ID,
            application_id=older.id,
            intro_text="the pointer is intentionally older",
            vouched_by_name="Vouch",
        )
    )
    await db_session.flush()

    refresh = await start_or_resume_refresh(db_session, user_id=USER_ID)
    answers = await QuestionnaireRepo.get_by_application(db_session, application_id=refresh.id)

    assert newer.id != older.id
    assert refresh.base_application_id == older.id
    assert [(answer.field_id, answer.answer_text) for answer in answers] == [
        ("referral", "@older_name")
    ]


@pytest.mark.asyncio
async def test_concrete_referral_is_copied_normalized_and_skips_q3(app_env, db_session) -> None:
    from bot.db.models import Intro
    from bot.db.repos.questionnaire import QuestionnaireRepo
    from bot.services.intro_workflow import (
        IntroWorkflowError,
        start_or_resume_refresh,
        write_answer,
    )

    await _seed_user(db_session, USER_ID, is_member=True)
    current = await _make_completed_application(
        db_session, user_id=USER_ID, referral="t.me/Owner_Name"
    )
    db_session.add(
        Intro(
            user_id=USER_ID,
            application_id=current.id,
            intro_text="old current intro",
            vouched_by_name="Vouch",
        )
    )
    await db_session.flush()
    refresh = await start_or_resume_refresh(db_session, user_id=USER_ID)

    assert [
        (answer.field_id, answer.answer_text)
        for answer in await QuestionnaireRepo.get_by_application(
            db_session, application_id=refresh.id
        )
    ] == [("referral", "@owner_name")]

    with pytest.raises(IntroWorkflowError):
        await write_answer(
            db_session,
            user_id=USER_ID,
            application_id=refresh.id,
            field_id="referral",
            answer_text="@different",
        )
    assert [
        (answer.field_id, answer.answer_text)
        for answer in await QuestionnaireRepo.get_by_application(
            db_session, application_id=refresh.id
        )
    ] == [("referral", "@owner_name")]

    await write_answer(
        db_session,
        user_id=USER_ID,
        application_id=refresh.id,
        field_id="name",
        answer_text="Сергей",
    )
    next_field = await write_answer(
        db_session,
        user_id=USER_ID,
        application_id=refresh.id,
        field_id="location",
        answer_text="UK",
    )
    assert next_field == "experience"


@pytest.mark.asyncio
async def test_refresh_redo_preserves_concrete_referral(app_env, db_session) -> None:
    from bot.db.models import Intro
    from bot.db.repos.questionnaire import QuestionnaireRepo
    from bot.services.intro_workflow import reset_draft, start_or_resume_refresh

    await _seed_user(db_session, USER_ID, is_member=True)
    current = await _make_completed_application(db_session, user_id=USER_ID, referral="@Owner_Name")
    db_session.add(
        Intro(
            user_id=USER_ID,
            application_id=current.id,
            intro_text="old current intro",
            vouched_by_name="Vouch",
        )
    )
    await db_session.flush()
    refresh = await start_or_resume_refresh(db_session, user_id=USER_ID)
    await _complete_draft(db_session, refresh.id, USER_ID)

    await reset_draft(
        db_session,
        user_id=USER_ID,
        application_id=refresh.id,
        digest=await _snapshot_digest(db_session, refresh.id),
    )

    assert [
        (answer.field_id, answer.answer_text)
        for answer in await QuestionnaireRepo.get_by_application(
            db_session, application_id=refresh.id
        )
    ] == [("referral", "@owner_name")]


@pytest.mark.asyncio
async def test_refresh_redo_rejects_stale_preview_and_preserves_newer_answers(
    app_env, db_session
) -> None:
    from bot.db.repos.questionnaire import QuestionnaireRepo
    from bot.services.intro_workflow import IntroWorkflowError, reset_draft, start_or_resume_refresh

    await _seed_user(db_session, USER_ID, is_member=True)
    refresh = await start_or_resume_refresh(db_session, user_id=USER_ID)
    await _complete_draft(db_session, refresh.id, USER_ID)
    first_digest = await _snapshot_digest(db_session, refresh.id)

    await reset_draft(db_session, user_id=USER_ID, application_id=refresh.id, digest=first_digest)
    revised_answers = [(field_id, answer_text) for field_id, answer_text in PREDKO_ANSWERS]
    revised_answers[0] = ("name", "Мария")
    await _complete_draft(db_session, refresh.id, USER_ID, revised_answers)
    second_digest = await _snapshot_digest(db_session, refresh.id)
    expected_answers = [
        (answer.field_id, answer.answer_text)
        for answer in await QuestionnaireRepo.get_by_application(
            db_session, application_id=refresh.id
        )
    ]

    assert first_digest != second_digest
    with pytest.raises(IntroWorkflowError):
        await reset_draft(
            db_session, user_id=USER_ID, application_id=refresh.id, digest=first_digest
        )

    assert [
        (answer.field_id, answer.answer_text)
        for answer in await QuestionnaireRepo.get_by_application(
            db_session, application_id=refresh.id
        )
    ] == expected_answers


@pytest.mark.asyncio
async def test_revoked_member_cannot_resume_existing_refresh(app_env, db_session) -> None:
    from bot.db.repos.user import UserRepo
    from bot.services.intro_workflow import IntroWorkflowError, start_or_resume_refresh

    await _seed_user(db_session, USER_ID, is_member=True)
    refresh = await start_or_resume_refresh(db_session, user_id=USER_ID)
    await UserRepo.set_member(db_session, USER_ID, False)

    with pytest.raises(IntroWorkflowError):
        await start_or_resume_refresh(db_session, user_id=USER_ID)

    assert refresh.status == "filling"


@pytest.mark.asyncio
async def test_revoked_member_cannot_confirm_refresh_or_enqueue_effect(app_env, db_session) -> None:
    from bot.db.repos.user import UserRepo
    from bot.services.intro_workflow import (
        IntroWorkflowError,
        confirm_application,
        start_or_resume_refresh,
    )

    await _seed_user(db_session, USER_ID, is_member=True)
    refresh = await start_or_resume_refresh(db_session, user_id=USER_ID)
    await _complete_draft(db_session, refresh.id, USER_ID)
    digest = await _snapshot_digest(db_session, refresh.id)
    await UserRepo.set_member(db_session, USER_ID, False)

    with pytest.raises(IntroWorkflowError):
        await confirm_application(
            db_session, user_id=USER_ID, application_id=refresh.id, digest=digest
        )

    assert await _outbox_rows(db_session, refresh.id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("has_current_intro", "expected_effect"),
    [(False, "member_intro"), (True, "refresh_intro")],
)
async def test_confirm_refresh_enqueues_effect_for_member_state(
    app_env, db_session, has_current_intro: bool, expected_effect: str
) -> None:
    from bot.db.models import Intro
    from bot.services.intro_workflow import confirm_application, start_or_resume_refresh

    await _seed_user(db_session, USER_ID, is_member=True)
    if has_current_intro:
        current = await _make_completed_application(
            db_session, user_id=USER_ID, referral="@owner_name"
        )
        db_session.add(
            Intro(
                user_id=USER_ID,
                application_id=current.id,
                intro_text="old current intro",
                vouched_by_name="Vouch",
            )
        )
        await db_session.flush()

    refresh = await start_or_resume_refresh(db_session, user_id=USER_ID)
    await _complete_draft(db_session, refresh.id, USER_ID)
    await confirm_application(
        db_session,
        user_id=USER_ID,
        application_id=refresh.id,
        digest=await _snapshot_digest(db_session, refresh.id),
    )

    assert [
        (effect.effect_kind, effect.status) for effect in await _outbox_rows(db_session, refresh.id)
    ] == [(expected_effect, "pending")]


@pytest.mark.asyncio
async def test_legacy_intro_without_pointer_asks_all_seven_and_never_parses_old_text(
    app_env, db_session
) -> None:
    from bot.db.models import Intro
    from bot.db.repos.questionnaire import QuestionnaireRepo
    from bot.services.intro_workflow import next_field_id, start_or_resume_refresh

    await _seed_user(db_session, USER_ID, is_member=True)
    db_session.add(
        Intro(
            user_id=USER_ID,
            application_id=None,
            intro_text="📍 Лондон\n🔗 От участника чата",
            vouched_by_name="old",
        )
    )
    await db_session.flush()

    refresh = await start_or_resume_refresh(db_session, user_id=USER_ID)
    answers = await QuestionnaireRepo.get_by_application(db_session, application_id=refresh.id)

    assert refresh.flow_kind == "refresh"
    assert refresh.base_application_id is None
    assert answers == []
    assert await next_field_id(db_session, user_id=USER_ID, application_id=refresh.id) == "name"


@pytest.mark.asyncio
async def test_member_without_intro_uses_member_flow_with_null_base(app_env, db_session) -> None:
    from bot.db.repos.questionnaire import QuestionnaireRepo
    from bot.services.intro_workflow import start_or_resume_refresh

    await _seed_user(db_session, USER_ID, is_member=True)

    application = await start_or_resume_refresh(db_session, user_id=USER_ID)

    assert application.flow_kind == "refresh"
    assert application.base_application_id is None
    assert (
        await QuestionnaireRepo.get_by_application(db_session, application_id=application.id) == []
    )


@pytest.mark.asyncio
async def test_refresh_does_not_mask_unrelated_integrity_error(
    app_env, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.exc import IntegrityError

    from bot.db.repos.application import ApplicationRepo
    from bot.services.intro_workflow import start_or_resume_refresh

    class OtherConstraintError(Exception):
        diag = SimpleNamespace(constraint_name="uq_other_constraint")

    async def fail_create(*args, **kwargs):
        raise IntegrityError("INSERT", {}, OtherConstraintError())

    await _seed_user(db_session, USER_ID, is_member=True)
    monkeypatch.setattr(ApplicationRepo, "create", fail_create)

    with pytest.raises(IntegrityError):
        await start_or_resume_refresh(db_session, user_id=USER_ID)


@pytest.mark.asyncio
async def test_concurrent_refresh_creates_or_resumes_exactly_one_active_application(
    app_env, postgres_engine
) -> None:
    from bot.db.models import Application
    from bot.services.intro_workflow import start_or_resume_refresh

    user_id = 800_000_000 + uuid.uuid4().int % 100_000_000
    Session = async_sessionmaker(bind=postgres_engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as setup_session, setup_session.begin():
        await _seed_user(setup_session, user_id, is_member=True)

    barrier = asyncio.Barrier(2)

    async def start_in_own_transaction() -> int:
        async with Session() as session, session.begin():
            await barrier.wait()
            application = await start_or_resume_refresh(session, user_id=user_id)
            return application.id

    try:
        first_id, second_id = await asyncio.gather(
            start_in_own_transaction(), start_in_own_transaction()
        )
        async with Session() as check_session:
            result = await check_session.execute(
                select(Application).where(
                    Application.user_id == user_id,
                    Application.flow_kind == "refresh",
                    Application.status.in_(("filling", "confirmed")),
                )
            )
            active = list(result.scalars())

        assert first_id == second_id
        assert [application.id for application in active] == [first_id]
    finally:
        await _cleanup_concurrent_user(postgres_engine, user_id)


@pytest.mark.asyncio
async def test_locked_workflow_reads_authoritative_status_after_concurrent_confirmation(
    app_env, postgres_engine
) -> None:
    from bot.db.models import Application
    from bot.services.intro_workflow import IntroWorkflowError, next_field_id

    user_id = 800_000_000 + uuid.uuid4().int % 100_000_000
    Session = async_sessionmaker(bind=postgres_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with Session() as setup_session, setup_session.begin():
            await _seed_user(setup_session, user_id)
            application = await _new_application(setup_session, user_id, flow_kind="admission")
            application_id = application.id

        async with Session() as session_a:
            loaded = await session_a.get(Application, application_id)
            assert loaded is not None and loaded.status == "filling"

            async with Session() as session_b, session_b.begin():
                confirmed = await session_b.get(Application, application_id)
                assert confirmed is not None
                confirmed.status = "confirmed"
                confirmed.confirmed_intro_html = "frozen snapshot"

            with pytest.raises(IntroWorkflowError):
                await next_field_id(session_a, user_id=user_id, application_id=application_id)
    finally:
        await _cleanup_concurrent_user(postgres_engine, user_id)


@pytest.mark.asyncio
async def test_stale_fsm_cannot_rebind_confirm_callback_identity(app_env, monkeypatch) -> None:
    from bot.services.intro_workflow import IntroWorkflowError

    questionnaire = import_module("bot.handlers.questionnaire")
    session = AsyncMock()
    state = AsyncMock()
    state.get_data.return_value = {"application_id": 222}
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=USER_ID, first_name="Test", username="test"),
        message=SimpleNamespace(edit_text=AsyncMock()),
        bot=SimpleNamespace(send_message=AsyncMock()),
        answer=AsyncMock(),
    )
    callback_data = SimpleNamespace(action="yes", application_id=111, digest="stale-preview")
    confirm = AsyncMock(side_effect=IntroWorkflowError("stale preview"))
    monkeypatch.setattr(questionnaire, "confirm_application", confirm, raising=False)

    await questionnaire.handle_confirm(callback, callback_data, state, session)

    confirm.assert_awaited_once_with(
        session,
        user_id=USER_ID,
        application_id=111,
        digest="stale-preview",
    )
    callback.bot.send_message.assert_not_awaited()
    state.clear.assert_not_awaited()


def test_confirm_callback_binds_action_application_and_digest_within_telegram_limit(
    app_env,
) -> None:
    from bot.keyboards.inline import ConfirmCallback

    callback = ConfirmCallback(
        action="yes",
        application_id=2_147_483_647,
        digest="mrpfJjmr1jpKAq5Aueol1w",
    )
    packed = callback.pack()
    unpacked = ConfirmCallback.unpack(packed)

    assert len(packed.encode("utf-8")) <= 64
    assert unpacked.action == "yes"
    assert unpacked.application_id == 2_147_483_647
    assert unpacked.digest == "mrpfJjmr1jpKAq5Aueol1w"
