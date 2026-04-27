from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from tests.conftest import import_module

VALID_WEB_PASSWORD = "valid-test-pass"
VALID_WEB_SESSION_SECRET = "s" * 32


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
        WEB_SESSION_SECRET="test-session-secret",
        DEV_MODE=True,
    )

    assert settings.BOT_TOKEN == "123456:test-token"
    assert settings.ADMIN_IDS == [149820031]
    assert settings.DEV_MODE is True
    assert settings.WEB_PASSWORD == "test-pass"
    assert settings.WEB_SESSION_SECRET == "test-session-secret"


def test_config_no_password_prod_raises(app_env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEB_PASSWORD")
    monkeypatch.setenv("WEB_SESSION_SECRET", VALID_WEB_SESSION_SECRET)
    monkeypatch.setenv("DEV_MODE", "false")

    with pytest.raises(
        ValidationError, match="WEB_PASSWORD must be at least 12 characters in production"
    ):
        import_module("bot.config")


def test_settings_no_password_prod_raises_direct_call(
    app_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = import_module("bot.config")
    Settings = config.Settings
    monkeypatch.delenv("WEB_PASSWORD")
    monkeypatch.setenv("WEB_SESSION_SECRET", VALID_WEB_SESSION_SECRET)
    monkeypatch.setenv("DEV_MODE", "false")

    with pytest.raises(
        ValidationError, match="WEB_PASSWORD must be at least 12 characters in production"
    ):
        Settings()


def test_config_no_password_dev_warns(
    app_env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    config = import_module("bot.config")
    Settings = config.Settings
    monkeypatch.delenv("WEB_PASSWORD")
    monkeypatch.setenv("DEV_MODE", "true")

    with caplog.at_level(logging.WARNING):
        settings = Settings()

    assert settings.WEB_PASSWORD
    assert settings.WEB_PASSWORD != "admin"
    assert "WEB_PASSWORD is not set" in caplog.text


def test_config_no_session_secret_prod_raises(
    app_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = import_module("bot.config")
    Settings = config.Settings
    monkeypatch.setenv("WEB_PASSWORD", VALID_WEB_PASSWORD)
    monkeypatch.delenv("WEB_SESSION_SECRET")
    monkeypatch.setenv("DEV_MODE", "false")

    with pytest.raises(
        ValidationError,
        match="WEB_SESSION_SECRET must be at least 32 characters in production",
    ):
        Settings()


def test_config_no_session_secret_dev_warns(
    app_env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    config = import_module("bot.config")
    Settings = config.Settings
    monkeypatch.delenv("WEB_SESSION_SECRET")
    monkeypatch.setenv("DEV_MODE", "true")

    with caplog.at_level(logging.WARNING):
        settings = Settings()

    assert settings.WEB_SESSION_SECRET
    assert len(settings.WEB_SESSION_SECRET) >= 32
    assert "WEB_SESSION_SECRET is not set" in caplog.text


def test_config_explicit_password_used(app_env, monkeypatch: pytest.MonkeyPatch) -> None:
    config = import_module("bot.config")
    Settings = config.Settings
    monkeypatch.setenv("WEB_PASSWORD", "explicit-pass")
    monkeypatch.setenv("WEB_SESSION_SECRET", VALID_WEB_SESSION_SECRET)
    monkeypatch.setenv("DEV_MODE", "false")

    settings = Settings()

    assert settings.WEB_PASSWORD == "explicit-pass"


def test_empty_web_session_secret_prod_raises(
    app_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = import_module("bot.config")
    Settings = config.Settings
    monkeypatch.setenv("WEB_PASSWORD", VALID_WEB_PASSWORD)
    monkeypatch.setenv("WEB_SESSION_SECRET", "")
    monkeypatch.setenv("DEV_MODE", "false")

    with pytest.raises(
        ValidationError,
        match="WEB_SESSION_SECRET must be at least 32 characters in production",
    ):
        Settings()


def test_short_web_session_secret_prod_raises(
    app_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = import_module("bot.config")
    Settings = config.Settings
    monkeypatch.setenv("WEB_PASSWORD", VALID_WEB_PASSWORD)
    monkeypatch.setenv("WEB_SESSION_SECRET", "s" * 16)
    monkeypatch.setenv("DEV_MODE", "false")

    with pytest.raises(
        ValidationError,
        match="WEB_SESSION_SECRET must be at least 32 characters in production",
    ):
        Settings()


def test_short_web_session_secret_dev_warns(
    app_env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    config = import_module("bot.config")
    Settings = config.Settings
    monkeypatch.setenv("WEB_SESSION_SECRET", "s" * 16)
    monkeypatch.setenv("DEV_MODE", "true")

    with caplog.at_level(logging.WARNING):
        settings = Settings()

    assert settings.WEB_SESSION_SECRET == "s" * 16
    assert "WEB_SESSION_SECRET is shorter than 32 characters" in caplog.text


def test_empty_web_password_prod_raises(app_env, monkeypatch: pytest.MonkeyPatch) -> None:
    config = import_module("bot.config")
    Settings = config.Settings
    monkeypatch.setenv("WEB_PASSWORD", "")
    monkeypatch.setenv("WEB_SESSION_SECRET", VALID_WEB_SESSION_SECRET)
    monkeypatch.setenv("DEV_MODE", "false")

    with pytest.raises(
        ValidationError, match="WEB_PASSWORD must be at least 12 characters in production"
    ):
        Settings()
