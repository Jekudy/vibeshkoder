from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from web.auth import get_user_from_cookie

_WEB_DIR = Path(__file__).resolve().parent

TEMPLATES = Jinja2Templates(directory=str(_WEB_DIR / "templates"))

# Paths that don't require authentication
_PUBLIC_PATHS = {"/login", "/docs", "/openapi.json", "/healthz"}


def create_app() -> FastAPI:
    app = FastAPI(title="Vibe Gatekeeper Admin")

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
    from web.routes.health import router as health_router
    from web.routes.members import router as members_router

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(dashboard_router)
    app.include_router(members_router)

    # Root redirect
    @app.get("/")
    async def root():
        return RedirectResponse(url="/dashboard", status_code=302)

    return app
