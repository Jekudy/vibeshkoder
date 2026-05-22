"""Member wiki router — T9-05 / Phase 9.

GET /wiki                     Member catalog (reviewed pages only).
GET /wiki/{slug}              Single page view with governance revalidation.
GET /wiki/public/{slug}       Public anonymous variant (gated by public_enabled).
GET /wiki/search              FTS search via plainto_tsquery('russian', :q).

Auth contract
-------------
- /wiki and /wiki/{slug} require role='member' or 'admin'.
  The auth middleware already redirects unauthenticated users to /login.
  Role check inside each route returns 403 if role not in {'admin','member'}.
- /wiki/public/{slug} is anonymous (no auth required — listed in _PUBLIC_PATHS
  prefix match in web/app.py).
- /wiki/search requires member or admin role.

Feature flag
------------
All routes check `memory.wiki.enabled` at the top of each handler.
Missing flag (default) → False → 503 Service Unavailable.

Cache-Control (HIGH F)
-----------------------
Every /wiki/public/{slug} response (200, 404, 410) includes:
  Cache-Control: no-store, max-age=0, must-revalidate

G1 lint: this file MUST NOT import neo4j, bot.services.graph_*, or
bot.services.llm_* modules.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.engine import async_session
from bot.db.repos.feature_flag import FeatureFlagRepo
from bot.services.wiki_renderer import WikiRenderResult, render_wiki_page
from web.app import TEMPLATES

router = APIRouter(prefix="/wiki", tags=["wiki"])

# Separate router (no prefix) for /robots.txt — must live at site root for
# crawler convention. Same Cache-Control invariant as /wiki/public/{slug}
# so that toggling public_enabled or unpublishing a page propagates to
# crawlers without intermediary caching.
robots_router = APIRouter(tags=["wiki"])

# ── Feature flag key ─────────────────────────────────────────────────────────

WIKI_FEATURE_FLAG = "memory.wiki.enabled"

# ── Cache-Control header value (HIGH F) ──────────────────────────────────────

_NO_STORE = "no-store, max-age=0, must-revalidate"

# Member-facing authenticated routes (GET /wiki/{slug}) require stronger
# cache directives: no-store to prevent storage, no-cache to force
# revalidation, private to block shared caches, must-revalidate per HTTP
# spec. Pragma and Expires are legacy headers for HTTP/1.0 compliance.
_MEMBER_NO_STORE = "no-store, no-cache, must-revalidate, private"
_MEMBER_CACHE_HEADERS = {
    "Cache-Control": _MEMBER_NO_STORE,
    "Pragma": "no-cache",
    "Expires": "0",
}

# ── Allowed roles for member routes ─────────────────────────────────────────

_MEMBER_ROLES = {"admin", "member"}


# ── Internal DB helpers (patchable in tests) ─────────────────────────────────


async def _wiki_enabled(session: AsyncSession) -> bool:
    """Return True iff memory.wiki.enabled flag is set."""
    return await FeatureFlagRepo.get(session, WIKI_FEATURE_FLAG)


async def _list_reviewed_pages(session: AsyncSession) -> list[Any]:
    """Return all wiki_pages with page_status='reviewed', ordered by title."""
    result = await session.execute(
        text(
            "SELECT id, slug, title, page_status "
            "FROM wiki_pages "
            "WHERE page_status = 'reviewed' "
            "ORDER BY title"
        )
    )
    return result.fetchall()


async def _get_page_by_slug(session: AsyncSession, slug: str) -> Any | None:
    """Fetch a reviewed wiki page by slug. Returns None if not found or not reviewed."""
    result = await session.execute(
        text(
            "SELECT id, slug, title, body_markdown, page_status, public_enabled "
            "FROM wiki_pages "
            "WHERE slug = :slug AND page_status = 'reviewed'"
        ),
        {"slug": slug},
    )
    return result.fetchone()


async def _get_page_by_slug_any_status(session: AsyncSession, slug: str) -> Any | None:
    """Fetch a wiki page by slug regardless of page_status.

    Used by wiki_page to distinguish between unknown slugs (→ 404) and
    existing-but-archived/stale pages (→ 410 Gone per 9.5-D).
    """
    result = await session.execute(
        text(
            "SELECT id, slug, title, body_markdown, page_status, public_enabled "
            "FROM wiki_pages "
            "WHERE slug = :slug"
        ),
        {"slug": slug},
    )
    return result.fetchone()


async def _get_public_page_by_slug(session: AsyncSession, slug: str) -> Any | None:
    """Fetch a reviewed wiki page by slug for the public route (no auth check)."""
    result = await session.execute(
        text(
            "SELECT id, slug, title, body_markdown, page_status, public_enabled "
            "FROM wiki_pages "
            "WHERE slug = :slug AND page_status = 'reviewed'"
        ),
        {"slug": slug},
    )
    return result.fetchone()


async def _list_robots_allowed_slugs(session: AsyncSession) -> list[str]:
    """Return slugs of reviewed pages with public_enabled=true AND robots_policy='index'.

    Each row's slug becomes an explicit `Allow:` line in /robots.txt. Pages
    with robots_policy='noindex' OR public_enabled=false are excluded — the
    default `Disallow: /` covers them.
    """
    result = await session.execute(
        text(
            "SELECT slug FROM wiki_pages "
            "WHERE page_status = 'reviewed' "
            "  AND public_enabled = true "
            "  AND robots_policy = 'index' "
            "ORDER BY slug"
        )
    )
    return [str(r.slug) for r in result.fetchall()]


async def _get_page_sources(session: AsyncSession, page_id: uuid.UUID) -> list[Any]:
    """Fetch message source IDs and card IDs cited by a wiki page."""
    mv_result = await session.execute(
        text(
            "SELECT message_version_id AS mv_id, NULL::uuid AS card_id "
            "FROM wiki_page_message_sources WHERE wiki_page_id = :pid "
            "UNION "
            "SELECT NULL AS mv_id, card_id "
            "FROM wiki_page_card_sources WHERE wiki_page_id = :pid"
        ),
        {"pid": str(page_id)},
    )
    return mv_result.fetchall()


async def _search_wiki_pages(session: AsyncSession, q: str) -> list[Any]:
    """FTS search over reviewed wiki pages using bound parameter (H2 SQL safety).

    Uses plainto_tsquery('russian', :q) with a bound parameter — never
    string-concatenates the query. Passing SQL metacharacters in q is safe.
    """
    result = await session.execute(
        text(
            "SELECT id, slug, title "
            "FROM wiki_pages "
            "WHERE page_status = 'reviewed' "
            "AND body_tsv @@ plainto_tsquery('russian', :q) "
            "ORDER BY title "
            "LIMIT 50"
        ),
        {"q": q},
    )
    return result.fetchall()


# ── Role guard helper ─────────────────────────────────────────────────────────


def _require_member_or_admin(request: Request) -> str | None:
    """Return role if user has member or admin role. Returns None if not authorized.

    The auth middleware already guarantees the request has a valid session cookie
    at this point (unauthenticated → redirected to /login before reaching routes).
    We only need to check the role.
    """
    user = getattr(request.state, "user", None)
    if user is None:
        return None
    role = user.get("role")
    if role not in _MEMBER_ROLES:
        return None
    return role


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get("")
@router.get("/")
async def wiki_catalog(request: Request) -> Response:
    """Member catalog: only page_status='reviewed' pages."""
    role = _require_member_or_admin(request)
    if role is None:
        return JSONResponse(
            status_code=403,
            content={"detail": "insufficient_role", "required": "member"},
        )

    async with async_session() as session:
        if not await _wiki_enabled(session):
            return JSONResponse(
                status_code=503,
                headers={"Retry-After": "3600"},
                content={"detail": "wiki_disabled"},
            )

        pages = await _list_reviewed_pages(session)

    return TEMPLATES.TemplateResponse(
        request=request,
        name="wiki/index.html",
        context={
            "request": request,
            "pages": pages,
            "user": getattr(request.state, "user", {}),
        },
    )


@router.get("/search")
async def wiki_search(request: Request, q: str = "") -> Response:
    """FTS search via plainto_tsquery('russian', :q). Member or admin only."""
    role = _require_member_or_admin(request)
    if role is None:
        return JSONResponse(
            status_code=403,
            content={"detail": "insufficient_role", "required": "member"},
        )

    results: list[Any] = []

    async with async_session() as session:
        if not await _wiki_enabled(session):
            return JSONResponse(
                status_code=503,
                headers={"Retry-After": "3600"},
                content={"detail": "wiki_disabled"},
            )

        if q.strip():
            results = await _search_wiki_pages(session, q)

    return TEMPLATES.TemplateResponse(
        request=request,
        name="wiki/search.html",
        context={
            "request": request,
            "results": results,
            "q": q,
            "user": getattr(request.state, "user", {}),
        },
    )


@router.get("/public/{slug}")
async def wiki_public_page(slug: str, request: Request) -> Response:
    """Public anonymous variant of a wiki page. Gated by public_enabled=True.

    Cache-Control: no-store on ALL responses (200, 404, 410) — HIGH F.
    """
    cache_headers = {"Cache-Control": _NO_STORE}

    async with async_session() as session:
        if not await _wiki_enabled(session):
            return JSONResponse(
                status_code=503,
                headers={"Retry-After": "3600", **cache_headers},
                content={"detail": "wiki_disabled"},
            )

        page = await _get_public_page_by_slug(session, slug)

        if page is None:
            return Response(status_code=404, headers=cache_headers)

        if not page.public_enabled:
            return Response(status_code=404, headers=cache_headers)

        # Governance revalidation
        page_id = uuid.UUID(str(page.id))
        render_result: WikiRenderResult = await render_wiki_page(
            session,
            page_id=page_id,
            role="member",
            body_markdown=page.body_markdown,
        )

        if render_result.page_archived:
            return Response(status_code=410, headers=cache_headers)

        sources = await _get_page_sources(session, page_id)

    template_response = TEMPLATES.TemplateResponse(
        request=request,
        name="wiki/page.html",
        context={
            "request": request,
            "page": page,
            "html_body": render_result.html_body,
            "sources": sources,
            "role": "member",
            "user": None,
        },
    )
    template_response.headers["Cache-Control"] = _NO_STORE
    return template_response


@router.get("/{slug}")
async def wiki_page(slug: str, request: Request) -> Response:
    """Single page view with governance revalidation. Member or admin only.

    Cache-Control (9.5-F): every response (200, 403, 404, 410, 503) carries
    ``Cache-Control: no-store, no-cache, must-revalidate, private`` so that
    stale browser/CDN caches cannot serve forgotten or redacted content.

    9.5-D: pages with page_status='archived' or 'stale' return 410 Gone with
    a template instead of silent 404. Unknown slugs still return 404.
    """
    role = _require_member_or_admin(request)
    if role is None:
        return JSONResponse(
            status_code=403,
            headers=_MEMBER_CACHE_HEADERS,
            content={"detail": "insufficient_role", "required": "member"},
        )

    async with async_session() as session:
        if not await _wiki_enabled(session):
            return JSONResponse(
                status_code=503,
                headers={**_MEMBER_CACHE_HEADERS, "Retry-After": "60"},
                content={"detail": "wiki_disabled"},
            )

        page = await _get_page_by_slug(session, slug)

        if page is None:
            # Check if an archived/stale page exists for 410 Gone (9.5-D).
            archived = await _get_page_by_slug_any_status(session, slug)
            if archived is not None and archived.page_status in ("archived", "stale"):
                gone_response = TEMPLATES.TemplateResponse(
                    request=request,
                    name="wiki/gone.html",
                    context={"request": request},
                    status_code=410,
                )
                for header, value in _MEMBER_CACHE_HEADERS.items():
                    gone_response.headers[header] = value
                return gone_response
            # Unknown slug or draft — not visible.
            return Response(status_code=404, headers=_MEMBER_CACHE_HEADERS)

        page_id = uuid.UUID(str(page.id))

        # Governance revalidation before rendering
        render_result: WikiRenderResult = await render_wiki_page(
            session,
            page_id=page_id,
            role=role,
            body_markdown=page.body_markdown,
        )

        if render_result.page_archived:
            # Render gone.html for UX parity with archived/stale status path above.
            gone_response = TEMPLATES.TemplateResponse(
                request=request,
                name="wiki/gone.html",
                context={"request": request},
                status_code=410,
            )
            for header, value in _MEMBER_CACHE_HEADERS.items():
                gone_response.headers[header] = value
            return gone_response

        sources = await _get_page_sources(session, page_id)

    template_response = TEMPLATES.TemplateResponse(
        request=request,
        name="wiki/page.html",
        context={
            "request": request,
            "page": page,
            "html_body": render_result.html_body,
            "sources": sources,
            "role": role,
            "user": getattr(request.state, "user", {}),
        },
    )
    for header, value in _MEMBER_CACHE_HEADERS.items():
        template_response.headers[header] = value
    return template_response


# ── /robots.txt — wiki variant (AC#9 HIGH F) ─────────────────────────────────


@robots_router.get("/robots.txt", response_class=PlainTextResponse)
async def wiki_robots() -> Response:
    """Wiki robots.txt — anonymous, no-store.

    Per Plan §T9-05 AC#9: every response carries
    ``Cache-Control: no-store, max-age=0, must-revalidate`` so crawlers
    re-fetch immediately after admin /wiki_unpublish or robots_policy flip.

    When ``memory.wiki.enabled=false`` the file disallows everything.
    Otherwise: explicit ``Allow:`` line per page where
    ``public_enabled=true AND robots_policy='index' AND page_status='reviewed'``,
    followed by default-deny ``Disallow: /`` so crawlers don't index admin
    routes, the auth surface, drafts, or non-public wiki content.
    """
    headers = {"Cache-Control": _NO_STORE}
    async with async_session() as session:
        if not await _wiki_enabled(session):
            return PlainTextResponse(
                "User-agent: *\nDisallow: /\n",
                headers=headers,
            )
        allowed_slugs = await _list_robots_allowed_slugs(session)

    lines = ["User-agent: *"]
    for slug in allowed_slugs:
        lines.append(f"Allow: /wiki/public/{slug}")
    lines.append("Disallow: /")
    return PlainTextResponse("\n".join(lines) + "\n", headers=headers)
