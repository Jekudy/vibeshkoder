from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import import_module
from tests.intro.test_contract import PREDKO_ANSWERS
from tests.intro.test_effect_worker import (
    EffectTestDatabase,
    RecordingBot,
    _confirmed_application,
    _effect,
    _worker_with_test_database,
)
from tests.intro.test_failure_safety import _current_intro


USER_ID = 781_201
OLD_ANSWERS = [
    ("name", "Старый Сергей"),
    ("location", "Лондон"),
    ("referral", "От участника чата"),
    ("experience", "старый опыт"),
    ("projects", "старые проекты"),
    ("hardest", "старое сложное"),
    ("goals", "старые цели"),
]


@pytest_asyncio.fixture
async def projection_test_db(postgres_engine):
    sessions = async_sessionmaker(bind=postgres_engine, class_=AsyncSession, expire_on_commit=False)
    database = EffectTestDatabase(sessions)
    await database.cleanup()
    try:
        yield database
    finally:
        await database.cleanup()


async def _add_answers(
    session: AsyncSession, application_id: int, answers: list[tuple[str, str]]
) -> None:
    from bot.db.models import QuestionnaireAnswer
    from bot.services.intro_contract import get_intro_catalog

    catalog = get_intro_catalog("intro-v2")
    for index, (field, answer) in enumerate(answers):
        session.add(
            QuestionnaireAnswer(
                user_id=USER_ID,
                application_id=application_id,
                field_id=field,
                question_index=index,
                question_text=catalog[index].question,
                answer_text=answer,
            )
        )
    await session.flush()


def _scalar_state(instance) -> tuple[tuple[str, object], ...]:
    return tuple(
        (column.key, getattr(instance, column.key)) for column in inspect(instance).mapper.columns
    )


async def _canonical_state(
    session: AsyncSession,
    *,
    old_application_id: int,
    current_application_id: int,
    intro_id: int,
) -> tuple[tuple[tuple[str, object], ...], ...]:
    from bot.db.models import Application, Intro, QuestionnaireAnswer

    old = await session.get(Application, old_application_id)
    current = await session.get(Application, current_application_id)
    intro = await session.get(Intro, intro_id)
    old_answers = list(
        (
            await session.execute(
                select(QuestionnaireAnswer)
                .where(QuestionnaireAnswer.application_id == old_application_id)
                .order_by(QuestionnaireAnswer.question_index)
            )
        ).scalars()
    )
    current_answers = list(
        (
            await session.execute(
                select(QuestionnaireAnswer)
                .where(QuestionnaireAnswer.application_id == current_application_id)
                .order_by(QuestionnaireAnswer.question_index)
            )
        ).scalars()
    )
    assert old is not None
    assert current is not None
    assert intro is not None
    return (
        _scalar_state(old),
        _scalar_state(current),
        _scalar_state(intro),
        tuple(_scalar_state(answer) for answer in old_answers),
        tuple(_scalar_state(answer) for answer in current_answers),
    )


def test_sheet_has_no_inbound_sync_entry_point(app_env) -> None:
    sheets = import_module("bot.services.sheets")

    assert not hasattr(sheets, "sync_all_from_sheet")


def test_sheet_headers_follow_intro_v2_catalog_labels_and_order(app_env) -> None:
    from bot.services.intro_contract import get_intro_catalog

    sheets = import_module("bot.services.sheets")

    assert sheets.HEADERS == [
        "Telegram ID",
        "Username",
        *(field.sheet_header for field in get_intro_catalog("intro-v2")),
        "Кто поручился",
        "Статус",
    ]


