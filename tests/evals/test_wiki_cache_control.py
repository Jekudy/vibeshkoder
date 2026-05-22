"""Phase 11 binding test — 9.5-F: Cache-Control: no-store on member /wiki/{slug}.

Member-facing wiki pages (GET /wiki/{slug}) must carry no-store cache headers
on every response (200, 404, 410). If a page becomes forgotten or redacted after
a browser visit, stale cached content must not persist.

Covered:
  - 200 (page found, governance OK)           → Cache-Control contains no-store, no-cache, private
  - 404 (page not found)                       → Cache-Control contains no-store
  - 410 (page archived by governance)          → Cache-Control contains no-store
  - Pragma: no-cache + Expires: 0              → present on successful 200

Run: pytest tests/evals/test_wiki_cache_control.py -x -v
Does NOT require a live database — all DB helpers are monkeypatched.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from tests.conftest import import_module


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_client(session_cookie: str | None = None):
    web_app = import_module("web.app")
    from fastapi.testclient import TestClient
    client = TestClient(web_app.create_app(), raise_server_exceptions=False)
    if session_cookie:
        client.cookies.set("session", session_cookie)
    return client


def _member_cookie() -> str:
    web_auth = import_module("web.auth")
    return web_auth.create_session_cookie(role="member")


def _make_page_row(
    *,
    page_id: uuid.UUID | None = None,
    slug: str = "test-slug",
    title: str = "Test Page",
    body_markdown: str = "Hello",
    page_status: str = "reviewed",
    public_enabled: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=page_id or uuid.uuid4(),
        slug=slug,
        title=title,
        body_markdown=body_markdown,
        page_status=page_status,
        public_enabled=public_enabled,
    )


async def _async_true():
    return True


async def _async_none():
    return None


async def _async_list(items):
    return items


async def _async_page(page):
    return page


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def wiki_app_env(app_env):
    """Reuse the shared app_env fixture."""
    yield


# ── Test: member /wiki/{slug} 200 has no-store, no-cache, private ─────────────


def test_member_wiki_has_no_store_header(monkeypatch) -> None:
    """GET /wiki/{slug} with valid member session → 200 with Cache-Control: no-store."""
    page_id = uuid.uuid4()
    page = _make_page_row(page_id=page_id, slug="test-slug", page_status="reviewed")

    async def _fake_render(session, *, page_id, role, body_markdown):
        from bot.services.wiki_renderer import WikiRenderResult
        return WikiRenderResult(html_body="<p>Hello</p>", page_archived=False)

    wiki_routes = import_module("web.routes.wiki")
    monkeypatch.setattr(wiki_routes, "_wiki_enabled", lambda session: _async_true())
    monkeypatch.setattr(wiki_routes, "_get_page_by_slug", lambda session, slug: _async_page(page))
    monkeypatch.setattr(wiki_routes, "render_wiki_page", _fake_render)
    monkeypatch.setattr(wiki_routes, "_get_page_sources", lambda session, page_id: _async_list([]))

    client = _make_client(session_cookie=_member_cookie())
    response = client.get("/wiki/test-slug", follow_redirects=False)

    assert response.status_code == 200
    cc = response.headers.get("Cache-Control", "")
    assert "no-store" in cc, f"expected 'no-store' in Cache-Control, got: {cc!r}"
    assert "no-cache" in cc, f"expected 'no-cache' in Cache-Control, got: {cc!r}"
    assert "private" in cc, f"expected 'private' in Cache-Control, got: {cc!r}"


def test_member_wiki_404_has_no_store_header(monkeypatch) -> None:
    """GET /wiki/{slug} when page not found → 404 with Cache-Control: no-store."""
    wiki_routes = import_module("web.routes.wiki")
    monkeypatch.setattr(wiki_routes, "_wiki_enabled", lambda session: _async_true())
    monkeypatch.setattr(wiki_routes, "_get_page_by_slug", lambda session, slug: _async_none())

    client = _make_client(session_cookie=_member_cookie())
    response = client.get("/wiki/missing-slug", follow_redirects=False)

    assert response.status_code == 404
    cc = response.headers.get("Cache-Control", "")
    assert "no-store" in cc, f"expected 'no-store' in Cache-Control on 404, got: {cc!r}"


def test_member_wiki_410_has_no_store_header(monkeypatch) -> None:
    """GET /wiki/{slug} when governance archives page → 410 with Cache-Control: no-store."""
    page_id = uuid.uuid4()
    page = _make_page_row(page_id=page_id, slug="archived-slug", page_status="reviewed")

    async def _fake_render_archived(session, *, page_id, role, body_markdown):
        from bot.services.wiki_renderer import WikiRenderResult
        return WikiRenderResult(html_body="", page_archived=True)

    wiki_routes = import_module("web.routes.wiki")
    monkeypatch.setattr(wiki_routes, "_wiki_enabled", lambda session: _async_true())
    monkeypatch.setattr(wiki_routes, "_get_page_by_slug", lambda session, slug: _async_page(page))
    monkeypatch.setattr(wiki_routes, "render_wiki_page", _fake_render_archived)

    client = _make_client(session_cookie=_member_cookie())
    response = client.get("/wiki/archived-slug", follow_redirects=False)

    assert response.status_code == 410
    cc = response.headers.get("Cache-Control", "")
    assert "no-store" in cc, f"expected 'no-store' in Cache-Control on 410, got: {cc!r}"


# ── Helper: full cache-control contract assertion ─────────────────────────────


def _assert_full_cache_control_contract(response) -> None:
    """Assert the complete cache-control contract on a wiki response.

    Checks: Cache-Control contains no-store, no-cache, must-revalidate, private;
    Pragma: no-cache; Expires: 0.  A regression in any single directive fails
    the test immediately, not just 'no-store'.
    """
    cc = response.headers.get("Cache-Control", "")
    assert "no-store" in cc, f"expected 'no-store' in Cache-Control, got: {cc!r}"
    assert "no-cache" in cc, f"expected 'no-cache' in Cache-Control, got: {cc!r}"
    assert "must-revalidate" in cc, f"expected 'must-revalidate' in Cache-Control, got: {cc!r}"
    assert "private" in cc, f"expected 'private' in Cache-Control, got: {cc!r}"
    pragma = response.headers.get("Pragma", "")
    assert pragma == "no-cache", f"expected Pragma: no-cache, got: {pragma!r}"
    expires = response.headers.get("Expires", "")
    assert expires == "0", f"expected Expires: 0, got: {expires!r}"


# ── Full contract tests for all response paths ────────────────────────────────


def test_member_wiki_200_has_full_cache_control_contract(monkeypatch) -> None:
    """GET /wiki/{slug} 200 → full cache-control contract (no-store, no-cache,
    must-revalidate, private, Pragma: no-cache, Expires: 0)."""
    page_id = uuid.uuid4()
    page = _make_page_row(page_id=page_id, slug="test-slug", page_status="reviewed")

    async def _fake_render(session, *, page_id, role, body_markdown):
        from bot.services.wiki_renderer import WikiRenderResult
        return WikiRenderResult(html_body="<p>Hello</p>", page_archived=False)

    wiki_routes = import_module("web.routes.wiki")
    monkeypatch.setattr(wiki_routes, "_wiki_enabled", lambda session: _async_true())
    monkeypatch.setattr(wiki_routes, "_get_page_by_slug", lambda session, slug: _async_page(page))
    monkeypatch.setattr(wiki_routes, "render_wiki_page", _fake_render)
    monkeypatch.setattr(wiki_routes, "_get_page_sources", lambda session, page_id: _async_list([]))

    client = _make_client(session_cookie=_member_cookie())
    response = client.get("/wiki/test-slug", follow_redirects=False)

    assert response.status_code == 200
    _assert_full_cache_control_contract(response)


def test_member_wiki_404_has_full_cache_control_contract(monkeypatch) -> None:
    """GET /wiki/{slug} 404 → full cache-control contract."""
    wiki_routes = import_module("web.routes.wiki")
    monkeypatch.setattr(wiki_routes, "_wiki_enabled", lambda session: _async_true())
    monkeypatch.setattr(wiki_routes, "_get_page_by_slug", lambda session, slug: _async_none())

    client = _make_client(session_cookie=_member_cookie())
    response = client.get("/wiki/missing-slug", follow_redirects=False)

    assert response.status_code == 404
    _assert_full_cache_control_contract(response)


def test_member_wiki_410_has_full_cache_control_contract(monkeypatch) -> None:
    """GET /wiki/{slug} 410 (archived) → full cache-control contract."""
    page_id = uuid.uuid4()
    page = _make_page_row(page_id=page_id, slug="archived-slug", page_status="reviewed")

    async def _fake_render_archived(session, *, page_id, role, body_markdown):
        from bot.services.wiki_renderer import WikiRenderResult
        return WikiRenderResult(html_body="", page_archived=True)

    wiki_routes = import_module("web.routes.wiki")
    monkeypatch.setattr(wiki_routes, "_wiki_enabled", lambda session: _async_true())
    monkeypatch.setattr(wiki_routes, "_get_page_by_slug", lambda session, slug: _async_page(page))
    monkeypatch.setattr(wiki_routes, "render_wiki_page", _fake_render_archived)

    client = _make_client(session_cookie=_member_cookie())
    response = client.get("/wiki/archived-slug", follow_redirects=False)

    assert response.status_code == 410
    _assert_full_cache_control_contract(response)


def test_member_wiki_403_has_full_cache_control_contract(monkeypatch) -> None:
    """GET /wiki/{slug} with authenticated-but-wrong-role session → 403 with full cache-control contract.

    Product MED (9.5-F): 403 insufficient_role response must carry
    Cache-Control: no-store, no-cache, must-revalidate, private + Pragma/Expires
    so that proxy caches cannot serve a stale 403 to a newly authorised member.

    A cookie with role='guest' passes auth middleware (authenticated=True) but
    fails _require_member_or_admin (role not in {'admin', 'member'}) → 403.
    """
    web_auth = import_module("web.auth")
    guest_cookie = web_auth.create_session_cookie(role="guest")
    client = _make_client(session_cookie=guest_cookie)
    response = client.get("/wiki/any-slug", follow_redirects=False)

    assert response.status_code == 403
    _assert_full_cache_control_contract(response)


def test_member_wiki_503_has_full_cache_control_contract(monkeypatch) -> None:
    """GET /wiki/{slug} when wiki feature flag is off → 503 with full cache-control contract.

    Product MED (9.5-F): 503 wiki_disabled response must carry
    Cache-Control: no-store, no-cache, must-revalidate, private + Pragma/Expires
    so that CDN caches do not lock users out after the flag is flipped on.
    """
    wiki_routes = import_module("web.routes.wiki")
    monkeypatch.setattr(wiki_routes, "_wiki_enabled", lambda session: _async_none())

    client = _make_client(session_cookie=_member_cookie())
    response = client.get("/wiki/any-slug", follow_redirects=False)

    assert response.status_code == 503
    _assert_full_cache_control_contract(response)
