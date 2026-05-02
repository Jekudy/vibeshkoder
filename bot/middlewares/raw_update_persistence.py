"""Raw update persistence middleware (T1-04).

Sits INSIDE ``DbSessionMiddleware`` (registered after it, so it runs nested) so the
session is open and the persistence happens in the SAME transaction the handler will
later commit. Per the ``#offrecord`` ordering rule from
``docs/memory-system/AUTHORIZED_SCOPE.md``, the raw row + policy detection + any
redaction land in one atomic commit.

Behavior is gated by feature flag ``memory.ingestion.raw_updates.enabled`` (default OFF).
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update
from sqlalchemy.exc import SQLAlchemyError

from bot.services.ingestion import record_update

logger = logging.getLogger(__name__)


class RawUpdatePersistenceMiddleware(BaseMiddleware):
    """Persist the raw aiogram ``Update`` before the handler runs.

    Failure-isolation policy (explicit, approved exception to global no-blanket-catch
    rule from CLAUDE.md):

    Raw-archive failure MUST NOT break the gatekeeper handler — uptime of onboarding,
    vouching, intro refresh outweighs archive completeness in this cycle. We isolate the
    failure with a SQL SAVEPOINT (``session.begin_nested()``) so a DB error inside
    ``record_update`` rolls back ONLY the raw insert, leaving the outer transaction
    clean for the handler. We catch ``SQLAlchemyError`` (the only family that can leave
    the session in a failed-tx state) and log with the update_id; non-DB exceptions
    propagate to ``DbSessionMiddleware`` for a normal full rollback.

    This is the ONE approved site in the codebase for this pattern. Other services must
    follow the global re-raise policy.
    """

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
                    async with session.begin_nested():
                        live_run_id = data.get("live_ingestion_run_id")
                        raw_row = await record_update(session, event, ingestion_run_id=live_run_id)
                        if raw_row is not None:
                            data["raw_update"] = raw_row
                except SQLAlchemyError as exc:
                    logger.warning(
                        "raw update persistence failed; continuing to handler",
                        extra={"update_id": getattr(event, "update_id", None)},
                        exc_info=exc,
                    )
                # Non-DB exceptions propagate so DbSessionMiddleware can roll back
                # the entire outer transaction and aiogram can log the trace.
        return await handler(event, data)
