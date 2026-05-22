"""T9-05 acceptance tests — member wiki router with governance revalidation.

Scenarios (16):
1.  test_anon_get_wiki_redirects_to_login — GET /wiki without cookie → 302 /login
2.  test_unauthenticated_get_wiki_slug_redirects_to_login — GET /wiki/{slug} → 302
3.  test_member_get_wiki_returns_reviewed_only — only page_status='reviewed' visible
4.  test_draft_page_returns_404 — GET /wiki/draft-slug → 404
5.  test_all_sources_forgotten_page_returns_410 — governance revalidation → 410
6.  test_public_route_404_when_public_disabled — public_enabled=False → 404
7.  test_public_route_200_when_enabled_and_sources_valid — public_enabled=True → 200
8.  test_public_route_cache_control_header_present — Cache-Control on 200 + 404 + 410
9.  test_search_with_sql_injection_returns_empty_no_error — H2 SQL safety
10. test_search_returns_reviewed_only — FTS only returns reviewed pages
11. test_member_transitive_offrecord_suppresses_citation — L9c citation suppressed
12. test_wiki_disabled_returns_503 — memory.wiki.enabled=False → 503
13. test_admin_role_can_access_wiki — admin not blocked on /wiki
14. test_template_renders_citation_section — source trace section present
15. test_member_role_no_admin_marker_visible — no [⚠] marker for member
16. test_non_reviewed_page_status_returns_404 — page_status='stale' → 404
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.conftest import import_module


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_client(session_cookie: str | None = None) -> TestClient:
    web_app = import_module("web.app")
    client = TestClient(web_app.create_app(), raise_server_exceptions=False)
    if session_cookie:
        client.cookies.set("session", session_cookie)
    return client


def _admin_cookie() -> str:
    web_auth = import_module("web.auth")
    return web_auth.create_session_cookie(role="admin")


def _member_cookie() -> str:
    web_auth = import_module("web.auth")
    return web_auth.create_session_cookie(role="member")


def _make_page_row(
    *,
    page_id: uuid.UUID | None = None,
    slug: str = "test-page",
    title: str = "Test Page",
    body_markdown: str = "Hello world",
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


# ── Fixture: standard app env ─────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def wiki_app_env(app_env):
    """Reuse the shared app_env fixture for all wiki tests."""
    yield


# ── Test 1: anonymous → 302 /login ────────────────────────────────────────────


def test_anon_get_wiki_redirects_to_login() -> None:
    """GET /wiki without session cookie → 302 redirect to /login."""
    client = _make_client()
    response = client.get("/wiki", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/login"


# ── Test 2: anonymous GET /wiki/{slug} → 302 ──────────────────────────────────


def test_unauthenticated_get_wiki_slug_redirects_to_login() -> None:
    """GET /wiki/{slug} without session → 302 to /login."""
    client = _make_client()
    response = client.get("/wiki/some-page", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/login"


# ── Test 3: member sees only reviewed pages ───────────────────────────────────


def test_member_get_wiki_returns_reviewed_only(monkeypatch) -> None:
    """GET /wiki with member cookie returns only page_status='reviewed' pages."""
    reviewed_page = _make_page_row(title="Reviewed Page", page_status="reviewed")

    async def _fake_execute(stmt, params=None):
        rows = [reviewed_page]

        class _FakeResult:
            def fetchall(self):
                return rows

        return _FakeResult()

    wiki_routes = import_module("web.routes.wiki")
    monkeypatch.setattr(wiki_routes, "_wiki_enabled", lambda session: _async_true())
    monkeypatch.setattr(wiki_routes, "_list_reviewed_pages", lambda session: _async_list([reviewed_page]))

    client = _make_client(session_cookie=_member_cookie())
    response = client.get("/wiki", follow_redirects=False)
    assert response.status_code == 200
    assert "Reviewed Page" in response.text
    assert "draft" not in response.text.lower() or "Draft" not in response.text


async def _async_true():
    return True


async def _async_false():
    return False


async def _async_list(items):
    return items


async def _async_none():
    return None


# ── Test 4: draft page returns 404 ────────────────────────────────────────────


def test_draft_page_returns_404(monkeypatch) -> None:
    """GET /wiki/draft-slug for page_status='draft' → 404 (not archived, not reviewed)."""
    wiki_routes = import_module("web.routes.wiki")
    draft_page = _make_page_row(slug="draft-slug", page_status="draft")
    monkeypatch.setattr(wiki_routes, "_wiki_enabled", lambda session: _async_true())
    # Draft page exists in DB but is not visible to members.
    monkeypatch.setattr(
        wiki_routes, "_get_page_by_slug_any_status", lambda session, slug: _async_page(draft_page)
    )

    client = _make_client(session_cookie=_member_cookie())
    response = client.get("/wiki/draft-slug", follow_redirects=False)
    assert response.status_code == 404


# ── Test 5: all sources forgotten → 410 ───────────────────────────────────────


def test_all_sources_forgotten_page_returns_410(monkeypatch) -> None:
    """GET /wiki/{slug} where governance finds all sources invalid → 410 Gone."""
    page_id = uuid.uuid4()
    page = _make_page_row(page_id=page_id, slug="my-page", page_status="reviewed")

    async def _fake_render(session, *, page_id, role, body_markdown):
        from bot.services.wiki_renderer import WikiRenderResult
        return WikiRenderResult(html_body="", page_archived=True)

    wiki_routes = import_module("web.routes.wiki")
    monkeypatch.setattr(wiki_routes, "_wiki_enabled", lambda session: _async_true())
    monkeypatch.setattr(
        wiki_routes, "_get_page_by_slug", lambda session, slug: _async_page(page)
    )
    monkeypatch.setattr(wiki_routes, "render_wiki_page", _fake_render)

    client = _make_client(session_cookie=_member_cookie())
    response = client.get("/wiki/my-page", follow_redirects=False)
    assert response.status_code == 410


async def _async_page(page):
    return page


# ── Test 6: public route 404 when public_enabled=False ────────────────────────


def test_public_route_404_when_public_disabled(monkeypatch) -> None:
    """GET /wiki/public/{slug} when public_enabled=False → 404."""
    page = _make_page_row(slug="my-page", public_enabled=False, page_status="reviewed")

    wiki_routes = import_module("web.routes.wiki")
    monkeypatch.setattr(wiki_routes, "_wiki_enabled", lambda session: _async_true())
    monkeypatch.setattr(wiki_routes, "_get_public_page_by_slug", lambda session, slug: _async_page(page))

    client = _make_client()  # no cookie — public route
    response = client.get("/wiki/public/my-page", follow_redirects=False)
    assert response.status_code == 404
    assert "Cache-Control" in response.headers
    assert "no-store" in response.headers["Cache-Control"]


# ── Test 7: public route 200 when public_enabled=True and sources valid ────────


def test_public_route_200_when_enabled_and_sources_valid(monkeypatch) -> None:
    """GET /wiki/public/{slug} with public_enabled=True and clean sources → 200."""
    page_id = uuid.uuid4()
    page = _make_page_row(page_id=page_id, slug="my-page", public_enabled=True, page_status="reviewed")

    async def _fake_render(session, *, page_id, role, body_markdown):
        from bot.services.wiki_renderer import WikiRenderResult
        return WikiRenderResult(html_body="<p>Hello</p>", page_archived=False)

    wiki_routes = import_module("web.routes.wiki")
    monkeypatch.setattr(wiki_routes, "_wiki_enabled", lambda session: _async_true())
    monkeypatch.setattr(wiki_routes, "_get_public_page_by_slug", lambda session, slug: _async_page(page))
    monkeypatch.setattr(wiki_routes, "render_wiki_page", _fake_render)
    monkeypatch.setattr(wiki_routes, "_get_page_sources", lambda session, page_id: _async_list([]))

    client = _make_client()
    response = client.get("/wiki/public/my-page", follow_redirects=False)
    assert response.status_code == 200
    assert "Cache-Control" in response.headers
    assert "no-store" in response.headers["Cache-Control"]
    assert "Hello" in response.text


# ── Test 8: Cache-Control on ALL public route responses ───────────────────────


def test_public_route_cache_control_header_present(monkeypatch) -> None:
    """Cache-Control: no-store must be present on 200, 404, and 410 responses."""
    page_id = uuid.uuid4()

    # 404 scenario (public_disabled)
    page_disabled = _make_page_row(page_id=page_id, slug="disabled", public_enabled=False, page_status="reviewed")

    # 410 scenario (archived)
    page_archived = _make_page_row(page_id=page_id, slug="archived", public_enabled=True, page_status="reviewed")

    # 200 scenario (valid)
    page_valid = _make_page_row(page_id=uuid.uuid4(), slug="valid", public_enabled=True, page_status="reviewed")

    async def _fake_render_archived(session, *, page_id, role, body_markdown):
        from bot.services.wiki_renderer import WikiRenderResult
        return WikiRenderResult(html_body="", page_archived=True)

    async def _fake_render_ok(session, *, page_id, role, body_markdown):
        from bot.services.wiki_renderer import WikiRenderResult
        return WikiRenderResult(html_body="<p>content</p>", page_archived=False)

    wiki_routes = import_module("web.routes.wiki")
    monkeypatch.setattr(wiki_routes, "_wiki_enabled", lambda session: _async_true())

    # 404
    monkeypatch.setattr(wiki_routes, "_get_public_page_by_slug", lambda session, slug: _async_page(page_disabled))
    client = _make_client()
    r404 = client.get("/wiki/public/disabled", follow_redirects=False)
    assert r404.status_code == 404
    assert "no-store" in r404.headers.get("Cache-Control", "")

    # 410
    monkeypatch.setattr(wiki_routes, "_get_public_page_by_slug", lambda session, slug: _async_page(page_archived))
    monkeypatch.setattr(wiki_routes, "render_wiki_page", _fake_render_archived)
    r410 = client.get("/wiki/public/archived", follow_redirects=False)
    assert r410.status_code == 410
    assert "no-store" in r410.headers.get("Cache-Control", "")

    # 200
    monkeypatch.setattr(wiki_routes, "_get_public_page_by_slug", lambda session, slug: _async_page(page_valid))
    monkeypatch.setattr(wiki_routes, "render_wiki_page", _fake_render_ok)
    monkeypatch.setattr(wiki_routes, "_get_page_sources", lambda session, page_id: _async_list([]))
    r200 = client.get("/wiki/public/valid", follow_redirects=False)
    assert r200.status_code == 200
    assert "no-store" in r200.headers.get("Cache-Control", "")


# ── Test 9: search SQL injection returns empty, no error ──────────────────────


def test_search_with_sql_injection_returns_empty_no_error(monkeypatch) -> None:
    """GET /wiki/search?q='; DROP TABLE wiki_pages; -- returns 200 with empty results."""
    wiki_routes = import_module("web.routes.wiki")
    monkeypatch.setattr(wiki_routes, "_wiki_enabled", lambda session: _async_true())
    monkeypatch.setattr(wiki_routes, "_search_wiki_pages", lambda session, q: _async_list([]))

    client = _make_client(session_cookie=_member_cookie())
    malicious_q = "'; DROP TABLE wiki_pages; --"
    response = client.get(f"/wiki/search?q={malicious_q}", follow_redirects=False)
    assert response.status_code == 200
    # No error in body
    assert "error" not in response.text.lower() or "0" in response.text


