"""GET /healthz — public, unauthenticated health endpoint.

Returns 200 when the app is healthy (DB reachable, settings sane), 503 otherwise. The
response body is small and contains no secrets. Used by Coolify / uptime probes / CI smoke.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from bot.services.health import report

router = APIRouter()


@router.get("/healthz", include_in_schema=False)
async def healthz() -> JSONResponse:
    h = await report()
    status_code = 200 if h.ok else 503
    return JSONResponse(content=h.to_dict(), status_code=status_code)
