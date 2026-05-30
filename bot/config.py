from __future__ import annotations

import logging
import secrets
import warnings

from pydantic import model_validator
from pydantic_settings import BaseSettings

_MIN_WEB_PASSWORD_LENGTH = 12
_MIN_WEB_SESSION_SECRET_LENGTH = 32


def _validate_password_field(value: str | None, field_name: str, dev_mode: bool) -> str:
    """Shared min-length validator for web password fields.

    Returns the value (possibly auto-generated) or raises ValueError.
    """
    if value is None:
        if dev_mode:
            logging.warning(
                "%s is not set; generated an ephemeral dev password", field_name
            )
            return secrets.token_urlsafe(16)
        raise ValueError(f"{field_name} must be at least 12 characters in production")

    if len(value) >= _MIN_WEB_PASSWORD_LENGTH:
        return value

    if dev_mode:
        logging.warning(
            "%s is shorter than 12 characters; accepted in DEV_MODE only", field_name
        )
        return value

    raise ValueError(f"{field_name} must be at least 12 characters in production")


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
    # ── Web passwords ─────────────────────────────────────────────────────────
    # WEB_PASSWORD is kept for backward compatibility. If WEB_PASSWORD is set and
    # WEB_ADMIN_PASSWORD is NOT set, WEB_PASSWORD is aliased to WEB_ADMIN_PASSWORD
    # with a DeprecationWarning. This alias is valid for one release cycle.
    WEB_PASSWORD: str | None = None
    WEB_ADMIN_PASSWORD: str | None = None
    WEB_MEMBER_PASSWORD: str | None = None
    WEB_SESSION_SECRET: str | None = None
    DEV_MODE: bool = False  # Permissive checks (e.g. ephemeral web password / session secret).
    # Note: postgres is required regardless of DEV_MODE (T0-02). See docs/memory-system/DEV_SETUP.md.
    HEALTHZ_PORT: int = 3000  # aiohttp /healthz server port (issue #168). Matches EXPOSE 3000 in Dockerfile.bot.

    # ── Neo4j (Phase 10 graph projection) ────────────────────────────────────
    # Production MUST set NEO4J_AUTH_PASSWORD to a 32+ char rotated value.
    # Production MUST use bolt+s:// (TLS). Dev default is plaintext bolt://.
    NEO4J_BOLT_URI: str = "bolt://neo4j:7687"
    NEO4J_AUTH_USER: str = "neo4j"
    NEO4J_AUTH_PASSWORD: str = "test_password_min_32_chars_for_neo4j_5"  # dev default
    NEO4J_DATABASE: str = "neo4j"

    # ── Butler / T12-07 undo controls ────────────────────────────────────────
    # How long after execution an action can be undone (minutes).
    # Env var: BUTLER_UNDO_TTL_MINUTES (pydantic maps case-insensitively).
    butler_undo_ttl_minutes: int = 60

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    @model_validator(mode="after")
    def validate_web_password(self) -> Settings:
        # Backward-compat alias: WEB_PASSWORD → WEB_ADMIN_PASSWORD if admin not set.
        # Empty string is treated as unset (falls through to "neither set" branch
        # which preserves the original WEB_PASSWORD-named error for deployers).
        if self.WEB_PASSWORD and self.WEB_ADMIN_PASSWORD is None:
            warnings.warn(
                "WEB_PASSWORD is deprecated; set WEB_ADMIN_PASSWORD instead. "
                "WEB_PASSWORD will be removed in a future release.",
                DeprecationWarning,
                stacklevel=2,
            )
            logging.warning(
                "WEB_PASSWORD is set but WEB_ADMIN_PASSWORD is not; "
                "aliasing WEB_PASSWORD → WEB_ADMIN_PASSWORD for one release cycle."
            )
            self.WEB_ADMIN_PASSWORD = self.WEB_PASSWORD

        # If neither WEB_ADMIN_PASSWORD nor WEB_PASSWORD was set (None OR empty
        # string), keep legacy behaviour: WEB_PASSWORD gets an ephemeral dev
        # value, OR prod raises with the WEB_PASSWORD-named error preserved.
        if not self.WEB_ADMIN_PASSWORD and not self.WEB_PASSWORD:
            if self.DEV_MODE:
                logging.warning("WEB_PASSWORD is not set; generated an ephemeral dev password")
                self.WEB_PASSWORD = secrets.token_urlsafe(16)
                self.WEB_ADMIN_PASSWORD = self.WEB_PASSWORD
                return self
            raise ValueError("WEB_PASSWORD must be at least 12 characters in production")

        # Validate WEB_ADMIN_PASSWORD length.
        self.WEB_ADMIN_PASSWORD = _validate_password_field(
            self.WEB_ADMIN_PASSWORD, "WEB_ADMIN_PASSWORD", self.DEV_MODE
        )
        # Keep WEB_PASSWORD in sync so existing callers of settings.WEB_PASSWORD still work.
        if self.WEB_PASSWORD is None:
            self.WEB_PASSWORD = self.WEB_ADMIN_PASSWORD

        # Validate WEB_MEMBER_PASSWORD length (optional — None means member login disabled).
        if self.WEB_MEMBER_PASSWORD is not None:
            self.WEB_MEMBER_PASSWORD = _validate_password_field(
                self.WEB_MEMBER_PASSWORD, "WEB_MEMBER_PASSWORD", self.DEV_MODE
            )

        # Guard: admin and member passwords must differ.
        if (
            self.WEB_MEMBER_PASSWORD is not None
            and self.WEB_MEMBER_PASSWORD == self.WEB_ADMIN_PASSWORD
        ):
            raise ValueError(
                "WEB_ADMIN_PASSWORD and WEB_MEMBER_PASSWORD must not be equal — "
                "use distinct passwords for each role."
            )

        return self

    @model_validator(mode="after")
    def validate_web_session_secret(self) -> Settings:
        if self.WEB_SESSION_SECRET is None:
            if self.DEV_MODE:
                logging.warning(
                    "WEB_SESSION_SECRET is not set; generated an ephemeral dev session secret"
                )
                self.WEB_SESSION_SECRET = secrets.token_urlsafe(32)
                return self

            raise ValueError("WEB_SESSION_SECRET must be at least 32 characters in production")

        if len(self.WEB_SESSION_SECRET) >= _MIN_WEB_SESSION_SECRET_LENGTH:
            return self

        if self.DEV_MODE:
            logging.warning(
                "WEB_SESSION_SECRET is shorter than 32 characters; accepted in DEV_MODE only"
            )
            return self

        raise ValueError("WEB_SESSION_SECRET must be at least 32 characters in production")


settings = Settings()  # type: ignore[call-arg]
