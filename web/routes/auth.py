from __future__ import annotations

from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse

from web.app import TEMPLATES
from web.auth import derive_role, create_session_cookie

router = APIRouter()


@router.get("/login")
async def login_page(request: Request, error: str = ""):
    return TEMPLATES.TemplateResponse(
        request=request,
        name="login.html",
        context={"request": request, "error": error},
    )


@router.post("/login")
async def login_submit(request: Request, password: str = Form(...)):
    """Verify password, derive role, set session cookie, redirect to dashboard.

    user_id is intentionally NOT accepted as a form field — role is derived
    solely from password match (R6.e: no user_id self-claim escalation).
    """
    role = derive_role(password)
    if role is None:
        return TEMPLATES.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "request": request,
                "error": "Invalid password. Please try again.",
            },
        )

    cookie_value = create_session_cookie(role=role)

    response = RedirectResponse(url="/dashboard", status_code=302)
    response.set_cookie(
        key="session",
        value=cookie_value,
        max_age=7 * 24 * 60 * 60,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("session")
    return response
