"""Governance-filtered full-text search for Phase 4 memory retrieval.

Phase 6 (T6-06) extension: ``include_cards`` parameter routes the SQL through
a UNION ALL of the Phase 4 message branch + an approved-knowledge-cards
branch. The card branch enforces:

1. ``card_status='approved'`` (governance gate per PHASE6_PLAN.md §1 #4).
2. NO source has been tombstoned (defense-in-depth NOT EXISTS over
   ``card_sources`` → ``message_versions`` → ``chat_messages`` →
   ``forget_events`` with the canonical 3-status tombstone-key construction).
3. NO source has ``memory_policy != 'normal'`` OR ``is_redacted=TRUE``
   (catches manual redaction without a ``forget_event``).

The cascade rule (``_cascade_card_sources_on_forget``) demotes a card to
``archived`` only when ALL sources are tombstoned; T6-06 enforces stricter
exclusion at the search boundary so ``/recall include_cards=True`` cannot
return a card paraphrasing now-tombstoned content before admin re-review.

The default is ``include_cards=True`` per PHASE6_PLAN.md §5.D; passing
``include_cards=False`` preserves Phase 4 behaviour byte-for-byte (literal
copy of the Phase 4 SQL, no UNION).

``SearchHit`` gains three defaulted fields (``source_type`` /
``card_id`` / ``card_source_message_version_ids``) so existing callers
treating hits as message-shape see no breaking change. For card hits the
``message_version_id`` / ``chat_message_id`` / ``chat_id`` / ``message_id``
columns point at the anchor source (lowest-position ``card_sources`` row)
so the downstream renderer (T6-07) can link to a Telegram message.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

MAX_QUERY_LENGTH = 256

# T6-06: multiplicative boost applied to ``ts_rank_cd`` of card hits so they
# rank slightly above equivalent raw message hits. Tunable; chosen empirically
# per T6-06_design.md §3 (Rank boost). A strong message match still beats a
# weak card match — the boost lifts cards above message hits of similar rank.
CARD_RANK_BOOST = 1.15


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
    # T6-06: card-hit discriminator. Defaults trip for message hits so every
    # Phase 4 caller (incl. fakes) sees the same dataclass shape it had.
    # For card hits: ``source_type='card'``, ``card_id`` is the UUID of the
    # ``knowledge_cards`` row, and ``card_source_message_version_ids`` is
    # the full ordered tuple of mvids attached via ``card_sources``.
    source_type: Literal["message", "card"] = "message"
    card_id: uuid.UUID | None = None
    card_source_message_version_ids: tuple[int, ...] = field(default_factory=tuple)


# ─── Phase 4 SQL — preserved verbatim for ``include_cards=False`` callers ────
#
# This is the literal Phase 4 statement that shipped in PR #151 + #156. It is
# kept as a string constant so the ``include_cards=False`` path executes the
# exact same SQL (modulo whitespace identical to the original) — the
# byte-for-byte guarantee in PHASE6_PLAN.md §5.D / T6-06_design.md §2 holds
# at the result-set level.
_PHASE4_SQL = """
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


