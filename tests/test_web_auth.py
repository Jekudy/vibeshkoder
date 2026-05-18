from __future__ import annotations

from tests.conftest import import_module


def test_password_and_session_cookie_roundtrip(app_env) -> None:
    auth = import_module("web.auth")

    assert auth.verify_password("test-pass") is True
    assert auth.verify_password("wrong-pass") is False

    cookie = auth.create_session_cookie()
    payload = auth.get_user_from_cookie(cookie)
    # T9-03: payload now carries role; default is admin for back-compat.
    assert payload is not None
    assert payload.get("authenticated") is True
    assert payload.get("role") == "admin"


def test_verify_password_constant_time_correct(app_env) -> None:
    auth = import_module("web.auth")

    assert auth.verify_password("test-pass") is True


def test_verify_password_constant_time_incorrect(app_env) -> None:
    auth = import_module("web.auth")

    assert auth.verify_password("wrong-pass") is False


def test_invalid_cookie_returns_none(app_env) -> None:
    auth = import_module("web.auth")

    assert auth.get_user_from_cookie("broken-cookie") is None
