"""Phase 12 — Butler Telegram handlers (T12-05).

Commands
--------
/butler <request>
    DM-only, member-only. Plans an action via ButlerService.plan_action and
    sends an inline-keyboard preview. Each pending confirmation gets its own
    preview message with Confirm/Cancel buttons.

/butler_status <action_id>
    Returns current action state + plan summary.

/butler_cancel <action_id>
    Cancels a pending/confirmed action (requester or admin only).

/butler_undo <action_id>
    Undo a previously-succeeded action via its inverse op (T12-07). Writes a
    linked child butler_actions row; the original audit row is immutable.

Inline keyboard callbacks
-------------------------
butler_confirm:<action_id>:<token>
    Confirm a pending_confirmation action.

butler_cancel_cb:<action_id>
    Cancel via inline keyboard (Cancel button on the preview message).

butler_affected_approve:<action_id>:<token>
    Affected user approves cross-user consent.

butler_affected_reject:<action_id>:<token>
    Affected user rejects cross-user consent.

Design decisions (binding per PHASE12_PLAN_REFRESH.md)
------------------------------------------------------
- DM-only baseline (§3.1): /butler is only valid in private chat with the bot.
- Auth = membership (§3.3): UserRepo.get + is_member or is_admin pattern
  from bot/handlers/qa.py:369 and bot/handlers/forward_lookup.py:46.
- Feature flag gate: memory.butler.enabled (default OFF). Master flag checked
  first; no service call when flag is OFF.
- No admin override of cross-user consent (§3.5/§10 decision 5).
- 8 §5.B rejection paths all surface as polite user-facing messages — NO
  internal state leaked.
- Module-level imports for all ButlerActionError subclasses (prevents class
  identity issues in combined-mode test runs — see commit db33b1c).
- Privacy: log entries never contain raw user content or query text.
- Non-members: silently rejected — no confirmation bot exists (§3.3).

Stub wiring (T12-05 scope)
--------------------------
ButlerService requires several collaborators (llm_gateway, evidence_builder,
repos). In T12-05 the handler builds them from concrete implementations where
available or uses lightweight stubs for parts that land in T12-06/T12-08.
The service wiring is isolated in _build_butler_service() so T12-06/T12-08
can replace stubs with real implementations without touching handler logic.
"""

from __future__ import annotations

import html
import logging
from typing import Any

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.repos.butler_action import ButlerActionRepo
from bot.db.repos.butler_action_confirmation import ButlerActionConfirmationRepo
from bot.db.repos.butler_rate_bucket import ButlerRateBucketRepo
from bot.db.repos.butler_tool_invocation import ButlerToolInvocationRepo
from bot.db.repos.feature_flag import FeatureFlagRepo
from bot.db.repos.user import UserRepo

# Module-level imports for all ButlerActionError subclasses — prevents class
# identity issues in combined-mode CI (see commit db33b1c, memory file
# feedback-codex-defaults.md for context on _clear_modules test isolation).
from bot.services.butler import (
    AffectedUserUnreachableError,
    ButlerActionError,
    ButlerActionExpiredError,
    ButlerActionRejectedError,
    ButlerService,
    ButlerUndoError,
    CascadeInFlightError,
    EvidenceStaleError,
    MembershipRevokedError,
)
from bot.db.repos.extraction_candidate import ExtractionCandidateRepo
from bot.services.butler_evidence import build_butler_evidence
from bot.filters.chat_type import PrivateChatFilter

logger = logging.getLogger(__name__)

router = Router(name="butler")

# Feature flag keys
BUTLER_MASTER_FLAG = "memory.butler.enabled"
BUTLER_RECALL_FLAG = "memory.butler.recall_evidence.enabled"
BUTLER_MEETING_FLAG = "memory.butler.schedule_meeting.enabled"
BUTLER_INTRO_FLAG = "memory.butler.send_intro.enabled"
BUTLER_UPDATE_INTRO_FLAG = "memory.butler.update_intro.enabled"
BUTLER_CARD_FLAG = "memory.butler.suggest_card_creation.enabled"

# Per-tool flag map for gate checks (M2 — enforced after plan_action returns tool name)
_TOOL_FLAGS: dict[str, str] = {
    "recall_evidence": BUTLER_RECALL_FLAG,
    "schedule_meeting": BUTLER_MEETING_FLAG,
    "send_intro": BUTLER_INTRO_FLAG,
    "update_intro": BUTLER_UPDATE_INTRO_FLAG,
    "suggest_card_creation": BUTLER_CARD_FLAG,
}

