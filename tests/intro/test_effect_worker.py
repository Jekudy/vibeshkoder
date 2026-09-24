from __future__ import annotations

import asyncio
import importlib
import inspect
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.intro.test_contract import PREDKO_ANSWERS


CHAT_ID = -100_123_456_7890


class RecordingBot:
    def __init__(self, responses: list[object] | None = None) -> None:
        self.responses = list(responses or [SimpleNamespace(message_id=7001)])
        self.calls: list[dict] = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class BlockingBot(RecordingBot):
    def __init__(self) -> None:
        super().__init__([SimpleNamespace(message_id=7001), SimpleNamespace(message_id=7002)])
        self.dispatched = asyncio.Event()
        self.release = asyncio.Event()

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        self.dispatched.set()
        await self.release.wait()
        return self.responses.pop(0)


def _user_id() -> int:
    return 7_000_000_000 + uuid.uuid4().int % 1_000_000_000


class EffectTestDatabase:
    def __init__(self, sessions) -> None:
        self.sessions = sessions
        self.user_ids: list[int] = []

    def user_id(self) -> int:
        user_id = _user_id()
        self.user_ids.append(user_id)
        return user_id

    async def cleanup(self) -> None:
        if not self.user_ids:
            return
        import bot.db.models as models
        from bot.db.models import (
            Application,
            Intro,
            IntroEffectOutbox,
            IntroRefreshTracking,
            QuestionnaireAnswer,
            User,
        )

        async with self.sessions.begin() as session:
            application_ids = select(Application.id).where(Application.user_id.in_(self.user_ids))
            reconciliation = getattr(models, "IntroEffectReconciliation", None)
            if reconciliation is not None:
                effect_ids = select(IntroEffectOutbox.id).where(
                    IntroEffectOutbox.application_id.in_(application_ids)
                )
                await session.execute(
                    delete(reconciliation).where(reconciliation.effect_id.in_(effect_ids))
                )
            await session.execute(
                delete(IntroEffectOutbox).where(
                    IntroEffectOutbox.application_id.in_(application_ids)
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
async def effect_test_db(postgres_engine):
    sessions = async_sessionmaker(bind=postgres_engine, class_=AsyncSession, expire_on_commit=False)
    database = EffectTestDatabase(sessions)
    await database.cleanup()
    try:
        yield database
    finally:
        await database.cleanup()


async def _confirmed_application(
    session: AsyncSession,
    *,
    user_id: int,
    flow_kind: str = "refresh",
    base_id: int | None = None,
):
    from bot.db.models import Application, User

    if await session.get(User, user_id) is None:
        session.add(
            User(
                id=user_id,
                username=f"effect_{user_id}",
                first_name="Effect",
                last_name=None,
                is_member=True,
            )
        )
        await session.flush()
    application = Application(
        user_id=user_id,
        status="confirmed",
        flow_kind=flow_kind,
        base_application_id=base_id,
        catalog_version="intro-v2",
        confirmed_intro_html="\n".join(f"{field}: {value}" for field, value in PREDKO_ANSWERS),
    )
    session.add(application)
    await session.flush()
    return application


async def _effect(
    session: AsyncSession, application_id: int, kind: str, *, status: str = "pending"
):
    from bot.db.models import IntroEffectOutbox

    effect = IntroEffectOutbox(
        application_id=application_id,
        effect_kind=kind,
        status=status,
        attempt_count=1 if status == "processing" else 0,
        attempt_started_at=datetime.now(timezone.utc) if status == "processing" else None,
    )
    session.add(effect)
    await session.flush()
    return effect


def _worker_with_test_database(monkeypatch, effect_test_db):
    worker = importlib.import_module("bot.services.intro_effect_worker")
    monkeypatch.setattr(worker, "async_session", effect_test_db.sessions)
    return worker, effect_test_db.sessions


@pytest.mark.asyncio
async def test_claim_is_committed_before_telegram_io_and_sent_effect_is_never_reclaimed(
    app_env, effect_test_db, monkeypatch
) -> None:
    worker, sessions = _worker_with_test_database(monkeypatch, effect_test_db)
    async with sessions.begin() as session:
        application = await _confirmed_application(
            session, user_id=effect_test_db.user_id(), flow_kind="admission"
        )
        effect = await _effect(session, application.id, "candidate_card")
        second_application = await _confirmed_application(
            session, user_id=effect_test_db.user_id(), flow_kind="admission"
        )
        second = await _effect(session, second_application.id, "candidate_card")
        effect_id = effect.id
        second_id = second.id

    bot = BlockingBot()
    task = asyncio.create_task(worker.process_intro_effects(bot, max_effects=2))
    try:
        await asyncio.wait_for(bot.dispatched.wait(), timeout=2)
        async with sessions() as observer:
            claimed = await observer.get(type(effect), effect_id)
            assert claimed.status == "processing"
            assert claimed.attempt_count == 1
            assert claimed.attempt_started_at is not None
            untouched = await observer.get(type(second), second_id)
            assert untouched.status == "pending"
            assert untouched.attempt_count == 0
    finally:
        bot.release.set()
        await task
    async with sessions() as observer:
        delivered = await observer.get(type(effect), effect_id)
        delivered_second = await observer.get(type(second), second_id)
        assert delivered.status == "sent"
        assert delivered_second.status == "sent"
    await worker.process_intro_effects(bot, max_effects=2)
    assert len(bot.calls) == 2


@pytest.mark.asyncio
async def test_tick_reaper_marks_expired_telegram_unknown_and_sheet_pending(
    app_env, effect_test_db, monkeypatch
) -> None:
    worker, sessions = _worker_with_test_database(monkeypatch, effect_test_db)
    async with sessions.begin() as session:
        application = await _confirmed_application(session, user_id=effect_test_db.user_id())
        telegram = await _effect(session, application.id, "refresh_intro", status="processing")
        sheet = await _effect(session, application.id, "sheet_projection", status="processing")
        for row in (telegram, sheet):
            row.attempt_started_at = datetime.now(timezone.utc) - timedelta(minutes=31)
        telegram_id, sheet_id = telegram.id, sheet.id

    await worker.process_intro_effects(RecordingBot(), max_effects=0)
    async with sessions() as observer:
        assert (await observer.get(type(telegram), telegram_id)).status == "unknown"
        assert (await observer.get(type(sheet), sheet_id)).status == "pending"


@pytest.mark.asyncio
async def test_tick_without_sheet_projector_skips_old_sheet_and_sends_later_telegram(
    app_env, effect_test_db, monkeypatch
) -> None:
    from bot.db.models import Intro

    worker, sessions = _worker_with_test_database(monkeypatch, effect_test_db)
    async with sessions.begin() as session:
        sheet_application = await _confirmed_application(
            session, user_id=effect_test_db.user_id(), flow_kind="admission"
        )
        sheet_application.status = "added"
        session.add(
            Intro(
                user_id=sheet_application.user_id,
                application_id=sheet_application.id,
                intro_text="current intro",
                vouched_by_name="@voucher",
            )
        )
        sheet = await _effect(session, sheet_application.id, "sheet_projection")
        candidate = await _confirmed_application(
            session, user_id=effect_test_db.user_id(), flow_kind="admission"
        )
        telegram = await _effect(session, candidate.id, "candidate_card")
        sheet_id, telegram_id = sheet.id, telegram.id
    bot = RecordingBot([SimpleNamespace(message_id=7003)])

    await worker.process_intro_effects(bot, max_effects=2)

    async with sessions() as observer:
        sheet = await observer.get(type(sheet), sheet_id)
        telegram = await observer.get(type(telegram), telegram_id)
        assert (sheet.status, sheet.attempt_count) == ("pending", 0)
        assert telegram.status == "sent"
    assert len(bot.calls) == 1


@pytest.mark.asyncio
async def test_semantically_invalid_effect_is_stale_before_io_with_zero_bot_calls(
    app_env, effect_test_db, monkeypatch
) -> None:
    worker, sessions = _worker_with_test_database(monkeypatch, effect_test_db)
    async with sessions.begin() as session:
        application = await _confirmed_application(session, user_id=effect_test_db.user_id())
        effect = await _effect(session, application.id, "candidate_card")
        effect_id = effect.id
    bot = RecordingBot()

    await worker.process_intro_effects(bot)
    async with sessions() as observer:
        row = await observer.get(type(effect), effect_id)
        assert row.status == "stale"
    assert bot.calls == []


@pytest.mark.asyncio
async def test_bad_request_and_forbidden_are_terminal(app_env, effect_test_db, monkeypatch) -> None:
    from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

    worker, sessions = _worker_with_test_database(monkeypatch, effect_test_db)
    async with sessions.begin() as session:
        candidate_app = await _confirmed_application(
            session, user_id=effect_test_db.user_id(), flow_kind="admission"
        )
        bad_request = await _effect(session, candidate_app.id, "candidate_card")
        admission_app = await _confirmed_application(
            session, user_id=effect_test_db.user_id(), flow_kind="admission"
        )
        admission_app.status = "added"
        forbidden = await _effect(session, admission_app.id, "admission_intro")
        ids = [bad_request.id, forbidden.id]

    await worker.process_intro_effects(
        RecordingBot(
            [TelegramBadRequest(None, "bad request"), TelegramForbiddenError(None, "blocked")]
        )
    )
    async with sessions() as observer:
        assert [(await observer.get(type(bad_request), effect_id)).status for effect_id in ids] == [
            "failed",
            "failed",
        ]
        assert (
            await observer.get(type(candidate_app), candidate_app.id)
        ).status == "delivery_failed"
        assert (await observer.get(type(admission_app), admission_app.id)).status == "added"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "network_error",
    ["response lost", "connection lost"],
)
async def test_telegram_network_error_becomes_unknown_and_never_blind_retries(
    app_env, effect_test_db, monkeypatch, network_error
) -> None:
    from aiogram.exceptions import TelegramNetworkError

    worker, sessions = _worker_with_test_database(monkeypatch, effect_test_db)
    async with sessions.begin() as session:
        application = await _confirmed_application(
            session, user_id=effect_test_db.user_id(), flow_kind="admission"
        )
        effect = await _effect(session, application.id, "candidate_card")
        effect_id = effect.id
    bot = RecordingBot([TelegramNetworkError(None, network_error)])

    await worker.process_intro_effects(bot)
    await worker.process_intro_effects(bot)
    async with sessions() as observer:
        assert (await observer.get(type(effect), effect_id)).status == "unknown"
    assert len(bot.calls) == 1


@pytest.mark.asyncio
async def test_retry_after_is_bounded_then_fails_delivery(
    app_env, effect_test_db, monkeypatch
) -> None:
    from aiogram.exceptions import TelegramRetryAfter

    worker, sessions = _worker_with_test_database(monkeypatch, effect_test_db)
    async with sessions.begin() as session:
        application = await _confirmed_application(
            session, user_id=effect_test_db.user_id(), flow_kind="admission"
        )
        effect = await _effect(session, application.id, "candidate_card")
        effect_id, application_id = effect.id, application.id
    bot = RecordingBot(
        [
            TelegramRetryAfter(None, "retry later", retry_after=1),
            TelegramRetryAfter(None, "retry later", retry_after=1),
        ]
    )

    await worker.process_intro_effects(bot, max_pre_dispatch_attempts=2)
    await worker.process_intro_effects(bot, max_pre_dispatch_attempts=2)
    async with sessions() as observer:
        effect = await observer.get(type(effect), effect_id)
        application = await observer.get(type(application), application_id)
        assert (effect.status, effect.attempt_count) == ("failed", 2)
        assert application.status == "delivery_failed"


@pytest.mark.asyncio
async def test_unexpected_error_reraises_and_leaves_claim_processing(
    app_env, effect_test_db, monkeypatch
) -> None:
    worker, sessions = _worker_with_test_database(monkeypatch, effect_test_db)
    async with sessions.begin() as session:
        application = await _confirmed_application(
            session, user_id=effect_test_db.user_id(), flow_kind="admission"
        )
        effect = await _effect(session, application.id, "candidate_card")
        effect_id = effect.id

    with pytest.raises(RuntimeError, match="unexpected"):
        await worker.process_intro_effects(RecordingBot([RuntimeError("unexpected")]))
    async with sessions() as observer:
        assert (await observer.get(type(effect), effect_id)).status == "processing"


@pytest.mark.asyncio
async def test_cancellation_persists_unknown_then_reraises_without_retry(
    app_env, effect_test_db, monkeypatch
) -> None:
    worker, sessions = _worker_with_test_database(monkeypatch, effect_test_db)
    async with sessions.begin() as session:
        application = await _confirmed_application(
            session, user_id=effect_test_db.user_id(), flow_kind="admission"
        )
        effect = await _effect(session, application.id, "candidate_card")
        effect_id = effect.id
    bot = RecordingBot([asyncio.CancelledError()])

    with pytest.raises(asyncio.CancelledError):
        await worker.process_intro_effects(bot)
    async with sessions() as observer:
        assert (await observer.get(type(effect), effect_id)).status == "unknown"
    await worker.process_intro_effects(bot)
    assert len(bot.calls) == 1


@pytest.mark.asyncio
async def test_refresh_uses_frozen_html_and_records_telegram_identity(
    app_env, effect_test_db, monkeypatch
) -> None:
    from bot.db.models import Intro

    worker, sessions = _worker_with_test_database(monkeypatch, effect_test_db)
    async with sessions.begin() as session:
        user_id = effect_test_db.user_id()
        base = await _confirmed_application(session, user_id=user_id, flow_kind="admission")
        base.status = "added"
        session.add(
            Intro(
                user_id=user_id,
                application_id=base.id,
                intro_text="old frozen intro",
                vouched_by_name="@voucher",
            )
        )
        application = await _confirmed_application(
            session, user_id=user_id, flow_kind="refresh", base_id=base.id
        )
        effect = await _effect(session, application.id, "refresh_intro")
        effect_id = effect.id
    bot = RecordingBot([SimpleNamespace(message_id=991)])

    await worker.process_intro_effects(bot)
    async with sessions() as observer:
        delivered = await observer.get(type(effect), effect_id)
        assert (delivered.status, delivered.chat_id, delivered.message_id) == ("sent", CHAT_ID, 991)
    assert "Обновлённое интро" in bot.calls[0]["text"]
    assert application.confirmed_intro_html in bot.calls[0]["text"]


@pytest.mark.asyncio
async def test_candidate_card_success_is_idempotent_and_moves_only_its_application_to_pending(
    app_env, effect_test_db, monkeypatch
) -> None:
    worker, sessions = _worker_with_test_database(monkeypatch, effect_test_db)
    async with sessions.begin() as session:
        application = await _confirmed_application(
            session, user_id=effect_test_db.user_id(), flow_kind="admission"
        )
        effect = await _effect(session, application.id, "candidate_card")
        application_id, effect_id = application.id, effect.id
    bot = RecordingBot([SimpleNamespace(message_id=887)])

    await worker.process_intro_effects(bot)
    await worker.process_intro_effects(bot)
    async with sessions() as observer:
        application = await observer.get(type(application), application_id)
        effect = await observer.get(type(effect), effect_id)
        assert (application.status, application.questionnaire_message_id) == ("pending", 887)
        assert application.submitted_at is not None
        assert effect.status == "sent"
    assert len(bot.calls) == 1


def test_reconciliation_has_no_telegram_parameter(app_env) -> None:
    from bot.services.intro_effect_worker import reconcile_intro_effect

    assert "bot" not in inspect.signature(reconcile_intro_effect).parameters


@pytest.mark.asyncio
async def test_poisoned_structural_snapshot_fails_and_does_not_block_next_effect_in_tick(
    app_env, effect_test_db, monkeypatch
) -> None:
    worker, sessions = _worker_with_test_database(monkeypatch, effect_test_db)
    async with sessions.begin() as session:
        poisoned = await _confirmed_application(
            session, user_id=effect_test_db.user_id(), flow_kind="admission"
        )
        poisoned.status = "added"
        poisoned.flow_kind = None
        poisoned.catalog_version = "legacy-v1"
        poisoned.confirmed_intro_html = None
        poisoned_effect = await _effect(session, poisoned.id, "admission_intro")
        valid = await _confirmed_application(
            session, user_id=effect_test_db.user_id(), flow_kind="admission"
        )
        valid_effect = await _effect(session, valid.id, "candidate_card")
        poisoned_id, valid_id = poisoned_effect.id, valid_effect.id

    bot = RecordingBot([SimpleNamespace(message_id=991)])
    await worker.process_intro_effects(bot, max_effects=2)

    async with sessions() as observer:
        assert (await observer.get(type(poisoned_effect), poisoned_id)).status == "failed"
        assert (await observer.get(type(valid_effect), valid_id)).status == "sent"
    assert len(bot.calls) == 1


@pytest.mark.asyncio
async def test_reconciliation_rejects_overlong_reason_without_state_or_audit_change(
    app_env, effect_test_db, monkeypatch
) -> None:
    from bot.db.models import IntroEffectReconciliation, User
    from bot.services.intro_effect_worker import IntroEffectReconcileError, reconcile_intro_effect

    _worker, sessions = _worker_with_test_database(monkeypatch, effect_test_db)
    operator_id = effect_test_db.user_id()
    async with sessions.begin() as session:
        application = await _confirmed_application(
            session, user_id=effect_test_db.user_id(), flow_kind="admission"
        )
        effect = await _effect(session, application.id, "candidate_card", status="processing")
        effect.status = "unknown"
        session.add(
            User(
                id=operator_id,
                username="operator",
                first_name="Operator",
                last_name=None,
                is_admin=True,
            )
        )
        effect_id = effect.id

    async with sessions.begin() as session:
        with pytest.raises(IntroEffectReconcileError):
            await reconcile_intro_effect(
                session,
                effect_id=effect_id,
                action="retry-absent",
                evidence_sha256="a" * 64,
                operator_user_id=operator_id,
                reason="x" * 501,
            )
        effect = await session.get(type(effect), effect_id)
        audits = list(
            (
                await session.execute(
                    select(IntroEffectReconciliation).where(
                        IntroEffectReconciliation.effect_id == effect_id
                    )
                )
            ).scalars()
        )
        assert effect.status == "unknown"
        assert audits == []
