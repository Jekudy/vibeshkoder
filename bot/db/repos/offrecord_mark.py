"""Repository for ``offrecord_marks`` (T1-13).

Thin data-access layer. The chat_messages handler (T1-12 wiring) calls
``create_for_message`` whenever ``detect_policy`` returns non-normal. Phase 3 admin
revoke flows will extend this with ``set_status`` etc.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import OffrecordMark


class OffrecordMarkRepo:
    @staticmethod
    async def create_for_message(
        session: AsyncSession,
        chat_message_id: int,
        mark_type: str,
        detected_by: str,
        set_by_user_id: int | None = None,
        thread_id: int | None = None,
    ) -> OffrecordMark:
        """Insert a mark with ``scope_type='message'`` for a single chat_messages row.

        Flushes; does not commit. Caller controls the transaction lifecycle (typically
        the chat_messages handler's session, which DbSessionMiddleware commits on
        handler success).

        ``mark_type`` must be ``'nomem'`` or ``'offrecord'`` — the DB CHECK enforces
        this; the repo does not pre-validate so the caller surfaces the postgres error
        directly.
        """
        row = OffrecordMark(
            mark_type=mark_type,
            scope_type="message",
            scope_id=str(chat_message_id),
            chat_message_id=chat_message_id,
            thread_id=thread_id,
            set_by_user_id=set_by_user_id,
            detected_by=detected_by,
            status="active",
        )
        session.add(row)
        await session.flush()
        return row
