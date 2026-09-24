from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from tests.intro.test_questionnaire_workflow import (
    USER_ID,
    _make_completed_application,
    _seed_user,
)


async def _seed_intro(db_session, *, user_id: int, referral: str = "@owner_name"):
    from bot.db.models import Intro

    base = await _make_completed_application(db_session, user_id=user_id, referral=referral)
    intro = Intro(
        user_id=user_id,
        application_id=base.id,
        intro_text=base.confirmed_intro_html,
        vouched_by_name="Vouch",
    )
    db_session.add(intro)
    await db_session.flush()
    return intro, base


@pytest.mark.asyncio
async def test_selected_refresh_persists_unselected_answers_and_resumes_selected_order(
    app_env, db_session
) -> None:
    from bot.db.repos.questionnaire import QuestionnaireRepo
    from bot.services.intro_workflow import start_or_resume_refresh, write_answer

    await _seed_user(db_session, USER_ID, is_member=True)
    _, base = await _seed_intro(db_session, user_id=USER_ID)

    refresh = await start_or_resume_refresh(
        db_session,
        user_id=USER_ID,
        editable_field_ids={"experience", "goals"},
    )
    answers = await QuestionnaireRepo.get_by_application(db_session, application_id=refresh.id)

    assert refresh.base_application_id == base.id
    assert [answer.field_id for answer in answers] == [
        "name",
        "location",
        "referral",
        "projects",
        "hardest",
    ]
    assert (
        await write_answer(
            db_session,
            user_id=USER_ID,
            application_id=refresh.id,
            field_id="experience",
            answer_text="Новый опыт",
        )
        == "goals"
    )


@pytest.mark.asyncio
async def test_last_selected_middle_block_still_enforces_total_telegram_limit(
    app_env, db_session
) -> None:
    from bot.services.intro_workflow import (
        IntroWorkflowError,
        next_field_id,
        start_or_resume_refresh,
        write_answer,
    )

    await _seed_user(db_session, USER_ID, is_member=True)
    await _seed_intro(db_session, user_id=USER_ID)
    refresh = await start_or_resume_refresh(
        db_session,
        user_id=USER_ID,
        editable_field_ids={"experience"},
    )

    with pytest.raises(IntroWorkflowError, match="too long"):
        await write_answer(
            db_session,
            user_id=USER_ID,
            application_id=refresh.id,
            field_id="experience",
            answer_text="x" * 3_300,
        )
    assert (
        await next_field_id(db_session, user_id=USER_ID, application_id=refresh.id) == "experience"
    )


@pytest.mark.asyncio
async def test_skip_keeps_a_generic_referral_without_forcing_username(app_env, db_session) -> None:
    from bot.db.repos.questionnaire import QuestionnaireRepo
    from bot.services.intro_workflow import keep_base_answer, start_or_resume_refresh

    await _seed_user(db_session, USER_ID, is_member=True)
    await _seed_intro(db_session, user_id=USER_ID, referral="Узнал на встрече")
    refresh = await start_or_resume_refresh(
        db_session,
        user_id=USER_ID,
        editable_field_ids={"referral"},
    )

    assert (
        await keep_base_answer(
            db_session,
            user_id=USER_ID,
            application_id=refresh.id,
            field_id="referral",
        )
        is None
    )
    answers = await QuestionnaireRepo.get_by_application(db_session, application_id=refresh.id)
    assert next(answer.answer_text for answer in answers if answer.field_id == "referral") == (
        "Узнал на встрече"
    )


@pytest.mark.asyncio
async def test_cancel_deletes_only_the_refresh_draft(app_env, db_session) -> None:
    from bot.db.models import Application, Intro, QuestionnaireAnswer
    from bot.services.intro_workflow import cancel_refresh, start_or_resume_refresh

    await _seed_user(db_session, USER_ID, is_member=True)
    intro, base = await _seed_intro(db_session, user_id=USER_ID)
    refresh = await start_or_resume_refresh(
        db_session,
        user_id=USER_ID,
        editable_field_ids={"experience"},
    )

    await cancel_refresh(db_session, user_id=USER_ID, application_id=refresh.id)

    assert await db_session.get(Application, refresh.id) is None
    assert await db_session.get(Application, base.id) is not None
    assert (await db_session.get(Intro, intro.id)).application_id == base.id
    assert (
        await db_session.execute(
            select(QuestionnaireAnswer).where(QuestionnaireAnswer.application_id == refresh.id)
        )
    ).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_refresh_resume_stops_after_membership_is_lost(app_env, db_session) -> None:
    from bot.db.repos.user import UserRepo
    from bot.services.intro_workflow import (
        IntroWorkflowError,
        next_field_id,
        start_or_resume_refresh,
    )

    await _seed_user(db_session, USER_ID, is_member=True)
    await _seed_intro(db_session, user_id=USER_ID)
    refresh = await start_or_resume_refresh(
        db_session,
        user_id=USER_ID,
        editable_field_ids={"experience"},
    )
    await UserRepo.set_member(db_session, USER_ID, False)

    with pytest.raises(IntroWorkflowError, match="current member"):
        await next_field_id(
            db_session,
            user_id=USER_ID,
            application_id=refresh.id,
        )