# ─── Phase 6 SQL — UNION ALL of message + card branches ──────────────────────
#
# The message branch is a literal mirror of ``_PHASE4_SQL`` body minus the
# outer ORDER BY / LIMIT (the wrapping query owns those). It emits NULL
# placeholders for ``card_id`` and ``card_source_message_version_ids`` so
# column counts match the card branch.
#
# The card branch:
#   - Filters approved cards FTS-matched on ``body_tsv`` (GIN index from
#     migration 032).
#   - Enforces defense-in-depth source-state via TWO ``NOT EXISTS``:
#     * one for forget-event tombstones (mirroring the Phase 4 3-key
#       construction across message:/message_hash:/user:);
#     * one for governance state (``memory_policy != 'normal'`` OR
#       ``is_redacted=TRUE`` on either ``chat_messages`` or
#       ``message_versions``).
#   - Resolves the anchor source (lowest-position ``card_sources`` row) so
#     the hit carries a citable Telegram message_id / chat_id.
#   - Aggregates ALL source mvids into ``card_source_message_version_ids``
#     so the renderer (T6-07) can list the full back-citation trace.
#
# Card hits are NOT filtered by ``:chat_id``: cards are admin-curated
# canonical knowledge potentially bridging chats. Phase 6 ingestion only
# touches a single community chat today, so this is a no-op; left permissive
# for the multi-chat future per T6-06_design.md §3 (Chat scope for card hits).
_PHASE6_SQL = """
WITH q AS (
    SELECT plainto_tsquery('russian', :query) AS tsq
),
message_hits AS (
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
        c.date AS message_date,
        'message'::text AS source_type,
        NULL::uuid AS card_id,
        ARRAY[]::int[] AS card_source_message_version_ids
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
),
approved_card_hits AS (
    SELECT
        kc.id AS card_id,
        ts_rank_cd(kc.body_tsv, q.tsq) AS rank,
        COALESCE(
            ts_headline(
                'russian',
                kc.body_markdown,
                q.tsq,
                :headline_options
            ),
            ''
        ) AS snippet,
        kc.approved_at AS approved_at
    FROM knowledge_cards AS kc
    CROSS JOIN q
    WHERE kc.card_status = 'approved'
        AND kc.body_tsv @@ q.tsq
        AND NOT EXISTS (
            -- Defense-in-depth #1: exclude card if ANY source is tombstoned
            -- (forget_event open in pending|processing|completed status).
            SELECT 1
            FROM card_sources cs2
            JOIN message_versions mv2 ON mv2.id = cs2.message_version_id
            JOIN chat_messages c2 ON c2.id = mv2.chat_message_id
            JOIN forget_events fe2 ON (
                fe2.tombstone_key
                    = 'message:' || c2.chat_id::text || ':' || c2.message_id::text
                OR (
                    c2.content_hash IS NOT NULL
                    AND fe2.tombstone_key = 'message_hash:' || c2.content_hash
                )
                OR (
                    c2.user_id IS NOT NULL
                    AND fe2.tombstone_key = 'user:' || c2.user_id::text
                )
            )
            WHERE cs2.card_id = kc.id
                AND fe2.status IN ('pending', 'processing', 'completed')
        )
        AND NOT EXISTS (
            -- Defense-in-depth #2: exclude card if ANY source has
            -- memory_policy != 'normal' OR is_redacted=TRUE.
            SELECT 1
            FROM card_sources cs3
            JOIN message_versions mv3 ON mv3.id = cs3.message_version_id
            JOIN chat_messages c3 ON c3.id = mv3.chat_message_id
            WHERE cs3.card_id = kc.id
                AND (
                    c3.memory_policy <> 'normal'
                    OR c3.is_redacted = TRUE
                    OR mv3.is_redacted = TRUE
                )
        )
),
card_anchors AS (
    SELECT DISTINCT ON (cs.card_id)
        cs.card_id,
        mv.id AS message_version_id,
        c.id AS chat_message_id,
        c.chat_id AS chat_id,
        c.message_id AS message_id,
        c.date AS source_message_date
    FROM card_sources cs
    JOIN message_versions mv ON mv.id = cs.message_version_id
    JOIN chat_messages c ON c.id = mv.chat_message_id
    WHERE cs.card_id IN (SELECT card_id FROM approved_card_hits)
    ORDER BY cs.card_id, cs.position ASC, cs.id ASC
),
card_source_lists AS (
    SELECT cs.card_id,
           ARRAY_AGG(cs.message_version_id ORDER BY cs.position ASC, cs.id ASC) AS mvids
    FROM card_sources cs
    WHERE cs.card_id IN (SELECT card_id FROM approved_card_hits)
    GROUP BY cs.card_id
),
card_hits AS (
    SELECT
        ca.message_version_id AS message_version_id,
        ca.chat_message_id AS chat_message_id,
        ca.chat_id AS chat_id,
        ca.message_id AS message_id,
        NULL::bigint AS user_id,
        ach.snippet AS snippet,
        ach.rank * :card_rank_boost AS rank,
        ach.approved_at AS captured_at,
        COALESCE(ach.approved_at, ca.source_message_date) AS message_date,
        'card'::text AS source_type,
        ach.card_id AS card_id,
        csl.mvids AS card_source_message_version_ids
    FROM approved_card_hits ach
    JOIN card_anchors ca ON ca.card_id = ach.card_id
    JOIN card_source_lists csl ON csl.card_id = ach.card_id
)
SELECT *
FROM (
    SELECT * FROM message_hits
    UNION ALL
    SELECT * FROM card_hits
) AS all_hits
ORDER BY rank DESC, captured_at DESC, message_version_id DESC
LIMIT :limit
"""


async def search_messages(
    session: AsyncSession,
    query: str,
    *,
    chat_id: int,
    limit: int = 3,
    headline_max_words: int = 35,
    include_cards: bool = True,
) -> list[SearchHit]:
    """Search visible message versions (+ optionally approved cards) in one chat.

    When ``include_cards=False`` returns Phase 4 results byte-for-byte (uses
    the literal Phase 4 SQL, no card branch). Default ``True`` per
    ``PHASE6_PLAN.md §5.D``: card hits surface alongside message hits with a
    multiplicative rank boost (``CARD_RANK_BOOST``) so admin-curated content
    ranks slightly above equivalent raw message hits.

    The governance filter is intentionally repeated here instead of depending
    on index shape for privacy.

    SQLite dialect: ``to_tsvector`` / ``ts_rank_cd`` / ``ts_headline`` are
    Postgres-only. For SQLite-bound sessions the card branch is silently
    skipped via dialect detection — the function returns only message hits
    (or an empty list if even the Phase 4 SQL isn't Postgres-compatible).
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

    # T6-06 dialect guard: SQLite has no Russian tsvector functions. Strip the
    # card branch on SQLite even if the caller asked for it (returns only the
    # Phase 4 result set, possibly empty). Production runs on Postgres; this
    # branch keeps ORM-only SQLite tests from breaking.
    dialect_name = session.bind.dialect.name if session.bind is not None else "postgresql"
    if dialect_name != "postgresql":
        include_cards = False

    if not include_cards:
        stmt = text(_PHASE4_SQL)
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

    stmt = text(_PHASE6_SQL)
    result = await session.execute(
        stmt,
        {
            "query": normalized_query,
            "chat_id": chat_id,
            "limit": limit,
            "headline_options": headline_options,
            "card_rank_boost": CARD_RANK_BOOST,
        },
    )

    hits: list[SearchHit] = []
    for row in result.mappings().all():
        raw_mvids = row.get("card_source_message_version_ids") or ()
        mvids_tuple = tuple(int(m) for m in raw_mvids)
        hits.append(
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
                source_type=row["source_type"],
                card_id=row["card_id"],
                card_source_message_version_ids=mvids_tuple,
            )
        )
    return hits
