"""Read-only web routes for approved knowledge cards (T6-08 / issue #240).

Admin-only, guarded by the cookie auth middleware in web/app.py.
No write endpoints — Telegram remains the canonical admin surface.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.engine import async_session
from bot.db.models import User
from bot.db.repos.card_source import CardSourceRepo
from bot.db.repos.knowledge_card import KnowledgeCardRepo
from web.app import TEMPLATES

router = APIRouter(prefix="/cards", tags=["cards"])


async def _get_user_by_id(session: AsyncSession, user_id: int) -> User | None:
    """Fetch a User by primary key; returns None if not found."""
    result = await session.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


@router.get("/")
async def cards_list(request: Request):
    """List all approved knowledge cards (newest first, up to 200)."""
    async with async_session() as session:
        cards = await KnowledgeCardRepo.list_approved(session, limit=200, offset=0)

        # Resolve approver display names in a single pass
        approver_ids = {c.approved_by_user_id for c in cards if c.approved_by_user_id}
        approver_names: dict[int, str] = {}
        for uid in approver_ids:
            user = await _get_user_by_id(session, uid)
            if user:
                name = user.first_name
                if user.last_name:
                    name = f"{name} {user.last_name}"
                approver_names[uid] = name

    card_rows = []
    for card in cards:
        approver = (
            approver_names.get(card.approved_by_user_id, "")
            if card.approved_by_user_id
            else ""
        )
        card_rows.append(
            {
                "id": card.id,
                "title": card.title,
                "approved_at": card.approved_at,
                "approved_by": approver,
            }
        )

    return TEMPLATES.TemplateResponse(
        request=request,
        name="cards.html",
        context={
            "request": request,
            "cards": card_rows,
            "user": request.state.user,
        },
    )


@router.get("/{card_id}")
async def card_detail(card_id: uuid.UUID, request: Request):
    """Detail page for a single approved card with full body + sources."""
    async with async_session() as session:
        card = await KnowledgeCardRepo.get_by_id(session, card_id)
        if card is None:
            raise HTTPException(status_code=404, detail="Card not found")

        sources = await CardSourceRepo.list_for_card(session, card_id)

        approver_name = ""
        if card.approved_by_user_id:
            user = await _get_user_by_id(session, card.approved_by_user_id)
            if user:
                approver_name = user.first_name
                if user.last_name:
                    approver_name = f"{approver_name} {user.last_name}"

    return TEMPLATES.TemplateResponse(
        request=request,
        name="card_detail.html",
        context={
            "request": request,
            "card": card,
            "sources": sources,
            "approved_by": approver_name,
            "user": request.state.user,
        },
    )
