"""Cross-handler advisory locks for memory-system races (#80).

Protects against TOCTOU between chat_messages, edited_message, and ingestion
paths that all read+write the same (chat_id, message_id) row.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def advisory_lock_chat_message(
    session: AsyncSession, chat_id: int, message_id: int
) -> None:
    """Acquire pg_advisory_xact_lock keyed by (chat_id, message_id).

    Releases automatically at end of transaction. Cooperative with ``with_for_update()``
    on subsequent SELECTs of ``chat_messages.id = ...`` rows.

    Key format: ``chat_msg:{chat_id}:{message_id}``. All handlers MUST use this format.
    """
    key = f"chat_msg:{chat_id}:{message_id}"
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key})