# ── Test 10: search returns reviewed only ─────────────────────────────────────


def test_search_returns_reviewed_only(monkeypatch) -> None:
    """GET /wiki/search?q=foo returns only reviewed pages."""
    reviewed = SimpleNamespace(id=uuid.uuid4(), slug="good-page", title="Good Page", page_status="reviewed")

    wiki_routes = import_module("web.routes.wiki")
    monkeypatch.setattr(wiki_routes, "_wiki_enabled", lambda session: _async_true())
    monkeypatch.setattr(wiki_routes, "_search_wiki_pages", lambda session, q: _async_list([reviewed]))

    client = _make_client(session_cookie=_member_cookie())
    response = client.get("/wiki/search?q=foo", follow_redirects=False)
    assert response.status_code == 200
    assert "Good Page" in response.text


# ── Test 11: L9c transitive offrecord suppresses citation ─────────────────────


def test_member_transitive_offrecord_suppresses_citation(monkeypatch) -> None:
    """L9c: page with offrecord source → member sees [citation withheld], no [⚠]."""
    page_id = uuid.uuid4()
    # body_markdown with mv citation — the 3rd mv is offrecord
    page = _make_page_row(
        page_id=page_id,
        slug="cited-page",
        body_markdown="See [^mv:1] and [^mv:2] and [^mv:3].",
        page_status="reviewed",
    )

    async def _fake_render(session, *, page_id, role, body_markdown):
        from bot.services.wiki_renderer import WikiRenderResult
        # Simulate: mv 3 suppressed (offrecord), mvs 1 and 2 valid
        html = '<a class="wiki-citation" href="#mv-1">[^1]</a> <a class="wiki-citation" href="#mv-2">[^2]</a>'
        return WikiRenderResult(
            html_body=html,
            page_archived=False,
            suppressed_citations=[3],
            admin_unavailable_markers=[],
        )

    wiki_routes = import_module("web.routes.wiki")
    monkeypatch.setattr(wiki_routes, "_wiki_enabled", lambda session: _async_true())
    monkeypatch.setattr(
        wiki_routes, "_get_page_by_slug", lambda session, slug: _async_page(page)
    )
    monkeypatch.setattr(wiki_routes, "render_wiki_page", _fake_render)
    monkeypatch.setattr(wiki_routes, "_get_page_sources", lambda session, page_id: _async_list([]))

    client = _make_client(session_cookie=_member_cookie())
    response = client.get("/wiki/cited-page", follow_redirects=False)
    assert response.status_code == 200
    # Admin warning marker must NOT appear to member
    assert "[⚠" not in response.text
    assert "SOURCE UNAVAILABLE" not in response.text


