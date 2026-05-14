"""Digest context builder — read-only governance-filtered query layer.

T7-03 / Phase 7 Wave 1: produces a structured context bundle for the digest
LLM synthesis step. The output is fed into `llm_gateway.synthesize_digest`
(T7-02, separate PR).

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
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


# Forget-event exclusion predicate. Both queries reference this to enforce
# defense-in-depth: a forget_event in 'pending'/'processing' state would not
# yet have flipped `is_redacted` to TRUE on the message_version, so the
# `is_redacted=FALSE` filter alone is insufficient. This predicate covers
# the three target_type cases that `_cascade_message_versions` matches:
#   - target_type='message' AND target_id = chat_messages.id (single message)
#   - target_type='user' AND target_id = users.telegram_id  (all user msgs)
#   - target_type='message_hash' AND target_id = mv.content_hash  (hash-based)
# Status filter 'completed' is included because completed events are
# durable and digests must respect them even after cascade finishes.
_FORGET_EVENT_NOT_EXISTS = """
    NOT EXISTS (
        SELECT 1 FROM forget_events fe
        WHERE fe.status IN ('pending', 'processing', 'completed')
          AND (
              (fe.target_type = 'message' AND fe.target_id = cm.id::text)
              OR
              (fe.target_type = 'user' AND fe.target_id = cm.user_id::text)
              OR
              (fe.target_type = 'message_hash' AND fe.target_id = mv.content_hash)
          )
    )
"""


@dataclass(frozen=True)
class DigestConfig:
    """Minimal DigestConfig for T7-03. T7-02 moves this to `bot/services/digests.py`
    with `load_digest_config()` for env var loading.
    """

    min_cards_threshold: int = 3
    raw_message_top_n: int = 15
    token_budget_input: int = 8000


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
    type: Literal["daily"]
    window_start: datetime
    window_end: datetime
    source_chat_id: int
    cards: list[DigestContextCard]
    messages: list[DigestContextMessage]


async def build_digest_context(
    session: AsyncSession,
    *,
    type: Literal["daily"],
    window_start: datetime,
    window_end: datetime,
    source_chat_id: int,
    digest_config: DigestConfig,
) -> DigestContext:
    """Build governance-filtered context for digest synthesis.

    Returns approved Phase 6 cards in window first; falls back to raw
    chronological messages if fewer than `min_cards_threshold` cards.
    Drops raw messages from the tail to fit `token_budget_input`.

    Governance filter applied to all sources:
    - chat_messages.memory_policy = 'normal'  (excludes nomem, offrecord, forgotten)
    - message_versions.is_redacted = FALSE     (excludes cascade-redacted rows)
    - chat_messages.chat_id = :source_chat_id  (single-chat MVP)
    - cm.date in [window_start, window_end)    (UTC range — original Telegram timestamp)
    - cards: knowledge_cards.card_status = 'approved'
    - cards: linked message_versions also pass the above filter
    """
    if type != "daily":
        raise ValueError(f"T7-03 only supports type='daily', got {type!r}")

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
          AND {_FORGET_EVENT_NOT_EXISTS}
        GROUP BY kc.id, kc.title, kc.body_markdown, kc.approved_at
        ORDER BY kc.approved_at DESC NULLS LAST
        LIMIT 30
    """)
    card_rows = (
        await session.execute(
            cards_sql,
            {"source_chat_id": source_chat_id, "ws": window_start, "we": window_end},
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
    if len(cards) < digest_config.min_cards_threshold:
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
              AND {_FORGET_EVENT_NOT_EXISTS}
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
                    "top_n": digest_config.raw_message_top_n,
                },
            )
        ).mappings().all()

        # Apply token budget — drop from tail
        headroom = digest_config.token_budget_input - 1000
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
