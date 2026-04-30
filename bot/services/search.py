"""Governance-filtered full-text search for Phase 4 memory retrieval."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

MAX_QUERY_LENGTH = 256


@dataclass(frozen=True)
class SearchHit:
    message_version_id: int
    chat_message_id: int
    chat_id: int
    message_id: int
    user_id: int | None
    snippet: str
    ts_rank: float
    captured_at: datetime
    message_date: datetime


async def search_messages(
    session: AsyncSession,
    query: str,
    *,
    chat_id: int,
    limit: int = 3,
    headline_max_words: int = 35,
) -> list[SearchHit]:
    """Search visible message versions in one chat.

    The governance filter is intentionally repeated here instead of depending on
    index shape for privacy.
    """
    normalized_query = query.strip()
    if not normalized_query:
        return []
    if limit < 1:
        raise ValueError("limit must be >= 1")
    if headline_max_words < 1:
        raise ValueError("headline_max_words must be >= 1")
    if len(normalized_query) > MAX_QUERY_LENGTH:
        logger.info(
            "search_messages: truncating overlong query",
            extra={"query_length": len(normalized_query), "max_length": MAX_QUERY_LENGTH},
        )
        normalized_query = normalized_query[:MAX_QUERY_LENGTH].strip()
        if not normalized_query:
            return []
    headline_options = (
        f"MaxWords={headline_max_words},MinWords=10,ShortWord=2,HighlightAll=false"
    )

    stmt = text(
        """
        WITH q AS (
            SELECT plainto_tsquery('russian', :query) AS tsq
        )
        SELECT
            mv.id AS message_version_id,
            mv.chat_message_id AS chat_message_id,
            c.chat_id AS chat_id,
            c.message_id AS message_id,
            c.user_id AS user_id,
            COALESCE(
                ts_headline(
                    'russian',
                    concat_ws(' ', mv.normalized_text, mv.caption),
                    q.tsq,
                    :headline_options
                ),
                ''
            ) AS snippet,
            ts_rank_cd(mv.search_tsv, q.tsq) AS rank,
            mv.captured_at AS captured_at,
            c.date AS message_date
        FROM message_versions AS mv
        JOIN chat_messages AS c
            ON c.id = mv.chat_message_id
            AND c.current_version_id = mv.id
        CROSS JOIN q
        WHERE c.chat_id = :chat_id
            AND c.memory_policy = 'normal'
            AND c.is_redacted = FALSE
            AND mv.is_redacted = FALSE
            AND mv.search_tsv @@ q.tsq
            AND NOT EXISTS (
                SELECT 1
                FROM forget_events AS fe
                WHERE (
                    fe.tombstone_key = 'message:' || c.chat_id::text || ':' || c.message_id::text
                    OR (
                        c.content_hash IS NOT NULL
                        AND fe.tombstone_key = 'message_hash:' || c.content_hash
                    )
                    OR (
                        c.user_id IS NOT NULL
                        AND fe.tombstone_key = 'user:' || c.user_id::text
                    )
                )
                AND fe.status IN ('pending', 'processing', 'completed')
            )
        ORDER BY rank DESC, mv.captured_at DESC, mv.id DESC
        LIMIT :limit
        """
    )
    result = await session.execute(
        stmt,
        {
            "query": normalized_query,
            "chat_id": chat_id,
            "limit": limit,
            "headline_options": headline_options,
        },
    )

    return [
        SearchHit(
            message_version_id=row["message_version_id"],
            chat_message_id=row["chat_message_id"],
            chat_id=row["chat_id"],
            message_id=row["message_id"],
            user_id=row["user_id"],
            snippet=row["snippet"],
            ts_rank=float(row["rank"]),
            captured_at=row["captured_at"],
            message_date=row["message_date"],
        )
        for row in result.mappings().all()
    ]
