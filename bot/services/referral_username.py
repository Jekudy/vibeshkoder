from __future__ import annotations

import re
from urllib.parse import urlsplit


_TELEGRAM_USERNAME_RE = re.compile(r"[A-Za-z0-9_]{5,32}\Z")


class InvalidReferralUsername(ValueError):
    """Raised when a referral answer is not a Telegram username."""


def normalize_referral_username(raw: str) -> str:
    """Return a Telegram username in canonical ``@username`` form."""
    value = raw.strip()
    if not value:
        raise InvalidReferralUsername("Telegram username is empty")

    if value.startswith("@"):
        username = value[1:]
    elif value.lower().startswith("t.me/"):
        username = _username_from_url(f"https://{value}")
    elif value.lower().startswith("https://"):
        username = _username_from_url(value)
    else:
        username = value

    if _TELEGRAM_USERNAME_RE.fullmatch(username) is None:
        raise InvalidReferralUsername("Invalid Telegram username")

    return f"@{username.lower()}"


def is_concrete_referral(value: str | None) -> bool:
    """Return whether a stored referral is a valid Telegram username."""
    if value is None or not value.strip():
        return False
    try:
        normalize_referral_username(value)
    except InvalidReferralUsername:
        return False
    return True


def _username_from_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise InvalidReferralUsername("Invalid Telegram profile URL") from error
    if parsed.scheme.lower() != "https" or parsed.netloc.lower() != "t.me":
        raise InvalidReferralUsername("Invalid Telegram profile URL")

    username = parsed.path.strip("/")
    if not username or "/" in username or parsed.fragment:
        raise InvalidReferralUsername("Invalid Telegram profile URL path")
    return username