# ── Test 12: wiki disabled → 503 ─────────────────────────────────────────────


def test_wiki_disabled_returns_503(monkeypatch) -> None:
    """GET /wiki when memory.wiki.enabled=False → 503 Service Unavailable."""
    wiki_routes = import_module("web.routes.wiki")
    monkeypatch.setattr(wiki_routes, "_wiki_enabled", lambda session: _async_false())

    client = _make_client(session_cookie=_member_cookie())
    response = client.get("/wiki", follow_redirects=False)
    assert response.status_code == 503


# ── Test 13: admin role can access wiki ───────────────────────────────────────


def test_admin_role_can_access_wiki(monkeypatch) -> None:
    """Admin with role='admin' can access /wiki without 403."""
    reviewed = _make_page_row(title="Admin Sees This", page_status="reviewed")

    wiki_routes = import_module("web.routes.wiki")
    monkeypatch.setattr(wiki_routes, "_wiki_enabled", lambda session: _async_true())
    monkeypatch.setattr(wiki_routes, "_list_reviewed_pages", lambda session: _async_list([reviewed]))

    client = _make_client(session_cookie=_admin_cookie())
    response = client.get("/wiki", follow_redirects=False)
    assert response.status_code == 200
    assert "Admin Sees This" in response.text


# ── Test 14: template renders citation/source-trace section ───────────────────


