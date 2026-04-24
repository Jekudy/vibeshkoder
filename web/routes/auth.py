from __future__ import annotations

from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse

from web.app import TEMPLATES
from web.auth import verify_password, create_session_cookie, COOKIE_MAX_AGE

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
    """Verify password, set session cookie, redirect to dashboard."""
    if not verify_password(password):
        return TEMPLATES.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "request": request,
                "error": "Invalid password. Please try again.",
            },
        )

    cookie_value = create_session_cookie()

    response = RedirectResponse(url="/dashboard", status_code=302)
    response.set_cookie(
        key="session",
        value=cookie_value,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("session")
    return response
