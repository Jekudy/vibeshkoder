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

    Constant-time across role decisions: both compare_digest calls always run
    (even when one config slot is unset — uses dummy bytes) so an attacker
    cannot distinguish admin vs member vs unset via response timing.
    """
    if not password:
        return None

    pw_bytes = password.encode()
    admin_pw = settings.WEB_ADMIN_PASSWORD
    member_pw = settings.WEB_MEMBER_PASSWORD

    # Dummy values keep both compare_digest calls running unconditionally.
    admin_target = admin_pw.encode() if admin_pw else b"\x00" * len(pw_bytes)
    member_target = member_pw.encode() if member_pw else b"\x00" * len(pw_bytes)

    admin_match = hmac.compare_digest(pw_bytes, admin_target) and admin_pw is not None
    member_match = hmac.compare_digest(pw_bytes, member_target) and member_pw is not None

    if admin_match:
        return "admin"
    if member_match:
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

    Inserts a wiki_publication_log row with action='legacy_cookie_grace' and
    wiki_page_id=NULL. Legacy-grace events are session-level, not page-level,
    so no wiki_page_id exists — migration 055 made the column NULLABLE for this
    action (CHECK: wiki_page_id IS NOT NULL OR action = 'legacy_cookie_grace').

    Wrapped in try/except — failure is logged as WARNING but does NOT block the
    request. Success is logged as INFO.
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
                        " NULL, "
                        " false, false, 'index', 'index', "
                        " '{\"reason\": \"missing_role_field\"}'::jsonb, "
                        " 'legacy_cookie_promoted_to_admin')"
                    )
                )
                await session.commit()
            logger.info("legacy_cookie_grace audit row inserted successfully")

        # Inside FastAPI middleware there's always a running loop. The sync
        # fallback (asyncio.run) is used only for direct test invocation outside
        # a running event loop.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        def _log_task_exception(task: asyncio.Task) -> None:
            """Attach as done_callback so insert failures are visible."""
            exc = task.exception()
            if exc is not None:
                logger.warning(
                    "legacy_cookie_grace audit insert failed: %s", exc
                )

        if loop is not None and loop.is_running():
            task = loop.create_task(_do_insert())
            task.add_done_callback(_log_task_exception)
        else:
            asyncio.run(_do_insert())
    except Exception as exc:  # noqa: BLE001
        logger.warning("best-effort legacy_cookie_grace audit insert failed: %s", exc)