@pytest.mark.asyncio
async def test_sheet_projection_uses_only_fresh_current_application_in_catalog_order(
    app_env, projection_test_db, monkeypatch
) -> None:
    projection_test_db.user_ids.append(USER_ID)
    worker, sessions = _worker_with_test_database(monkeypatch, projection_test_db)
    async with sessions.begin() as session:
        old = await _confirmed_application(session, user_id=USER_ID, flow_kind="admission")
        old.status = "added"
        await _add_answers(session, old.id, OLD_ANSWERS)
        fresh = await _confirmed_application(
            session,
            user_id=USER_ID,
            flow_kind="refresh",
            base_id=old.id,
        )
        await _add_answers(session, fresh.id, PREDKO_ANSWERS)
        fresh.confirmed_intro_html = "frozen fresh snapshot"
        fresh.status = "added"
        intro = await _current_intro(session, USER_ID, fresh.id)
        effect = await _effect(session, fresh.id, "sheet_projection")
        old_id, fresh_id, intro_id, effect_id = old.id, fresh.id, intro.id, effect.id
    async with sessions() as observer:
        canonical_before = await _canonical_state(
            observer,
            old_application_id=old_id,
            current_application_id=fresh_id,
            intro_id=intro_id,
        )

    project_sheet = AsyncMock()
    await worker.process_intro_effects(RecordingBot(), project_sheet=project_sheet)

    async with sessions() as observer:
        from bot.db.models import IntroEffectOutbox

        delivered = await observer.get(IntroEffectOutbox, effect_id)
        canonical_after = await _canonical_state(
            observer,
            old_application_id=old_id,
            current_application_id=fresh_id,
            intro_id=intro_id,
        )

        assert delivered.status == "sent"
        assert canonical_after == canonical_before

    project_sheet.assert_awaited_once_with(
        user_id=USER_ID,
        application_id=fresh_id,
        username="@effect_781201",
        vouched_by="@voucher",
        answers_by_field_id=dict(PREDKO_ANSWERS),
    )


@pytest.mark.asyncio
async def test_stale_sheet_projection_is_marked_stale_without_write_to_canonical_state(
    app_env, projection_test_db, monkeypatch
) -> None:
    projection_test_db.user_ids.append(USER_ID)
    worker, sessions = _worker_with_test_database(monkeypatch, projection_test_db)
    async with sessions.begin() as session:
        current = await _confirmed_application(session, user_id=USER_ID, flow_kind="admission")
        current.status = "added"
        await _add_answers(session, current.id, OLD_ANSWERS)
        intro = await _current_intro(session, USER_ID, current.id)
        stale = await _confirmed_application(
            session,
            user_id=USER_ID,
            flow_kind="refresh",
            base_id=current.id,
        )
        await _add_answers(session, stale.id, PREDKO_ANSWERS)
        stale.confirmed_intro_html = "frozen refresh snapshot"
        effect = await _effect(session, stale.id, "sheet_projection")
        current_id, stale_id, intro_id, effect_id = current.id, stale.id, intro.id, effect.id
    async with sessions() as observer:
        canonical_before = await _canonical_state(
            observer,
            old_application_id=current_id,
            current_application_id=stale_id,
            intro_id=intro_id,
        )

    project_sheet = AsyncMock()
    await worker.process_intro_effects(RecordingBot(), project_sheet=project_sheet)

    async with sessions() as observer:
        from bot.db.models import IntroEffectOutbox

        delivered = await observer.get(IntroEffectOutbox, effect_id)
        canonical_after = await _canonical_state(
            observer,
            old_application_id=current_id,
            current_application_id=stale_id,
            intro_id=intro_id,
        )

        assert delivered.status == "stale"
        assert canonical_after == canonical_before
    project_sheet.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("newer_status", "expected_status"),
    [
        ("sent", "pending"),
        ("failed", "pending"),
        ("stale", "pending"),
        ("pending", "pending"),
        ("processing", "processing"),
    ],
)
async def test_projection_pointer_flip_stales_old_effect_and_requeues_current_projection(
    app_env, projection_test_db, monkeypatch, newer_status, expected_status
) -> None:
    from bot.db.models import Intro, IntroEffectOutbox

    projection_test_db.user_ids.append(USER_ID)
    worker, sessions = _worker_with_test_database(monkeypatch, projection_test_db)
    async with sessions.begin() as session:
        old = await _confirmed_application(session, user_id=USER_ID, flow_kind="admission")
        old.status = "added"
        await _add_answers(session, old.id, OLD_ANSWERS)
        newer = await _confirmed_application(session, user_id=USER_ID, flow_kind="refresh")
        newer.status = "added"
        await _add_answers(session, newer.id, PREDKO_ANSWERS)
        intro = await _current_intro(session, USER_ID, old.id)
        old_effect = await _effect(session, old.id, "sheet_projection")
        newer_effect = await _effect(session, newer.id, "sheet_projection", status=newer_status)
        intro_id, old_effect_id, newer_id, newer_effect_id = (
            intro.id,
            old_effect.id,
            newer.id,
            newer_effect.id,
        )

    async def flip_pointer(**kwargs) -> None:
        async with sessions.begin() as session:
            (await session.get(Intro, intro_id, with_for_update=True)).application_id = newer_id

    await worker.process_intro_effects(
        RecordingBot(), max_effects=1, project_sheet=AsyncMock(side_effect=flip_pointer)
    )

    async with sessions() as observer:
        old_effect = await observer.get(IntroEffectOutbox, old_effect_id)
        newer_effect = await observer.get(IntroEffectOutbox, newer_effect_id)
        assert old_effect.status == "stale"
        assert newer_effect.status == expected_status


