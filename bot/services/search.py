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
                -- #255 + T6-02 round 3 lesson: ``message_hash:`` tombstone MUST
                -- match against ``message_versions.content_hash`` (NOT NULL by
                -- schema), NOT ``chat_messages.content_hash`` (nullable — live
                -- ingestion via ``MessageRepo.save`` never populates it; only
                -- import path does). Filtering on ``c.content_hash`` silently
                -- leaks live messages whose hash IS tombstoned.
                mv.content_hash IS NOT NULL
                AND fe.tombstone_key = 'message_hash:' || mv.content_hash
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
                    -- #255: see ``_PHASE4_SQL`` comment — same fix here for the
                    -- Phase 6 ``message_hits`` branch.
                    mv.content_hash IS NOT NULL
                    AND fe.tombstone_key = 'message_hash:' || mv.content_hash
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
            --
            -- The ``message_hash:`` branch MUST filter on ``mv2.content_hash``
            -- (NOT NULL by schema), NOT on ``c2.content_hash`` (nullable; live
            -- persistence in ``bot/db/repos/message.py::MessageRepo.save``
            -- leaves it NULL). Filtering on ``c2.content_hash`` silently
            -- no-op's every ``message_hash:`` tombstone for live messages,
            -- letting tombstoned content leak via cards. Same class as the
            -- T6-02 round 3 fix in ``bot/services/extractor.py`` — keep the
            -- two query bodies in lock-step.
            SELECT 1
            FROM card_sources cs2
            JOIN message_versions mv2 ON mv2.id = cs2.message_version_id
            JOIN chat_messages c2 ON c2.id = mv2.chat_message_id
            JOIN forget_events fe2 ON (
                fe2.tombstone_key
                    = 'message:' || c2.chat_id::text || ':' || c2.message_id::text
                OR (
                    mv2.content_hash IS NOT NULL
                    AND fe2.tombstone_key = 'message_hash:' || mv2.content_hash
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
-- Per-source tombstone re-check. ``approved_card_hits`` excludes a card
-- when ANY source is tombstoned, so in normal flow no tombstoned source
-- reaches the anchor or the mvid array. This CTE re-applies the same
-- 3-key tombstone check per individual source as belt-and-suspenders
-- against:
--   1. A future refactor of ``approved_card_hits`` that loosens the
--      exclusion condition.
--   2. A race window where the cascade has not yet deleted tombstoned
--      ``card_sources`` rows but the forget_event is already visible.
-- This guarantees no tombstoned mvid can appear in the anchor row or
-- the ``card_source_message_version_ids`` array. Per-source re-check
-- requested by Codex round-2 review (CRITICAL #2).
unforgotten_card_sources AS (
    SELECT cs.id AS card_source_id,
           cs.card_id,
           cs.message_version_id,
           cs.position
    FROM card_sources cs
    JOIN message_versions mv ON mv.id = cs.message_version_id
    JOIN chat_messages c ON c.id = mv.chat_message_id
    WHERE cs.card_id IN (SELECT card_id FROM approved_card_hits)
        AND NOT EXISTS (
            SELECT 1
            FROM forget_events AS fe
            WHERE (
                fe.tombstone_key
                    = 'message:' || c.chat_id::text || ':' || c.message_id::text
                OR (
                    mv.content_hash IS NOT NULL
                    AND fe.tombstone_key = 'message_hash:' || mv.content_hash
                )
                OR (
                    c.user_id IS NOT NULL
                    AND fe.tombstone_key = 'user:' || c.user_id::text
                )
            )
            AND fe.status IN ('pending', 'processing', 'completed')
        )
),
card_anchors AS (
    SELECT DISTINCT ON (ucs.card_id)
        ucs.card_id,
        mv.id AS message_version_id,
        c.id AS chat_message_id,
        c.chat_id AS chat_id,
        c.message_id AS message_id,
        c.date AS source_message_date
    FROM unforgotten_card_sources ucs
    JOIN message_versions mv ON mv.id = ucs.message_version_id
    JOIN chat_messages c ON c.id = mv.chat_message_id
    ORDER BY ucs.card_id, ucs.position ASC, ucs.card_source_id ASC
),
card_source_lists AS (
    SELECT ucs.card_id,
           ARRAY_AGG(
               ucs.message_version_id
               ORDER BY ucs.position ASC, ucs.card_source_id ASC
           ) AS mvids
    FROM unforgotten_card_sources ucs
    GROUP BY ucs.card_id
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

    SQLite dialect: this function is **Postgres-only** at the contract
    level. Both ``_PHASE4_SQL`` and ``_PHASE6_SQL`` rely on
    ``plainto_tsquery`` / ``ts_rank_cd`` / ``ts_headline`` and the
    ``message_versions.search_tsv`` (and, for cards, ``knowledge_cards.body_tsv``)
    GIN-indexed ``tsvector`` columns. None of those exist in SQLite.

    The dialect guard below flips ``include_cards=False`` on non-Postgres
    sessions to remove the **card branch** from the SQL — that branch alone
    references tables and indexes (``knowledge_cards``, ``card_sources``,
    ``body_tsv``) that ORM-only SQLite tests do not create. The guard does
    **not** make the Phase 4 SQL itself SQLite-safe. ORM-only SQLite tests
    that call this function will fail at the database layer; the guard's
    only contract is "the card-extension does not make Phase 4 callers any
    less SQLite-compatible than they already were." Production runs on
    Postgres.
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

    # T6-06 dialect guard — SCOPE: card-branch removal only, NOT full
    # SQLite safety. The Phase 4 SQL underneath still uses
    # ``plainto_tsquery`` / ``ts_rank_cd`` / ``ts_headline`` which are
    # Postgres-only; running this on SQLite will fail in the message
    # branch regardless. The guard's sole purpose is to avoid widening
    # the SQLite-incompatibility surface by referencing card tables that
    # ORM-only SQLite tests do not create (``knowledge_cards`` /
    # ``card_sources`` / ``body_tsv``). Production runs on Postgres.
    # See the docstring above for the full contract.
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