# ---------------------------------------------------------------------------
# User-facing message strings (§5.B rejection paths)
# ---------------------------------------------------------------------------

_MSG_FLAG_OFF = "Butler ещё не включён. Следите за обновлениями."
_MSG_NOT_DM = "Команда /butler работает только в личных сообщениях с ботом."
_MSG_EMPTY_QUERY = "Использование: /butler <запрос>"
_MSG_RATE_LIMIT = "Вы достигли лимита запросов. Попробуйте позже."
_MSG_EVIDENCE_STALE = (
    "Данные успели измениться — пожалуйста, повторите запрос заново."
)
_MSG_EXPIRED = "Время подтверждения истекло. Повторите /butler."
_MSG_CASCADE_IN_FLIGHT = "Система занята. Попробуйте через несколько секунд."
_MSG_FORBIDDEN = "У вас нет прав для этого действия."
_MSG_NOT_FOUND = "Действие не найдено."
_MSG_BAD_TOKEN = "Ссылка на подтверждение недействительна."
_MSG_ALREADY_CONFIRMED = "Вы уже подтвердили это действие."
_MSG_PLAN_ERROR = "Не удалось спланировать действие. Попробуйте другой запрос."
_MSG_TOOL_DISABLED = "Этот инструмент сейчас недоступен."
_MSG_UNDO_USAGE = "Использование: /butler_undo <action_id>"
_MSG_UNDO_DONE = "✅ Действие отменено (откат выполнен)."
_MSG_UNDO_NOT_REVERSIBLE = (
    "Это действие нельзя откатить автоматически — отмена записана в журнал."
)
_MSG_UNDO_WRONG_STATUS = "Это действие нельзя откатить (уже отменено или не выполнялось)."
_MSG_UNDO_FAILED = "Не удалось выполнить откат. Попробуйте позже."
_MSG_AFFECTED_UNREACHABLE = (
    "Не удалось отправить запрос участнику(ам) — действие не будет выполнено."
)

# §5.B — all 8 error_kind strings mapped to user messages (M1)
_MSG_GOVERNANCE_REJECT = "Действие не прошло проверку допустимости."
_MSG_HALLUCINATED_ARGS = "Butler не смог правильно понять запрос. Попробуйте переформулировать."
_MSG_CROSS_USER_CONSENT_MISSING = "Для этого действия требуется согласие других участников."
_MSG_DRY_RUN_FAILURE = "Тестовый запуск действия не удался. Попробуйте позже."
_MSG_BUDGET_EXCEEDED = "Достигнут бюджетный лимит. Попробуйте позже."
_MSG_TTL_EXPIRED = "Время ожидания истекло. Повторите /butler."
_MSG_PLAN_FAILED = "Планирование действия завершилось ошибкой. Попробуйте другой запрос."

# Cross-user consent messages
_MSG_CONSENT_REQUEST_PREFIX = "🔔 Запрос на действие от участника сообщества:\n\n"
_MSG_CONSENT_APPROVED = "Вы одобрили запрос. Ожидайте выполнения."
_MSG_CONSENT_REJECTED = "Вы отклонили запрос."
_MSG_CONSENT_REVOKED_EFFECT = (
    "❌ Согласие отозвано участником. Действие отменено."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_admin(user_id: int) -> bool:
    return user_id in settings.ADMIN_IDS


def _confirm_keyboard(action_id: int, token: str) -> InlineKeyboardMarkup:
    """Build inline keyboard for requester confirmation."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить",
                    callback_data=f"butler_confirm:{action_id}:{token}",
                ),
                InlineKeyboardButton(
                    text="❌ Отменить",
                    callback_data=f"butler_cancel_cb:{action_id}",
                ),
            ]
        ]
    )


def _consent_keyboard(action_id: int, token: str) -> InlineKeyboardMarkup:
    """Build inline keyboard for affected-user consent."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Одобрить",
                    callback_data=f"butler_affected_approve:{action_id}:{token}",
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=f"butler_affected_reject:{action_id}:{token}",
                ),
            ]
        ]
    )


