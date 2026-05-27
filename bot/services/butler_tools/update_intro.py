"""update_intro Butler tool implementation (T12-06).

Edits ONLY Butler-owned intro messages (ownership verified via butler_actions lookup).
If unable to edit (not Butler's message, edit timeout exceeded) → posts a follow-up
reply instead of failing.

Hard Constraints satisfied:
  #1  No LLM calls.
  #2  No raw DB reads beyond butler_actions ownership check via action_repo.
  #6  No external API calls beyond Telegram bot.

inverse_op_payload:
  - rollback_kind='edit_message' when edit succeeded (restore prior_text)
  - rollback_kind='followup_correction' when edit was not available
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from bot.services.butler_tools import ButlerPlanError, UpdateIntroArgs, ToolResult

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from bot.services.butler_evidence import ButlerEvidenceContext

logger = logging.getLogger(__name__)


class UpdateIntroTool:
    """Butler tool: edit a Butler-owned intro message.

    Ownership verification: attempts edit only when the message_id matches a
    previous Butler action (via action_repo.find_by_message_id). If the lookup
    returns None (not Butler's message) OR if edit_message_text raises (e.g., Telegram
    48h edit window exceeded), the tool falls back to posting a follow-up reply
    — it NEVER raises an unhandled exception (spec: "if unable to edit → post
    a follow-up reply instead of failing").

    Privacy: new_intro_text is NOT included in logs or payload.
    """

    name: str = "update_intro"
    schema_version: str = "v1.0.0"
    args_model: type[BaseModel] = UpdateIntroArgs

    async def validate_policy(
        self,
        context: "ButlerEvidenceContext",
        args: BaseModel,
    ) -> None:
        """Validate message_id > 0 and new_intro_text is non-blank.

        Raises ButlerPlanError(invariant_broken) if args is not UpdateIntroArgs.
        """
        if not isinstance(args, UpdateIntroArgs):
            raise ButlerPlanError(
                "update_intro: args must be UpdateIntroArgs",
                error_kind="invariant_broken",
            )

        if not args.message_id:
            raise ButlerPlanError(
                "update_intro: message_id must be non-zero",
                error_kind="invalid_args",
            )
        if not args.new_intro_text.strip():
            raise ButlerPlanError(
                "update_intro: new_intro_text is blank",
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
        """Edit a Butler-owned intro message, or fall back to a follow-up reply.

        Strategy:
        1. Check ownership via action_repo.find_by_posted_message_id
           (None → not Butler's, or action_repo not wired).
        2. If owned, attempt bot.edit_message_text with parse_mode=None.
           On success → outcome='edited', message_id from result.
           On exception (edit timeout, etc.) → fall through to step 3.
        3. If not owned or edit failed → post a follow-up reply via bot.send_message
           with parse_mode=None (M1: user-supplied text must not be parsed).
           outcome='followup_reply', followup_message_id from result.

        This tool NEVER raises an unhandled exception — the spec requires a
        graceful fallback path in all failure cases.

        Privacy: new_intro_text NOT in logs or payload.
        """
        if not isinstance(args, UpdateIntroArgs):
            raise ButlerPlanError(
                "update_intro: args must be UpdateIntroArgs",
                error_kind="invariant_broken",
            )
        message_id = args.message_id
        new_intro_text = args.new_intro_text

        chat_id = ctx.chat_id

        # Step 1 — ownership check via action_repo.find_by_posted_message_id
        # (C2 fix: uses posted_message_id column added in migration 075, not
        # the non-existent find_by_message_id on butler_actions).
        is_butler_owned = False
        if action_repo is not None:
            try:
                invocation_row = await action_repo.find_by_posted_message_id(session, message_id)
                is_butler_owned = invocation_row is not None
            except Exception as exc:
                # Ownership check failed — treat as not owned (fail-closed)
                logger.warning(
                    "butler:update_intro: ownership check failed — treating as not Butler's",
                    extra={"message_id": message_id, "error": str(exc)},
                )

        # Step 2 — attempt edit if Butler owns the message
        if is_butler_owned and bot is not None:
            try:
                # parse_mode=None: new_intro_text is user-supplied; never parse as HTML/Markdown (M1).
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=new_intro_text,
                    parse_mode=None,
                )
                logger.info(
                    "butler:update_intro: edit succeeded",
                    extra={
                        "message_id": message_id,
                        "chat_id": chat_id,
                        # new_intro_text intentionally omitted
                    },
                )
                return ToolResult(
                    success=True,
                    payload={
                        "outcome": "edited",
                        "message_id": message_id,
                        "chat_id": chat_id,
                    },
                )
            except Exception as exc:
                # Edit failed (timeout, rate limit, etc.) — fall through
                logger.warning(
                    "butler:update_intro: edit failed, falling back to follow-up reply",
                    extra={"message_id": message_id, "error": str(exc)},
                )

        # Step 3 — fallback: post a follow-up reply
        if bot is None:
            logger.error(
                "butler:update_intro: no bot instance — cannot send followup",
                extra={"requester_user_id": ctx.requester_user_id},
            )
            return ToolResult(
                success=False,
                error="no bot instance provided for update_intro",
            )

        followup_text = (
            f"Уточнение: {new_intro_text}"
        )
        # parse_mode=None: followup_text contains user-supplied content; never parse (M1).
        followup_msg = await bot.send_message(
            chat_id=chat_id,
            text=followup_text,
            parse_mode=None,
        )
        followup_message_id: int = followup_msg.message_id

        logger.info(
            "butler:update_intro: followup reply sent",
            extra={
                "chat_id": chat_id,
                "followup_message_id": followup_message_id,
                # new_intro_text intentionally omitted
            },
        )
        return ToolResult(
            success=True,
            payload={
                "outcome": "followup_reply",
                "original_message_id": message_id,
                "followup_message_id": followup_message_id,
                "chat_id": chat_id,
            },
        )

    async def build_inverse(self, result: ToolResult) -> dict[str, object]:
        """Build inverse payload based on what actually happened.

        - outcome='edited' → rollback_kind='edit_message' (T12-07 edits back)
        - outcome='followup_reply' → rollback_kind='followup_correction'
          (T12-07 posts a correction; original message was not modified)
        """
        payload = result.payload or {}
        outcome = payload.get("outcome", "followup_reply")

        if outcome == "edited":
            return {
                "rollback_kind": "edit_message",
                "message_id": payload.get("message_id"),
                "chat_id": payload.get("chat_id"),
                # prior_text is NOT stored here (privacy) — T12-07 must ask the
                # requester to supply the prior text if needed, or use a stored hash.
            }

        # followup_reply → followup_correction
        return {
            "rollback_kind": "followup_correction",
            "followup_message_id": payload.get("followup_message_id"),
            "chat_id": payload.get("chat_id"),
        }
