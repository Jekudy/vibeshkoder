from __future__ import annotations

import logging
import secrets

from pydantic import model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    BOT_TOKEN: str
    COMMUNITY_CHAT_ID: int = 0
    ADMIN_IDS: list[int] = []
    DATABASE_URL: str = "postgresql+asyncpg://vibe:changeme@db:5432/vibe_gatekeeper"
    REDIS_URL: str = "redis://redis:6379/0"
    GOOGLE_SHEETS_CREDS_FILE: str = ""
    GOOGLE_SHEET_ID: str = ""
    WEB_BASE_URL: str = "http://localhost:8080"
    WEB_BOT_USERNAME: str = ""
    VOUCH_TIMEOUT_HOURS: int = 72
    NUDGE_TIMEOUT_HOURS: int = 48
    INTRO_REFRESH_DAYS: int = 90
    WEB_PASSWORD: str | None = None
    WEB_SESSION_SECRET: str | None = None
    DEV_MODE: bool = False  # Use SQLite + MemoryStorage for local testing

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    @model_validator(mode="after")
    def validate_web_password(self) -> Settings:
        if self.WEB_PASSWORD is not None:
            return self

        if self.DEV_MODE:
            logging.warning("WEB_PASSWORD is not set; generated an ephemeral dev password")
            self.WEB_PASSWORD = secrets.token_urlsafe(16)
            return self

        raise ValueError("WEB_PASSWORD is required when DEV_MODE=false")

    @model_validator(mode="after")
    def validate_web_session_secret(self) -> Settings:
        if self.WEB_SESSION_SECRET is not None:
            return self

        if self.DEV_MODE:
            logging.warning("WEB_SESSION_SECRET is not set; generated an ephemeral dev session secret")
            self.WEB_SESSION_SECRET = secrets.token_urlsafe(32)
            return self

        raise ValueError("WEB_SESSION_SECRET is required when DEV_MODE=false")


settings = Settings()  # type: ignore[call-arg]