def _render_preview(action: Any) -> str:
    """Render a human-readable preview of a planned action.

    Reads action.plan_summary (NOT NULL post-migration-074).
    Never reads raw evidence content — only structural metadata from the ORM row.
    HTML-escapes all LLM-generated fields to prevent parse errors (M7).
    """
    plan_summary = html.escape(action.plan_summary)
    action_id = action.id
    tool_name = html.escape(action.tool_name)
    risk_level = action.risk_level
    visibility = html.escape(action.visibility_scope)

    # Map risk_level to a user-facing label
    risk_label = {"low": "низкий", "medium": "средний", "high": "высокий"}.get(
        risk_level, risk_level
    )

    lines = [
        f"🤖 <b>Butler запрос #{action_id}</b>",
        f"Инструмент: <code>{tool_name}</code>",
        f"Риск: {risk_label}",
        f"Видимость: <code>{visibility}</code>",
        "",
        plan_summary,
    ]
    return "\n".join(lines)


def _dispatch_butler_error(exc: ButlerActionError) -> str:
    """Return the correct user-facing message for a ButlerActionError subclass.

    Maps all 8 §5.B error_kind strings to distinct messages (H1 + M1).
    Unknown error_kind → raise invariant_broken (consistent with line ~358 pattern).
    """
    kind = exc.error_kind or ""

    # Named subclasses first
    if isinstance(exc, ButlerActionExpiredError):
        return _MSG_EXPIRED
    if isinstance(exc, EvidenceStaleError):
        return _MSG_EVIDENCE_STALE
    if isinstance(exc, CascadeInFlightError):
        return _MSG_CASCADE_IN_FLIGHT
    if isinstance(exc, ButlerActionRejectedError):
        if kind == "forbidden":
            return _MSG_FORBIDDEN
        if kind == "wrong_status":
            return "Это действие уже нельзя отменить."
        if kind == "affected_user_consent_revoked":
            return _MSG_CONSENT_REVOKED_EFFECT

    # error_kind dispatch — §5.B all 8 paths (M1)
    _kind_map: dict[str, str] = {
        "rate_limit_exceeded": _MSG_RATE_LIMIT,
        "evidence_stale": _MSG_EVIDENCE_STALE,
        "governance_reject": _MSG_GOVERNANCE_REJECT,
        "hallucinated_args": _MSG_HALLUCINATED_ARGS,
        "cross_user_consent_missing": _MSG_CROSS_USER_CONSENT_MISSING,
        "dry_run_failure": _MSG_DRY_RUN_FAILURE,
        "budget_exceeded": _MSG_BUDGET_EXCEEDED,
        "ttl_expired": _MSG_TTL_EXPIRED,
        "plan_failed": _MSG_PLAN_FAILED,
        "not_found": _MSG_NOT_FOUND,
        "bad_token": _MSG_BAD_TOKEN,
        "already_confirmed_by_user": _MSG_ALREADY_CONFIRMED,
        "wrong_status": "Это действие уже нельзя отменить.",
        "forbidden": _MSG_FORBIDDEN,
        "cascade_in_flight": _MSG_CASCADE_IN_FLIGHT,
        "expired": _MSG_EXPIRED,
        "affected_user_consent_revoked": _MSG_CONSENT_REVOKED_EFFECT,
        "undo_failed": _MSG_UNDO_FAILED,
        # invariant_broken is not user-facing — raise to trigger unexpected error handler
    }

    if kind in _kind_map:
        return _kind_map[kind]

    # Completely unknown error_kind — raise invariant_broken
    raise ButlerActionError(
        f"handler: unknown ButlerActionError error_kind={kind!r}",
        error_kind="invariant_broken",
    )


# Module-level cached service builder components (L1 — avoid re-creating stubs on every call)
class _EvidenceBuilderAdapter:
    """Adapter wrapping build_butler_evidence free function as an object."""

    async def build_butler_evidence(self, **kwargs: Any) -> Any:
        return await build_butler_evidence(**kwargs)


class _StubGateway:
    """Stub LLM gateway — will be replaced in T12-03 wiring."""

    async def plan_butler_action(self, **kwargs: Any) -> Any:
        # This path is never reached while memory.butler.enabled=False.
        raise NotImplementedError(
            "LLM gateway for Butler not yet wired (T12-03 scope)"
        )


class _StubSettings:
    butler_plan_ttl_seconds: int = 900
    butler_confirmation_ttl_seconds: int = 300
    user_plans_day_ceiling: int = 10
    user_execs_day_ceiling: int = 5
    chat_actions_day_ceiling: int = 50
    tool_hour_ceiling: int = 20