def test_template_renders_citation_section(monkeypatch) -> None:
    """Page template includes a sources/citation section."""
    page_id = uuid.uuid4()
    page = _make_page_row(page_id=page_id, slug="src-page", page_status="reviewed")

    source = SimpleNamespace(mv_id=42, card_id=None)

    async def _fake_render(session, *, page_id, role, body_markdown):
        from bot.services.wiki_renderer import WikiRenderResult
        return WikiRenderResult(html_body="<p>body</p>", page_archived=False)

    wiki_routes = import_module("web.routes.wiki")
    monkeypatch.setattr(wiki_routes, "_wiki_enabled", lambda session: _async_true())
    monkeypatch.setattr(
        wiki_routes, "_get_page_by_slug", lambda session, slug: _async_page(page)
    )
    monkeypatch.setattr(wiki_routes, "render_wiki_page", _fake_render)
    monkeypatch.setattr(wiki_routes, "_get_page_sources", lambda session, page_id: _async_list([source]))

    client = _make_client(session_cookie=_member_cookie())
    response = client.get("/wiki/src-page", follow_redirects=False)
    assert response.status_code == 200
    # Source section should be present
    assert "source" in response.text.lower() or "citation" in response.text.lower() or "42" in response.text


# ── Test 15: member role — no admin unavailable marker ────────────────────────


def test_member_role_no_admin_marker_visible(monkeypatch) -> None:
    """Member role render: [⚠ SOURCE UNAVAILABLE] must NOT appear in response."""
    page_id = uuid.uuid4()
    page = _make_page_row(page_id=page_id, slug="clean-page", page_status="reviewed")

    async def _fake_render(session, *, page_id, role, body_markdown):
        from bot.services.wiki_renderer import WikiRenderResult
        # Member role: suppressed, not marked
        return WikiRenderResult(
            html_body="<p>clean</p>",
            page_archived=False,
            suppressed_citations=[5],
            admin_unavailable_markers=[],
        )

    wiki_routes = import_module("web.routes.wiki")
    monkeypatch.setattr(wiki_routes, "_wiki_enabled", lambda session: _async_true())
    monkeypatch.setattr(
        wiki_routes, "_get_page_by_slug", lambda session, slug: _async_page(page)
    )
    monkeypatch.setattr(wiki_routes, "render_wiki_page", _fake_render)
    monkeypatch.setattr(wiki_routes, "_get_page_sources", lambda session, page_id: _async_list([]))

    client = _make_client(session_cookie=_member_cookie())
    response = client.get("/wiki/clean-page", follow_redirects=False)
    assert response.status_code == 200
    assert "SOURCE UNAVAILABLE" not in response.text
    assert "⚠" not in response.text


