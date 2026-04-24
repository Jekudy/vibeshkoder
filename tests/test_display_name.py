from __future__ import annotations

import pytest
from unittest.mock import MagicMock


@pytest.fixture()
def make_user():
    """Factory: create a mock User with the given attributes."""

    def _make(first_name=None, last_name=None, id=123):
        user = MagicMock()
        user.id = id
        user.first_name = first_name
        user.last_name = last_name
        return user

    return _make


def test_display_name_both_names(app_env, make_user):
    """User with first_name and last_name returns 'first last'."""
    from web.routes.members import _display_name

    user = make_user(first_name="Alice", last_name="Smith")
    assert _display_name(user) == "Alice Smith"


def test_display_name_first_only(app_env, make_user):
    """User with only first_name returns 'first'."""
    from web.routes.members import _display_name

    user = make_user(first_name="Alice", last_name=None)
    assert _display_name(user) == "Alice"


def test_display_name_no_names(app_env, make_user):
    """User with no names returns fallback 'User #<id>'."""
    from web.routes.members import _display_name

    user = make_user(first_name=None, last_name=None, id=42)
    assert _display_name(user) == "User #42"