# Cached singleton instances (L1)
_EVIDENCE_BUILDER = _EvidenceBuilderAdapter()
_STUB_GATEWAY = _StubGateway()
_STUB_SETTINGS = _StubSettings()


def _build_butler_service(session: AsyncSession) -> ButlerService:
    """Construct a ButlerService with concrete repos + stub LLM collaborators.

    The evidence_builder and llm_gateway stubs will be replaced with real
    implementations once T12-02/T12-03 wiring is complete. For T12-05, the
    handler can reach plan_action only when the feature flag is ON, which
    is OFF by default, so the stubs are safe.
    """
    return ButlerService(
        session=session,
        ledger_repo=None,  # type: ignore[arg-type]
        butler_action_repo=ButlerActionRepo,
        butler_action_confirmation_repo=ButlerActionConfirmationRepo,
        butler_tool_invocation_repo=ButlerToolInvocationRepo,
        butler_rate_bucket_repo=ButlerRateBucketRepo,
        user_repo=UserRepo,
        llm_gateway=_STUB_GATEWAY,
        evidence_builder=_EVIDENCE_BUILDER,
        settings=_STUB_SETTINGS,
        extraction_candidate_repo=ExtractionCandidateRepo,
    )


# ---------------------------------------------------------------------------
# /butler command
# ---------------------------------------------------------------------------


@router.message(Command("butler"), PrivateChatFilter())
async def handle_butler(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
) -> None:
    """Handle /butler <request> — DM-only, member-gated, feature-flag-gated."""

    # Feature flag gate (master)
    if not await FeatureFlagRepo.get(session, BUTLER_MASTER_FLAG):
        # Silent no-op when flag is OFF (spec: flag OFF → command silently no-ops)
        return

    if message.from_user is None:
        return

    # Auth check: membership gate
    # H3: non-members are silently rejected — no confirmation bot exists
    user = await UserRepo.get(session, message.from_user.id)
    if user is None or not (
        getattr(user, "is_member", False) or getattr(user, "is_admin", False)
    ):
        # Silent return — no reply (H3: non-members get no confirmation bot exists)
        return

    query = (command.args or "").strip()
    if not query:
        await message.reply(_MSG_EMPTY_QUERY)
        return

    butler = _build_butler_service(session)

    try:
        action = await butler.plan_action(
            requester_user_id=message.from_user.id,
            chat_id=None,  # DM invocation; result may post to target chat
            query=query,
            visibility_scope="member",
        )
    except MembershipRevokedError:
        # Silent return — membership was revoked between check and plan
        return
    except ButlerActionError as exc:
        kind = exc.error_kind or ""
        if kind == "rate_limit_exceeded":
            await message.reply(_MSG_RATE_LIMIT)
        elif kind == "evidence_stale":
            await message.reply(_MSG_EVIDENCE_STALE)
        elif kind == "governance_reject":
            await message.reply(_MSG_GOVERNANCE_REJECT)
        elif kind == "hallucinated_args":
            await message.reply(_MSG_HALLUCINATED_ARGS)
        elif kind == "cross_user_consent_missing":
            await message.reply(_MSG_CROSS_USER_CONSENT_MISSING)
        elif kind == "dry_run_failure":
            await message.reply(_MSG_DRY_RUN_FAILURE)
        elif kind == "budget_exceeded":
            await message.reply(_MSG_BUDGET_EXCEEDED)
        elif kind == "ttl_expired":
            await message.reply(_MSG_TTL_EXPIRED)
        elif kind == "plan_failed":
            await message.reply(_MSG_PLAN_FAILED)
        else:
            logger.info(
                "butler: plan_action rejected",
                extra={"error_kind": kind, "user_id": message.from_user.id},
            )
            await message.reply(_MSG_PLAN_ERROR)
        return
    except Exception:
        logger.exception(
            "butler: unexpected error in plan_action",
            extra={"user_id": message.from_user.id},
        )
        await message.reply(_MSG_PLAN_ERROR)
        return

    # M2: per-tool flag gate — check after plan_action returns the tool name
    tool_flag_key = _TOOL_FLAGS.get(action.tool_name)
    if tool_flag_key is not None:
        tool_enabled = await FeatureFlagRepo.get(session, tool_flag_key)
        if not tool_enabled:
            logger.info(
                "butler: tool disabled by per-tool flag, action rejected",
                extra={"tool_name": action.tool_name, "user_id": message.from_user.id},
            )
            # Explicit rollback required: DbSessionMiddleware commits unconditionally on
            # normal handler return. Without this, the pending butler_actions row created
            # by plan_action (flushed but not yet committed) would survive and linger
            # until TTL expiry — a silent failure. Rollback here before return so the
            # middleware sees a clean session with no work to commit.
            await session.rollback()
            await message.reply(_MSG_TOOL_DISABLED)
            return

    # Render preview and send with confirmation keyboard
    preview_text = _render_preview(action)

    # Get requester confirmation row to find the token
    confirmations = await ButlerActionConfirmationRepo.list_for_action(
        session, action.id
    )
    requester_conf = next(
        (c for c in confirmations if c.confirmation_role == "requester"),
        None,
    )

    if requester_conf is None:
        logger.error(
            "butler: no requester confirmation row after plan_action",
            extra={"action_id": action.id},
        )
        await message.reply(_MSG_PLAN_ERROR)
        return

    # Read confirmation_token directly — NOT NULL post-migration-074.
    # A None value means a schema invariant is broken — raise immediately.
    token = requester_conf.confirmation_token
    if token is None:
        raise ButlerActionError(
            f"action {action.id} confirmation_token is None post-migration-074",
            error_kind="invariant_broken",
            action_id=action.id,
        )

    await message.reply(
        preview_text,
        parse_mode="HTML",
        reply_markup=_confirm_keyboard(action.id, token),
    )

    # Send DMs to affected users for cross-user consent (H2: raise on failure)
    affected_confs = [
        c for c in confirmations if c.confirmation_role == "affected_user"
    ]
    if affected_confs and message.bot is not None:
        try:
            await _send_consent_requests(
                bot=message.bot,
                action=action,
                affected_confirmations=affected_confs,
                preview_text=preview_text,
            )
        except AffectedUserUnreachableError:
            # Explicit rollback required: DbSessionMiddleware commits unconditionally on
            # normal handler return. Without this, the pending butler_actions row created
            # by plan_action (flushed but not yet committed) would survive and linger
            # until TTL expiry — a silent failure. Rollback here before return so the
            # middleware sees a clean session with no work to commit.
            await session.rollback()
            await message.reply(_MSG_AFFECTED_UNREACHABLE)
            return

    await session.commit()


