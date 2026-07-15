"""Repository for ``llm_synthesis_cache`` (T5-03).

Flush-only — NEVER calls ``session.commit()`` or ``session.rollback()``.
The caller (gateway / handler) owns the transaction lifecycle.
"""

from __future__ import annotations

import logging

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import LlmSynthesisCache

_log = logging.getLogger(__name__)


class SynthesisCacheRepo:
    """Data-access layer for ``llm_synthesis_cache``.

    All methods are ``@staticmethod`` and flush-only. They mirror the
    ``SynthesisCacheRepoProtocol`` defined in ``bot/services/llm_gateway.py``.

    ``invalidate_by_citation`` uses PostgreSQL JSONB containment (``@>``) in
    production.  For SQLite (used in unit tests), it falls back to a portable
    Python-side filter because SQLite has no ``@>`` operator.
    """

    @staticmethod
    async def get_or_none(
        session: AsyncSession,
        *,
        input_hash: str,
    ) -> LlmSynthesisCache | None:
        """Lookup by ``input_hash``. Returns row or None. No side effects."""
        result = await session.execute(
            select(LlmSynthesisCache).where(LlmSynthesisCache.input_hash == input_hash)
        )
        return result.scalars().first()

    @staticmethod
    async def store(
        session: AsyncSession,
        *,
        input_hash: str,
        answer_text: str,
        citation_ids: list[int],
        model: str,
    ) -> LlmSynthesisCache:
        """Insert a cache row. Flushes; caller commits.

        Caller MUST handle ``IntegrityError`` on UNIQUE-violation of ``input_hash``
        (concurrent races) by falling back to ``get_or_none`` and bumping ``hit_count``.
        Race-handling contract is in T5-01 ``STEP_CITATION_ENFORCEMENT``.
        """
        row = LlmSynthesisCache(
            input_hash=input_hash,
            answer_text=answer_text,
            citation_ids=list(citation_ids),
            model=model,
        )
        session.add(row)
        await session.flush()
        _log.debug("llm_synthesis_cache: inserted row id=%s input_hash=%.8s...", row.id, input_hash)
        return row

    @staticmethod
    async def bump_hit(
        session: AsyncSession,
        *,
        cache_id: int,
    ) -> None:
        """UPDATE last_hit_at = now(), hit_count = hit_count + 1. Flushes."""
        stmt = (
            update(LlmSynthesisCache)
            .where(LlmSynthesisCache.id == cache_id)
            .values(
                last_hit_at=func.now(),
                hit_count=LlmSynthesisCache.hit_count + 1,
            )
        )
        await session.execute(stmt)
        await session.flush()
        _log.debug("llm_synthesis_cache: bumped hit_count for id=%s", cache_id)

    @staticmethod
    async def delete_by_id(
        session: AsyncSession,
        *,
        cache_id: int,
    ) -> int:
        """Delete one exact cache row and return the affected-row count."""

        result = await session.execute(
            delete(LlmSynthesisCache).where(LlmSynthesisCache.id == cache_id)
        )
        rowcount = int(result.rowcount or 0)
        if rowcount:
            await session.flush()
        _log.debug("llm_synthesis_cache: deleted unsafe row id=%s", cache_id)
        return rowcount

    @staticmethod
    async def invalidate_by_citation(
        session: AsyncSession,
        *,
        message_version_id: int,
    ) -> int:
        """DELETE every cache row whose citation_ids JSONB array contains ``message_version_id``.

        Returns rowcount (number of cache rows invalidated).

        PostgreSQL: uses JSONB containment operator ``@>`` via ``sqlalchemy.text()`` for
        correctness and index-friendliness.

        SQLite: falls back to a portable Python-side filter (load all rows, delete matching
        ones). SQLite has no ``@>`` operator. This path is acceptable for tests because the
        cache table is small in fixtures.

        The dialect is detected via ``session.bind.dialect.name``.
        """
        # Detect dialect via the connection's dialect on the bound engine / connection.
        # AsyncSession wraps a connection; we use get_bind() which is the sync engine/connection.
        bind = session.get_bind()
        dialect_name = bind.dialect.name

        if dialect_name == "postgresql":
            stmt = text(
                "DELETE FROM llm_synthesis_cache "
                "WHERE citation_ids @> CAST(:id AS jsonb) "
                "RETURNING id"
            )
            result = await session.execute(stmt, {"id": f"[{message_version_id}]"})
            deleted_ids = result.fetchall()
            rowcount = len(deleted_ids)
        else:
            # Portable fallback for SQLite and any other dialect.
            all_rows_result = await session.execute(
                select(LlmSynthesisCache.id, LlmSynthesisCache.citation_ids)
            )
            all_rows = all_rows_result.all()  # list of (id, citation_ids) tuples
            matching_ids = [
                row_id
                for row_id, citation_ids in all_rows
                if message_version_id in (citation_ids or [])
            ]
            if matching_ids:
                del_stmt = delete(LlmSynthesisCache).where(LlmSynthesisCache.id.in_(matching_ids))
                del_result = await session.execute(del_stmt)
                rowcount = del_result.rowcount
            else:
                rowcount = 0
            if rowcount:
                await session.flush()

        if rowcount and dialect_name == "postgresql":
            await session.flush()

        _log.debug(
            "llm_synthesis_cache: invalidated %s rows for message_version_id=%s",
            rowcount,
            message_version_id,
        )
        return rowcount
