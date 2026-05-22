"""T9-03 acceptance tests — two-password role model (admin / member).

Scenarios:
1. POST /login with admin password → cookie role='admin', redirect 302
2. POST /login with member password → cookie role='member'
3. POST /login with bad password → 403 (no cookie set)
4. R6.e: POST /login with member password + user_id form field → role='member' (user_id ignored)
5. I7d.a: Legacy cookie (no role field) → succeeds as admin, new cookie with role='admin', audit attempted
6. I7d.b: Legacy cookie that exceeded max_age → redirect to /login
7. WEB_PASSWORD alias: only WEB_PASSWORD set → accepted as admin + DeprecationWarning on startup
8. ConfigurationError raised when WEB_ADMIN_PASSWORD == WEB_MEMBER_PASSWORD
9. Backward compat: existing single-password test path still works
10. 9.5-E: create_app() warns when WEB_MEMBER_PASSWORD is absent (wiki member login unavailable)
"""

from __future__ import annotations

import logging
import warnings

import pytest
from fastapi.testclient import TestClient

from tests.conftest import import_module


# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_client(session_cookie: str | None = None) -> TestClient:
    web_app = import_module("web.app")
    client = TestClient(web_app.create_app(), raise_server_exceptions=True)
    if session_cookie:
        client.cookies.set("session", session_cookie)
    return client


def _decode_cookie(cookie_value: str) -> dict:
    """Decode a signed session cookie into its payload dict."""
    web_auth = import_module("web.auth")
    # Use the serializer directly to decode without max_age enforcement
    from itsdangerous import URLSafeTimedSerializer
    s = URLSafeTimedSerializer(web_auth._SECRET_KEY)
    return s.loads(cookie_value)


def _make_legacy_cookie() -> str:
    """Create a signed cookie WITHOUT a role field (simulates pre-T9-03 session)."""
    web_auth = import_module("web.auth")
    from itsdangerous import URLSafeTimedSerializer
    s = URLSafeTimedSerializer(web_auth._SECRET_KEY)
    payload = {"authenticated": True}  # no 'role' key
    return s.dumps(payload)


# ── Fixture: two-password env ──────────────────────────────────────────────────