async def _send_consent_requests(
    *,
    bot: Bot,
    action: Any,
    affected_confirmations: list[Any],
    preview_text: str,
) -> None:
    """Send DM consent requests to affected users.

    §3.5: NO admin override — each affected user must consent independently.
    §10 decision 5: unbypassable.

    H2: if ANY required DM fails → raises AffectedUserUnreachableError BEFORE
    handler commits, preventing silent phantom confirmations.
    """
    for conf in affected_confirmations:
        affected_user_id = conf.confirmer_tg_id
        token = conf.confirmation_token

        if token is None:
            raise ButlerActionError(
                f"action {action.id} affected_user confirmation_token is None",
                error_kind="invariant_broken",
                action_id=action.id,
            )

        consent_text = (
            _MSG_CONSENT_REQUEST_PREFIX
            + preview_text
            + "\n\n<i>Это действие затрагивает вас. Одобрите или отклоните.</i>"
        )

        try:
            await bot.send_message(
                chat_id=affected_user_id,
                text=consent_text,
                parse_mode="HTML",
                reply_markup=_consent_keyboard(action.id, token),
            )
        except Exception:
            logger.warning(
                "butler: failed to send consent DM to affected user",
                extra={"action_id": action.id, "affected_user_id": affected_user_id},
            )
            raise AffectedUserUnreachableError(
                f"failed to send consent DM to affected_user_id={affected_user_id} "
                f"for action_id={action.id}",
                error_kind="affected_user_unreachable",
                action_id=action.id,
            )


# ---------------------------------------------------------------------------
# /butler_status command
# ---------------------------------------------------------------------------


