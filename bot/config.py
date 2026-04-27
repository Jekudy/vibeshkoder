from __future__ import annotations

import logging
import secrets

from pydantic import model_validator
from pydantic_settings import BaseSettings

_MIN_WEB_PASSWORD_LENGTH = 12
_MIN_WEB_SESSION_SECRET_LENGTH = 32


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
    DEV_MODE: bool = False  # Permissive checks (e.g. ephemeral web password / session secret).
    # Note: postgres is required regardless of DEV_MODE (T0-02). See docs/memory-system/DEV_SETUP.md.

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    @model_validator(mode="after")
    def validate_web_password(self) -> Settings:
        if self.WEB_PASSWORD is None:
            if self.DEV_MODE:
                logging.warning("WEB_PASSWORD is not set; generated an ephemeral dev password")
                self.WEB_PASSWORD = secrets.token_urlsafe(16)
                return self

            raise ValueError("WEB_PASSWORD must be at least 12 characters in production")

        if len(self.WEB_PASSWORD) >= _MIN_WEB_PASSWORD_LENGTH:
            return self

        if self.DEV_MODE:
            logging.warning("WEB_PASSWORD is shorter than 12 characters; accepted in DEV_MODE only")
            return self

        raise ValueError("WEB_PASSWORD must be at least 12 characters in production")

    @model_validator(mode="after")
    def validate_web_session_secret(self) -> Settings:
        if self.WEB_SESSION_SECRET is None:
            if self.DEV_MODE:
                logging.warning(
                    "WEB_SESSION_SECRET is not set; generated an ephemeral dev session secret"
                )
                self.WEB_SESSION_SECRET = secrets.token_urlsafe(32)
                return self

            raise ValueError(
                "WEB_SESSION_SECRET must be at least 32 characters in production"
            )

        if len(self.WEB_SESSION_SECRET) >= _MIN_WEB_SESSION_SECRET_LENGTH:
            return self

        if self.DEV_MODE:
            logging.warning(
                "WEB_SESSION_SECRET is shorter than 32 characters; accepted in DEV_MODE only"
            )
            return self

        raise ValueError("WEB_SESSION_SECRET must be at least 32 characters in production")


settings = Settings()  # type: ignore[call-arg]