@pytest.mark.asyncio
async def test_sheet_snapshot_uses_field_ids_not_shuffled_legacy_indexes(
    app_env, projection_test_db, monkeypatch
) -> None:
    projection_test_db.user_ids.append(USER_ID)
    worker, sessions = _worker_with_test_database(monkeypatch, projection_test_db)
    from bot.db.models import QuestionnaireAnswer

    async with sessions.begin() as session:
        application = await _confirmed_application(session, user_id=USER_ID, flow_kind="admission")
        application.status = "added"
        await _add_answers(session, application.id, PREDKO_ANSWERS)
        answers = list(
            (
                await session.execute(
                    select(QuestionnaireAnswer).where(
                        QuestionnaireAnswer.application_id == application.id
                    )
                )
            ).scalars()
        )
        for answer in answers:
            answer.question_index = 99 - answer.question_index
        await _current_intro(session, USER_ID, application.id)
        await _effect(session, application.id, "sheet_projection")

    project_sheet = AsyncMock()
    await worker.process_intro_effects(RecordingBot(), project_sheet=project_sheet)

    assert project_sheet.await_args.kwargs["answers_by_field_id"] == dict(PREDKO_ANSWERS)


@pytest.mark.parametrize("row_number", [None, 7])
def test_sheet_writes_formula_like_values_as_raw_literals(app_env, monkeypatch, row_number) -> None:
    sheets = import_module("bot.services.sheets")
    worksheet = MagicMock()
    monkeypatch.setattr(sheets, "_find_row_by_telegram_id", lambda *_args: row_number)

    sheets._project_row(
        worksheet,
        user_id=USER_ID,
        username="=SUM(1,1)",
        vouched_by="=1+1",
        answers_by_field_id={
            field_id: '=HYPERLINK("https://bad.example")' for field_id, _ in PREDKO_ANSWERS
        },
    )

    if row_number is None:
        assert worksheet.append_row.call_args.kwargs["value_input_option"] == "RAW"
    else:
        assert worksheet.update.call_args.kwargs["raw"] is True


@pytest.mark.asyncio
async def test_google_auth_error_is_wrapped_as_projection_error(app_env, monkeypatch) -> None:
    from google.auth.exceptions import GoogleAuthError
    from bot.services.sheets import SheetProjectionError, project_intro_to_sheet

    monkeypatch.setattr("bot.services.sheets._is_configured", lambda: True)
    monkeypatch.setattr(
        "bot.services.sheets._get_sheet", lambda: (_ for _ in ()).throw(GoogleAuthError("bad auth"))
    )

    with pytest.raises(SheetProjectionError, match="Google Sheet projection failed"):
        await project_intro_to_sheet(
            user_id=USER_ID,
            application_id=1,
            username=None,
            vouched_by="—",
            answers_by_field_id={},
        )
