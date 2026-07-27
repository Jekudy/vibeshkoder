from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Application, Intro, IntroEffectOutbox, QuestionnaireAnswer, User
from bot.db.repos.application import ApplicationRepo
from bot.db.repos.questionnaire import QuestionnaireRepo
from bot.services.intro_contract import (
    IntroContractError,
    get_intro_catalog,
    intro_digest,
    normalize_intro_answer,
    render_intro_html,
)
from bot.services.referral_username import (
    InvalidReferralUsername,
    is_concrete_referral,
    normalize_referral_username,
)


class IntroWorkflowError(ValueError):
    """The requested questionnaire transition is no longer valid."""


class InvalidReferralAnswer(IntroWorkflowError):
    """The supplied referral is not a concrete Telegram username."""


async def _locked_application(
    session: AsyncSession, *, user_id: int, application_id: int
) -> Application:
    result = await session.execute(
        select(Application)
        .execution_options(populate_existing=True)
        .where(Application.id == application_id)
        .with_for_update()
    )
    application = result.scalar_one_or_none()
    if application is None or application.user_id != user_id:
        raise IntroWorkflowError("Questionnaire is not owned by this user")
    return application


def _field_index(application: Application, field_id: str) -> int:
    for index, field in enumerate(get_intro_catalog(application.catalog_version)):
        if field.field_id == field_id:
            return index
    raise IntroWorkflowError("Unknown questionnaire field")


async def _next_missing_field(application: Application, session: AsyncSession) -> str | None:
    answers = await QuestionnaireRepo.get_by_application(session, application_id=application.id)
    answered = {answer.field_id for answer in answers}
    for field in get_intro_catalog(application.catalog_version):
        if field.field_id not in answered:
            return field.field_id
    return None


async def write_answer(
    session: AsyncSession,
    *,
    user_id: int,
    application_id: int,
    field_id: str,
    answer_text: str,
) -> str | None:
    application = await _locked_application(session, user_id=user_id, application_id=application_id)
    if application.status != "filling":
        raise IntroWorkflowError("Questionnaire is not filling")

    try:
        expected_field_id = await _next_missing_field(application, session)
        if expected_field_id != field_id:
            raise IntroWorkflowError("Questionnaire field is stale or already answered")
        field_index = _field_index(application, field_id)
    except IntroContractError as error:
        raise IntroWorkflowError("Unknown questionnaire catalog") from error

    try:
        answer_text = normalize_intro_answer(answer_text)
    except IntroContractError as error:
        raise IntroWorkflowError("Questionnaire answer is blank") from error

    if field_id == "referral":
        try:
            answer_text = normalize_referral_username(answer_text)
        except InvalidReferralUsername as error:
            raise InvalidReferralAnswer("Invalid Telegram referral username") from error

    field = get_intro_catalog(application.catalog_version)[field_index]
    if field_index == len(get_intro_catalog(application.catalog_version)) - 1:
        answers = await QuestionnaireRepo.get_by_application(session, application_id=application.id)
        try:
            render_intro_html(
                [(answer.field_id, answer.answer_text) for answer in answers]
                + [(field_id, answer_text)],
                catalog_version=application.catalog_version,
            )
        except IntroContractError as error:
            raise IntroWorkflowError("Questionnaire answer is too long") from error
    await QuestionnaireRepo.save_answer(
        session,
        user_id=user_id,
        application_id=application.id,
        field_id=field_id,
        question_index=field_index,
        question_text=field.question,
        answer_text=answer_text,
    )
    return await _next_missing_field(application, session)


async def next_field_id(session: AsyncSession, *, user_id: int, application_id: int) -> str | None:
    application = await _locked_application(session, user_id=user_id, application_id=application_id)
    if application.status != "filling":
        raise IntroWorkflowError("Questionnaire is not filling")
    try:
        return await _next_missing_field(application, session)
    except IntroContractError as error:
        raise IntroWorkflowError("Unknown questionnaire catalog") from error


