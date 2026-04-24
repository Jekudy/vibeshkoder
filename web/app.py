from __future__ import annotations

import ipaddress
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from web.auth import get_user_from_cookie

_WEB_DIR = Path(__file__).resolve().parent

TEMPLATES = Jinja2Templates(directory=str(_WEB_DIR / "templates"))

# Paths that don't require authentication
_PUBLIC_PATHS = {"/login", "/docs", "/openapi.json"}


def _is_trusted_proxy(client_ip: str, trusted_spec: str) -> bool:
    """Return True if client_ip matches the trusted proxy specification.

    trusted_spec is a comma-separated list of IPs or CIDRs, or the wildcard "*".
    "*" means trust all — ONLY safe when the app is known to be behind a proxy
    network (e.g. Coolify's internal Docker network) and is never reachable directly.
    """
    if trusted_spec == "*":
        return True
    try:
        client = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    for entry in trusted_spec.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            if "/" in entry:
                if client in ipaddress.ip_network(entry, strict=False):
                    return True
            else:
                if client == ipaddress.ip_address(entry):
                    return True
        except ValueError:
            continue
    return False


def _get_client_ip(request: Request) -> str:
    """Extract client IP for rate-limiting.

    Trust model: X-Forwarded-For is only honoured when request.client.host
    matches an entry in settings.TRUSTED_PROXY_HOSTS. Prod (vibe-gatekeeper)
    is currently direct-exposed with no proxy, so the default is to ignore XFF
    entirely and use the real TCP peer address.

    This prevents an attacker from spoofing X-Forwarded-For to rotate rate-limit
    buckets and bypass the /login brute-force protection.
    """
    from bot.config import settings

    client_host = request.client.host if request.client else ""
    trusted = (settings.TRUSTED_PROXY_HOSTS or "").strip()

    if trusted and client_host and _is_trusted_proxy(client_host, trusted):
        forwarded = request.headers.get("X-Forwarded-For", "")
        first_hop = forwarded.split(",")[0].strip() if forwarded else ""
        if first_hop:
            return first_hop

    return client_host or "unknown"


# Module-level limiter instance — routes import this to apply decorators.
# Uses in-memory storage (suitable for single-process deployments).
# _get_client_ip honours X-Forwarded-For only from trusted proxies (TRUSTED_PROXY_HOSTS).
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
