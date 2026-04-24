from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.db.models import User, Application
from web.app import TEMPLATES
from web.dependencies import get_session

router = APIRouter()


def _display_name(user: User) -> str:
    """Return a human-readable display name for a user.

    Priority: "first last" → "first" → "User #<id>".
    """
    if user.first_name and user.last_name:
        return f"{user.first_name} {user.last_name}"
    if user.first_name:
        return user.first_name
    return f"User #{user.id}"


@router.get("/members")
async def members(
    request: Request,
    name: str = "",
    session: AsyncSession = Depends(get_session),
):
    stmt = (
        select(User)
        .where(User.is_member.is_(True))
        .options(selectinload(User.intro))
        .order_by(User.first_name)
    )

    if name:
        pattern = f"%{name}%"
        stmt = stmt.where(
            User.first_name.ilike(pattern)
            | User.last_name.ilike(pattern)
            | User.username.ilike(pattern)
        )

    result = await session.execute(stmt)
    users = result.scalars().all()

    # For each user, get the latest application to find vouched_by info
    user_ids = [u.id for u in users]
    vouch_stmt = (
        select(Application)
        .where(
            Application.user_id.in_(user_ids),
            Application.vouched_by.is_not(None),
        )
        .order_by(Application.created_at.desc())
    )
    vouch_result = await session.execute(vouch_stmt)
    vouch_apps = vouch_result.scalars().all()

    # Map user_id -> voucher user_id (latest app)
    vouch_map: dict[int, int] = {}
    for app in vouch_apps:
        if app.user_id not in vouch_map:
            vouch_map[app.user_id] = app.vouched_by

    # Fetch voucher names
    voucher_ids = set(vouch_map.values())
    voucher_names: dict[int, str] = {}
    if voucher_ids:
        vouchers_q = await session.execute(select(User).where(User.id.in_(voucher_ids)))
        for v in vouchers_q.scalars():
            voucher_names[v.id] = _display_name(v)

    # Build member list for template
    member_list = []
    for u in users:
        voucher_id = vouch_map.get(u.id)
        vouched_by = voucher_names.get(voucher_id, "") if voucher_id else ""

        member_list.append(
            {
                "name": _display_name(u),
                "username": u.username or "",
                "has_intro": u.intro is not None,
                "vouched_by": vouched_by,
                "joined_at": u.joined_at,
            }
        )

    return TEMPLATES.TemplateResponse(
        request=request,
        name="members.html",
        context={
            "request": request,
            "members": member_list,
            "search_name": name,
            "user": request.state.user,
        },
    )
