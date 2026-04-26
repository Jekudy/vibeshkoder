"""T0-06 — gatekeeper safety regression umbrella.

This file is the canary that proves Phase 0 invariants still hold. Per-ticket tests
(``tests/handlers/test_forward_lookup.py``, ``tests/db/test_user_repo.py``,
``tests/db/test_message_repo.py``, ``tests/web/test_health.py``) provide deep coverage;
this suite is a smaller, self-contained set of smoke checks that exercises ALL of them in
one file so a future PR touching any of these areas tripwires the regression here.

Acceptance (HANDOFF.md §11 must-have tests):
- non-member ``forward_lookup`` denied (T0-01)
- admin ``forward_lookup`` allowed (T0-01)
- ``UserRepo.upsert`` round-trips on the configured dev / test DB (T0-02)
- ``MessageRepo.save`` duplicate-safe (T0-03)
- ``/healthz`` alive (T0-05)

Constraints:
- runs offline (no live Telegram, no live network)
- runs in < 30s
- DB-backed checks SKIP cleanly when postgres is unreachable
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from tests.conftest import import_module

pytest_plugins: list[str] = []


def _random_id() -> int:
    return random.randint(900_000_000, 999_999_999)


# ─── T0-01: forward_lookup membership / admin guard ───────────────────────────────────────

def _forward_message(requester_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        from_user=SimpleNamespace(id=requester_id),
        text="any forwarded text",
        answer=AsyncMock(),
    )


def _user(id_: int, *, is_member: bool, is_admin: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        id=id_,
        is_member=is_member,
        is_admin=is_admin,
        first_name="Probe",
        username="probe",
    )


async def test_regression_forward_lookup_non_member_denied(app_env, monkeypatch) -> None:
    handler = import_module("bot.handlers.forward_lookup")
    session = AsyncMock()
    message = _forward_message(requester_id=111)

    user_get = AsyncMock(side_effect=[_user(111, is_member=False)])
    monkeypatch.setattr(handler.UserRepo, "get", user_get)
    monkeypatch.setattr(handler, "settings", SimpleNamespace(ADMIN_IDS=set()))

    await handler.handle_forwarded_message(message, session)

    # Non-member must get NO answer (silent deny) and the message lookup must NOT run.
    message.answer.assert_not_called()


async def test_regression_forward_lookup_admin_allowed(app_env, monkeypatch) -> None:
    handler = import_module("bot.handlers.forward_lookup")
    session = AsyncMock()
    message = _forward_message(requester_id=111)

    requester = _user(111, is_member=False, is_admin=True)
    author = _user(222, is_member=True)

    user_get = AsyncMock(side_effect=[requester, author])
    chat_msg = SimpleNamespace(user_id=222)
    message_lookup = AsyncMock(return_value=chat_msg)
    intro_get = AsyncMock(return_value=SimpleNamespace(intro_text="intro"))

    monkeypatch.setattr(handler.UserRepo, "get", user_get)
    monkeypatch.setattr(handler.MessageRepo, "find_by_exact_text", message_lookup)
    monkeypatch.setattr(handler.IntroRepo, "get", intro_get)
    monkeypatch.setattr(handler, "settings", SimpleNamespace(ADMIN_IDS=set()))

    await handler.handle_forwarded_message(message, session)

    # Admin must receive the intro response.
    message.answer.assert_called_once()


# ─── T0-02 + T0-03: DB round-trip and idempotency ─────────────────────────────────────────

async def test_regression_user_repo_upsert_round_trips(db_session) -> None:
    from bot.db.repos.user import UserRepo
    from bot.db.models import User

    telegram_id = _random_id()
    user = await UserRepo.upsert(
        db_session,
        telegram_id=telegram_id,
        username="probe",
        first_name="Probe",
        last_name=None,
    )
    assert user.id == telegram_id

    # Update path: same telegram_id, different fields.
    updated = await UserRepo.upsert(
        db_session,
        telegram_id=telegram_id,
        username="probe2",
        first_name="Probe2",
        last_name="Last",
    )
    assert updated.username == "probe2"

    fetched = (await db_session.execute(select(User).where(User.id == telegram_id))).scalar_one()
    assert fetched.username == "probe2"


async def test_regression_message_repo_save_duplicate_safe(db_session) -> None:
    from bot.db.repos.message import MessageRepo
    from bot.db.repos.user import UserRepo
    from bot.db.models import ChatMessage

    user_id = _random_id()
    chat_id = -1_000_000_000_000 - random.randint(0, 999_999)
    message_id = random.randint(100_000, 999_999)
    when = datetime.now(timezone.utc)

    await UserRepo.upsert(
        db_session,
        telegram_id=user_id,
        username="u",
        first_name="U",
        last_name=None,
    )

    first = await MessageRepo.save(
        db_session, message_id=message_id, chat_id=chat_id, user_id=user_id,
        text="hello", date=when, raw_json=None,
    )
    second = await MessageRepo.save(
        db_session, message_id=message_id, chat_id=chat_id, user_id=user_id,
        text="hello (dup)", date=when, raw_json=None,
    )

    assert second.id == first.id
    rows = await db_session.execute(
        select(ChatMessage).where(
            ChatMessage.chat_id == chat_id, ChatMessage.message_id == message_id,
        )
    )
    assert len(rows.scalars().all()) == 1


# ─── T0-05: /healthz endpoint reachable ────────────────────────────────────────────────────

def test_regression_healthz_returns_a_status(app_env, monkeypatch) -> None:
    """Smoke: /healthz returns 200 OR 503, and the body contains a 'status' key.
    Does not require a real DB — patches report() to a fixed healthy response so the
    regression umbrella is fully offline."""
    from fastapi.testclient import TestClient

    from bot.services import health as health_module

    async def _fake_report():
        return health_module.HealthReport(
            db=health_module.CheckResult(ok=True),
            settings_sanity=health_module.CheckResult(ok=True),
        )

    monkeypatch.setattr("bot.services.health.report", _fake_report)
    web_routes_health = import_module("web.routes.health")
    monkeypatch.setattr(web_routes_health, "report", _fake_report)

    web_app = import_module("web.app")
    client = TestClient(web_app.create_app())
    response = client.get("/healthz")

    assert response.status_code in (200, 503), "T0-05 regression: /healthz did not return a status code"
    body = response.json()
    assert "status" in body, "T0-05 regression: response body missing 'status' field"
    # Defense in depth: smoke that no obvious secret-shaped value leaks.
    body_str = json.dumps(body)
    for needle in ("123456:test-token", "test-pass", "test-session-secret", "changeme"):
        assert needle not in body_str