@pytest.mark.asyncio
async def test_return_to_selection_keeps_edits_until_explicit_restart(app_env, db_session) -> None:
    from bot.db.repos.questionnaire import QuestionnaireRepo
    from bot.services.intro_contract import intro_digest, render_intro_html
    from bot.services.intro_workflow import (
        keep_base_answer,
        restart_refresh_selection,
        start_or_resume_refresh,
        verify_refresh_preview,
        write_answer,
    )

    await _seed_user(db_session, USER_ID, is_member=True)
    _, base = await _seed_intro(db_session, user_id=USER_ID)
    base_answers = await QuestionnaireRepo.get_by_application(db_session, application_id=base.id)
    base_experience = next(
        answer.answer_text for answer in base_answers if answer.field_id == "experience"
    )
    refresh = await start_or_resume_refresh(
        db_session,
        user_id=USER_ID,
        editable_field_ids={"experience", "goals"},
    )
    await write_answer(
        db_session,
        user_id=USER_ID,
        application_id=refresh.id,
        field_id="experience",
        answer_text="Новый опыт",
    )
    await keep_base_answer(
        db_session,
        user_id=USER_ID,
        application_id=refresh.id,
        field_id="goals",
    )
    answers = await QuestionnaireRepo.get_by_application(db_session, application_id=refresh.id)
    digest = intro_digest(
        render_intro_html(
            [(answer.field_id, answer.answer_text) for answer in answers],
            catalog_version="intro-v2",
        )
    )

    await verify_refresh_preview(
        db_session,
        user_id=USER_ID,
        application_id=refresh.id,
        digest=digest,
    )
    unchanged = await QuestionnaireRepo.get_by_application(db_session, application_id=refresh.id)
    assert (
        next(answer.answer_text for answer in unchanged if answer.field_id == "experience")
        == "Новый опыт"
    )

    assert (
        await restart_refresh_selection(
            db_session,
            user_id=USER_ID,
            application_id=refresh.id,
            digest=digest,
            editable_field_ids={"goals"},
        )
        == "goals"
    )
    restarted = await QuestionnaireRepo.get_by_application(db_session, application_id=refresh.id)
    assert (
        next(answer.answer_text for answer in restarted if answer.field_id == "experience")
        == base_experience
    )


@pytest.mark.asyncio
async def test_wave_candidates_use_last_successful_prompt_and_skip_active_refresh(
    app_env, db_session
) -> None:
    from bot.db.models import Intro, IntroRefreshTracking
    from bot.db.repos.application import ApplicationRepo
    from bot.db.repos.intro import IntroRepo

    cutoff = datetime(2026, 4, 1, 7, tzinfo=timezone.utc)
    eligible, recent, departed, active = 720_001, 720_002, 720_003, 720_004
    for user_id, is_member in (
        (eligible, True),
        (recent, True),
        (departed, False),
        (active, True),
    ):
        await _seed_user(db_session, user_id, is_member=is_member)
        db_session.add(
            Intro(
                user_id=user_id,
                intro_text=f"intro {user_id}",
                vouched_by_name="Vouch",
                updated_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            )
        )
    db_session.add(
        IntroRefreshTracking(
            user_id=recent,
            cycle_started_at=datetime(2026, 3, 1, 7, tzinfo=timezone.utc),
            reminders_sent=1,
            last_reminder_at=datetime(2026, 5, 1, 7, tzinfo=timezone.utc),
            phase="offer_sent",
            completed=True,
        )
    )
    await ApplicationRepo.create(
        db_session,
        user_id=active,
        flow_kind="refresh",
        base_application_id=None,
        catalog_version="intro-v2",
    )
    await db_session.flush()

    candidates = await IntroRepo.get_refresh_wave_candidates(db_session, cutoff=cutoff)

    assert [intro.user_id for intro in candidates] == [eligible]
