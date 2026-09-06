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


async def _require_refresh_member(session: AsyncSession, application: Application) -> None:
    if application.flow_kind != "refresh":
        return
    user = await session.get(User, application.user_id)
    if user is None or not user.is_member:
        raise IntroWorkflowError("Refresh requires a current member")


async def _refresh_base_values(
    session: AsyncSession, base_application_id: int | None
) -> dict[str, str]:
    if base_application_id is None:
        return {}
    answers = await QuestionnaireRepo.get_by_application(
        session, application_id=base_application_id
    )
    return {
        answer.field_id: answer.answer_text for answer in answers if answer.field_id is not None
    }


def _validate_refresh_selection(
    application: Application,
    base_values: dict[str, str],
    editable_field_ids: set[str] | None,
) -> None:
    if editable_field_ids is None:
        return
    valid_field_ids = {field.field_id for field in get_intro_catalog(application.catalog_version)}
    if not editable_field_ids or not editable_field_ids <= valid_field_ids:
        raise IntroWorkflowError("Refresh selection is empty or invalid")
    if application.base_application_id is None or set(base_values) != valid_field_ids:
        raise IntroWorkflowError("Refresh selection requires a complete versioned intro")
    if "referral" in editable_field_ids and is_concrete_referral(base_values["referral"]):
        raise IntroWorkflowError("A concrete referral cannot be selected")


async def _copy_refresh_answers(
    session: AsyncSession,
    application: Application,
    base_values: dict[str, str],
    editable_field_ids: set[str] | None,
) -> None:
    catalog = get_intro_catalog(application.catalog_version)
    if editable_field_ids is not None:
        for index, field in enumerate(catalog):
            if field.field_id in editable_field_ids:
                continue
            await QuestionnaireRepo.save_answer(
                session,
                user_id=application.user_id,
                application_id=application.id,
                field_id=field.field_id,
                question_index=index,
                question_text=field.question,
                answer_text=base_values[field.field_id],
            )
    elif is_concrete_referral(base_values.get("referral")):
        field = catalog[2]
        await QuestionnaireRepo.save_answer(
            session,
            user_id=application.user_id,
            application_id=application.id,
            field_id=field.field_id,
            question_index=2,
            question_text=field.question,
            answer_text=normalize_referral_username(base_values["referral"]),
        )


async def _current_snapshot(session: AsyncSession, application: Application) -> str:
    answers = await QuestionnaireRepo.get_by_application(session, application_id=application.id)
    try:
        return render_intro_html(
            [(answer.field_id, answer.answer_text) for answer in answers],
            catalog_version=application.catalog_version,
        )
    except IntroContractError as error:
        raise IntroWorkflowError("Questionnaire is incomplete") from error


async def write_answer(
    session: AsyncSession,
    *,
    user_id: int,
    application_id: int,
    field_id: str,
    answer_text: str,
) -> str | None:
    application = await _locked_application(session, user_id=user_id, application_id=application_id)
    await _require_refresh_member(session, application)
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

    catalog = get_intro_catalog(application.catalog_version)
    field = catalog[field_index]
    answers = await QuestionnaireRepo.get_by_application(session, application_id=application.id)
    if len(answers) == len(catalog) - 1:
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
    await _require_refresh_member(session, application)
    if application.status != "filling":
        raise IntroWorkflowError("Questionnaire is not filling")
    try:
        return await _next_missing_field(application, session)
    except IntroContractError as error:
        raise IntroWorkflowError("Unknown questionnaire catalog") from error


async def start_or_resume_refresh(
    session: AsyncSession,
    *,
    user_id: int,
    editable_field_ids: set[str] | None = None,
) -> Application:
    user = await session.get(User, user_id)
    if user is None or not user.is_member:
        raise IntroWorkflowError("Refresh requires a current member")

    active = await ApplicationRepo.get_active_refresh(session, user_id)
    if active is not None:
        return active

    intro_result = await session.execute(select(Intro).where(Intro.user_id == user_id))
    intro = intro_result.scalar_one_or_none()
    base_application_id = intro.application_id if intro is not None else None

    base_values = await _refresh_base_values(session, base_application_id)

    try:
        async with session.begin_nested():
            application = await ApplicationRepo.create(
                session,
                user_id=user_id,
                flow_kind="refresh",
                base_application_id=base_application_id,
                catalog_version="intro-v2",
            )
            _validate_refresh_selection(application, base_values, editable_field_ids)
            await _copy_refresh_answers(session, application, base_values, editable_field_ids)
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


