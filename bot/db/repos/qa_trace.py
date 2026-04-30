"""Repository for ``qa_traces`` (T4-05)."""

from __future__ import annotations

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
    ) -> QaTrace:
        """Insert a q&a audit trace. Flushes; caller commits."""
        trace = QaTrace(
            user_tg_id=user_tg_id,
            chat_id=chat_id,
            query_redacted=redact_query,
            query_text=None if redact_query else query,
            evidence_ids=list(evidence_ids),
            abstained=abstained,
        )
        session.add(trace)
        await session.flush()
        return trace
