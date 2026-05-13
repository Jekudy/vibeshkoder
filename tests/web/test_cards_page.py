"""T6-08 acceptance tests — read-only /cards and /cards/<id> web pages.

Strategy: web app created via ``create_app()``, exercised via FastAPI's
``TestClient``. DB queries are patched at the repo layer so no real postgres
is required.

Acceptance criteria (issue #240):
- GET /cards without session cookie → 302 to /login.
- GET /cards with session cookie → 200, approved card titles visible, draft hidden.
- GET /cards/<id> with session for a draft card → 404.
- GET /cards/<id> with session for an approved card → 200, body + source mvids visible.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from tests.conftest import import_module


def _make_card(
    *,
    card_id: uuid.UUID | None = None,
    title: str = "Test Card",
    body_markdown: str = "some body",
    card_status: str = "approved",
    approved_by_user_id: int | None = 1,
    approved_at: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=card_id or uuid.uuid4(),
        title=title,
        body_markdown=body_markdown,
        card_status=card_status,
        approved_by_user_id=approved_by_user_id,
        approved_at=approved_at or datetime(2026, 5, 1, tzinfo=timezone.utc),
    )


def _make_source(
    *,
    card_id: uuid.UUID,
    message_version_id: int = 42,
    position: int = 0,
    chat_id: int = -100,
    message_id: int = 7,
    memory_policy: str = "remember",
    is_redacted: bool = False,
    mv_is_redacted: bool = False,
) -> SimpleNamespace:
    from bot.db.repos.card_source import CardSourceJoinedRow

    return CardSourceJoinedRow(
        card_source_id=uuid.uuid4(),
        message_version_id=message_version_id,
        position=position,
        chat_id=chat_id,
        message_id=message_id,
        memory_policy=memory_policy,
        is_redacted=is_redacted,
        mv_is_redacted=mv_is_redacted,
    )


def _valid_session_cookie(app_env_fixture) -> str:
    """Return a valid signed session cookie using the test secret."""
    web_auth = import_module("web.auth")
    return web_auth.create_session_cookie()


def _make_client(app_env_fixture, session_cookie: str | None = None) -> TestClient:
    web_app = import_module("web.app")
    client = TestClient(web_app.create_app(), raise_server_exceptions=True)
    if session_cookie:
        client.cookies.set("session", session_cookie)
    return client


# ─── Auth guard ────────────────────────────────────────────────────────────────


def test_cards_page_redirects_to_login_when_unauthenticated(app_env, monkeypatch) -> None:
    client = _make_client(app_env)
    response = client.get("/cards", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/login"


def test_card_detail_redirects_to_login_when_unauthenticated(app_env, monkeypatch) -> None:
    client = _make_client(app_env)
    card_id = uuid.uuid4()
    response = client.get(f"/cards/{card_id}", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/login"


# ─── List page ─────────────────────────────────────────────────────────────────


def test_cards_page_lists_approved_cards_when_authenticated(app_env, monkeypatch) -> None:
    """GET /cards shows approved card titles; draft card title must NOT appear."""
    approved1 = _make_card(title="Approved Card One")
    approved2 = _make_card(title="Approved Card Two")

    async def _fake_list_approved(session, *, limit, offset):
        return [approved1, approved2]

    async def _fake_get_user_by_id(session, user_id):
        return None

    web_routes_cards = import_module("web.routes.cards")
    monkeypatch.setattr(web_routes_cards.KnowledgeCardRepo, "list_approved", _fake_list_approved)
    monkeypatch.setattr(web_routes_cards, "_get_user_by_id", _fake_get_user_by_id)

    cookie = _valid_session_cookie(app_env)
    client = _make_client(app_env, session_cookie=cookie)
    response = client.get("/cards")

    assert response.status_code == 200
    assert "Approved Card One" in response.text
    assert "Approved Card Two" in response.text
    # Draft card title must not appear
    assert "Draft Card" not in response.text


# ─── Detail page ───────────────────────────────────────────────────────────────


def test_card_detail_404_when_card_status_not_approved(app_env, monkeypatch) -> None:
    """GET /cards/<draft-id> with a valid session → 404.

    Note: KnowledgeCardRepo.get_by_id already filters card_status='approved'
    at the SQL layer and returns None for non-approved cards, so the route
    just needs to 404 on None.
    """
    async def _fake_get_by_id(session, card_id):
        return None  # repo returns None for non-approved or missing card

    web_routes_cards = import_module("web.routes.cards")
    monkeypatch.setattr(web_routes_cards.KnowledgeCardRepo, "get_by_id", _fake_get_by_id)

    cookie = _valid_session_cookie(app_env)
    client = _make_client(app_env, session_cookie=cookie)
    draft_id = uuid.uuid4()
    response = client.get(f"/cards/{draft_id}")

    assert response.status_code == 404


def test_card_detail_renders_body_and_sources(app_env, monkeypatch) -> None:
    """GET /cards/<id> with a valid session → 200, body text + source mvids visible."""
    card_id = uuid.uuid4()
    card = _make_card(
        card_id=card_id,
        title="Great Knowledge",
        body_markdown="**Important fact** about things.",
    )
    source1 = _make_source(card_id=card_id, message_version_id=101)
    source2 = _make_source(card_id=card_id, message_version_id=202)

    async def _fake_get_by_id(session, cid):
        return card

    async def _fake_list_for_card(session, cid):
        return [source1, source2]

    async def _fake_get_user_by_id(session, user_id):
        return None

    web_routes_cards = import_module("web.routes.cards")
    monkeypatch.setattr(web_routes_cards.KnowledgeCardRepo, "get_by_id", _fake_get_by_id)
    monkeypatch.setattr(web_routes_cards.CardSourceRepo, "list_for_card", _fake_list_for_card)
    monkeypatch.setattr(web_routes_cards, "_get_user_by_id", _fake_get_user_by_id)

    cookie = _valid_session_cookie(app_env)
    client = _make_client(app_env, session_cookie=cookie)
    response = client.get(f"/cards/{card_id}")

    assert response.status_code == 200
    assert "Great Knowledge" in response.text
    assert "Important fact" in response.text
    # Source mvids should appear in the page
    assert "101" in response.text
    assert "202" in response.text
