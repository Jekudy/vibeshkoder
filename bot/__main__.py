from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot.config import settings
from bot.handlers import (
    admin,
    chat_events,
    chat_messages,
    edited_message,
    forget_reply,
    forward_lookup,
    questionnaire,
    start,
    vouch,
)
from bot.middlewares.db_session import DbSessionMiddleware
from bot.middlewares.raw_update_persistence import RawUpdatePersistenceMiddleware
from bot.services.scheduler import start_scheduler, stop_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# Canonical list of Telegram update types this bot subscribes to.
#
# Rule (per docs/memory-system/HANDOFF.md §8 'allowed_updates rollout'): do NOT add an
# update type here unless a handler AND its persistence layer exist. Adding an update type
# without a handler means silent data loss — Telegram delivers updates we cannot process.
#
# Currently authorized:
#   - message              (chat_messages handler + others)
#   - callback_query       (vouch / questionnaire callbacks)
#   - chat_member          (chat_events handler — join / leave events)
#   - my_chat_member       (chat_events handler — bot-as-member status changes)
#   - edited_message       (T1-14 edited_message handler — appends v(n+1) message_versions)
#
# Phase 5 will add 'message_reaction' / 'message_reaction_count' once the reactions table
# and handler exist. Until then, leave them out.
_ALLOWED_UPDATES: tuple[str, ...] = (
    "message",
    "edited_message",
    "callback_query",
    "chat_member",
    "my_chat_member",
)


async def _init_db() -> None:
    """Ensure tables exist when running in dev mode without alembic.

    Production uses ``alembic upgrade head`` against postgres. Dev mode against an empty
    postgres can rely on this helper to bootstrap the schema directly from SQLAlchemy
    metadata, mirroring what ``alembic upgrade head`` would have produced.
    """
    from bot.db.engine import engine
    from bot.db.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables ensured")


async def main() -> None:
    # Storage: Redis in prod, in-memory FSM in dev. The DB driver is postgres in both modes
    # (T0-02; see bot/db/engine.py).
    if settings.DEV_MODE:
        from aiogram.fsm.storage.memory import MemoryStorage

        storage = MemoryStorage()
        redis = None
        await _init_db()
    else:
        from aiogram.fsm.storage.redis import RedisStorage
        from redis.asyncio import Redis

        redis = Redis.from_url(settings.REDIS_URL)
        storage = RedisStorage(redis=redis)

    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=storage)

    # Register middleware on all update types.
    # DbSessionMiddleware is OUTERMOST so the session is open before raw persistence
    # runs. RawUpdatePersistenceMiddleware (T1-04) persists the raw update inside the
    # same DB transaction the handler will commit. The persistence path is gated by
    # feature flag ``memory.ingestion.raw_updates.enabled`` (default OFF), so this
    # change is a behavior-preserving wiring until operators enable the flag.
    dp.update.middleware(DbSessionMiddleware())
    dp.update.middleware(RawUpdatePersistenceMiddleware())

    # Include routers (order matters — more specific first)
    dp.include_routers(
        start.router,
        questionnaire.router,
        vouch.router,
        admin.router,
        chat_events.router,
        forward_lookup.router,
        edited_message.router,  # T1-14: edited_message handler (before chat_messages catch-all)
        forget_reply.router,   # T3-02: /forget command handler (before chat_messages catch-all)
        chat_messages.router,  # lowest priority — catches all group messages
    )

    # Startup / shutdown hooks
    async def on_startup() -> None:
        from bot.services.health import report, startup_log_lines

        start_scheduler(bot)
        bot_info = await bot.me()
        logger.info("Bot started: @%s id=%s", bot_info.username, bot_info.id)
        # Log non-secret startup banner lines (T0-05).
        for line in startup_log_lines():
            logger.info("startup: %s", line)
        h = await report()
        logger.info(
            "startup health: db.ok=%s settings_sanity.ok=%s",
            h.db.ok,
            h.settings_sanity.ok,
        )
        if not h.ok:
            logger.warning(
                "startup health degraded: db.reason=%r settings.reason=%r",
                h.db.reason,
                h.settings_sanity.reason,
            )
        # Log allowed_updates so we can verify the rollout invariant
        # (no update type without a handler — see HANDOFF.md §8).
        logger.info("startup: allowed_updates=%s", _ALLOWED_UPDATES)

    async def on_shutdown() -> None:
        stop_scheduler()
        if redis is not None:
            await redis.aclose()
        logger.info("Bot stopped")

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    # Start polling — see _ALLOWED_UPDATES above for the canonical list.
    # aiogram expects a list, so materialize the immutable tuple here.
    await dp.start_polling(bot, allowed_updates=list(_ALLOWED_UPDATES))


if __name__ == "__main__":
    asyncio.run(main())