async def start_or_resume_refresh(session: AsyncSession, *, user_id: int) -> Application:
    user = await session.get(User, user_id)
    if user is None or not user.is_member:
        raise IntroWorkflowError("Refresh requires a current member")

    active = await ApplicationRepo.get_active_refresh(session, user_id)
    if active is not None:
        return active

    intro_result = await session.execute(select(Intro).where(Intro.user_id == user_id))
    intro = intro_result.scalar_one_or_none()
    base_application_id = intro.application_id if intro is not None else None

    try:
        async with session.begin_nested():
            application = await ApplicationRepo.create(
                session,
                user_id=user_id,
                flow_kind="refresh",
                base_application_id=base_application_id,
                catalog_version="intro-v2",
            )
            if base_application_id is not None:
                base_answers = await QuestionnaireRepo.get_by_application(
                    session, application_id=base_application_id
                )
                referral = next(
                    (
                        answer.answer_text
                        for answer in base_answers
                        if answer.field_id == "referral"
                    ),
                    None,
                )
                if is_concrete_referral(referral):
                    field = get_intro_catalog(application.catalog_version)[2]
                    await QuestionnaireRepo.save_answer(
                        session,
                        user_id=user_id,
                        application_id=application.id,
                        field_id=field.field_id,
                        question_index=2,
                        question_text=field.question,
                        answer_text=normalize_referral_username(referral),
                    )
            return application
    except IntegrityError as error:
        errors = (error.orig, error.orig.__cause__, error.orig.__context__)
        if not any(
            getattr(item, "constraint_name", None) == "uq_applications_active_refresh"
            or getattr(getattr(item, "diag", None), "constraint_name", None)
            == "uq_applications_active_refresh"
            for item in errors
            if item is not None
        ):
            raise
        active = await ApplicationRepo.get_active_refresh(session, user_id)
        if active is None:
            raise
        return active


async def reset_draft(
    session: AsyncSession, *, user_id: int, application_id: int, digest: str
) -> str | None:
    application = await _locked_application(session, user_id=user_id, application_id=application_id)
    if application.status != "filling":
        raise IntroWorkflowError("Questionnaire is not filling")

    answers = await QuestionnaireRepo.get_by_application(session, application_id=application.id)
    try:
        snapshot = render_intro_html(
            [(answer.field_id, answer.answer_text) for answer in answers],
            catalog_version=application.catalog_version,
        )
    except IntroContractError as error:
        raise IntroWorkflowError("Questionnaire is incomplete") from error
    if intro_digest(snapshot) != digest:
        raise IntroWorkflowError("Confirmation digest is stale")
    referral = next(
        (answer.answer_text for answer in answers if answer.field_id == "referral"), None
    )
    await session.execute(
        delete(QuestionnaireAnswer).where(
            QuestionnaireAnswer.application_id == application.id,
            QuestionnaireAnswer.is_current.is_(True),
        )
    )
    await session.flush()
    if is_concrete_referral(referral):
        field = get_intro_catalog(application.catalog_version)[2]
        await QuestionnaireRepo.save_answer(
            session,
            user_id=user_id,
            application_id=application.id,
            field_id=field.field_id,
            question_index=2,
            question_text=field.question,
            answer_text=normalize_referral_username(referral),
        )
    return await _next_missing_field(application, session)


async def confirm_application(
    session: AsyncSession,
    *,
    user_id: int,
    application_id: int,
    digest: str,
) -> Application:
    application = await _locked_application(session, user_id=user_id, application_id=application_id)

    if application.flow_kind == "refresh":
        user = await session.get(User, user_id)
        if user is None or not user.is_member:
            raise IntroWorkflowError("Refresh requires a current member")

    if application.status == "confirmed":
        if (
            application.confirmed_intro_html is None
            or intro_digest(application.confirmed_intro_html) != digest
        ):
            raise IntroWorkflowError("Confirmation digest is stale")
        return application
    if application.status != "filling":
        raise IntroWorkflowError("Questionnaire is not filling")

    answers = await QuestionnaireRepo.get_by_application(session, application_id=application.id)
    try:
        snapshot = render_intro_html(
            [(answer.field_id, answer.answer_text) for answer in answers],
            catalog_version=application.catalog_version,
        )
    except IntroContractError as error:
        raise IntroWorkflowError("Questionnaire is incomplete") from error
    if intro_digest(snapshot) != digest:
        raise IntroWorkflowError("Confirmation digest is stale")

    if application.flow_kind == "admission":
        effect_kind = "candidate_card"
    elif application.flow_kind == "refresh":
        intro_result = await session.execute(select(Intro.id).where(Intro.user_id == user_id))
        effect_kind = (
            "refresh_intro" if intro_result.scalar_one_or_none() is not None else "member_intro"
        )
    else:
        raise IntroWorkflowError("Questionnaire flow is invalid")

    application.confirmed_intro_html = snapshot
    application.status = "confirmed"
    session.add(
        IntroEffectOutbox(
            application_id=application.id,
            effect_kind=effect_kind,
            status="pending",
        )
    )
    await session.flush()
    return application
