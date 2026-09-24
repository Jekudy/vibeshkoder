from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Awaitable, Callable

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.engine import async_session
from bot.db.models import (
    Application,
    Intro,
    IntroEffectOutbox,
    IntroRefreshTracking,
    QuestionnaireAnswer,
    User,
)
from bot.db.repos.intro import IntroRepo
from bot.db.repos.intro_effect_outbox import IntroEffectOutboxRepo
from bot.keyboards.inline import vouch_keyboard
from bot.services.intro_contract import get_intro_catalog
from bot.services.sheets import SheetProjectionError

logger = logging.getLogger(__name__)

INTRO_EFFECT_BATCH_SIZE = 10
MAX_PRE_DISPATCH_ATTEMPTS = 5
PROCESSING_TIMEOUT_MINUTES = 30
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class IntroEffectReconcileError(ValueError):
    """Raised when an operator reconciliation is not a safe state transition."""


@dataclass(frozen=True)
class ClaimedEffect:
    effect_id: int
    attempt_count: int
    application_id: int
    effect_kind: str
    user_id: int
    confirmed_intro_html: str
    first_name: str
    answers_by_field_id: dict[str, str]
    username: str | None
    voucher_name: str
    snapshot_error: str | None = None


def _voucher_name(application: Application, voucher: User | None) -> str:
    if application.vouched_by is None or voucher is None:
        return "—"
    return f"@{voucher.username}" if voucher.username else voucher.first_name


async def _snapshot_claim(session: AsyncSession, effect: IntroEffectOutbox) -> ClaimedEffect:
    application = await session.get(Application, effect.application_id)
    if application is None or application.confirmed_intro_html is None:
        raise IntroEffectReconcileError("Effect application has no frozen snapshot")
    user = await session.get(User, application.user_id)
    if user is None:
        raise IntroEffectReconcileError("Effect application has no user")
    voucher = await session.get(User, application.vouched_by) if application.vouched_by else None
    current_intro = await session.scalar(
        select(Intro).where(
            Intro.user_id == application.user_id,
            Intro.application_id == application.id,
        )
    )
    answers = list(
        (
            await session.execute(
                select(QuestionnaireAnswer).where(
                    QuestionnaireAnswer.application_id == application.id,
                    QuestionnaireAnswer.is_current.is_(True),
                )
            )
        ).scalars()
    )
    answers_by_field_id: dict[str, str] = {}
    if effect.effect_kind == "sheet_projection":
        expected_field_ids = {
            field.field_id for field in get_intro_catalog(application.catalog_version)
        }
        answers_by_field_id = {
            answer.field_id: answer.answer_text
            for answer in answers
            if answer.field_id in expected_field_ids
        }
    return ClaimedEffect(
        effect_id=effect.id,
        attempt_count=effect.attempt_count,
        application_id=application.id,
        effect_kind=effect.effect_kind,
        user_id=application.user_id,
        confirmed_intro_html=application.confirmed_intro_html,
        first_name=user.first_name,
        answers_by_field_id=answers_by_field_id,
        username=user.username,
        voucher_name=(
            current_intro.vouched_by_name
            if current_intro is not None
            else _voucher_name(application, voucher)
        ),
    )


def _telegram_payload(effect: ClaimedEffect) -> dict:
    if effect.effect_kind == "candidate_card":
        identity = f"{escape(effect.first_name)} (@{escape(effect.username or '')})"
        return {
            "chat_id": settings.COMMUNITY_CHAT_ID,
            "text": f"📋 Новая анкета от {identity}\n\n{effect.confirmed_intro_html}",
            "parse_mode": "HTML",
            "reply_markup": vouch_keyboard(effect.application_id),
        }
    if effect.effect_kind == "admission_intro":
        header = (
            f"🎉 Новый участник: {escape(effect.first_name)} (@{escape(effect.username or '')})\n"
            f"Поручился: {escape(effect.voucher_name)}"
        )
    else:
        header = "🔄 Обновлённое интро" if effect.effect_kind == "refresh_intro" else "🎉 Интро"
    return {
        "chat_id": settings.COMMUNITY_CHAT_ID,
        "text": f"{header}\n\n{effect.confirmed_intro_html}",
        "parse_mode": "HTML",
    }


