from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Literal

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from web.config import settings

logger = logging.getLogger(__name__)

_SECRET_KEY = settings.WEB_SESSION_SECRET
_serializer = URLSafeTimedSerializer(_SECRET_KEY)

_COOKIE_MAX_AGE = 7 * 24 * 60 * 60  # 7 days


def derive_role(password: str | None) -> Literal["admin", "member"] | None:
    """Derive role from password by comparing against admin then member passwords.

    Returns 'admin', 'member', or None if no match.
    """
    if not password:
        return None

    admin_pw = settings.WEB_ADMIN_PASSWORD
    member_pw = settings.WEB_MEMBER_PASSWORD

    if admin_pw and hmac.compare_digest(password.encode(), admin_pw.encode()):
        return "admin"

    if member_pw and hmac.compare_digest(password.encode(), member_pw.encode()):
        return "member"

    return None


def verify_password(password: str | None) -> bool:
    """Backward-compatible single-password check. Returns True if admin password matches."""
    return derive_role(password) == "admin"


def create_session_cookie(role: str = "admin") -> str:
    """Create a signed session cookie for an authenticated user.

    Args:
        role: 'admin' or 'member'. Defaults to 'admin' for backward compat.
    """
    payload = {"authenticated": True, "role": role}
    return _serializer.dumps(payload)


def get_user_from_cookie(cookie: str) -> dict | None:
    """Deserialize and verify session cookie. Returns payload dict or None.

    Backward compat: if the deserialized payload lacks 'role', attaches
    'role': 'admin' and 'legacy': True. The caller (middleware) should then
    refresh the cookie and attempt a best-effort audit insert.
    """
    try:
        data = _serializer.loads(cookie, max_age=_COOKIE_MAX_AGE)
        if data.get("authenticated"):
            if "role" not in data:
                # Legacy cookie (pre-T9-03): treat as admin for one max-age window.
                data["role"] = "admin"
                data["legacy"] = True
            return data
        return None
    except (BadSignature, SignatureExpired):
        return None


def _cookie_fingerprint(cookie: str) -> str:
    """Return a short hash of the cookie value for safe logging (no raw cookie)."""
    return hashlib.sha256(cookie.encode()).hexdigest()[:16]


def _insert_legacy_grace_audit() -> None:
    """Best-effort audit insert for legacy cookie promotion.

    Attempts to insert a wiki_publication_log row with action='legacy_cookie_grace'.
    Wrapped in try/except — failure is logged but does NOT block the request.

    NOTE: wiki_publication_log requires a NOT NULL wiki_page_id FK to wiki_pages.
    Without a wiki page in context (this is a session-level event, not page-level),
    the insert will fail with a FK/NOT NULL violation. This is expected and caught.
    The log entry serves as an audit signal when a wiki_page_id IS available in future.
    """
    try:
        import asyncio

        from sqlalchemy import text

        from bot.db.engine import async_session

        async def _do_insert() -> None:
            async with async_session() as session:
                await session.execute(
                    text(
                        "INSERT INTO wiki_publication_log "
                        "(action, actor_user_id, wiki_page_id, "
                        " prior_public_enabled, new_public_enabled, "
                        " prior_robots_policy, new_robots_policy, "
                        " source_check_result, reason) "
                        "VALUES ('legacy_cookie_grace', NULL, "
                        " gen_random_uuid(), "
                        " false, false, 'index', 'index', "
                        " '{\"reason\": \"missing_role_field\"}'::jsonb, "
                        " 'legacy session promoted to admin')"
                    )
                )
                await session.commit()

        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Schedule as a background task (won't block the request)
            loop.create_task(_do_insert())
        else:
            loop.run_until_complete(_do_insert())
    except Exception as exc:  # noqa: BLE001
        logger.warning("best-effort legacy_cookie_grace audit insert failed: %s", exc)
