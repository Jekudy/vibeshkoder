"""Digest context builder — read-only governance-filtered query layer.

T7-03 / Phase 7 Wave 1: produces a structured context bundle for the digest
LLM synthesis step. The output is fed into `llm_gateway.synthesize_digest`
(T7-02, separate PR).

T8-03 / Phase 8 Wave 3: widens `type` to `Literal['daily','weekly']`. The
weekly path reuses the SAME two SQL queries as daily (no third inline copy
of the forget-events predicate — see comment block below). Only the
parameter values change: window bounds (7d span vs 24h), cards LIMIT
(100 vs 30), raw-message LIMIT (`weekly_raw_message_top_n` vs
`raw_message_top_n`), token budget (`weekly_token_budget_input - 1000` vs
`token_budget_input - 1000`), and the min-cards fallback threshold
(`weekly_min_cards_threshold` vs `min_cards_threshold`).

Governance filter (all sources):
- chat_messages.memory_policy = 'normal' (excludes nomem, offrecord, forgotten)
- message_versions.is_redacted = FALSE   (excludes cascade-redacted rows)
- NO active forget_event ('pending' / 'processing' / 'completed') targeting
  this message_version via message_id, user_id, or message_hash. This is the
  defense-in-depth check that catches forget_events whose cascade hasn't yet
  flipped is_redacted to TRUE.

PHASE 7.5 CARRYOVER: per PHASE7_PLAN.md §5.C, the forget-events predicate
SHOULD eventually be extracted into a shared helper used by both this module
and `bot/services/forget_cascade.py` (DRY guard against drift). This PR
INLINES the predicate (does not extract). The match logic must stay in sync
with `forget_cascade._resolve_affected_mvids` (forget_cascade.py:812+).

TODO(#291): extract `_forget_excludes_predicate` to a shared helper. Today
the predicate is inlined verbatim in TWO queries in this file (cards-first
+ raw fallback) and once in `forget_cascade.py:255+` — i.e. three textual
copies in production. Phase 8 T8-03 does NOT add a fourth copy: the weekly
path reuses the same two queries with different parameter values. If #291
lands before Phase 9, the helper extraction collapses all three to one and
any future widening (e.g. Phase 9 reflection layer) inherits the predicate
automatically.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.forget_predicate import forget_excludes_sql_fragment

# Forget-event exclusion predicate — sourced from the shared helper.
# Issue #291 extracted the inline SQL fragment to bot/services/forget_predicate.py
# so that digest_context.py, llm_gateway.py, and forget_cascade.py all use the
# SAME predicate string.  Changing the predicate semantics requires updating
# forget_predicate.py AND the golden snapshot in test_forget_predicate_parity.py.
_FORGET_EXCLUDES = forget_excludes_sql_fragment()


# Cards SQL LIMIT per digest type. Daily window is 24h; weekly is 7d
# (7× more coverage) so the cap widens correspondingly.
_CARDS_LIMIT_BY_TYPE: dict[str, int] = {
    "daily": 30,
    "weekly": 100,
}


@dataclass(frozen=True)
class DigestConfig:
    """Minimal DigestConfig for T7-03. T7-02 moves this to `bot/services/digests.py`
    with `load_digest_config()` for env var loading.

    Phase 8 (T8-03) adds `weekly_*` fields. The parent `DigestConfig` in
    `bot/services/digests.py` is the env-loaded one; its `to_context_config()`
    helper forwards both daily and weekly fields into this dataclass so the
    SQL builder reads from a single source.
    """

    # Phase 7 (daily) fields
    min_cards_threshold: int = 3
    raw_message_top_n: int = 15
    token_budget_input: int = 8000
    # Phase 8 (weekly) fields — defaults mirror PHASE8_PLAN.md §5.B.
    weekly_min_cards_threshold: int = 8
    weekly_raw_message_top_n: int = 60
    weekly_token_budget_input: int = 24000


@dataclass(frozen=True)
class DigestContextCard:
    card_id: UUID
    title: str
    body_markdown: str
    source_count: int
    card_source_ids: list[UUID]


@dataclass(frozen=True)
class DigestContextMessage:
    message_version_id: int
    chat_message_id: int
    author_display: str  # html-escape applied
    text: str
    ts: datetime
    # NOTE: reaction_count / reply_count fields intentionally absent.
    # chat_messages has no such columns; chronological ordering only.


@dataclass(frozen=True)
class DigestContext:
    type: Literal["daily", "weekly"]
    window_start: datetime
    window_end: datetime
    source_chat_id: int
    cards: list[DigestContextCard]
    messages: list[DigestContextMessage]


def _weekly_overrides(
    digest_config: DigestConfig, *, type: Literal["daily", "weekly"]
) -> tuple[int, int, int, int]:
    """Type-aware param resolution in one place — keeps the SQL builder
    reading from a single source.

    Returns:
        (cards_limit, raw_top_n, token_budget_input, min_cards_threshold)
    """
    if type == "weekly":
        return (
            _CARDS_LIMIT_BY_TYPE["weekly"],
            digest_config.weekly_raw_message_top_n,
            digest_config.weekly_token_budget_input,
            digest_config.weekly_min_cards_threshold,
        )
    # daily
    return (
        _CARDS_LIMIT_BY_TYPE["daily"],
        digest_config.raw_message_top_n,
        digest_config.token_budget_input,
        digest_config.min_cards_threshold,
    )


async def build_digest_context(
    session: AsyncSession,
    *,
    type: Literal["daily", "weekly"],
    window_start: datetime,
    window_end: datetime,
    source_chat_id: int,
    digest_config: DigestConfig,
) -> DigestContext:
    """Build governance-filtered context for digest synthesis.

    Returns approved Phase 6 cards in window first; falls back to raw
    chronological messages if fewer than the type-aware min-cards threshold.
    Drops raw messages from the tail to fit the type-aware token budget.

    For ``type='daily'`` uses `digest_config.{min_cards_threshold,
    raw_message_top_n, token_budget_input}` (Phase 7 defaults).
    For ``type='weekly'`` uses `digest_config.weekly_*` counterparts and
    widens the cards SQL LIMIT from 30 → 100.

    Governance filter applied to all sources (identical for daily and weekly):
    - chat_messages.memory_policy = 'normal'  (excludes nomem, offrecord, forgotten)
    - message_versions.is_redacted = FALSE     (excludes cascade-redacted rows)
    - chat_messages.chat_id = :source_chat_id  (single-chat MVP)
    - cm.date in [window_start, window_end)    (UTC range — original Telegram timestamp)
    - cards: knowledge_cards.card_status = 'approved'
    - cards: linked message_versions also pass the above filter
    """
    if type not in ("daily", "weekly"):
        raise ValueError(
            f"build_digest_context: unsupported type {type!r}; "
            "expected 'daily' or 'weekly'"
        )

    cards_limit, raw_top_n, token_budget, min_threshold = _weekly_overrides(
        digest_config, type=type
    )

    # ---- cards-first query ----
    cards_sql = text(f"""
        SELECT
            kc.id::text AS card_id,
            kc.title,
            kc.body_markdown,
            COUNT(cs.id) AS source_count,
            ARRAY_AGG(cs.id::text ORDER BY cs.created_at, cs.id) AS card_source_ids
        FROM knowledge_cards kc
        JOIN card_sources cs ON cs.card_id = kc.id
        JOIN message_versions mv ON mv.id = cs.message_version_id
        JOIN chat_messages cm ON cm.id = mv.chat_message_id
        WHERE kc.card_status = 'approved'
          AND cm.chat_id = :source_chat_id
          AND cm.date >= :ws
          AND cm.date <  :we
          AND cm.memory_policy = 'normal'
          AND mv.is_redacted = FALSE
          AND {_FORGET_EXCLUDES}
        GROUP BY kc.id, kc.title, kc.body_markdown, kc.approved_at
        ORDER BY kc.approved_at DESC NULLS LAST
        LIMIT :cards_limit
    """)
    card_rows = (
        await session.execute(
            cards_sql,
            {
                "source_chat_id": source_chat_id,
                "ws": window_start,
                "we": window_end,
                "cards_limit": cards_limit,
            },
        )
    ).mappings().all()

    cards: list[DigestContextCard] = []
    for row in card_rows:
        cards.append(
            DigestContextCard(
                card_id=UUID(row["card_id"]),
                title=row["title"],
                body_markdown=row["body_markdown"],
                source_count=row["source_count"],
                card_source_ids=[UUID(s) for s in row["card_source_ids"]],
            )
        )

    # ---- raw fallback only when cards too few ----
    messages: list[DigestContextMessage] = []
    if len(cards) < min_threshold:
        raw_sql = text(f"""
            SELECT
                mv.id AS message_version_id,
                mv.chat_message_id,
                u.first_name AS author_display,
                mv.normalized_text AS text,
                cm.date AS ts
            FROM message_versions mv
            JOIN chat_messages cm ON cm.id = mv.chat_message_id
            JOIN users u ON u.id = cm.user_id
            WHERE cm.chat_id = :source_chat_id
              AND cm.current_version_id = mv.id
              AND cm.date >= :ws
              AND cm.date <  :we
              AND cm.memory_policy = 'normal'
              AND mv.is_redacted = FALSE
              AND {_FORGET_EXCLUDES}
            ORDER BY cm.date ASC
            LIMIT :top_n
        """)
        raw_rows = (
            await session.execute(
                raw_sql,
                {
                    "source_chat_id": source_chat_id,
                    "ws": window_start,
                    "we": window_end,
                    "top_n": raw_top_n,
                },
            )
        ).mappings().all()

        # Apply token budget — drop from tail.
        # `-1000` headroom mirrors Phase 7 behaviour: leaves room for the
        # prompt template overhead so the gateway does not have to do a
        # second pass on overflow.
        headroom = token_budget - 1000
        used = 0
        # Rough estimate: len(text) // 3.5 ≈ tokens
        for row in raw_rows:
            txt = row["text"] or ""
            est = int(len(txt) / 3.5)
            if used + est > headroom:
                break
            used += est
            messages.append(
                DigestContextMessage(
                    message_version_id=row["message_version_id"],
                    chat_message_id=row["chat_message_id"],
                    author_display=row["author_display"],
                    text=txt,
                    ts=row["ts"],
                )
            )

    return DigestContext(
        type=type,
        window_start=window_start,
        window_end=window_end,
        source_chat_id=source_chat_id,
        cards=cards,
        messages=messages,
    )