def _valid_semantic_pair(effect: IntroEffectOutbox, application: Application) -> bool:
    pairs = {
        "candidate_card": ("admission", "confirmed"),
        "admission_intro": ("admission", "added"),
        "member_intro": ("refresh", "confirmed"),
        "refresh_intro": ("refresh", "confirmed"),
        "sheet_projection": ("published", "added"),
    }
    expected = pairs.get(effect.effect_kind)
    if expected is None:
        return False
    if effect.effect_kind == "sheet_projection":
        return application.status == "added" and application.flow_kind in {"admission", "refresh"}
    return (application.flow_kind, application.status) == expected


async def _claim_is_semantically_valid(session: AsyncSession, claimed: ClaimedEffect) -> bool:
    effect = await IntroEffectOutboxRepo.get_claimed_for_update(
        session, effect_id=claimed.effect_id, attempt_count=claimed.attempt_count
    )
    if effect is None:
        return False
    application = await session.get(Application, claimed.application_id, with_for_update=True)
    if application is None or not _valid_semantic_pair(effect, application):
        return False
    user = await session.get(User, claimed.user_id, with_for_update=True)
    if effect.effect_kind in {"member_intro", "refresh_intro"} and (
        user is None or not user.is_member
    ):
        return False
    current = await IntroRepo.get_for_update(session, application.user_id)
    if effect.effect_kind == "refresh_intro":
        return current is not None and current.application_id == application.base_application_id
    if effect.effect_kind in {"admission_intro", "member_intro"}:
        return current is None
    if effect.effect_kind == "sheet_projection":
        return current is not None and current.application_id == application.id
    return True


async def _mark_result(
    session: AsyncSession,
    claimed: ClaimedEffect,
    *,
    status: str,
    error: str | None = None,
) -> IntroEffectOutbox | None:
    effect = await IntroEffectOutboxRepo.get_claimed_for_update(
        session, effect_id=claimed.effect_id, attempt_count=claimed.attempt_count
    )
    if effect is None:
        return None
    effect.status = status
    effect.last_error = error[:500] if error else None
    if status in {"sent", "failed", "stale"}:
        effect.completed_at = datetime.now(timezone.utc)
    await session.flush()
    return effect


async def _mark_delivery_failed(
    session: AsyncSession, claimed: ClaimedEffect, *, status: str, error: str | None = None
) -> IntroEffectOutbox | None:
    effect = await _mark_result(session, claimed, status=status, error=error)
    if effect is None or claimed.effect_kind not in {
        "candidate_card",
        "member_intro",
        "refresh_intro",
    }:
        return effect
    application = await session.get(Application, claimed.application_id, with_for_update=True)
    if application is not None and application.status == "confirmed":
        application.status = "delivery_failed"
        await session.flush()
    return effect


async def _enqueue_projection(session: AsyncSession, application_id: int) -> None:
    await IntroEffectOutboxRepo.enqueue_once(
        session, application_id=application_id, effect_kind="sheet_projection"
    )


