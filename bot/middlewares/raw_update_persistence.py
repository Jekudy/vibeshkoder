"""Raw update persistence middleware (T1-04).

Sits INSIDE ``DbSessionMiddleware`` (registered after it, so it runs nested) so the
session is open and the persistence happens in the SAME transaction the handler will
later commit. Per the ``#offrecord`` ordering rule from
``docs/memory-system/AUTHORIZED_SCOPE.md``, the raw row + policy detection + any
redaction land in one atomic commit.

Behavior is gated by feature flag ``memory.ingestion.raw_updates.enabled`` (default OFF).
Failures in the raw-archive path are logged but do NOT break the user-facing handler —
the gatekeeper bot must keep working even if the memory pipeline misbehaves.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from bot.services.ingestion import record_update

logger = logging.getLogger(__name__)


class RawUpdatePersistenceMiddleware(BaseMiddleware):
    """Persist the raw aiogram ``Update`` before the handler runs."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Update):
            session = data.get("session")
            if session is not None:
                try:
                    await record_update(session, event)
                except Exception:  # pragma: no cover — defensive; real failures logged
                    logger.exception(
                        "raw update persistence failed; continuing to handler"
                    )
        return await handler(event, data)
