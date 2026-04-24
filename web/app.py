from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from web.auth import get_user_from_cookie

_WEB_DIR = Path(__file__).resolve().parent

TEMPLATES = Jinja2Templates(directory=str(_WEB_DIR / "templates"))

# Paths that don't require authentication
_PUBLIC_PATHS = {"/login", "/docs", "/openapi.json"}


def _get_client_ip(request: Request) -> str:
    """Return the real client IP, honouring X-Forwarded-For from a trusted proxy.

    Starlette's ProxyHeadersMiddleware is not available in the installed version,
    so we extract the leftmost (original client) address from X-Forwarded-For.
    This is safe because Coolify places the app behind a non-public, container-
    network-only proxy — external traffic cannot inject a spoofed header that the
    proxy does not strip/overwrite.

    Falls back to request.client.host when the header is absent (e.g. direct
    connections in tests without the header).
    """
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return get_remote_address(request)


# Module-level limiter instance — routes import this to apply decorators.
# Uses in-memory storage (suitable for single-process deployments).
# _get_client_ip reads X-Forwarded-For so each real client gets its own bucket
# even when all requests arrive from the same proxy/container IP.
limiter = Limiter(key_func=_get_client_ip, storage_uri="memory://")


def create_app() -> FastAPI:
    app = FastAPI(title="Vibe Gatekeeper Admin")

    # Register slowapi limiter and its 429 handler.
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # Mount static files
    app.mount("/static", StaticFiles(directory=str(_WEB_DIR / "static")), name="static")

    # Auth middleware: redirect unauthenticated users to /login
    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = request.url.path

        # Allow public paths and static files
        if path in _PUBLIC_PATHS or path.startswith("/static"):
            return await call_next(request)

        cookie = request.cookies.get("session")
        user = get_user_from_cookie(cookie) if cookie else None
        if not user:
            return RedirectResponse(url="/login", status_code=302)

        # Attach user to request state for use in routes
        request.state.user = user
        return await call_next(request)

    # Import and include route modules
    from web.routes.auth import router as auth_router
    from web.routes.dashboard import router as dashboard_router
    from web.routes.members import router as members_router

    app.include_router(auth_router)
    app.include_router(dashboard_router)
    app.include_router(members_router)

    # Root redirect
    @app.get("/")
    async def root():
        return RedirectResponse(url="/dashboard", status_code=302)

    return app
