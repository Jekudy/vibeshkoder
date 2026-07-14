"""Repository for ``qa_traces`` (T4-05 base + T5-04 LLM extension).

Flush-only — NEVER calls ``session.commit()`` or ``session.rollback()``.
The caller (handler) owns the transaction lifecycle.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import QaTrace


class QaTraceRepo:
    @staticmethod
    async def create(
        session: AsyncSession,
        *,
        user_tg_id: int,
        chat_id: int,
        query: str,
        evidence_ids: list[int],
        abstained: bool,
        redact_query: bool,
        source_chat_message_id: int | None = None,
    ) -> QaTrace:
        """Insert a q&a audit trace. Flushes; caller commits."""
        trace = QaTrace(
            user_tg_id=user_tg_id,
            chat_id=chat_id,
            query_redacted=redact_query,
            query_text=None if redact_query else query,
            evidence_ids=list(evidence_ids),
            abstained=abstained,
            source_chat_message_id=source_chat_message_id,
        )
        session.add(trace)
        await session.flush()
        return trace

    @staticmethod
    async def get_by_source_chat_message_id(
        session: AsyncSession,
        source_chat_message_id: int,
    ) -> QaTrace | None:
        """Return the one audit trace bound to an incoming Telegram message."""

        return await session.scalar(
            select(QaTrace).where(QaTrace.source_chat_message_id == source_chat_message_id)
        )

    @staticmethod
    async def update_llm_fields(
        session: AsyncSession,
        *,
        qa_trace_id: int,
        llm_call_id: int,
        llm_response_summary: str | None,
        llm_response_redacted: bool,
        cost_usd: Decimal,
    ) -> None:
        """Update the four Phase 5 LLM-extension columns on an existing QaTrace.

        Called by ``bot/handlers/qa.py`` step 3 of the binding 4-step ORDER
        (CREATE QaTrace → synthesize_answer → UPDATE QaTrace → render) per
        contracts.md §6.1 + §12.3.

        Touches ONLY the 4 Phase 5 columns — ``query_text``, ``evidence_ids``,
        ``abstained``, ``query_redacted`` MUST NOT be modified. Tested in
        ``tests/db/test_qa_trace.py::test_update_llm_fields_touches_only_phase5_columns``.

        Returns None per contracts.md §12.3. Flushes; caller commits.
        Raises ``LookupError`` if ``qa_trace_id`` is not found — the handler
        guarantees the trace was created in step 1, so a missing row signals
        a bug rather than a recoverable runtime condition.
        """
        stmt = (
            update(QaTrace)
            .where(QaTrace.id == qa_trace_id)
            .values(
                llm_call_id=llm_call_id,
                llm_response_summary=llm_response_summary,
                llm_response_redacted=llm_response_redacted,
                cost_usd=cost_usd,
            )
        )
        result = await session.execute(stmt)
        rowcount: int = result.rowcount
        if rowcount == 0:
            raise LookupError(
                f"QaTrace(id={qa_trace_id}) not found — handler step 1 (create) "
                "must precede step 3 (update_llm_fields)"
            )
        await session.flush()
