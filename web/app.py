from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from web.auth import get_user_from_cookie, create_session_cookie, _cookie_fingerprint, _insert_legacy_grace_audit

logger = logging.getLogger(__name__)

_WEB_DIR = Path(__file__).resolve().parent

TEMPLATES = Jinja2Templates(directory=str(_WEB_DIR / "templates"))

# Paths that don't require authentication
_PUBLIC_PATHS = {"/login", "/docs", "/openapi.json", "/healthz"}

# Path prefixes accessible to members AND admins (T9-05 wiki member routes).
# Everything else is admin-only. Members reaching admin-only paths get 403.
_MEMBER_READABLE_PREFIXES: tuple[str, ...] = ("/wiki/",)


def _is_admin_only_path(path: str) -> bool:
    """True iff the path requires role='admin'."""
    if path in _PUBLIC_PATHS or path.startswith("/static") or path == "/":
        return False
    # /wiki (catalog root) and /wiki/* (sub-paths) are member-readable.
    if path == "/wiki" or any(path.startswith(p) for p in _MEMBER_READABLE_PREFIXES):
        return False
    return True


def create_app() -> FastAPI:
    app = FastAPI(title="Vibe Gatekeeper Admin")

    # Mount static files
    app.mount("/static", StaticFiles(directory=str(_WEB_DIR / "static")), name="static")

    # Auth middleware: redirect unauthenticated users to /login
    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = request.url.path

        # Allow public paths, static files, and the wiki public prefix (T9-05).
        if path in _PUBLIC_PATHS or path.startswith("/static") or path.startswith("/wiki/public/"):
            return await call_next(request)

        cookie = request.cookies.get("session")
        user = get_user_from_cookie(cookie) if cookie else None
        if not user:
            return RedirectResponse(url="/login", status_code=302)

        is_legacy = user.pop("legacy", False)

        # T9-03 role-based ACL: members are denied on admin-only paths.
        # Wiki member routes (T9-05) are reachable to both roles.
        if user.get("role") != "admin" and _is_admin_only_path(path):
            return JSONResponse(
                status_code=403,
                content={"detail": "insufficient_role", "required": "admin"},
            )

        if is_legacy:
            # Legacy cookie without 'role' field: treat as admin for this request
            # and refresh cookie on the response.
            fingerprint = _cookie_fingerprint(cookie)
            logger.warning(
                "legacy session cookie promoted to admin: %s", fingerprint
            )
            # Best-effort audit insert (failure is caught inside _insert_legacy_grace_audit)
            _insert_legacy_grace_audit()

        # Attach user to request state for use in routes
        request.state.user = user
        response = await call_next(request)

        if is_legacy:
            # Refresh cookie with explicit role='admin'
            refreshed = create_session_cookie(role="admin")
            response.set_cookie(
                key="session",
                value=refreshed,
                max_age=7 * 24 * 60 * 60,
                httponly=True,
                samesite="lax",
            )

        return response

    # Import and include route modules
    from web.routes.auth import router as auth_router
    from web.routes.cards import router as cards_router
    from web.routes.dashboard import router as dashboard_router
    from web.routes.health import router as health_router
    from web.routes.members import router as members_router
    from web.routes.wiki import router as wiki_router

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(dashboard_router)
    app.include_router(members_router)
    app.include_router(cards_router)
    app.include_router(wiki_router)

    # Root redirect
    @app.get("/")
    async def root():
        return RedirectResponse(url="/dashboard", status_code=302)

    return app
