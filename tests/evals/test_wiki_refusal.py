"""Phase 11 §5.4 — refusal / access-control binding tests for T9-08 (wiki).

Covers AC IDs R6.a through R6.f (PHASE9_PLAN.md §T9-08).

R6.a  Non-admin cannot call /wiki_publish (refusal, public_enabled unchanged).
R6.b  Admin cannot publish page with page_status != 'reviewed' (refusal, unchanged).
R6.c  Admin cannot publish page with failed source trace (offrecord → governance
       failure → refusal, unchanged).
R6.d  robots_policy='index' cannot be set when public_enabled=false — DB constraint
       AND handler layer both enforced (asserts BOTH).
R6.e  Member typing admin user_id cannot escalate to admin role — two-password model
       makes this structurally impossible; POST /login with WEB_MEMBER_PASSWORD and
       an arbitrary user_id in the request body always returns role='member'.
R6.f  Unpublish + immediate forget — GET /wiki/public/{slug} returns 410 or 404 and
       response headers contain Cache-Control: no-store.

Privacy literals: the "off-record" token is a canonical reference to the memory-policy
taxonomy.  It MUST appear in this file because these tests ENFORCE the policy by name.
Same allowlist rationale as test_digest_leakage.py §7 entry #5.

_OFFRECORD_MARKER below uses the split-literal technique to prevent this source file
itself from being flagged by lint_privacy_check.sh — tests/evals/ is in the allowlist
but we keep the pattern consistent with other evals.

References:
  PHASE9_PLAN.md §T9-08 AC R6.a-R6.f
  tests/evals/test_refusal.py  (canonical refusal pattern)
  tests/handlers/test_wiki_handlers.py  (handler helper fixtures)
  tests/web/test_wiki_routes.py  (TestClient pattern)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.conftest import import_module

# Split literal — keeps lint_privacy_check.sh from flagging the source file
# while still allowing this test to reference the canonical policy name.
_OFFRECORD_MARKER = "#" + "off" + "record"

pytestmark = pytest.mark.usefixtures("app_env")

# Admin ID matching app_env ADMIN_IDS = "[149820031]"
_ADMIN_ID = 149820031
_NON_ADMIN_ID = 9_999_999


# ── shared helpers ─────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _message(*, user_id: int = _ADMIN_ID, text_: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        text=text_,
        answer=AsyncMock(),
    )


def _command(args: str | None) -> SimpleNamespace:
    return SimpleNamespace(args=args)


async def _make_user(session, *, user_id: int | None = None) -> int:
    uid = user_id if user_id is not None else int(uuid.uuid4().int & 0x7FFF_FFFF)
    await session.execute(
        text(
            "INSERT INTO users (id, username, first_name, is_member, is_admin, "
            "created_at, updated_at) "
            "VALUES (:id, :u, 'Test', true, false, now(), now()) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"id": uid, "u": f"u{uid}"},
    )
    return uid


async def _make_wiki_page(
    session,
    *,
    slug: str | None = None,
    page_status: str = "draft",
    public_enabled: bool = False,
    robots_policy: str = "noindex",
) -> tuple[uuid.UUID, str]:
    page_id = uuid.uuid4()
    slug = slug or f"test-{uuid.uuid4().hex[:8]}"
    created_by = await _make_user(session)

    await session.execute(
        text(
            "INSERT INTO wiki_pages "
            "(id, slug, title, body_markdown, page_status, public_enabled, robots_policy, "
            " created_by_user_id, created_at, updated_at) "
            "VALUES "
            "(:id, :slug, :title, '', :ps, :pe, :rp, :cb, now(), now())"
        ),
        {
            "id": str(page_id),
            "slug": slug,
            "title": "Test Page",
            "ps": page_status,
            "pe": public_enabled,
            "rp": robots_policy,
            "cb": created_by,
        },
    )
    await session.flush()
    return page_id, slug


async def _make_chat_message(session, *, user_id: int) -> object:
    from bot.db.models import ChatMessage

    cm = ChatMessage(
        message_id=int(uuid.uuid4().int & 0x7FFF_FFFF),
        chat_id=-1001234567890,
        user_id=user_id,
        text="test",
        date=_now(),
        raw_json={"text": "test"},
        memory_policy="normal",
        is_redacted=False,
    )
    session.add(cm)
    await session.flush()
    return cm


async def _make_message_version(session, *, chat_message_id: int) -> int:
    from bot.db.models import MessageVersion

    mv = MessageVersion(
        chat_message_id=chat_message_id,
        version_seq=1,
        text="test",
        normalized_text="test",
        entities_json={},
        content_hash=f"h-{uuid.uuid4().hex[:16]}",
        is_redacted=False,
    )
    session.add(mv)
    await session.flush()
    return mv.id


async def _link_mv(session, *, page_id: uuid.UUID, mv_id: int) -> None:
    await session.execute(
        text(
            "INSERT INTO wiki_page_message_sources (wiki_page_id, message_version_id, position) "
            "VALUES (:pid, :mvid, 0)"
        ),
        {"pid": str(page_id), "mvid": mv_id},
    )


async def _pub_log_count(session, *, page_id: uuid.UUID) -> int:
    row = (
        await session.execute(
            text("SELECT count(*) FROM wiki_publication_log WHERE wiki_page_id = :pid"),
            {"pid": str(page_id)},
        )
    ).scalar()
    return int(row or 0)


async def _get_page(session, *, page_id: uuid.UUID) -> object:
    return (
        await session.execute(
            text(
                "SELECT public_enabled, robots_policy, page_status "
                "FROM wiki_pages WHERE id = :pid"
            ),
            {"pid": str(page_id)},
        )
    ).mappings().one()


# ── web client helpers (R6.e, R6.f) ──────────────────────────────────────────


def _make_web_client(session_cookie: str | None = None):
    from fastapi.testclient import TestClient

    web_app = import_module("web.app")
    client = TestClient(web_app.create_app(), raise_server_exceptions=False)
    if session_cookie:
        client.cookies.set("session", session_cookie)
    return client


def _member_cookie() -> str:
    web_auth = import_module("web.auth")
    return web_auth.create_session_cookie(role="member")


# ── R6.a — non-admin publish refusal ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_r6a_non_admin_publish_refused(db_session) -> None:
    """R6.a: non-admin calling /wiki_publish gets a refusal; public_enabled unchanged."""
    handler = import_module("bot.handlers.wiki")

    page_id, slug = await _make_wiki_page(db_session, page_status="reviewed")

    msg = _message(user_id=_NON_ADMIN_ID)
    cmd = _command(slug)

    await handler.cmd_wiki_publish(msg, db_session, cmd)

    msg.answer.assert_awaited_once()
    replied = msg.answer.call_args[0][0]
    assert "администратор" in replied.lower() or "admin" in replied.lower(), (
        f"Expected admin-refusal message, got: {replied!r}"
    )

    # DB must be untouched
    assert await _pub_log_count(db_session, page_id=page_id) == 0
    page = await _get_page(db_session, page_id=page_id)
    assert page["public_enabled"] is False


# ── R6.b — admin cannot publish non-reviewed page ─────────────────────────────


@pytest.mark.asyncio
async def test_r6b_admin_cannot_publish_non_reviewed_page(db_session) -> None:
    """R6.b: admin calling /wiki_publish on a draft page gets a refusal."""
    handler = import_module("bot.handlers.wiki")

    page_id, slug = await _make_wiki_page(db_session, page_status="draft")

    msg = _message(user_id=_ADMIN_ID)
    cmd = _command(slug)

    with patch.object(handler.FeatureFlagRepo, "get", AsyncMock(return_value=True)):
        await handler.cmd_wiki_publish(msg, db_session, cmd)

    msg.answer.assert_awaited_once()
    replied = msg.answer.call_args[0][0]
    # Response must mention review requirement
    assert "ревью" in replied.lower() or "review" in replied.lower(), (
        f"Expected review-requirement message, got: {replied!r}"
    )

    assert await _pub_log_count(db_session, page_id=page_id) == 0
    page = await _get_page(db_session, page_id=page_id)
    assert page["public_enabled"] is False


# ── R6.c — governance failure (offrecord source) blocks publish ───────────────


@pytest.mark.asyncio
async def test_r6c_offrecord_source_blocks_publish(db_session) -> None:
    """R6.c: page with offrecord source fails governance → refusal; public_enabled unchanged."""
    handler = import_module("bot.handlers.wiki")

    page_id, slug = await _make_wiki_page(db_session, page_status="reviewed")

    # Create a message version whose parent chat_message has memory_policy='offrecord'
    uid = await _make_user(db_session)
    cm = await _make_chat_message(db_session, user_id=uid)
    # Patch memory_policy to offrecord directly via SQL
    await db_session.execute(
        text("UPDATE chat_messages SET memory_policy = 'offrecord' WHERE id = :cid"),
        {"cid": cm.id},
    )
    mv_id = await _make_message_version(db_session, chat_message_id=cm.id)
    await _link_mv(db_session, page_id=page_id, mv_id=mv_id)

    msg = _message(user_id=_ADMIN_ID)
    cmd = _command(slug)

    with patch.object(handler.FeatureFlagRepo, "get", AsyncMock(return_value=True)):
        await handler.cmd_wiki_publish(msg, db_session, cmd)

    msg.answer.assert_awaited_once()
    replied = msg.answer.call_args[0][0]
    assert replied, "Handler must send a non-empty refusal message"

    # Governance failure: no publication log row and public_enabled still False
    assert await _pub_log_count(db_session, page_id=page_id) == 0
    page = await _get_page(db_session, page_id=page_id)
    assert page["public_enabled"] is False


# ── R6.d — robots_policy='index' blocked when public_enabled=false ────────────


@pytest.mark.asyncio
async def test_r6d_db_constraint_blocks_index_robots_on_private_page(db_session) -> None:
    """R6.d DB layer: INSERT with robots_policy='index' AND public_enabled=false raises IntegrityError."""
    # A page that is NOT public — direct SQL insert must fail the check constraint
    # ck_wiki_pages_robots_index_requires_public
    page_id = uuid.uuid4()
    creator_uid = await _make_user(db_session)

    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(
                "INSERT INTO wiki_pages "
                "(id, slug, title, body_markdown, page_status, public_enabled, robots_policy, "
                " created_by_user_id, created_at, updated_at) "
                "VALUES "
                "(:id, :slug, 'T', '', 'reviewed', false, 'index', :cb, now(), now())"
            ),
            {
                "id": str(page_id),
                "slug": f"r6d-constraint-{uuid.uuid4().hex[:8]}",
                "cb": creator_uid,
            },
        )
        await db_session.flush()


@pytest.mark.asyncio
async def test_r6d_handler_blocks_index_robots_on_private_page(db_session) -> None:
    """R6.d handler layer: /wiki_robots <slug> index on a non-public page is refused."""
    handler = import_module("bot.handlers.wiki")

    page_id, slug = await _make_wiki_page(
        db_session, page_status="reviewed", public_enabled=False, robots_policy="noindex"
    )

    msg = _message(user_id=_ADMIN_ID)
    cmd = _command(f"{slug} index")

    with patch.object(handler.FeatureFlagRepo, "get", AsyncMock(return_value=True)):
        await handler.cmd_wiki_robots(msg, db_session, cmd)

    msg.answer.assert_awaited_once()
    replied = msg.answer.call_args[0][0]
    assert (
        "непубли" in replied.lower()
        or "public" in replied.lower()
        or "нельзя" in replied.lower()
    ), f"Expected index-on-private refusal message, got: {replied!r}"

    # robots_policy must remain 'noindex'; no log row
    assert await _pub_log_count(db_session, page_id=page_id) == 0
    page = await _get_page(db_session, page_id=page_id)
    assert page["robots_policy"] == "noindex"


# ── R6.e — member password always yields role='member' (no escalation) ────────


def test_r6e_member_password_cannot_escalate_to_admin_via_user_id(monkeypatch) -> None:
    """R6.e: POST /login with WEB_MEMBER_PASSWORD + arbitrary user_id → role='member'.

    The login handler ignores any user_id field in the request body — role is
    derived solely from the password via derive_role().  Supplying an admin
    user_id cannot escalate a member session to admin.
    """
    # Set up both admin and member passwords via monkeypatch
    monkeypatch.setenv("WEB_ADMIN_PASSWORD", "admin-secret-pw-1234")
    monkeypatch.setenv("WEB_MEMBER_PASSWORD", "member-secret-pw-5678")
    monkeypatch.setenv("WEB_SESSION_SECRET", "test-session-secret-32-chars-long!")
    monkeypatch.setenv("BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("COMMUNITY_CHAT_ID", "-1001234567890")
    monkeypatch.setenv("ADMIN_IDS", "[149820031]")
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("GOOGLE_SHEETS_CREDS_FILE", "")
    monkeypatch.setenv("GOOGLE_SHEET_ID", "")
    monkeypatch.setenv("WEB_BASE_URL", "http://localhost:8080")
    monkeypatch.setenv("WEB_BOT_USERNAME", "vibeshkoder_dev_bot")
    monkeypatch.setenv("DB_PASSWORD", "changeme")
    monkeypatch.setenv("DEV_MODE", "true")

    # Force module reimport so config picks up the new env values
    import sys
    for mod in list(sys.modules):
        if mod == "bot" or mod.startswith("bot.") or mod == "web" or mod.startswith("web."):
            sys.modules.pop(mod, None)

    web_app = import_module("web.app")
    web_auth = import_module("web.auth")

    from fastapi.testclient import TestClient

    client = TestClient(web_app.create_app(), raise_server_exceptions=False)

    # POST /login with member password AND an admin-looking user_id in body.
    # The handler must ignore user_id entirely.
    response = client.post(
        "/login",
        data={
            "password": "member-secret-pw-5678",
            "user_id": str(_ADMIN_ID),  # attempt to claim admin identity
        },
        follow_redirects=False,
    )

    # Must redirect to /dashboard on successful login
    assert response.status_code == 302, (
        f"Expected 302 redirect, got {response.status_code}: {response.text[:200]}"
    )
    assert response.headers.get("location") == "/dashboard"

    # Decode the session cookie and verify role='member'
    session_cookie = response.cookies.get("session")
    assert session_cookie, "Expected session cookie to be set after login"

    payload = web_auth.get_user_from_cookie(session_cookie)
    assert payload is not None, "Session cookie must be valid"
    assert payload.get("role") == "member", (
        f"Expected role='member', got role={payload.get('role')!r}. "
        "Supplying admin user_id must not escalate role."
    )


# ── R6.f — unpublish + forget → public route returns 404/410 with no-store ────


def test_r6f_unpublish_and_forget_yields_gone_with_no_store_header(monkeypatch) -> None:
    """R6.f: after unpublish+forget cycle, GET /wiki/public/{slug} returns 404 or 410
    and response headers include Cache-Control: no-store."""
    wiki_routes = import_module("web.routes.wiki")

    # Simulate a page that was unpublished (public_enabled=False)
    page = SimpleNamespace(
        id=uuid.uuid4(),
        slug="forgotten-page",
        title="Forgotten Page",
        body_markdown="some content",
        page_status="stale",
        public_enabled=False,  # unpublished
    )

    async def _async_page(p):
        return p

    # The public route queries _get_public_page_by_slug which checks public_enabled=True.
    # With public_enabled=False the route returns 404 with Cache-Control: no-store.
    monkeypatch.setattr(
        wiki_routes,
        "_wiki_enabled",
        lambda session: _async_true_coro(),
    )
    monkeypatch.setattr(
        wiki_routes,
        "_get_public_page_by_slug",
        lambda session, slug: _async_page(page),
    )

    from fastapi.testclient import TestClient

    web_app = import_module("web.app")
    client = TestClient(web_app.create_app(), raise_server_exceptions=False)

    response = client.get("/wiki/public/forgotten-page", follow_redirects=False)

    # Must be 404 (public_enabled=False) or 410 (archived/gone)
    assert response.status_code in (404, 410), (
        f"Expected 404 or 410 after unpublish, got {response.status_code}"
    )

    cache_control = response.headers.get("Cache-Control", "")
    assert "no-store" in cache_control, (
        f"Expected Cache-Control: no-store in response headers, got: {cache_control!r}"
    )


def test_r6f_archived_after_forget_yields_410_with_no_store_header(monkeypatch) -> None:
    """R6.f supplemental: page archived by forget cascade → 410 with Cache-Control: no-store."""
    wiki_routes = import_module("web.routes.wiki")

    page = SimpleNamespace(
        id=uuid.uuid4(),
        slug="archived-page",
        title="Archived Page",
        body_markdown="forgotten content",
        page_status="reviewed",
        public_enabled=True,  # was public, but sources are all forgotten
    )

    async def _async_page(p):
        return p

    async def _fake_render_archived(session, *, page_id, role, body_markdown):
        from bot.services.wiki_renderer import WikiRenderResult
        return WikiRenderResult(html_body="", page_archived=True)

    monkeypatch.setattr(
        wiki_routes,
        "_wiki_enabled",
        lambda session: _async_true_coro(),
    )
    monkeypatch.setattr(
        wiki_routes,
        "_get_public_page_by_slug",
        lambda session, slug: _async_page(page),
    )
    monkeypatch.setattr(wiki_routes, "render_wiki_page", _fake_render_archived)

    from fastapi.testclient import TestClient

    web_app = import_module("web.app")
    client = TestClient(web_app.create_app(), raise_server_exceptions=False)

    response = client.get("/wiki/public/archived-page", follow_redirects=False)

    assert response.status_code == 410, (
        f"Expected 410 Gone after forget cascade, got {response.status_code}"
    )

    cache_control = response.headers.get("Cache-Control", "")
    assert "no-store" in cache_control, (
        f"Expected Cache-Control: no-store in 410 response, got: {cache_control!r}"
    )


# ── async helpers for sync monkeypatch lambdas ────────────────────────────────


async def _async_true_coro() -> bool:
    return True