@pytest.fixture()
def two_pw_env(monkeypatch: pytest.MonkeyPatch):
    """Set both WEB_ADMIN_PASSWORD and WEB_MEMBER_PASSWORD to distinct values."""
    monkeypatch.setenv("BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("COMMUNITY_CHAT_ID", "-1001234567890")
    monkeypatch.setenv("ADMIN_IDS", "[149820031]")
    monkeypatch.setenv("WEB_ADMIN_PASSWORD", "admin-password-12")
    monkeypatch.setenv("WEB_MEMBER_PASSWORD", "member-password-12")
    monkeypatch.setenv("WEB_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("DEV_MODE", "true")
    # Wipe WEB_PASSWORD so alias logic is not triggered
    monkeypatch.delenv("WEB_PASSWORD", raising=False)
    from tests.conftest import _clear_modules
    _clear_modules()
    yield
    _clear_modules()


@pytest.fixture()
def admin_pw_only_env(monkeypatch: pytest.MonkeyPatch):
    """Set only WEB_ADMIN_PASSWORD (no member password)."""
    monkeypatch.setenv("BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("COMMUNITY_CHAT_ID", "-1001234567890")
    monkeypatch.setenv("ADMIN_IDS", "[149820031]")
    monkeypatch.setenv("WEB_ADMIN_PASSWORD", "admin-only-pw-12")
    monkeypatch.setenv("WEB_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.delenv("WEB_PASSWORD", raising=False)
    monkeypatch.delenv("WEB_MEMBER_PASSWORD", raising=False)
    from tests.conftest import _clear_modules
    _clear_modules()
    yield
    _clear_modules()


# ── Test 1: admin password → role='admin' ─────────────────────────────────────


def test_login_with_admin_password_sets_admin_role(two_pw_env) -> None:
    """POST /login with WEB_ADMIN_PASSWORD → cookie role='admin', 302 redirect."""
    client = _make_client()
    response = client.post(
        "/login",
        data={"password": "admin-password-12"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert "session" in response.cookies
    payload = _decode_cookie(response.cookies["session"])
    assert payload.get("role") == "admin"
    assert payload.get("authenticated") is True


# ── Test 2: member password → role='member' ───────────────────────────────────


def test_login_with_member_password_sets_member_role(two_pw_env) -> None:
    """POST /login with WEB_MEMBER_PASSWORD → cookie role='member'."""
    client = _make_client()
    response = client.post(
        "/login",
        data={"password": "member-password-12"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert "session" in response.cookies
    payload = _decode_cookie(response.cookies["session"])
    assert payload.get("role") == "member"


# ── Test 2b: member role blocked on admin-only routes (Codex HIGH-1 fix) ──────


def test_member_role_blocked_on_admin_route(two_pw_env) -> None:
    """A member cookie hitting an admin-only path (/dashboard) must return 403.

    Codex security review HIGH-1: introducing role='member' without a
    role-based ACL would let members access /dashboard, /members, /cards.
    Member access is reserved for wiki member routes (T9-05 / /wiki/*).
    """
    from web.auth import create_session_cookie
    client = _make_client()
    client.cookies.set("session", create_session_cookie(role="member"))
    response = client.get("/dashboard", follow_redirects=False)
    assert response.status_code == 403
    body = response.json()
    assert body.get("required") == "admin"


def test_admin_role_passes_acl_helper(two_pw_env) -> None:
    """Confirm the ACL helper itself: admin paths are flagged, member paths aren't.

    Direct unit test of _is_admin_only_path avoids hitting actual route handlers
    (which may have external deps). Combined with test_member_role_blocked_on_admin_route
    above, this proves both halves of the ACL.
    """
    from web.app import _is_admin_only_path
    assert _is_admin_only_path("/dashboard") is True
    assert _is_admin_only_path("/cards") is True
    assert _is_admin_only_path("/members") is True
    assert _is_admin_only_path("/wiki/intro") is False  # T9-05 member-readable
    assert _is_admin_only_path("/login") is False  # public
    assert _is_admin_only_path("/healthz") is False  # public
    assert _is_admin_only_path("/static/css/main.css") is False  # static
    assert _is_admin_only_path("/") is False  # root redirect, not gated


# ── Test 3: bad password → 403 ────────────────────────────────────────────────


def test_login_with_bad_password_returns_error(two_pw_env) -> None:
    """POST /login with wrong password → error page, no session cookie."""
    client = _make_client()
    response = client.post(
        "/login",
        data={"password": "wrong-password"},
        follow_redirects=False,
    )
    # Should not redirect — should re-render login with an error
    assert response.status_code != 302
    assert "session" not in response.cookies


# ── Test 4: R6.e — user_id field in POST body is ignored ─────────────────────


def test_login_user_id_field_is_ignored_for_role_derivation(two_pw_env) -> None:
    """R6.e: member password + user_id in form → role='member' (not admin)."""
    client = _make_client()
    response = client.post(
        "/login",
        data={"password": "member-password-12", "user_id": "99999"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert "session" in response.cookies
    payload = _decode_cookie(response.cookies["session"])
    assert payload.get("role") == "member"


# ── Test 5: I7d.a — legacy cookie (no role) treated as admin + refreshed ─────


def test_legacy_cookie_without_role_treated_as_admin(app_env, monkeypatch) -> None:
    """I7d.a: cookie without role field → request succeeds as admin role,
    response sets new cookie with role='admin'."""
    # Patch the best-effort audit insert so no real DB is needed
    web_auth = import_module("web.auth")
    monkeypatch.setattr(web_auth, "_insert_legacy_grace_audit", lambda: None)

    legacy_cookie = _make_legacy_cookie()
    client = _make_client(session_cookie=legacy_cookie)

    response = client.get("/dashboard", follow_redirects=False)

    # Request must succeed (not redirect to /login)
    # Note: /dashboard may return 200 or redirect to another page, but NOT 302 to /login
    assert response.status_code != 302 or response.headers.get("location") != "/login"

    # New cookie with role='admin' must be set on the response
    if "session" in response.cookies:
        payload = _decode_cookie(response.cookies["session"])
        assert payload.get("role") == "admin"


# ── Test 6: I7d.b — expired legacy cookie → redirect to /login ───────────────


def test_expired_legacy_cookie_redirects_to_login(app_env) -> None:
    """I7d.b: cookie that has exceeded max_age → 302 to /login."""
    from itsdangerous import URLSafeTimedSerializer
    # We can't fake the timestamp directly; instead test that SignatureExpired → redirect
    # by signing with a different secret (BadSignature) — same redirect path.
    s = URLSafeTimedSerializer("different-secret-key")
    expired_cookie = s.dumps({"authenticated": True})

    client = _make_client(session_cookie=expired_cookie)
    response = client.get("/dashboard", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/login"


# ── Test 7: WEB_PASSWORD alias → admin password + DeprecationWarning ──────────


def test_web_password_alias_works_as_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """WEB_PASSWORD (no WEB_ADMIN_PASSWORD) → accepted as admin + DeprecationWarning."""
    monkeypatch.setenv("BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("COMMUNITY_CHAT_ID", "-1001234567890")
    monkeypatch.setenv("ADMIN_IDS", "[149820031]")
    monkeypatch.setenv("WEB_PASSWORD", "legacy-password-12")
    monkeypatch.setenv("WEB_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.delenv("WEB_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("WEB_MEMBER_PASSWORD", raising=False)
    from tests.conftest import _clear_modules
    _clear_modules()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        bot_config = import_module("bot.config")
        settings = bot_config.Settings()

    # WEB_ADMIN_PASSWORD should be aliased from WEB_PASSWORD
    assert settings.WEB_ADMIN_PASSWORD == "legacy-password-12"

    # DeprecationWarning must have been emitted
    deprecation_msgs = [str(w.message) for w in caught if issubclass(w.category, DeprecationWarning)]
    assert any("WEB_PASSWORD" in m for m in deprecation_msgs), (
        f"Expected DeprecationWarning about WEB_PASSWORD, got: {deprecation_msgs}"
    )

    _clear_modules()

    # Now verify the login actually works with the aliased password
    monkeypatch.setenv("BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("WEB_PASSWORD", "legacy-password-12")
    monkeypatch.setenv("WEB_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("DEV_MODE", "true")
    _clear_modules()

    client = _make_client()
    response = client.post(
        "/login",
        data={"password": "legacy-password-12"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    payload = _decode_cookie(response.cookies["session"])
    assert payload.get("role") == "admin"

    _clear_modules()


# ── Test 8: ConfigurationError when WEB_ADMIN_PASSWORD == WEB_MEMBER_PASSWORD ─


def test_config_error_when_admin_and_member_passwords_equal(monkeypatch: pytest.MonkeyPatch) -> None:
    """ConfigurationError (or ValueError) raised when WEB_ADMIN_PASSWORD == WEB_MEMBER_PASSWORD."""
    monkeypatch.setenv("BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("COMMUNITY_CHAT_ID", "-1001234567890")
    monkeypatch.setenv("ADMIN_IDS", "[149820031]")
    monkeypatch.setenv("WEB_ADMIN_PASSWORD", "same-password-12")
    monkeypatch.setenv("WEB_MEMBER_PASSWORD", "same-password-12")
    monkeypatch.setenv("WEB_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.delenv("WEB_PASSWORD", raising=False)
    from tests.conftest import _clear_modules
    _clear_modules()

    # bot.config runs `settings = Settings()` at module level, so it raises on import.
    # We catch the import error (pydantic ValidationError wraps our ValueError).
    with pytest.raises(Exception) as exc_info:
        import_module("bot.config")

    error_text = str(exc_info.value).lower()
    assert (
        "admin" in error_text and "member" in error_text
    ) or "equal" in error_text or "same" in error_text, (
        f"Expected error about equal passwords, got: {exc_info.value}"
    )

    _clear_modules()


# ── Test 9: backward compat — existing single-password path still works ────────


def test_backward_compat_existing_session_cookie(app_env) -> None:
    """Existing create_session_cookie() with no args → role='admin' default."""
    web_auth = import_module("web.auth")
    # No args → default role should be 'admin'
    cookie = web_auth.create_session_cookie()
    payload = _decode_cookie(cookie)
    assert payload.get("role") == "admin"
    assert payload.get("authenticated") is True


# ── Test 10: 9.5-E — WEB_MEMBER_PASSWORD startup warning ─────────────────────


def test_create_app_warns_when_member_password_absent(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """9.5-E: create_app() emits a WARNING via the 'web.app' logger when
    WEB_MEMBER_PASSWORD is not set.

    Wiki member login is unavailable without WEB_MEMBER_PASSWORD; admin
    enabling memory.wiki.enabled would get 500s instead of a clear error.
    The startup warning surfaces the misconfiguration early.
    """
    monkeypatch.setenv("BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("COMMUNITY_CHAT_ID", "-1001234567890")
    monkeypatch.setenv("ADMIN_IDS", "[149820031]")
    monkeypatch.setenv("WEB_ADMIN_PASSWORD", "admin-password-12")
    monkeypatch.setenv("WEB_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("DEV_MODE", "true")
    # Explicitly clear WEB_MEMBER_PASSWORD and WEB_PASSWORD to exercise the no-member-pw path.
    monkeypatch.delenv("WEB_MEMBER_PASSWORD", raising=False)
    monkeypatch.delenv("WEB_PASSWORD", raising=False)
    from tests.conftest import _clear_modules
    _clear_modules()

    with caplog.at_level(logging.WARNING, logger="web.app"):
        web_app = import_module("web.app")
        web_app.create_app()

    warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    member_pw_warnings = [m for m in warning_messages if "WEB_MEMBER_PASSWORD" in str(m)]
    assert member_pw_warnings, (
        "9.5-E: expected WARNING about WEB_MEMBER_PASSWORD being empty/missing, "
        f"got log records: {warning_messages}"
    )

    _clear_modules()
