"""Build the complete governance-eligible context for one digest window.

Every eligible current message is returned in chronological order. The query
never applies cards-first selection, top-N selection, or token-budget
truncation; prompt-size enforcement happens later and fails the whole run.

Governance filter:
- chat_messages.memory_policy = 'normal' (excludes nomem, offrecord, forgotten)
- message_versions.is_redacted = FALSE   (excludes cascade-redacted rows)
- NO active forget_event ('pending' / 'processing' / 'completed') targeting
  this message_version via message_id, user_id, or message_hash. This is the
  defense-in-depth check that catches forget_events whose cascade hasn't yet
  flipped is_redacted to TRUE.

The shared forget/control predicates keep this query aligned with the other
derived-memory pipelines without copying their SQL rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.forget_predicate import forget_excludes_sql_fragment
from bot.services.control_messages import control_message_excludes_sql_fragment

# Forget-event exclusion predicate — sourced from the shared helper.
# Issue #291 extracted the inline SQL fragment to bot/services/forget_predicate.py
# so that digest_context.py, llm_gateway.py, and forget_cascade.py all use the
# SAME predicate string.  Changing the predicate semantics requires updating
# forget_predicate.py AND the golden snapshot in test_forget_predicate_parity.py.
_FORGET_EXCLUDES = forget_excludes_sql_fragment()
_CONTROL_EXCLUDES = control_message_excludes_sql_fragment()


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
    author_display: str
    text: str
    ts: datetime
    author_username: str | None = None
    telegram_message_id: int | None = None
    caption: str | None = None
    message_kind: str | None = None
    reply_to_message_id: int | None = None
    message_thread_id: int | None = None
    media_kind: str | None = None
    media_description: str | None = None
    forward_origin_type: str | None = None
    forward_origin_display: str | None = None
    forward_origin_date: str | None = None


@dataclass(frozen=True)
class DigestContext:
    type: Literal["daily", "weekly"]
    window_start: datetime
    window_end: datetime
    source_chat_id: int
    cards: list[DigestContextCard]
    messages: list[DigestContextMessage]


async def build_digest_context(
    session: AsyncSession,
    *,
    type: Literal["daily", "weekly"],
    window_start: datetime,
    window_end: datetime,
    source_chat_id: int,
) -> DigestContext:
    """Build the full governance-filtered context for digest synthesis.

    Governance filter applied to every returned message:
    - chat_messages.memory_policy = 'normal'  (excludes nomem, offrecord, forgotten)
    - message_versions.is_redacted = FALSE     (excludes cascade-redacted rows)
    - chat_messages.chat_id = :source_chat_id  (single-chat MVP)
    - cm.date in [window_start, window_end)    (UTC range — original Telegram timestamp)
    """
    if type not in ("daily", "weekly"):
        raise ValueError(
            f"build_digest_context: unsupported type {type!r}; expected 'daily' or 'weekly'"
        )

    # ---- complete current-message window ----
    messages: list[DigestContextMessage] = []
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- module-level governance fragments contain no user input.
    raw_sql = text(f"""
        SELECT
            mv.id AS message_version_id,
            mv.chat_message_id,
            cm.message_id AS telegram_message_id,
            concat_ws(' ', u.first_name, u.last_name) AS author_display,
            u.username AS author_username,
            COALESCE(mv.normalized_text, mv.text, '') AS text,
            mv.caption,
            cm.message_kind,
            cm.reply_to_message_id,
            cm.message_thread_id,
            mm.media_kind,
            CASE WHEN mm.description_status = 'ready' THEN mm.description END
                AS media_description,
            cm.raw_json #>> '{{forward_origin,type}}' AS forward_origin_type,
            cm.raw_json #>> '{{forward_origin,date}}' AS forward_origin_date,
            COALESCE(
                NULLIF(concat_ws(
                    ' ',
                    cm.raw_json #>> '{{forward_origin,sender_user,first_name}}',
                    cm.raw_json #>> '{{forward_origin,sender_user,last_name}}'
                ), ''),
                cm.raw_json #>> '{{forward_origin,sender_user_name}}',
                cm.raw_json #>> '{{forward_origin,chat,title}}',
                cm.raw_json #>> '{{forward_origin,author_signature}}'
            ) AS forward_origin_display,
            cm.date AS ts
        FROM message_versions mv
        JOIN chat_messages cm ON cm.id = mv.chat_message_id
        JOIN users u ON u.id = cm.user_id
        LEFT JOIN message_media mm ON mm.chat_message_id = cm.id
        WHERE cm.chat_id = :source_chat_id
          AND cm.current_version_id = mv.id
          AND cm.date >= :ws
          AND cm.date <  :we
          AND cm.memory_policy = 'normal'
          AND cm.is_redacted = FALSE
          AND mv.is_redacted = FALSE
          AND {_CONTROL_EXCLUDES}
          AND {_FORGET_EXCLUDES}
        ORDER BY cm.date ASC, cm.message_id ASC
    """)
    raw_rows = (
        (
            await session.execute(
                raw_sql,
                {"source_chat_id": source_chat_id, "ws": window_start, "we": window_end},
            )
        )
        .mappings()
        .all()
    )
    messages = [
        DigestContextMessage(
            message_version_id=row["message_version_id"],
            chat_message_id=row["chat_message_id"],
            telegram_message_id=row["telegram_message_id"],
            author_display=row["author_display"],
            author_username=row["author_username"],
            text=row["text"],
            caption=row["caption"],
            message_kind=row["message_kind"],
            reply_to_message_id=row["reply_to_message_id"],
            message_thread_id=row["message_thread_id"],
            media_kind=row["media_kind"],
            media_description=row["media_description"],
            forward_origin_type=row["forward_origin_type"],
            forward_origin_display=row["forward_origin_display"],
            forward_origin_date=row["forward_origin_date"],
            ts=row["ts"],
        )
        for row in raw_rows
    ]

    return DigestContext(
        type=type,
        window_start=window_start,
        window_end=window_end,
        source_chat_id=source_chat_id,
        cards=[],
        messages=messages,
    )
