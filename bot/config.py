from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    BOT_TOKEN: str
    COMMUNITY_CHAT_ID: int = 0
    ADMIN_IDS: list[int] = []
    DATABASE_URL: str
    REDIS_URL: str = "redis://redis:6379/0"
    GOOGLE_SHEETS_CREDS_FILE: str = ""
    GOOGLE_SHEET_ID: str = ""
    WEB_BASE_URL: str = "http://localhost:8080"
    WEB_BOT_USERNAME: str = ""
    VOUCH_TIMEOUT_HOURS: int = 72
    NUDGE_TIMEOUT_HOURS: int = 48
    INTRO_REFRESH_DAYS: int = 90
    INTRO_NUDGE_PHASE_1_MAX: int = 5
    INTRO_NUDGE_PHASE_2_MAX: int = 8
    WEB_PASSWORD: str
    DEV_MODE: bool = False  # Use SQLite + MemoryStorage for local testing

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()  # type: ignore[call-arg]
