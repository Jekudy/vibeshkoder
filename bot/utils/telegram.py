from __future__ import annotations


def mention_for(user) -> str:
    """Return a display mention for a user object.

    Returns ``@username`` if the user has a username, otherwise returns
    ``first_name`` (or ``"участник"`` if first_name is also absent).

    Works with both aiogram Telegram user objects and SQLAlchemy User models
    via duck-typed attribute access.
    """
    username = getattr(user, "username", None)
    if username:
        return f"@{username}"
    first_name = getattr(user, "first_name", None) or "участник"
    return first_name
