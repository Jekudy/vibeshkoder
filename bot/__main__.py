from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot.config import settings
from bot.handlers import admin, alias, chat_events, chat_messages, forward_lookup, questionnaire, start, vouch
from bot.middlewares.db_session import DbSessionMiddleware
from bot.services.scheduler import start_scheduler, stop_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def _init_db() -> None:
    """Create tables in dev mode (SQLite)."""
    from bot.db.engine import engine
    from bot.db.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables ensured")


async def main() -> None:
    # Storage: Redis in prod, Memory in dev
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

    # Register middleware on all update types
    dp.update.middleware(DbSessionMiddleware())

    # Include routers (order matters — more specific first)
    dp.include_routers(
        start.router,
        questionnaire.router,
        vouch.router,
        admin.router,
        alias.router,
        chat_events.router,
        forward_lookup.router,
        chat_messages.router,  # lowest priority — catches all group messages
    )

    # Startup / shutdown hooks
    async def on_startup() -> None:
        start_scheduler(bot)
        bot_info = await bot.me()
        logger.info("Bot started: @%s", bot_info.username)

    async def on_shutdown() -> None:
        stop_scheduler()
        if redis is not None:
            await redis.aclose()
        logger.info("Bot stopped")

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    # Start polling — allowed_updates includes chat_member for join/leave
    await dp.start_polling(
        bot,
        allowed_updates=[
            "message",
            "callback_query",
            "chat_member",
            "my_chat_member",
        ],
    )


if __name__ == "__main__":
    asyncio.run(main())
