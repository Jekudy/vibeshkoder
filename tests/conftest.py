from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator

import pytest


def _clear_modules() -> None:
    for name in list(sys.modules):
        if name == "bot" or name.startswith("bot.") or name == "web" or name.startswith("web."):
            sys.modules.pop(name, None)


@pytest.fixture()
def app_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("COMMUNITY_CHAT_ID", "-1001234567890")
    monkeypatch.setenv("ADMIN_IDS", "[149820031]")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://vibe:changeme@db:5432/vibe_gatekeeper")
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("GOOGLE_SHEETS_CREDS_FILE", "")
    monkeypatch.setenv("GOOGLE_SHEET_ID", "")
    monkeypatch.setenv("WEB_BASE_URL", "http://localhost:8080")
    monkeypatch.setenv("WEB_BOT_USERNAME", "vibeshkoder_dev_bot")
    monkeypatch.setenv("DB_PASSWORD", "changeme")
    monkeypatch.setenv("WEB_PASSWORD", "test-pass")
    monkeypatch.setenv("WEB_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("DEV_MODE", "true")
    _clear_modules()
    yield
    _clear_modules()


def import_module(name: str):
    return importlib.import_module(name)
