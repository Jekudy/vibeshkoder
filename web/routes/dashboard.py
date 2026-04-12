from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import func, select

from bot.db.engine import async_session
from bot.db.models import Application
from web.app import TEMPLATES

router = APIRouter()

# Statuses that count as "filled" (completed the questionnaire)
_FILLED_STATUSES = {"pending", "vouched", "added", "rejected"}


@router.get("/dashboard")
async def dashboard(request: Request):
    async with async_session() as session:
        # Total started (all applications)
        total_q = await session.execute(select(func.count(Application.id)))
        total_started = total_q.scalar() or 0

        # Count by status
        status_counts_q = await session.execute(
            select(Application.status, func.count(Application.id)).group_by(
                Application.status
            )
        )
        status_counts = dict(status_counts_q.all())

        filled = sum(status_counts.get(s, 0) for s in _FILLED_STATUSES)
        waiting = status_counts.get("pending", 0)
        added = status_counts.get("added", 0)
        rejected = status_counts.get("rejected", 0)

    stats = {
        "total_started": total_started,
        "filled": filled,
        "waiting": waiting,
        "added": added,
        "rejected": rejected,
    }

    return TEMPLATES.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={"request": request, "stats": stats, "user": request.state.user},
    )
