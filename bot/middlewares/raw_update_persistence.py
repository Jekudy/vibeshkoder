"""Raw update persistence middleware (T1-04).

Sits INSIDE ``DbSessionMiddleware`` (registered after it, so it runs nested) and commits
the raw source row before normalized persistence or product dispatch.  A later handler
failure can therefore roll back its own transaction without deleting the source archive.
Per the ``#offrecord`` ordering rule from ``docs/memory-system/AUTHORIZED_SCOPE.md``,
policy detection, any redaction, and the raw insert still land in that one durable commit.

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

    Raw persistence is the source-of-truth boundary.  Its transaction commits before
    downstream dispatch for every aiogram ``Update`` shape.  A failed raw write therefore
    aborts the update before normalized/derived persistence or product handlers can run.
    Database errors are logged using taxonomy-only structured fields and then re-raised;
    exception messages, SQL parameters, update payloads and tracebacks are deliberately
    excluded from this middleware's log record.
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
                    live_run_id = data.get("live_ingestion_run_id")
                    raw_row = await record_update(session, event, ingestion_run_id=live_run_id)
                    if raw_row is not None:
                        # This is an intentional durability boundary.  The same session
                        # starts a fresh transaction when downstream middleware next uses
                        # it, and DbSessionMiddleware may roll that later transaction back
                        # without removing the already archived source update.
                        await session.commit()
                        data["raw_update"] = raw_row
                except SQLAlchemyError as exc:
                    logger.error(
                        "raw_update_persistence_failed",
                        extra={
                            "update_id": getattr(event, "update_id", None),
                            "error_class": type(exc).__name__,
                        },
                    )
                    raise
                # Non-DB exceptions propagate.  A downstream rollback cannot undo the
                # raw commit above.
        return await handler(event, data)
