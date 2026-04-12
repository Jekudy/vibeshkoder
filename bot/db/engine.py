from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine, AsyncSession

from bot.config import settings

if settings.DEV_MODE:
    _url = "sqlite+aiosqlite:///vibe_gatekeeper.db"
    engine = create_async_engine(_url, echo=False)
else:
    _url = settings.DATABASE_URL
    engine = create_async_engine(_url, echo=False)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
