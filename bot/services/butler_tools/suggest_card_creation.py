"""suggest_card_creation Butler tool implementation (T12-06).

Creates a ``card_suggestions`` row (via ``extraction_candidates`` status='pending')
AND a ``butler_card_suggestions`` mapping row. NEVER creates an active card.
Phase 6 admin review flow is unchanged.

Hard Constraints satisfied:
  #1  No LLM calls.
  #2  DB writes ONLY to extraction_candidates + butler_card_suggestions (no reads).
  #6  No external API calls.

Spec §4.6 (PHASE12_PLAN_REFRESH.md): one suggestion per butler_action (UNIQUE constraint
on butler_action_id). The mapping row is written first; the candidate row follows.
extraction_candidate_id on the mapping is NULLABLE — set after candidate flush.

inverse_op_payload: rollback_kind='cancel_pending' — T12-07 marks the suggestion
with status='dismissed_by_butler_undo' (never deletes the audit row).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from bot.db.models import ButlerCardSuggestion, ExtractionCandidate
from bot.services.butler_tools import ButlerPlanError, SuggestCardCreationArgs, ToolResult

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from bot.services.butler_evidence import ButlerEvidenceContext

logger = logging.getLogger(__name__)


class SuggestCardCreationTool:
    """Butler tool: create a pending card suggestion for admin review.

    Writes two rows atomically (within the caller's transaction):
    1. ``extraction_candidates`` row with status='pending' (pending admin review).
    2. ``butler_card_suggestions`` mapping row linking the Butler action to the candidate.

    Admin review flow (Phase 6) is unchanged — admin sees a normal extraction_candidates
    row. The butler_card_suggestions table provides Butler-side audit linkage.

    NEVER creates a card with status='approved' or 'active'. The admin must explicitly
    approve the candidate through the Phase 6 review surface.
    """

    name: str = "suggest_card_creation"
    schema_version: str = "v1.0.0"
    args_model: type[BaseModel] = SuggestCardCreationArgs

    async def validate_policy(
        self,
        context: "ButlerEvidenceContext",
        args: BaseModel,
    ) -> None:
        """Validate that title is non-blank.

        Raises ButlerPlanError(invariant_broken) if args is not SuggestCardCreationArgs.
        """
        if not isinstance(args, SuggestCardCreationArgs):
            raise ButlerPlanError(
                "suggest_card_creation: args must be SuggestCardCreationArgs",
                error_kind="invariant_broken",
            )

        if not args.title.strip():
            raise ButlerPlanError(
                "suggest_card_creation: title is blank",
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
        """Write extraction_candidates + butler_card_suggestions rows.

        Uses session.add() + session.flush() only — no session.execute() or
        raw SQL (Hard Constraint #2). The caller owns the transaction lifecycle.

        The extraction_candidates row is written with status='pending' so it
        enters the Phase 6 admin review queue immediately. The butler_card_suggestions
        row links the Butler action_id to the candidate for audit replay.

        Arguments
        ---------
        ctx
            Sealed ButlerEvidenceContext.
        args
            Validated SuggestCardCreationArgs.
        session
            Async SQLAlchemy session. Caller owns commit/rollback.
        bot
            Not used by this tool (no Telegram output). Accepted for Protocol
            compatibility with the ButlerTool Protocol.
        action_repo
            Not used by this tool. Accepted for Protocol compatibility.
        action_id
            The butler_actions.id for the mapping row. Required — no default.
            FK constraint on butler_card_suggestions.butler_action_id is NOT NULL
            REFERENCES butler_actions(id) ON DELETE RESTRICT; id=0 would FK-violate.
            Raises ButlerPlanError(invariant_broken) if 0 or absent (defense in depth).
        """
        if not isinstance(args, SuggestCardCreationArgs):
            raise ButlerPlanError(
                "suggest_card_creation: args must be SuggestCardCreationArgs",
                error_kind="invariant_broken",
            )
        # C3: action_id=0 default removed — caller MUST pass real PK.
        # Defense in depth: if 0 slips through, fail before FK violation.
        if not action_id:
            raise ButlerPlanError(
                "suggest_card_creation: action_id is required and must be non-zero",
                error_kind="invariant_broken",
            )
        title = args.title
        summary = args.summary or ""
        tags = list(args.tags)

        # Build the candidate JSON payload (audit-safe, no raw user content beyond title)
        candidate_json = {
            "title": title,
            "summary": summary,
            "tags": tags,
            "butler_suggested": True,
        }

        # Step 1 — INSERT extraction_candidates with status='pending'
        # This enters the Phase 6 admin review queue (unchanged flow).
        # NEVER use status='approved' — the admin must explicitly approve.
        candidate = ExtractionCandidate(
            candidate_json=candidate_json,
            source_message_version_ids=list(ctx.evidence_ids),
            status="pending",
        )
        session.add(candidate)
        # flush() to get the candidate.id for the mapping row
        await session.flush()

        # Step 2 — INSERT butler_card_suggestions mapping row
        # suggested_card_payload mirrors candidate_json for Butler-side audit.
        suggestion = ButlerCardSuggestion(
            butler_action_id=action_id,
            extraction_candidate_id=candidate.id,
            suggested_card_payload=candidate_json,
            created_by_user_id=ctx.requester_user_id,
        )
        session.add(suggestion)
        await session.flush()

        logger.info(
            "butler:suggest_card_creation: pending suggestion created",
            extra={
                "requester_user_id": ctx.requester_user_id,
                "action_id": action_id,
                # title intentionally omitted — may contain user-supplied content
            },
        )

        # Use suggestion.id as the audit anchor for inverse_op_payload.
        # After flush() the ORM object has its auto-generated id.
        butler_suggestion_id = suggestion.id

        return ToolResult(
            success=True,
            payload={
                "butler_card_suggestion_id": butler_suggestion_id,
                "candidate_id": str(candidate.id) if candidate.id else None,
                # status exposed so callers can assert 'pending' (never 'approved')
                "candidate_status": "pending",
            },
        )

    async def build_inverse(self, result: ToolResult) -> dict[str, object]:
        """Return a cancel_pending inverse payload.

        T12-07 marks the butler_card_suggestions row with a 'dismissed_by_butler_undo'
        status in the suggested_card_payload (or equivalent Phase 6 integration).
        The audit row is NEVER deleted — only marked as dismissed.
        """
        payload = result.payload or {}
        return {
            "rollback_kind": "cancel_pending",
            "butler_card_suggestion_id": payload.get("butler_card_suggestion_id"),
            "candidate_id": payload.get("candidate_id"),
        }
