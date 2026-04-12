from __future__ import annotations

from tests.conftest import import_module


def test_settings_accept_explicit_values(app_env) -> None:
    config = import_module("bot.config")
    Settings = config.Settings
    settings = Settings(
        BOT_TOKEN="123456:test-token",
        COMMUNITY_CHAT_ID=-1001234567890,
        ADMIN_IDS=[149820031],
        DATABASE_URL="postgresql+asyncpg://vibe:changeme@db:5432/vibe_gatekeeper",
        REDIS_URL="redis://redis:6379/0",
        GOOGLE_SHEETS_CREDS_FILE="",
        GOOGLE_SHEET_ID="",
        WEB_BASE_URL="http://localhost:8080",
        WEB_BOT_USERNAME="vibeshkoder_dev_bot",
        DB_PASSWORD="changeme",
        WEB_PASSWORD="test-pass",
        DEV_MODE=True,
    )

    assert settings.BOT_TOKEN == "123456:test-token"
    assert settings.ADMIN_IDS == [149820031]
    assert settings.DEV_MODE is True
    assert settings.WEB_PASSWORD == "test-pass"
