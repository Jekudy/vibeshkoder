"""recall_evidence Butler tool implementation (T12-06).

Delegates ALL memory reads to the sealed ButlerEvidenceContext — no direct DB access.

Hard Constraints satisfied:
  #1  No LLM calls.
  #2  No raw DB reads — all reads go through the sealed ctx passed to execute().
  #6  No external API calls.

inverse_op_payload: rollback_kind='not_reversible' — recall_evidence posts nothing
to Telegram, so there is nothing to undo.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from bot.services.butler_tools import ButlerPlanError, RecallEvidenceArgs, ToolResult

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from bot.services.butler_evidence import ButlerEvidenceContext

logger = logging.getLogger(__name__)


class RecallEvidenceTool:
    """Butler tool: returns the sealed EvidenceBundle from the governance context.

    This tool performs no Telegram side effects — it surfaces the evidence set
    assembled by build_butler_evidence(). All DB access already happened at
    plan_action time when the ButlerEvidenceContext was built.

    The only output is the evidence_ids list and context_hash, which allows
    downstream display / citation logic (T12-05 handler) to render sources.
    """

    name: str = "recall_evidence"
    schema_version: str = "v1.0.0"
    args_model: type[BaseModel] = RecallEvidenceArgs

    async def validate_policy(
        self,
        context: "ButlerEvidenceContext",
        args: BaseModel,
    ) -> None:
        """Validate governance policy for recall_evidence.

        Raises ButlerPlanError if the query is blank (nothing to recall).
        Raises ButlerActionError(invariant_broken) if args is not a RecallEvidenceArgs.
        Context is pre-filtered by build_butler_evidence — no additional
        DB access needed.
        """
        if not isinstance(args, RecallEvidenceArgs):
            raise ButlerPlanError(
                "recall_evidence: args must be RecallEvidenceArgs",
                error_kind="invariant_broken",
            )

        if not args.query.strip():
            raise ButlerPlanError(
                "recall_evidence: query is blank — nothing to recall",
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
        """Return the sealed evidence set from the context.

        Does NOT issue any SQL — all reads were performed when the
        ButlerEvidenceContext was constructed by build_butler_evidence().

        Payload keys (audit-safe, no raw user content):
          evidence_ids  — list[int] of message_version_ids
          context_hash  — sha256 hash of the sealed context
          item_count    — number of items in the bundle
          abstained     — bool: True if no evidence was found
        """
        if not isinstance(args, RecallEvidenceArgs):
            raise ButlerPlanError(
                "recall_evidence: args must be RecallEvidenceArgs",
                error_kind="invariant_broken",
            )
        # Hard Constraint #2: we only touch the sealed ctx — no session.execute calls.
        evidence_ids = ctx.evidence_ids
        context_hash = ctx.context_hash
        item_count = len(ctx.bundle.items)
        abstained = ctx.bundle.abstained

        logger.info(
            "butler:recall_evidence executed",
            extra={
                "requester_user_id": ctx.requester_user_id,
                "item_count": item_count,
                "abstained": abstained,
                # No query text logged — privacy rule (no user content unmasked)
            },
        )

        return ToolResult(
            success=True,
            payload={
                "evidence_ids": list(evidence_ids),
                "context_hash": context_hash,
                "item_count": item_count,
                "abstained": abstained,
            },
        )

    async def build_inverse(self, result: ToolResult) -> dict[str, object]:
        """Return a not_reversible inverse payload.

        recall_evidence has no Telegram side effect — there is nothing to undo.
        T12-07 will honour rollback_kind='not_reversible' by explaining to the
        requester that recall cannot be undone.
        """
        return {
            "rollback_kind": "not_reversible",
            "reason": "recall_evidence produces no Telegram output",
        }