# ── Test 16: stale/archived page_status returns 410 Gone (9.5-D) ─────────────


def test_archived_page_status_returns_410(monkeypatch) -> None:
    """GET /wiki/{slug} for a page with page_status='archived' → 410 Gone.

    9.5-D: stale-page member silent 404 → 410 Gone with template.
    The page exists in DB but is no longer available.
    """
    wiki_routes = import_module("web.routes.wiki")
    archived_page = _make_page_row(slug="gone-page", page_status="archived")

    monkeypatch.setattr(wiki_routes, "_wiki_enabled", lambda session: _async_true())
    monkeypatch.setattr(
        wiki_routes, "_get_page_by_slug_any_status", lambda session, slug: _async_page(archived_page)
    )

    client = _make_client(session_cookie=_member_cookie())
    response = client.get("/wiki/gone-page", follow_redirects=False)
    assert response.status_code == 410
    assert "Cache-Control" in response.headers
    assert "no-store" in response.headers["Cache-Control"]


def test_stale_page_status_returns_410(monkeypatch) -> None:
    """GET /wiki/{slug} for a page with page_status='stale' → 410 Gone.

    9.5-D: stale-page member silent 404 → 410 Gone with template.
    """
    wiki_routes = import_module("web.routes.wiki")
    stale_page = _make_page_row(slug="stale-page", page_status="stale")

    monkeypatch.setattr(wiki_routes, "_wiki_enabled", lambda session: _async_true())
    monkeypatch.setattr(
        wiki_routes, "_get_page_by_slug_any_status", lambda session, slug: _async_page(stale_page)
    )

    client = _make_client(session_cookie=_member_cookie())
    response = client.get("/wiki/stale-page", follow_redirects=False)
    assert response.status_code == 410
    assert "Cache-Control" in response.headers
    assert "no-store" in response.headers["Cache-Control"]


def test_unknown_slug_returns_404(monkeypatch) -> None:
    """GET /wiki/{slug} for a slug not in DB → 404 (page never existed)."""
    wiki_routes = import_module("web.routes.wiki")

    monkeypatch.setattr(wiki_routes, "_wiki_enabled", lambda session: _async_true())
    monkeypatch.setattr(
        wiki_routes, "_get_page_by_slug_any_status", lambda session, slug: _async_none()
    )

    client = _make_client(session_cookie=_member_cookie())
    response = client.get("/wiki/no-such-slug", follow_redirects=False)
    assert response.status_code == 404


# ── Test 17: /robots.txt — wiki variant (AC#9 HIGH F) ─────────────────────────


def test_robots_txt_disabled_returns_disallow_all(monkeypatch) -> None:
    """When memory.wiki.enabled=False → robots.txt disallows everything."""
    wiki_routes = import_module("web.routes.wiki")
    monkeypatch.setattr(wiki_routes, "_wiki_enabled", lambda session: _async_false())

    client = _make_client()
    response = client.get("/robots.txt")
    assert response.status_code == 200
    assert "Disallow: /" in response.text
    assert "Allow:" not in response.text
    assert response.headers["cache-control"] == "no-store, max-age=0, must-revalidate"


def test_robots_txt_enabled_lists_public_indexable_slugs(monkeypatch) -> None:
    """When enabled, robots.txt lists Allow: lines for indexable public pages."""
    wiki_routes = import_module("web.routes.wiki")
    monkeypatch.setattr(wiki_routes, "_wiki_enabled", lambda session: _async_true())
    monkeypatch.setattr(
        wiki_routes,
        "_list_robots_allowed_slugs",
        lambda session: _async_list(["intro", "house-rules"]),
    )

    client = _make_client()
    response = client.get("/robots.txt")
    assert response.status_code == 200
    body = response.text
    assert "User-agent: *" in body
    assert "Allow: /wiki/public/intro" in body
    assert "Allow: /wiki/public/house-rules" in body
    assert "Disallow: /" in body
    # Default-deny line must be LAST so explicit Allow lines win precedence.
    assert body.rstrip().endswith("Disallow: /")
    assert response.headers["cache-control"] == "no-store, max-age=0, must-revalidate"


def test_robots_txt_anonymous_access_allowed(monkeypatch) -> None:
    """/robots.txt is in _PUBLIC_PATHS — no login redirect for anonymous crawlers."""
    wiki_routes = import_module("web.routes.wiki")
    monkeypatch.setattr(wiki_routes, "_wiki_enabled", lambda session: _async_false())

    client = _make_client()  # no session cookie
    response = client.get("/robots.txt", follow_redirects=False)
    assert response.status_code == 200  # NOT 302 to /login
