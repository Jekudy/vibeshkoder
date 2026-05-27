"""schedule_meeting Butler tool implementation (T12-06).

Posts a Telegram-native text-only meeting proposal to the originating chat.
NO external calendar API, NO out-of-band notifications.

Hard Constraints satisfied:
  #1  No LLM calls.
  #2  No raw DB reads.
  #6  No money/calendar/email/browser/shell — Telegram-native proposal only.

inverse_op_payload: rollback_kind='delete_message' — T12-07 deletes the posted
proposal message if Telegram still permits deletion.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from bot.services.butler_tools import ButlerPlanError, ScheduleMeetingArgs, ToolResult

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from bot.services.butler_evidence import ButlerEvidenceContext

logger = logging.getLogger(__name__)


class ScheduleMeetingTool:
    """Butler tool: post a Telegram-native meeting proposal.

    The proposal is sent as a text message to the originating chat (the
    chat stored in ctx.chat_id). No calendar events are created — this
    is intentionally a community-coordination proposal only, per Hard
    Constraint #6 (no money/calendar/email/browser/shell).
    """

    name: str = "schedule_meeting"
    schema_version: str = "v1.0.0"
    args_model: type[BaseModel] = ScheduleMeetingArgs

    async def validate_policy(
        self,
        context: "ButlerEvidenceContext",
        args: BaseModel,
    ) -> None:
        """Validate that a non-blank topic is provided.

        Raises ButlerPlanError(invariant_broken) if args is not ScheduleMeetingArgs.
        """
        if not isinstance(args, ScheduleMeetingArgs):
            raise ButlerPlanError(
                "schedule_meeting: args must be ScheduleMeetingArgs",
                error_kind="invariant_broken",
            )

        if not args.topic.strip():
            raise ButlerPlanError(
                "schedule_meeting: topic is blank",
                error_kind="invalid_args",
            )

    async def execute(
        self,
        ctx: "ButlerEvidenceContext",
        args: BaseModel,
        *,
        session: "AsyncSession",
        bot: Any = None,
        action_repo: Any = None,
        action_id: int,
    ) -> ToolResult:
        """Post a Telegram meeting proposal message.

        The Bot instance is threaded through via the ``bot`` kwarg (pattern from
        Phase 7 FHR fix: ``event._runtime_bot = bot`` / ``args=[bot]``). Tests
        inject a mock; the production handler passes the real Bot instance.

        Returns payload with:
          chat_id    — the target chat where the proposal was posted
          message_id — Telegram message_id of the posted proposal
        """
        if not isinstance(args, ScheduleMeetingArgs):
            raise ButlerPlanError(
                "schedule_meeting: args must be ScheduleMeetingArgs",
                error_kind="invariant_broken",
            )
        topic = args.topic
        proposed_time_text = args.proposed_time_text
        participant_user_ids = list(args.participant_user_ids)

        chat_id = ctx.chat_id

        # Build proposal text (Telegram-native, text-only)
        parts = [f"📅 Предложение о встрече: {topic}"]
        if proposed_time_text:
            parts.append(f"Время: {proposed_time_text}")
        if participant_user_ids:
            # Mention participant IDs — handler renders these as @mention links
            participants_str = ", ".join(str(uid) for uid in participant_user_ids)
            parts.append(f"Участники: {participants_str}")
        parts.append(f"\nРеквестер: {ctx.requester_user_id}")
        proposal_text = "\n".join(parts)

        sent_message_id: int
        if bot is not None:
            # Production path: send via real Bot instance
            # parse_mode=None: proposal_text may contain user-supplied content;
            # never parse as HTML/Markdown to prevent injection (M1).
            msg = await bot.send_message(chat_id=chat_id, text=proposal_text, parse_mode=None)
            sent_message_id = msg.message_id
        else:
            # Fallback: no bot available — log and mark as failed
            logger.error(
                "butler:schedule_meeting: no bot instance provided — cannot send proposal",
                extra={"requester_user_id": ctx.requester_user_id},
            )
            return ToolResult(
                success=False,
                error="no bot instance provided for schedule_meeting",
            )

        logger.info(
            "butler:schedule_meeting: proposal posted",
            extra={
                "requester_user_id": ctx.requester_user_id,
                "chat_id": chat_id,
                "message_id": sent_message_id,
                # topic NOT logged — may contain user-supplied content
            },
        )

        return ToolResult(
            success=True,
            payload={
                "chat_id": chat_id,
                "message_id": sent_message_id,
            },
        )

    async def build_inverse(self, result: ToolResult) -> dict[str, object]:
        """Return a delete_message inverse payload.

        T12-07 uses this to delete the proposal message if Telegram permits.
        The payload carries chat_id + message_id so T12-07 can issue
        bot.delete_message(chat_id, message_id) without re-reading the DB.
        """
        payload = result.payload or {}
        return {
            "rollback_kind": "delete_message",
            "chat_id": payload.get("chat_id"),
            "message_id": payload.get("message_id"),
        }