async def _finalize_telegram_success(
    session: AsyncSession,
    claimed: ClaimedEffect,
    *,
    chat_id: int,
    message_id: int,
) -> IntroEffectOutbox | None:
    effect = await IntroEffectOutboxRepo.get_claimed_for_update(
        session, effect_id=claimed.effect_id, attempt_count=claimed.attempt_count
    )
    if effect is None:
        return None
    application = await session.get(Application, claimed.application_id, with_for_update=True)
    if application is None or not _valid_semantic_pair(effect, application):
        effect.status = "stale"
        effect.chat_id = chat_id
        effect.message_id = message_id
        effect.completed_at = datetime.now(timezone.utc)
        if (
            application is not None
            and effect.effect_kind in {"candidate_card", "member_intro", "refresh_intro"}
            and application.status == "confirmed"
        ):
            application.status = "delivery_failed"
        await session.flush()
        return effect

    user = await session.get(User, claimed.user_id, with_for_update=True)
    if effect.effect_kind in {"member_intro", "refresh_intro"} and (
        user is None or not user.is_member
    ):
        effect.status = "stale"
        effect.chat_id = chat_id
        effect.message_id = message_id
        effect.completed_at = datetime.now(timezone.utc)
        if application.status == "confirmed":
            application.status = "delivery_failed"
        await session.flush()
        return effect

    effect.chat_id = chat_id
    effect.message_id = message_id
    effect.last_error = None
    effect.completed_at = datetime.now(timezone.utc)

    if effect.effect_kind == "candidate_card":
        application.status = "pending"
        application.questionnaire_message_id = message_id
        application.submitted_at = datetime.now(timezone.utc)
    elif effect.effect_kind == "admission_intro":
        current = await IntroRepo.get_for_update(session, application.user_id)
        if current is not None:
            effect.status = "stale"
            await session.flush()
            return effect
        session.add(
            Intro(
                user_id=application.user_id,
                application_id=application.id,
                intro_text=claimed.confirmed_intro_html,
                vouched_by_name=claimed.voucher_name,
            )
        )
        await _enqueue_projection(session, application.id)
    elif effect.effect_kind == "member_intro":
        current = await IntroRepo.get_for_update(session, application.user_id)
        if current is not None:
            effect.status = "stale"
            if application.status == "confirmed":
                application.status = "delivery_failed"
            await session.flush()
            return effect
        application.status = "added"
        session.add(
            Intro(
                user_id=application.user_id,
                application_id=application.id,
                intro_text=claimed.confirmed_intro_html,
                vouched_by_name=claimed.voucher_name,
            )
        )
        await _enqueue_projection(session, application.id)
    elif effect.effect_kind == "refresh_intro":
        current = await IntroRepo.get_for_update(session, application.user_id)
        if current is None or current.application_id != application.base_application_id:
            effect.status = "stale"
            if application.status == "confirmed":
                application.status = "delivery_failed"
            await session.flush()
            return effect
        promoted = await IntroRepo.promote_if_current(
            session,
            user_id=application.user_id,
            base_application_id=application.base_application_id,
            application_id=application.id,
            intro_text=claimed.confirmed_intro_html,
        )
        if not promoted:
            effect.status = "stale"
            if application.status == "confirmed":
                application.status = "delivery_failed"
            await session.flush()
            return effect
        application.status = "added"
        await session.execute(
            update(IntroRefreshTracking)
            .where(
                IntroRefreshTracking.user_id == application.user_id,
                IntroRefreshTracking.completed.is_(False),
            )
            .values(completed=True, phase="done")
        )
        await _enqueue_projection(session, application.id)
    else:
        effect.status = "stale"
        await session.flush()
        return effect

    effect.status = "sent"
    await session.flush()
    return effect


async def _finalize_sheet_success(
    session: AsyncSession, claimed: ClaimedEffect
) -> IntroEffectOutbox | None:
    effect = await IntroEffectOutboxRepo.get_claimed_for_update(
        session, effect_id=claimed.effect_id, attempt_count=claimed.attempt_count
    )
    if effect is None:
        return None
    current = await IntroRepo.get_for_update(session, claimed.user_id)
    if current is None or current.application_id != claimed.application_id:
        effect.status = "stale"
        effect.completed_at = datetime.now(timezone.utc)
        if current is not None and current.application_id is not None:
            await IntroEffectOutboxRepo.ensure_projection_pending(
                session, application_id=current.application_id
            )
    else:
        effect.status = "sent"
        effect.completed_at = datetime.now(timezone.utc)
    await session.flush()
    return effect


async def _finalize_with_new_session(
    finalize: Callable[[AsyncSession], Awaitable[object]],
) -> object:
    async with async_session.begin() as session:
        return await finalize(session)