@router.message(Command("butler_status"), PrivateChatFilter())
async def handle_butler_status(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
) -> None:
    """Return current action state + preview."""
    if not await FeatureFlagRepo.get(session, BUTLER_MASTER_FLAG):
        return

    if message.from_user is None:
        return

    user = await UserRepo.get(session, message.from_user.id)
    if user is None or not (
        getattr(user, "is_member", False) or getattr(user, "is_admin", False)
    ):
        # H3: silent rejection for non-members
        return

    args = (command.args or "").strip()
    if not args:
        await message.reply("Использование: /butler_status <action_id>")
        return

    try:
        action_id = int(args)
    except ValueError:
        await message.reply("Неверный формат action_id.")
        return

    action = await ButlerActionRepo.get(session, action_id)
    if action is None:
        await message.reply(_MSG_NOT_FOUND)
        return

    # Auth: only requester or admin can view status
    if action.requester_tg_id != message.from_user.id and not _is_admin(
        message.from_user.id
    ):
        await message.reply(_MSG_FORBIDDEN)
        return

    preview = _render_preview(action)
    status_label = {
        "pending_confirmation": "ожидает подтверждения",
        "confirmed": "подтверждено",
        "executing": "выполняется",
        "succeeded": "успешно выполнено",
        "expired": "истёк срок",
        "cancelled": "отменено",
        "rejected": "отклонено",
        "execution_failed": "ошибка выполнения",
    }.get(action.status, action.status)

    text = f"{preview}\n\n<b>Статус:</b> {status_label}"
    await message.reply(text, parse_mode="HTML")


# ---------------------------------------------------------------------------
# /butler_cancel command
# ---------------------------------------------------------------------------


@router.message(Command("butler_cancel"), PrivateChatFilter())
async def handle_butler_cancel(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
) -> None:
    """Cancel a pending or confirmed action."""
    if not await FeatureFlagRepo.get(session, BUTLER_MASTER_FLAG):
        return

    if message.from_user is None:
        return

    user = await UserRepo.get(session, message.from_user.id)
    if user is None or not (
        getattr(user, "is_member", False) or getattr(user, "is_admin", False)
    ):
        # H3: silent rejection for non-members
        return

    args = (command.args or "").strip()
    if not args:
        await message.reply("Использование: /butler_cancel <action_id>")
        return

    try:
        action_id = int(args)
    except ValueError:
        await message.reply("Неверный формат action_id.")
        return

    butler = _build_butler_service(session)

    try:
        await butler.cancel_action(
            action_id=action_id,
            cancelling_user_id=message.from_user.id,
            is_admin=_is_admin(message.from_user.id),
        )
    except ButlerActionError as exc:
        await message.reply(_dispatch_butler_error(exc))
        return

    await message.reply("✅ Действие отменено.")
    await session.commit()


# ---------------------------------------------------------------------------
# /butler_undo command (T12-07)
# ---------------------------------------------------------------------------


@router.message(Command("butler_undo"), PrivateChatFilter())
async def handle_butler_undo(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
) -> None:
    """Undo a previously-succeeded butler action via its inverse operation.

    Auth = membership gate (handler) + requester/admin/affected_user (service).
    The original action row is immutable; the undo is recorded on a linked child
    row. Irreversible actions report a "logged, not reversible" notice.
    """
    if not await FeatureFlagRepo.get(session, BUTLER_MASTER_FLAG):
        return

    if message.from_user is None:
        return

    user = await UserRepo.get(session, message.from_user.id)
    if user is None or not (
        getattr(user, "is_member", False) or getattr(user, "is_admin", False)
    ):
        # H3: silent rejection for non-members
        return

    args = (command.args or "").strip()
    if not args:
        await message.reply(_MSG_UNDO_USAGE)
        return

    try:
        action_id = int(args)
    except ValueError:
        await message.reply("Неверный формат action_id.")
        return

    butler = _build_butler_service(session)

    try:
        child = await butler.undo_action(
            action_id=action_id,
            requester_user_id=message.from_user.id,
            is_admin=_is_admin(message.from_user.id),
            bot=message.bot,
        )
    except ButlerUndoError:
        # Child row already persisted as undo_failed by the service — commit the
        # audit row (not rollback), then report failure.
        await session.commit()
        await message.reply(_MSG_UNDO_FAILED)
        return
    except ButlerActionRejectedError as exc:
        # Guards raise before any child row is written; roll back the read-only
        # transaction (incl. the FOR UPDATE lock) so DbSessionMiddleware does not
        # commit it — same discipline as the T12-05 early-return paths.
        await session.rollback()
        if (exc.error_kind or "") == "wrong_status":
            await message.reply(_MSG_UNDO_WRONG_STATUS)
        else:
            await message.reply(_MSG_FORBIDDEN)
        return
    except ButlerActionError as exc:
        await session.rollback()
        kind = exc.error_kind or ""
        if kind == "wrong_status":
            await message.reply(_MSG_UNDO_WRONG_STATUS)
        elif kind == "cascade_in_flight":
            await message.reply(_MSG_CASCADE_IN_FLIGHT)
        else:
            await message.reply(_dispatch_butler_error(exc))
        return

    # Distinguish a real rollback from a not_reversible audit-only undo. Key off
    # the child's OWN recorded result_payload (set by the service) rather than the
    # inherited inverse_op_payload, so the message reflects what actually happened.
    result = getattr(child, "result_payload", None) or {}
    if result.get("rollback_kind") == "not_reversible":
        await message.reply(_MSG_UNDO_NOT_REVERSIBLE)
    else:
        await message.reply(_MSG_UNDO_DONE)

    await session.commit()


