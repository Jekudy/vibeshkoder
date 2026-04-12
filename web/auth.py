from __future__ import annotations

import hashlib

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from web.config import settings

_SECRET_KEY = hashlib.sha256(settings.BOT_TOKEN.encode()).hexdigest()
_serializer = URLSafeTimedSerializer(_SECRET_KEY)

_COOKIE_MAX_AGE = 7 * 24 * 60 * 60  # 7 days


def verify_password(password: str) -> bool:
    """Check password against the configured WEB_PASSWORD."""
    return password == settings.WEB_PASSWORD


def create_session_cookie() -> str:
    """Create a signed session cookie for an authenticated user."""
    payload = {"authenticated": True}
    return _serializer.dumps(payload)


def get_user_from_cookie(cookie: str) -> dict | None:
    """Deserialize and verify session cookie. Returns payload dict or None."""
    try:
        data = _serializer.loads(cookie, max_age=_COOKIE_MAX_AGE)
        if data.get("authenticated"):
            return data
        return None
    except (BadSignature, SignatureExpired):
        return None
