"""send_intro Butler tool implementation (T12-06).

Sends the CONFIRMED intro text from the action args to the target user.
The text is NOT re-fetched from DB — it must come from the confirmation
context (args bound at plan time, validated by pydantic before execution).

Hard Constraints satisfied:
  #1  No LLM calls.
  #2  No raw DB reads — intro_text is taken from confirmed args only.
  #5  Cross-user consent unbypassable — affected_user_ids in ButlerActionStep
      must include target_user_id; the confirmation gate enforces this before
      execute() is ever called (T12-04 / T12-05 responsibility).
  #6  No external API calls beyond Telegram bot.

inverse_op_payload: rollback_kind='delete_message' — T12-07 deletes the posted
intro message if Telegram still permits deletion.

Privacy rule: intro_text is NEVER included in logs or result payload — the
ToolResult payload carries only IDs and hashes.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from bot.services.butler_tools import ButlerPlanError, SendIntroArgs, ToolResult

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from bot.services.butler_evidence import ButlerEvidenceContext

logger = logging.getLogger(__name__)


class SendIntroTool:
    """Butler tool: send a confirmed introduction message.

    The intro_text is bound at planning time and presented to the requester
    for confirmation. execute() NEVER re-fetches the text from DB — it reads
    exclusively from the confirmed args (Hard Constraint #2 + spec rule
    "uses the confirmed intro text, NOT re-fetched").
    """

    name: str = "send_intro"
    schema_version: str = "v1.0.0"
    args_model: type[BaseModel] = SendIntroArgs

    async def validate_policy(
        self,
        context: "ButlerEvidenceContext",
        args: BaseModel,
    ) -> None:
        """Validate that intro_text is non-blank and target_user_id is set."""
        if isinstance(args, SendIntroArgs):
            intro_text = args.intro_text
            target_user_id = args.target_user_id
        else:
            intro_text = getattr(args, "intro_text", "") or ""
            target_user_id = getattr(args, "target_user_id", 0)

        if not intro_text.strip():
            raise ButlerPlanError(
                "send_intro: intro_text is blank",
                error_kind="invalid_args",
            )
        if not target_user_id:
            raise ButlerPlanError(
                "send_intro: target_user_id must be non-zero",
                error_kind="invalid_args",
            )

    async def execute(
        self,
        ctx: "ButlerEvidenceContext",
        args: BaseModel,
        *,
        session: "AsyncSession",
        bot: Any = None,
    ) -> ToolResult:
        """Send the confirmed intro text to the target user.

        The text is read ONLY from args (never from DB). This enforces
        the spec invariant "uses the confirmed intro text, NOT re-fetched".

        The Bot instance is threaded through via the ``bot`` kwarg (pattern
        from Phase 7 FHR fix: ``event._runtime_bot = bot`` / ``args=[bot]``).

        Privacy: intro_text is NOT included in logs or the returned payload.
        Result payload carries only IDs (target_user_id, message_id).
        """
        if isinstance(args, SendIntroArgs):
            intro_text = args.intro_text
            target_user_id = args.target_user_id
        else:
            intro_text = getattr(args, "intro_text", "")
            target_user_id = getattr(args, "target_user_id", 0)

        if bot is None:
            logger.error(
                "butler:send_intro: no bot instance provided — cannot send intro",
                extra={"requester_user_id": ctx.requester_user_id},
            )
            return ToolResult(
                success=False,
                error="no bot instance provided for send_intro",
            )

        # Send to target_user_id as a DM, using the confirmed text
        # (not re-fetched — this is the binding invariant)
        msg = await bot.send_message(chat_id=target_user_id, text=intro_text)
        sent_message_id: int = msg.message_id

        # Privacy: never log intro_text or its hash
        logger.info(
            "butler:send_intro: intro message sent",
            extra={
                "requester_user_id": ctx.requester_user_id,
                "target_user_id": target_user_id,
                "message_id": sent_message_id,
                # intro_text intentionally omitted
            },
        )

        return ToolResult(
            success=True,
            payload={
                "target_user_id": target_user_id,
                "message_id": sent_message_id,
                # intro_text intentionally NOT included in payload (privacy)
            },
        )

    async def build_inverse(self, result: ToolResult) -> dict[str, object]:
        """Return a delete_message inverse payload.

        T12-07 will attempt bot.delete_message(chat_id=target_user_id, message_id).
        If deletion is no longer available (>48h), T12-07 falls back to
        followup_correction.
        """
        payload = result.payload or {}
        return {
            "rollback_kind": "delete_message",
            "target_user_id": payload.get("target_user_id"),
            "message_id": payload.get("message_id"),
        }