async def _process_claimed(
    bot,
    claimed: ClaimedEffect,
    *,
    max_pre_dispatch_attempts: int,
    project_sheet: Callable[..., Awaitable[None]] | None,
) -> None:
    if claimed.snapshot_error is not None:
        await _finalize_with_new_session(
            lambda db: _mark_result(db, claimed, status="failed", error=claimed.snapshot_error)
        )
        return

    valid = await _finalize_with_new_session(lambda db: _claim_is_semantically_valid(db, claimed))
    if not valid:
        await _finalize_with_new_session(
            lambda db: _mark_delivery_failed(db, claimed, status="stale")
        )
        return

    if claimed.effect_kind == "sheet_projection":
        if project_sheet is None:
            await _finalize_with_new_session(lambda db: _mark_result(db, claimed, status="pending"))
            return
        try:
            await project_sheet(
                user_id=claimed.user_id,
                application_id=claimed.application_id,
                username=(f"@{claimed.username}" if claimed.username else None),
                vouched_by=claimed.voucher_name,
                answers_by_field_id=claimed.answers_by_field_id,
            )
        except asyncio.CancelledError:
            await _finalize_with_new_session(
                lambda db: _mark_result(db, claimed, status="pending", error="cancelled")
            )
            raise
        except SheetProjectionError as error:
            status = "failed" if claimed.attempt_count >= max_pre_dispatch_attempts else "pending"
            error_text = str(error)
            await _finalize_with_new_session(
                lambda db: _mark_result(db, claimed, status=status, error=error_text)
            )
            return
        await _finalize_with_new_session(lambda db: _finalize_sheet_success(db, claimed))
        return

    try:
        response = await bot.send_message(**_telegram_payload(claimed))
    except asyncio.CancelledError:
        await _finalize_with_new_session(
            lambda db: _mark_result(db, claimed, status="unknown", error="cancelled")
        )
        raise
    except (TelegramBadRequest, TelegramForbiddenError) as error:
        error_text = str(error)
        await _finalize_with_new_session(
            lambda db: _mark_delivery_failed(db, claimed, status="failed", error=error_text)
        )
        return
    except TelegramRetryAfter as error:
        error_text = str(error)
        status = "failed" if claimed.attempt_count >= max_pre_dispatch_attempts else "pending"
        finalizer = _mark_delivery_failed if status == "failed" else _mark_result
        await _finalize_with_new_session(
            lambda db: finalizer(db, claimed, status=status, error=error_text)
        )
        return
    except TelegramNetworkError as error:
        error_text = str(error)
        await _finalize_with_new_session(
            lambda db: _mark_result(db, claimed, status="unknown", error=error_text)
        )
        return
    await _finalize_with_new_session(
        lambda db: _finalize_telegram_success(
            db,
            claimed,
            chat_id=settings.COMMUNITY_CHAT_ID,
            message_id=response.message_id,
        )
    )


async def _claim_one(
    session: AsyncSession,
    *,
    high_watermark: int | None,
    excluded_ids: set[int],
    include_sheet: bool,
) -> ClaimedEffect | None:
    effects = await IntroEffectOutboxRepo.claim_pending(
        session,
        limit=1,
        include_sheet=include_sheet,
        high_watermark=high_watermark,
        excluded_ids=excluded_ids,
    )
    if not effects:
        return None
    effect = effects[0]
    try:
        return await _snapshot_claim(session, effect)
    except IntroEffectReconcileError as error:
        return ClaimedEffect(
            effect_id=effect.id,
            attempt_count=effect.attempt_count,
            application_id=effect.application_id,
            effect_kind=effect.effect_kind,
            user_id=0,
            confirmed_intro_html="",
            first_name="",
            answers_by_field_id={},
            username=None,
            voucher_name="",
            snapshot_error=str(error),
        )