# ---------------------------------------------------------------------------
# Inline keyboard: Confirm (requester)
# ---------------------------------------------------------------------------


@router.callback_query(lambda cb: cb.data and cb.data.startswith("butler_confirm:"))
async def handle_butler_confirm_callback(
    callback: CallbackQuery,
    session: AsyncSession,
) -> None:
    """Handle requester confirmation via inline keyboard."""
    await callback.answer()

    if callback.from_user is None or callback.data is None:
        return

    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        if callback.message:
            await callback.message.reply(_MSG_BAD_TOKEN)
        return

    try:
        action_id = int(parts[1])
        token = parts[2]
    except (ValueError, IndexError):
        if callback.message:
            await callback.message.reply(_MSG_BAD_TOKEN)
        return

    # Verify actor matches requester
    action = await ButlerActionRepo.get(session, action_id)
    if action is None:
        if callback.message:
            await callback.message.edit_reply_markup()
        await callback.answer(_MSG_NOT_FOUND, show_alert=True)
        return

    if action.requester_tg_id != callback.from_user.id:
        await callback.answer(_MSG_FORBIDDEN, show_alert=True)
        return

    butler = _build_butler_service(session)

    try:
        await butler.confirm_action(
            action_id=action_id,
            confirming_user_id=callback.from_user.id,
            confirmation_token=token,
        )
    except ButlerActionExpiredError:
        if callback.message:
            await callback.message.edit_reply_markup()
        await callback.answer(_MSG_EXPIRED, show_alert=True)
        return
    except EvidenceStaleError:
        if callback.message:
            await callback.message.edit_reply_markup()
        await callback.answer(_MSG_EVIDENCE_STALE, show_alert=True)
        return
    except CascadeInFlightError:
        await callback.answer(_MSG_CASCADE_IN_FLIGHT, show_alert=True)
        return
    except ButlerActionError as exc:
        await callback.answer(_dispatch_butler_error(exc), show_alert=True)
        return

    # Update the keyboard to remove buttons after confirmation
    if callback.message:
        await callback.message.edit_text(
            (callback.message.text or "") + "\n\n✅ <i>Подтверждено</i>",
            parse_mode="HTML",
            reply_markup=None,
        )

    await session.commit()


# ---------------------------------------------------------------------------
# Inline keyboard: Cancel (requester via button)
# ---------------------------------------------------------------------------


@router.callback_query(lambda cb: cb.data and cb.data.startswith("butler_cancel_cb:"))
async def handle_butler_cancel_callback(
    callback: CallbackQuery,
    session: AsyncSession,
) -> None:
    """Handle inline Cancel button press."""
    await callback.answer()

    if callback.from_user is None or callback.data is None:
        return

    parts = callback.data.split(":", 1)
    if len(parts) != 2:
        return

    try:
        action_id = int(parts[1])
    except ValueError:
        return

    # Verify actor
    action = await ButlerActionRepo.get(session, action_id)
    if action is None:
        if callback.message:
            await callback.message.edit_reply_markup()
        return

    if action.requester_tg_id != callback.from_user.id and not _is_admin(
        callback.from_user.id
    ):
        await callback.answer(_MSG_FORBIDDEN, show_alert=True)
        return

    butler = _build_butler_service(session)

    try:
        await butler.cancel_action(
            action_id=action_id,
            cancelling_user_id=callback.from_user.id,
            is_admin=_is_admin(callback.from_user.id),
        )
    except ButlerActionError as exc:
        await callback.answer(_dispatch_butler_error(exc), show_alert=True)
        return

    if callback.message:
        await callback.message.edit_text(
            (callback.message.text or "") + "\n\n❌ <i>Отменено</i>",
            parse_mode="HTML",
            reply_markup=None,
        )

    await session.commit()


