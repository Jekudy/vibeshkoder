from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiohttp import web

from bot.config import settings
from bot.services.health import check_db, report
from bot.handlers import (
    admin,
    admin_cards,
    admin_extract,
    admin_graph,
    chat_events,
    chat_messages,
    digest,
    edited_message,
    forget_me,
    forget_reply,
    forward_lookup,
    questionnaire,
    qa,
    start,
    vouch,
    wiki,
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


async def _healthz_handler(request: web.Request) -> web.Response:
    """GET /healthz — returns 200 when healthy, 503 otherwise. No secrets in body."""
    h = await report()
    status_code = 200 if h.ok else 503
    return web.json_response(h.to_dict(), status=status_code)


async def _healthz_db_handler(request: web.Request) -> web.Response:
    """GET /healthz/db — DB-only roundtrip check. Fast, no other checks.

    Returns 200 + {"db": "ok"} when the DB is reachable.
    Returns 503 + {"db": "fail", "reason": "<exc class>"} when not.

    No secrets are included: check_db() returns only the exception class name,
    never the full connection string or credentials.
    Consumed by ops/healing/healthcheck.py after issue #270.
    """
    result = await check_db()
    if result.ok:
        return web.json_response({"db": "ok"}, status=200)
    return web.json_response({"db": "fail", "reason": result.reason}, status=503)


async def start_healthz_runner(
    host: str = "0.0.0.0",
    port: int = 3000,
) -> tuple[web.AppRunner, int]:
    """Set up and start the aiohttp /healthz server.

    Returns ``(runner, bound_port)`` so callers can discover the actual port when
    port=0 is passed (OS picks a free port — used in tests for isolation).
    """
    app = web.Application()
    app.router.add_get("/healthz", _healthz_handler)
    app.router.add_get("/healthz/db", _healthz_db_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    # Resolve the actual bound port (important when port=0 was passed).
    bound_port: int = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    logger.info("healthz_server_started", extra={"port": bound_port})
    return runner, bound_port


async def run_healthz_server(port: int = 3000) -> None:
    """Start the aiohttp /healthz server and keep running until cancelled.

    Designed to be used in ``asyncio.gather(run_polling(...), run_healthz_server(...))``.
    On cancellation, runner.cleanup() is called to release the socket.
    """
    runner, bound_port = await start_healthz_runner(host="0.0.0.0", port=port)
    try:
        # Block until cancelled — asyncio.Event.wait() is cancellation-safe.
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


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
        admin_extract.router,  # T6-03: /admin_extract — Phase 6 backfill (private chat + admin gated)
        admin_cards.router,    # T6-04/T6-05: /candidates /approve /reject /cards /card
        admin_graph.router,    # T10-07: /graph_project_now /graph_stats /graph_query /graph_purge_now
        digest.router,         # T7-06: /digest_now /digest_preview /digest_history
        wiki.router,           # T9-06: /wiki_publish /wiki_unpublish /wiki_robots (admin-only)
        chat_events.router,
        edited_message.router,  # T1-14: edited_message handler (before chat_messages catch-all)
        forget_me.router,  # T3-03: /forget_me command (DM or in-chat)
        forget_reply.router,   # T3-02: /forget command handler (before chat_messages catch-all)
        qa.router,  # T4-04: /recall q&a handler, runtime-gated by memory.qa.enabled
        forward_lookup.router,
        chat_messages.router,  # lowest priority — catches all group messages
    )

    # Startup / shutdown hooks
    async def on_startup() -> None:
        from bot.db.engine import async_session
        from bot.services.health import startup_log_lines
        from bot.services.ingestion import get_or_create_live_run

        # Cache the live ingestion run id so the RawUpdatePersistenceMiddleware can pass
        # it to record_update without a per-update DB query.  Accessible via data dict in
        # all middlewares/handlers through aiogram's dp workflow-data mechanism.
        async with async_session() as session:
            live_run = await get_or_create_live_run(session)
            await session.commit()
        dp["live_ingestion_run_id"] = live_run.id

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

    # Run Telegram polling and the /healthz HTTP server concurrently.
    # asyncio.gather propagates the first exception to both coroutines and cancels the other,
    # so a crash in either coroutine brings down the process cleanly.
    await asyncio.gather(
        dp.start_polling(bot, allowed_updates=list(_ALLOWED_UPDATES)),
        run_healthz_server(port=settings.HEALTHZ_PORT),
    )


if __name__ == "__main__":
    asyncio.run(main())