async def process_intro_effects(
    bot,
    *,
    max_effects: int = INTRO_EFFECT_BATCH_SIZE,
    max_pre_dispatch_attempts: int = MAX_PRE_DISPATCH_ATTEMPTS,
    project_sheet: Callable[..., Awaitable[None]] | None = None,
) -> None:
    """Claim exactly one durable effect immediately before each external call."""
    async with async_session.begin() as session:
        await IntroEffectOutboxRepo.mark_stale_processing_unknown(
            session,
            older_than=datetime.now(timezone.utc) - timedelta(minutes=PROCESSING_TIMEOUT_MINUTES),
        )
        high_watermark = await IntroEffectOutboxRepo.pending_high_watermark(session)
    claimed_ids: set[int] = set()
    for _ in range(max_effects):
        async with async_session.begin() as session:
            effect = await _claim_one(
                session,
                high_watermark=high_watermark,
                excluded_ids=claimed_ids,
                include_sheet=project_sheet is not None,
            )
        if effect is None:
            return
        claimed_ids.add(effect.effect_id)
        await _process_claimed(
            bot,
            effect,
            max_pre_dispatch_attempts=max_pre_dispatch_attempts,
            project_sheet=project_sheet,
        )


async def reconcile_intro_effect(
    session: AsyncSession,
    *,
    effect_id: int,
    action: str,
    chat_id: int | None = None,
    message_id: int | None = None,
    evidence_sha256: str | None = None,
    operator_user_id: int,
    reason: str,
) -> IntroEffectOutbox:
    if action not in {"record-sent", "retry-absent"}:
        raise IntroEffectReconcileError("unknown reconciliation action")
    reason = reason.strip()
    if not 1 <= len(reason) <= 500:
        raise IntroEffectReconcileError("reason is required")
    operator = await session.get(User, operator_user_id)
    if operator is None or (operator_user_id not in settings.ADMIN_IDS and not operator.is_admin):
        raise IntroEffectReconcileError("operator is not an admin")
    if action == "record-sent":
        if (
            chat_id != settings.COMMUNITY_CHAT_ID
            or message_id is None
            or message_id <= 0
            or evidence_sha256 is not None
        ):
            raise IntroEffectReconcileError("record-sent requires only a valid community identity")
    elif (
        chat_id is not None
        or message_id is not None
        or evidence_sha256 is None
        or _SHA256.fullmatch(evidence_sha256) is None
    ):
        raise IntroEffectReconcileError("retry-absent requires only lowercase SHA-256 evidence")
    effect = await session.scalar(
        select(IntroEffectOutbox).where(IntroEffectOutbox.id == effect_id).with_for_update()
    )
    if (
        effect is None
        or effect.status != "unknown"
        or effect.attempt_count <= 0
        or effect.effect_kind == "sheet_projection"
    ):
        raise IntroEffectReconcileError("only unknown effects can be reconciled")
    from bot.db.models import IntroEffectReconciliation

    if action == "retry-absent":
        effect.status = "pending"
        effect.last_error = None
    elif action == "record-sent":
        effect.status = "processing"
        claimed = await _snapshot_claim(session, effect)
        finalized = await _finalize_telegram_success(
            session, claimed, chat_id=chat_id, message_id=message_id
        )
        if finalized is None:
            raise IntroEffectReconcileError("effect finalization lost its attempt identity")
        effect = finalized
    session.add(
        IntroEffectReconciliation(
            effect_id=effect_id,
            action=action,
            operator_user_id=operator_user_id,
            reason=reason,
            evidence_sha256=evidence_sha256,
            attempt_count=effect.attempt_count,
            chat_id=chat_id,
            message_id=message_id,
        )
    )
    await session.flush()
    logger.info(
        "intro_effect_reconciled",
        extra={
            "effect_id": effect_id,
            "action": action,
            "operator_user_id": operator_user_id,
            "reason": reason,
            "evidence_sha256": evidence_sha256,
            "identity": (f"{chat_id}:{message_id}" if chat_id is not None else None),
        },
    )
    return effect