# ---------------------------------------------------------------------------
# Inline keyboard: Affected user approve
# ---------------------------------------------------------------------------


@router.callback_query(
    lambda cb: cb.data and cb.data.startswith("butler_affected_approve:")
)
async def handle_butler_affected_approve(
    callback: CallbackQuery,
    session: AsyncSession,
) -> None:
    """Handle affected user's approval of cross-user consent."""
    await callback.answer()

    if callback.from_user is None or callback.data is None:
        return

    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        return

    try:
        action_id = int(parts[1])
        token = parts[2]
    except (ValueError, IndexError):
        if callback.message:
            await callback.message.edit_reply_markup()
        return

    butler = _build_butler_service(session)

    try:
        await butler.confirm_action(
            action_id=action_id,
            confirming_user_id=callback.from_user.id,
            confirmation_token=token,
        )
    except ButlerActionExpiredError:
        if callback.message:
            await callback.message.edit_reply_markup()
        await callback.answer(_MSG_EXPIRED, show_alert=True)
        return
    except EvidenceStaleError:
        if callback.message:
            await callback.message.edit_reply_markup()
        await callback.answer(_MSG_EVIDENCE_STALE, show_alert=True)
        return
    except CascadeInFlightError:
        await callback.answer(_MSG_CASCADE_IN_FLIGHT, show_alert=True)
        return
    except ButlerActionError as exc:
        await callback.answer(_dispatch_butler_error(exc), show_alert=True)
        return

    if callback.message:
        await callback.message.edit_text(
            _MSG_CONSENT_APPROVED,
            reply_markup=None,
        )

    await session.commit()


# ---------------------------------------------------------------------------
# Inline keyboard: Affected user reject
# ---------------------------------------------------------------------------


@router.callback_query(
    lambda cb: cb.data and cb.data.startswith("butler_affected_reject:")
)
async def handle_butler_affected_reject(
    callback: CallbackQuery,
    session: AsyncSession,
) -> None:
    """Handle affected user's rejection of cross-user consent.

    Rejection cancels the entire action (§3.5 — no admin override).
    C1 fix: uses revoke_affected_user_consent instead of cancel_action with is_admin=True.
    M6: notifies requester that consent was revoked.
    """
    await callback.answer()

    if callback.from_user is None or callback.data is None:
        return

    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        return

    try:
        action_id = int(parts[1])
        # token is in parts[2] but rejection does not need it —
        # revoke_affected_user_consent identifies via actor identity + action_id.
    except (ValueError, IndexError):
        if callback.message:
            await callback.message.edit_reply_markup()
        return

    # Look up action to verify it exists and is addressable
    action = await ButlerActionRepo.get(session, action_id)
    if action is None:
        if callback.message:
            await callback.message.edit_reply_markup()
        return

    # Verify this user is actually an affected_user on this action
    confs = await ButlerActionConfirmationRepo.list_for_action(session, action_id)
    affected_conf = next(
        (
            c
            for c in confs
            if c.confirmer_tg_id == callback.from_user.id
            and c.confirmation_role == "affected_user"
        ),
        None,
    )
    if affected_conf is None:
        await callback.answer(_MSG_FORBIDDEN, show_alert=True)
        return

    # C1 fix: call revoke_affected_user_consent (no is_admin bypass)
    butler = _build_butler_service(session)

    try:
        await butler.revoke_affected_user_consent(
            action_id=action_id,
            affected_user_id=callback.from_user.id,
        )
    except ButlerActionError as exc:
        await callback.answer(_dispatch_butler_error(exc), show_alert=True)
        return

    # Update affected user's own message
    if callback.message:
        await callback.message.edit_text(
            _MSG_CONSENT_REJECTED,
            reply_markup=None,
        )

    # M6: notify requester that consent was revoked
    # originating_message_id stores the requester preview message_id (if available)
    requester_chat_id = action.requester_tg_id
    requester_msg_id = getattr(action, "originating_message_id", None)
    if requester_msg_id is not None and callback.bot is not None:
        try:
            await callback.bot.edit_message_text(
                chat_id=requester_chat_id,
                message_id=requester_msg_id,
                text=_MSG_CONSENT_REVOKED_EFFECT,
                reply_markup=None,
            )
        except Exception:
            logger.warning(
                "butler: failed to notify requester of consent revocation",
                extra={"action_id": action_id, "requester_tg_id": requester_chat_id},
            )

    await session.commit()