async def refresh_base_answer(
    session: AsyncSession, *, user_id: int, application_id: int, field_id: str
) -> str | None:
    application = await _locked_application(session, user_id=user_id, application_id=application_id)
    await _require_refresh_member(session, application)
    return await _refresh_base_answer(session, application, field_id)


async def _refresh_base_answer(
    session: AsyncSession, application: Application, field_id: str
) -> str | None:
    if application.flow_kind != "refresh" or application.base_application_id is None:
        return None
    answers = await QuestionnaireRepo.get_by_application(
        session, application_id=application.base_application_id
    )
    return next(
        (answer.answer_text for answer in answers if answer.field_id == field_id),
        None,
    )


async def keep_base_answer(
    session: AsyncSession, *, user_id: int, application_id: int, field_id: str
) -> str | None:
    application = await _locked_application(session, user_id=user_id, application_id=application_id)
    await _require_refresh_member(session, application)
    if application.status != "filling" or application.base_application_id is None:
        raise IntroWorkflowError("Refresh draft has no base answer")
    if await _next_missing_field(application, session) != field_id:
        raise IntroWorkflowError("Questionnaire field is stale or already answered")
    base_answer = await _refresh_base_answer(session, application, field_id)
    if base_answer is None:
        raise IntroWorkflowError("Refresh draft has no base answer")
    field_index = _field_index(application, field_id)
    field = get_intro_catalog(application.catalog_version)[field_index]
    await QuestionnaireRepo.save_answer(
        session,
        user_id=user_id,
        application_id=application.id,
        field_id=field_id,
        question_index=field_index,
        question_text=field.question,
        answer_text=base_answer,
    )
    return await _next_missing_field(application, session)


async def cancel_refresh(session: AsyncSession, *, user_id: int, application_id: int) -> None:
    application = await _locked_application(session, user_id=user_id, application_id=application_id)
    if application.flow_kind != "refresh" or application.status != "filling":
        raise IntroWorkflowError("Refresh draft cannot be cancelled")
    await session.execute(
        delete(QuestionnaireAnswer).where(QuestionnaireAnswer.application_id == application.id)
    )
    await session.delete(application)
    await session.flush()


async def reset_draft(
    session: AsyncSession, *, user_id: int, application_id: int, digest: str
) -> str | None:
    application = await _locked_application(session, user_id=user_id, application_id=application_id)
    await _require_refresh_member(session, application)
    if application.status != "filling":
        raise IntroWorkflowError("Questionnaire is not filling")

    answers = await QuestionnaireRepo.get_by_application(session, application_id=application.id)
    snapshot = await _current_snapshot(session, application)
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


async def verify_refresh_preview(
    session: AsyncSession, *, user_id: int, application_id: int, digest: str
) -> Application:
    application = await _locked_application(session, user_id=user_id, application_id=application_id)
    await _require_refresh_member(session, application)
    if application.flow_kind != "refresh" or application.status != "filling":
        raise IntroWorkflowError("Refresh preview is stale")
    if intro_digest(await _current_snapshot(session, application)) != digest:
        raise IntroWorkflowError("Confirmation digest is stale")
    return application


async def restart_refresh_selection(
    session: AsyncSession,
    *,
    user_id: int,
    application_id: int,
    digest: str,
    editable_field_ids: set[str] | None,
) -> str | None:
    application = await verify_refresh_preview(
        session,
        user_id=user_id,
        application_id=application_id,
        digest=digest,
    )
    base_values = await _refresh_base_values(session, application.base_application_id)
    _validate_refresh_selection(application, base_values, editable_field_ids)
    await session.execute(
        delete(QuestionnaireAnswer).where(
            QuestionnaireAnswer.application_id == application.id,
            QuestionnaireAnswer.is_current.is_(True),
        )
    )
    await session.flush()
    await _copy_refresh_answers(session, application, base_values, editable_field_ids)
    return await _next_missing_field(application, session)


async def confirm_application(
    session: AsyncSession,
    *,
    user_id: int,
    application_id: int,
    digest: str,
) -> Application:
    application = await _locked_application(session, user_id=user_id, application_id=application_id)
    await _require_refresh_member(session, application)

    if application.status == "confirmed":
        if (
            application.confirmed_intro_html is None
            or intro_digest(application.confirmed_intro_html) != digest
        ):
            raise IntroWorkflowError("Confirmation digest is stale")
        return application
    if application.status != "filling":
        raise IntroWorkflowError("Questionnaire is not filling")

    snapshot = await _current_snapshot(session, application)
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
