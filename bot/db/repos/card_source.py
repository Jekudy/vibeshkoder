"""Repository for ``card_sources`` (T6-04 / T6-05).

Flush-only — NEVER calls ``session.commit()`` or ``session.rollback()``.
The caller (handler) owns the transaction lifecycle.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import CardSource, ChatMessage, MessageVersion


@dataclass(frozen=True)
class CardSourceJoinedRow:
    """Result shape for ``list_for_card`` — joins card_sources, message_versions,
    and chat_messages so the ``/card <id>`` renderer can show back-citations.
    """

    card_source_id: uuid.UUID
    message_version_id: int
    position: int
    chat_id: int
    message_id: int
    memory_policy: str
    is_redacted: bool
    mv_is_redacted: bool


class CardSourceRepo:
    @staticmethod
    async def bulk_create(
        session: AsyncSession,
        *,
        card_id: uuid.UUID,
        message_version_ids: list[int],
    ) -> list[CardSource]:
        """Insert one ``card_sources`` row per mvid with enumerated position.

        Step 6 of the §5.C ``/approve`` 8-step protocol. The DB UNIQUE on
        ``(card_id, message_version_id)`` enforces idempotency of repeat
        approvals; a duplicate insert raises ``IntegrityError`` and the
        caller's transaction rolls back.
        """
        rows = [
            CardSource(
                card_id=card_id,
                message_version_id=mvid,
                position=idx,
            )
            for idx, mvid in enumerate(message_version_ids)
        ]
        session.add_all(rows)
        await session.flush()
        return rows

    @staticmethod
    async def list_for_card(
        session: AsyncSession,
        card_id: uuid.UUID,
    ) -> list[CardSourceJoinedRow]:
        """Return joined rows for back-citation rendering.

        Filter / rendering logic in ``/card <id>`` consults
        ``memory_policy``, ``is_redacted``, and ``mv_is_redacted`` to decide
        whether to render a Telegram message link or a redacted placeholder
        per T6-05 design §3.
        """
        stmt = (
            select(
                CardSource.id.label("card_source_id"),
                CardSource.message_version_id.label("message_version_id"),
                CardSource.position.label("position"),
                ChatMessage.chat_id.label("chat_id"),
                ChatMessage.message_id.label("message_id"),
                ChatMessage.memory_policy.label("memory_policy"),
                ChatMessage.is_redacted.label("is_redacted"),
                MessageVersion.is_redacted.label("mv_is_redacted"),
            )
            .join(
                MessageVersion,
                MessageVersion.id == CardSource.message_version_id,
            )
            .join(
                ChatMessage,
                ChatMessage.id == MessageVersion.chat_message_id,
            )
            .where(CardSource.card_id == card_id)
            .order_by(CardSource.position.asc(), CardSource.id.asc())
        )
        result = await session.execute(stmt)
        return [
            CardSourceJoinedRow(
                card_source_id=row.card_source_id,
                message_version_id=row.message_version_id,
                position=row.position,
                chat_id=row.chat_id,
                message_id=row.message_id,
                memory_policy=row.memory_policy,
                is_redacted=row.is_redacted,
                mv_is_redacted=row.mv_is_redacted,
            )
            for row in result.all()
        ]
