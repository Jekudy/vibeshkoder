"""Tests for T7-05 — digest_publisher, digest_renderer, digest_redactor.

Covers Phase 7 §5.F (publisher single-transaction flow), §5.G (HTML
rendering), §5.H (forget cascade redactor + bullet masking).
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.usefixtures("app_env")

_chat_counter = itertools.count(start=8500)


def _next_chat_id() -> int:
    return -1_000_000_000_000 - next(_chat_counter)


def _next_uid() -> int:
    return -1 * (1000 + next(_chat_counter))


# ── renderer unit tests ──────────────────────────────────────────────────────


def test_renderer_strips_citation_tokens_and_adds_safe_navigation_link():
    from bot.services.digest_renderer import render_digest_html

    body = "- Topic body [[mv:123]]\n\n— Topic survived [[mv:123]]"
    ws = datetime(2026, 5, 15, 9, 0, 0, tzinfo=timezone.utc)
    out = render_digest_html(
        body,
        window_start_utc=ws,
        source_links_by_citation={"[[mv:123]]": "https://t.me/c/123/123"},
    )
    assert "[[mv:" not in out
    assert "Topic" in out
    assert "https://t.me/c/123/123" in out


def test_renderer_escapes_html_entities():
    from bot.services.digest_renderer import render_digest_html

    body = "- <script>alert('x')</script> attack [[mv:1]]\n\n— Safe close [[mv:1]]"
    ws = datetime(2026, 5, 15, 9, 0, 0, tzinfo=timezone.utc)
    out = render_digest_html(
        body,
        window_start_utc=ws,
        source_links_by_citation={"[[mv:1]]": "https://t.me/c/123/1"},
    )
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_renderer_rejects_body_above_telegram_limit():
    from bot.services.digest_renderer import render_digest_html

    paragraphs = "Lorem ipsum dolor sit amet. " * 200  # > 3800 chars
    body = f"- {paragraphs} [[mv:1]]\n\n— Safe close [[mv:1]]"
    ws = datetime(2026, 5, 15, 9, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="Telegram message limit"):
        render_digest_html(
            body,
            window_start_utc=ws,
            source_links_by_citation={"[[mv:1]]": "https://t.me/c/123/1"},
        )


def test_renderer_uses_exact_daily_navigation_layout():
    from bot.services.digest_renderer import render_digest_html

    # window_start = 2026-05-15 05:00 MSK = 2026-05-15 02:00 UTC
    ws = datetime(2026, 5, 15, 2, 0, 0, tzinfo=timezone.utc)
    body = "- Один пункт. [[mv:1]]\n\n— И точка. [[mv:1]]"
    out = render_digest_html(
        body,
        window_start_utc=ws,
        source_links_by_citation={"[[mv:1]]": "https://t.me/c/123/1"},
    )
    assert (
        out
        == 'Что было в чате — 15 мая\n\n- Один пункт. [<a href="https://t.me/c/123/1">↗ источник</a>]\n\n'
        '<i>— И точка. [<a href="https://t.me/c/123/1">↗ источник</a>]</i>\n\n#дайджест'
    )


def test_renderer_escapes_markdown_as_plain_navigation_text():
    from bot.services.digest_renderer import render_digest_html

    body = "- **bold** and *italic* text [[mv:1]]\n\n— Safe close [[mv:1]]"
    ws = datetime(2026, 5, 15, 9, 0, 0, tzinfo=timezone.utc)
    out = render_digest_html(
        body,
        window_start_utc=ws,
        source_links_by_citation={"[[mv:1]]": "https://t.me/c/123/1"},
    )
    assert "**bold**" in out
    assert "<b>" not in out


def test_renderer_keeps_one_line_bullets():
    from bot.services.digest_renderer import render_digest_html

    body = "- First [[mv:1]]\n- Second [[mv:2]]\n\n— Safe close [[mv:1]]"
    ws = datetime(2026, 5, 15, 9, 0, 0, tzinfo=timezone.utc)
    out = render_digest_html(
        body,
        window_start_utc=ws,
        source_links_by_citation={
            "[[mv:1]]": "https://t.me/c/123/1",
            "[[mv:2]]": "https://t.me/c/123/2",
        },
    )
    assert "- First" in out
    assert "- Second" in out


# ── redactor unit tests ──────────────────────────────────────────────────────


def test_mask_bullets_replaces_indices():
    from bot.services.digest_redactor import _mask_bullets_in_body

    body = (
        "TL;DR header.\n"
        "\n"
        "- First bullet [[mv:1]]\n"
        "  detail line\n"
        "- Second bullet [[mv:2]]\n"
        "- Third bullet [[cs:abc]]\n"
    )
    masked = _mask_bullets_in_body(body, bullet_indices={0, 2})
    assert "First bullet" not in masked
    assert "Second bullet" in masked
    assert "Third bullet" not in masked
    assert "[REDACTED — забыто]" in masked
    # TL;DR preserved.
    assert "TL;DR header." in masked


def test_mask_bullets_empty_indices_returns_body_unchanged():
    from bot.services.digest_redactor import _mask_bullets_in_body

    body = "TL;DR.\n\n- One [[mv:1]]"
    assert _mask_bullets_in_body(body, bullet_indices=set()) == body


# ── publisher tests (DB-light, mocked Bot) ───────────────────────────────────


async def test_publisher_skipped_no_destination(db_session):
    """destination_chat_id=None → status='skipped_no_destination', no send."""
    from bot.db.models import Digest
    from bot.services.digest_publisher import publish_digest
    from bot.services.digests import DigestConfig

    digest = Digest(
        type="daily",
        window_start=datetime.now(timezone.utc) - timedelta(days=1),
        window_end=datetime.now(timezone.utc),
        body_markdown="TL;DR.\n\n- One [[mv:1]]",
        citations=[{"kind": "message_version", "id": 1, "position": 0}],
        status="draft",
    )
    db_session.add(digest)
    await db_session.flush()

    cfg = DigestConfig(destination_chat_id=None)
    bot_mock = MagicMock()
    bot_mock.send_message = AsyncMock()

    result = await publish_digest(db_session, bot=bot_mock, digest=digest, digest_config=cfg)
    assert result.status == "skipped_no_destination"
    bot_mock.send_message.assert_not_called()


async def test_publisher_rejects_non_draft_status(db_session):
    """Calling publish on a posted row → DigestPublisherInvalidState."""
    from bot.db.models import Digest
    from bot.services.digest_publisher import (
        DigestPublisherInvalidState,
        publish_digest,
    )
    from bot.services.digests import DigestConfig

    digest = Digest(
        type="daily",
        window_start=datetime.now(timezone.utc) - timedelta(days=1),
        window_end=datetime.now(timezone.utc),
        body_markdown="TL;DR.\n\n- One [[mv:1]]",
        citations=[{"kind": "message_version", "id": 1, "position": 0}],
        status="posted",
        posted_chat_id=-42,
        posted_message_id=999,
        posted_at=datetime.now(timezone.utc),
    )
    db_session.add(digest)
    await db_session.flush()

    cfg = DigestConfig(destination_chat_id=-42)
    bot_mock = MagicMock()
    with pytest.raises(DigestPublisherInvalidState):
        await publish_digest(db_session, bot=bot_mock, digest=digest, digest_config=cfg)


async def test_publisher_nowait_contention_keeps_outer_transaction_usable(
    postgres_engine,
    monkeypatch,
):
    """A real 55P03 must roll back only its savepoint, not the outer transaction.

    The lock holder is released only after the third failed NOWAIT attempt.  The
    publisher must then be able to flush its ``publish_lock_timeout`` audit and
    execute another statement in the same outer transaction.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from bot.db.models import Digest
    from bot.services import digest_publisher as publisher_module
    from bot.services.digest_publisher import publish_digest
    from bot.services.digests import DigestConfig

    session_factory = async_sessionmaker(
        bind=postgres_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    now = datetime.now(timezone.utc)

    async with session_factory() as setup_session:
        digest = Digest(
            type="daily",
            window_start=now - timedelta(days=1),
            window_end=now,
            body_markdown="TL;DR.\n\n- Lock contention regression.",
            citations=[],
            status="draft",
        )
        setup_session.add(digest)
        await setup_session.commit()
        digest_id = digest.id

    try:
        async with session_factory() as lock_session, session_factory() as publish_session:
            await lock_session.execute(
                text("SELECT id FROM digests WHERE id=:id FOR UPDATE"),
                {"id": digest_id},
            )
            publish_digest_row = await publish_session.get(Digest, digest_id)
            assert publish_digest_row is not None

            sleep_calls = 0

            async def release_lock_after_last_retry(_delay: float) -> None:
                nonlocal sleep_calls
                sleep_calls += 1
                if sleep_calls == 3:
                    await lock_session.rollback()

            monkeypatch.setattr(
                publisher_module.asyncio,
                "sleep",
                release_lock_after_last_retry,
            )
            notify_mock = AsyncMock()
            monkeypatch.setattr(
                publisher_module,
                "notify_admins_digest_failure",
                notify_mock,
            )
            bot_mock = MagicMock()
            bot_mock.send_message = AsyncMock()

            result = await publish_digest(
                publish_session,
                bot=bot_mock,
                digest=publish_digest_row,
                digest_config=DigestConfig(destination_chat_id=-1001234567890),
            )

            assert sleep_calls == 3
            assert result.status == "failed"
            assert result.error_text == "publish_lock_timeout"
            assert (await publish_session.execute(text("SELECT 1"))).scalar_one() == 1
            notify_mock.assert_awaited_once()
            bot_mock.send_message.assert_not_awaited()
            await publish_session.rollback()
    finally:
        async with session_factory() as cleanup_session:
            await cleanup_session.execute(
                text("DELETE FROM digest_runs WHERE digest_id=:id"),
                {"id": digest_id},
            )
            await cleanup_session.execute(
                text("DELETE FROM digests WHERE id=:id"),
                {"id": digest_id},
            )
            await cleanup_session.commit()


@pytest.mark.parametrize("winner_status", ["posting", "posted"])
async def test_publisher_lock_exhaustion_does_not_overwrite_concurrent_winner(
    postgres_engine,
    monkeypatch,
    winner_status,
):
    """A loser must not replace a concurrently committed winner with ``failed``."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from bot.db.models import Digest
    from bot.services import digest_publisher as publisher_module
    from bot.services.digest_publisher import (
        DigestPublisherInvalidState,
        publish_digest,
    )
    from bot.services.digests import DigestConfig

    session_factory = async_sessionmaker(
        bind=postgres_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    now = datetime.now(timezone.utc)
    async with session_factory() as setup_session:
        digest = Digest(
            type="daily",
            window_start=now - timedelta(days=1),
            window_end=now,
            body_markdown="TL;DR.\n\n- Concurrent winner regression.",
            citations=[],
            status="draft",
        )
        setup_session.add(digest)
        await setup_session.commit()
        digest_id = digest.id

    try:
        async with session_factory() as lock_session, session_factory() as publish_session:
            if winner_status == "posting":
                await lock_session.execute(
                    text(
                        "UPDATE digests SET status='posting', posting_started_at=now() WHERE id=:id"
                    ),
                    {"id": digest_id},
                )
            else:
                await lock_session.execute(
                    text(
                        "UPDATE digests SET status='posted', posted_chat_id=-10042, "
                        "posted_message_id=4242, posted_at=now() WHERE id=:id"
                    ),
                    {"id": digest_id},
                )

            stale_digest = await publish_session.get(Digest, digest_id)
            assert stale_digest is not None
            assert stale_digest.status == "draft"
            sleep_calls = 0

            async def commit_winner_after_last_retry(_delay: float) -> None:
                nonlocal sleep_calls
                sleep_calls += 1
                if sleep_calls == 3:
                    await lock_session.commit()

            monkeypatch.setattr(
                publisher_module.asyncio,
                "sleep",
                commit_winner_after_last_retry,
            )
            notify_mock = AsyncMock()
            monkeypatch.setattr(
                publisher_module,
                "notify_admins_digest_failure",
                notify_mock,
            )
            bot_mock = MagicMock()
            bot_mock.send_message = AsyncMock()

            with pytest.raises(DigestPublisherInvalidState) as exc_info:
                await publish_digest(
                    publish_session,
                    bot=bot_mock,
                    digest=stale_digest,
                    digest_config=DigestConfig(destination_chat_id=-1001234567890),
                )

            assert sleep_calls == 3
            assert exc_info.value.digest_id == digest_id
            assert exc_info.value.current_status == winner_status
            notify_mock.assert_not_awaited()
            bot_mock.send_message.assert_not_awaited()
            await publish_session.rollback()

        async with session_factory() as verify_session:
            winner = (
                await verify_session.execute(
                    text("SELECT status, error_text FROM digests WHERE id=:id"),
                    {"id": digest_id},
                )
            ).one()
            assert tuple(winner) == (winner_status, None)
            failed_runs = (
                await verify_session.execute(
                    text(
                        "SELECT count(*) FROM digest_runs WHERE digest_id=:id AND status='failed'"
                    ),
                    {"id": digest_id},
                )
            ).scalar_one()
            assert failed_runs == 0
    finally:
        async with session_factory() as cleanup_session:
            await cleanup_session.execute(
                text("DELETE FROM digest_runs WHERE digest_id=:id"),
                {"id": digest_id},
            )
            await cleanup_session.execute(
                text("DELETE FROM digests WHERE id=:id"),
                {"id": digest_id},
            )
            await cleanup_session.commit()


async def test_publisher_nowait_retry_does_not_mask_other_db_failures(
    db_session,
    monkeypatch,
):
    """Only SQLSTATE 55P03 is retryable; unrelated DB failures fail fast."""
    from sqlalchemy.exc import DBAPIError

    from bot.db.models import Digest
    from bot.services import digest_publisher as publisher_module
    from bot.services.digest_publisher import publish_digest
    from bot.services.digests import DigestConfig

    digest = Digest(
        type="daily",
        window_start=datetime.now(timezone.utc) - timedelta(days=1),
        window_end=datetime.now(timezone.utc),
        body_markdown="TL;DR.\n\n- Unexpected database error.",
        citations=[],
        status="draft",
    )
    db_session.add(digest)
    await db_session.flush()

    class ConnectionFailure(Exception):
        sqlstate = "08006"

    expected_error = DBAPIError(
        "SELECT ... FOR UPDATE NOWAIT",
        {"id": digest.id},
        ConnectionFailure("connection failure"),
        False,
    )
    real_execute = db_session.execute

    async def execute_with_unexpected_lock_error(statement, parameters=None, **kwargs):
        if "FOR UPDATE NOWAIT" in str(statement):
            raise expected_error
        return await real_execute(statement, parameters, **kwargs)

    monkeypatch.setattr(db_session, "execute", execute_with_unexpected_lock_error)
    sleep_mock = AsyncMock()
    monkeypatch.setattr(publisher_module.asyncio, "sleep", sleep_mock)
    bot_mock = MagicMock()
    bot_mock.send_message = AsyncMock()

    with pytest.raises(DBAPIError) as exc_info:
        await publish_digest(
            db_session,
            bot=bot_mock,
            digest=digest,
            digest_config=DigestConfig(destination_chat_id=-1001234567890),
        )

    assert exc_info.value is expected_error
    sleep_mock.assert_not_awaited()
    bot_mock.send_message.assert_not_awaited()
    assert (await real_execute(text("SELECT 1"))).scalar_one() == 1


async def test_publisher_happy_path_with_clean_citations(db_session, monkeypatch):
    """Insert real message+version + card+source, citations point to them,
    publisher transitions draft→posting→posted."""
    from bot.db.models import (
        ChatMessage,
        Digest,
        MessageVersion,
    )
    from bot.db.repos.user import UserRepo
    from bot.services.digest_publisher import publish_digest
    from bot.services.digests import DigestConfig

    chat_id = -1001234567890
    uid = -1 * (5000 + next(_chat_counter))
    await UserRepo.upsert(
        db_session, telegram_id=uid, username=f"u{uid}", first_name="T", last_name=None
    )
    now = datetime.now(timezone.utc)
    cm = ChatMessage(
        message_id=900_000 + next(_chat_counter),
        chat_id=chat_id,
        user_id=uid,
        text="hello",
        date=now - timedelta(hours=6),
        raw_json={"text": "hello"},
        memory_policy="normal",
        is_redacted=False,
    )
    db_session.add(cm)
    await db_session.flush()
    mv = MessageVersion(
        chat_message_id=cm.id,
        version_seq=1,
        text="hello",
        normalized_text="hello",
        entities_json={"entities": []},
        content_hash=f"h-{cm.id}",
        is_redacted=False,
    )
    db_session.add(mv)
    await db_session.flush()
    cm.current_version_id = mv.id
    await db_session.flush()

    digest = Digest(
        type="daily",
        window_start=now - timedelta(days=1),
        window_end=now,
        body_markdown=(
            f"- Обсуждали приветствие. [[mv:{mv.id}]]\n\n"
            f"— Приветствие состоялось. [[mv:{mv.id}]]"
        ),
        citations=[
            {"kind": "message_version", "id": mv.id, "position": 0},
            {"kind": "message_version", "id": mv.id, "position": 1},
        ],
        status="draft",
    )
    db_session.add(digest)
    await db_session.flush()

    cfg = DigestConfig(source_chat_id=chat_id, destination_chat_id=chat_id)
    events: list[str] = []
    real_commit = db_session.commit

    async def tracked_commit():
        events.append("commit")
        await real_commit()

    monkeypatch.setattr(db_session, "commit", tracked_commit)
    bot_mock = MagicMock()
    bot_mock.send_message = AsyncMock()
    sent_msg = MagicMock()
    sent_msg.message_id = 777

    async def send_message(**kwargs):
        events.append("send")
        return sent_msg

    bot_mock.send_message.side_effect = send_message

    result = await publish_digest(db_session, bot=bot_mock, digest=digest, digest_config=cfg)
    assert result.status == "posted", f"got status={result.status} err={result.error_text}"
    assert result.posted_message_id == 777
    assert result.posted_chat_id == cfg.destination_chat_id
    bot_mock.send_message.assert_awaited_once()
    assert events == ["commit", "send", "commit"]


async def test_redactor_masks_affected_bullet(db_session):
    """Redactor with affected_mvids={mv.id} → bullet 0 masked, surviving cits filtered."""
    from bot.db.models import Digest
    from bot.services.digest_redactor import redact_digest_for_forget

    digest = Digest(
        type="daily",
        window_start=datetime.now(timezone.utc) - timedelta(days=1),
        window_end=datetime.now(timezone.utc),
        body_markdown="TL;DR.\n\n- First [[mv:1]]\n- Second [[mv:2]]",
        citations=[
            {"kind": "message_version", "id": 1, "position": 0},
            {"kind": "message_version", "id": 2, "position": 1},
        ],
        status="draft",
    )
    db_session.add(digest)
    await db_session.flush()
    did = digest.id

    await redact_digest_for_forget(
        db_session,
        digest_id=did,
        affected_mvids={1},
        affected_card_source_ids=set(),
        bot=None,  # no Telegram side-effect in test
    )

    # Re-fetch
    row = (
        (
            await db_session.execute(
                text("SELECT body_markdown, citations, status FROM digests WHERE id = :id"),
                {"id": did},
            )
        )
        .mappings()
        .one()
    )
    assert row["status"] == "redacted"
    assert "[REDACTED — забыто]" in row["body_markdown"]
    assert "First" not in row["body_markdown"]
    assert "Second" in row["body_markdown"]
    # Surviving citation is mv:2.
    assert len(row["citations"]) == 1
    assert int(row["citations"][0]["id"]) == 2


async def test_idempotency_path_returns_session_attached_digest(db_session):
    """F4: when run_digest returns a digest via the idempotency path (row already exists),
    the returned object must be session-attached so mutations to its state persist.

    Previously _row_to_digest() returned a detached Digest() constructed with the
    primary key from a raw SQL SELECT — SQLAlchemy would NOT track mutations on that
    object. After the fix (session.get(Digest, id)), the same ORM identity is returned
    and mutations propagate to the DB on flush.
    """
    from decimal import Decimal
    from bot.db.models import Digest
    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from bot.services.digests import DigestConfig, run_digest
    from bot.services.llm_gateway import LLMGatewayConfig

    chat_id = _next_chat_id()
    now = datetime.now(timezone.utc)
    ws = now - timedelta(days=1)
    we = now

    # Pre-insert a draft digest for the same window (simulates first run)
    pre_existing = Digest(
        type="daily",
        window_start=ws,
        window_end=we,
        body_markdown="TL;DR.\n\n- Pre-existing",
        citations=[],
        status="draft",
    )
    db_session.add(pre_existing)
    await db_session.flush()
    pre_id = pre_existing.id

    # Second call with the same window — must hit idempotency path
    gateway_config = LLMGatewayConfig(
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        daily_ceiling_usd=Decimal("10.00"),
        monthly_ceiling_usd=Decimal("100.00"),
        prompt_template_version="digest-v0.1.0",
    )
    digest_config = DigestConfig(source_chat_id=chat_id)
    idempotency_digest = await run_digest(
        db_session,
        type="daily",
        window_start=ws,
        window_end=we,
        ledger_repo=LedgerRepo(),
        provider=None,  # not reached — idempotency returns early
        config=gateway_config,
        digest_config=digest_config,
    )
    assert idempotency_digest.id == pre_id, "idempotency must return same row"

    # Critical: verify the returned object is session-attached.
    # If it were detached, `session.get(Digest, pre_id)` would return a DIFFERENT
    # Python object (the identity-map entry) and mutations on `idempotency_digest`
    # would not be visible. With session.get(), both must be the same object.
    attached_from_session = await db_session.get(Digest, pre_id)
    assert attached_from_session is idempotency_digest, (
        "run_digest idempotency path must return the session-tracked ORM identity, "
        "not a detached copy. If this fails, state mutations (draft→posting→posted) "
        "silently do not persist."
    )

    # Also verify that mutating the returned object does persist to the DB.
    idempotency_digest.status = "posting"
    await db_session.flush()
    row = (
        (
            await db_session.execute(
                __import__("sqlalchemy").text("SELECT status FROM digests WHERE id = :id"),
                {"id": pre_id},
            )
        )
        .mappings()
        .one()
    )
    assert row["status"] == "posting", (
        f"Mutation on idempotency-returned digest must persist, got {row['status']!r}"
    )


async def test_cascade_worker_with_bot_calls_edit_message_text(db_session):
    """F2: when bot is threaded through cascade_worker_tick → run_cascade_worker_once
    → _process_one_event → _cascade_digests → redact_digest_for_forget, the
    bot.edit_message_text call must happen for a 'posted' digest.

    Without the fix, bot is never passed through the call chain and the
    Telegram side-effect (_cascade_digests calls redact_digest_for_forget(bot=None))
    is silently skipped in production.
    """
    from bot.db.models import (
        ChatMessage,
        Digest,
        ForgetEvent,
        MessageVersion,
    )
    from bot.db.repos.user import UserRepo
    from bot.services.forget_cascade import run_cascade_worker_once

    chat_id = _next_chat_id()
    uid = -1 * (7000 + next(_chat_counter))
    await UserRepo.upsert(
        db_session, telegram_id=uid, username=f"u{uid}", first_name="T", last_name=None
    )
    now = datetime.now(timezone.utc)
    cm = ChatMessage(
        message_id=960_000 + next(_chat_counter),
        chat_id=chat_id,
        user_id=uid,
        text="secret content",
        date=now - timedelta(hours=6),
        raw_json={"text": "secret content"},
        memory_policy="normal",
        is_redacted=False,
    )
    db_session.add(cm)
    await db_session.flush()
    mv = MessageVersion(
        chat_message_id=cm.id,
        version_seq=1,
        text="secret content",
        normalized_text="secret content",
        entities_json={"entities": []},
        content_hash=f"h2-{cm.id}",
        is_redacted=False,
    )
    db_session.add(mv)
    await db_session.flush()
    cm.current_version_id = mv.id

    # Digest is 'posted' — has a posted_message_id so redactor will try edit_message_text
    digest = Digest(
        type="daily",
        window_start=now - timedelta(days=1),
        window_end=now,
        body_markdown=f"TL;DR.\n\n- One bullet [[mv:{mv.id}]]",
        citations=[{"kind": "message_version", "id": mv.id, "position": 0}],
        status="posted",
        posted_chat_id=-1009999999999,
        posted_message_id=555,
        posted_at=now - timedelta(minutes=5),
    )
    db_session.add(digest)
    await db_session.flush()
    did = digest.id

    fe = ForgetEvent(
        target_type="message",
        target_id=str(cm.id),
        actor_user_id=None,
        authorized_by="self",
        tombstone_key=f"message:{chat_id}:{cm.id}:b2",
        policy="forgotten",
        status="pending",
    )
    db_session.add(fe)
    await db_session.flush()

    # Set up bot mock — edit_message_text must be called
    bot_mock = MagicMock()
    bot_mock.edit_message_text = AsyncMock(return_value=MagicMock())

    # Pass bot through the cascade chain
    await run_cascade_worker_once(db_session, bot=bot_mock, batch_size=10)

    # The bot.edit_message_text call must have happened — proves bot threading works
    assert bot_mock.edit_message_text.called, (
        "bot.edit_message_text must be called when bot is threaded through "
        "run_cascade_worker_once. Without F2 fix, bot is never forwarded and "
        "the Telegram redaction side-effect is silently skipped."
    )

    # Verify the digest was also properly redacted in the DB
    row = (
        (
            await db_session.execute(
                __import__("sqlalchemy").text("SELECT status FROM digests WHERE id = :id"),
                {"id": did},
            )
        )
        .mappings()
        .one()
    )
    assert row["status"] in ("redacted", "redacted_edit_failed"), (
        f"Digest must be redacted after cascade, got status={row['status']!r}"
    )


async def test_cascade_digests_layer_redacts_via_cascade_worker(db_session):
    """Insert message + digest citing it, fire forget_event on the message,
    run cascade worker → digest redacted, status='redacted'."""
    from bot.db.models import (
        ChatMessage,
        Digest,
        ForgetEvent,
        MessageVersion,
    )
    from bot.db.repos.user import UserRepo
    from bot.services.forget_cascade import run_cascade_worker_once

    chat_id = _next_chat_id()
    uid = -1 * (6000 + next(_chat_counter))
    await UserRepo.upsert(
        db_session, telegram_id=uid, username=f"u{uid}", first_name="T", last_name=None
    )
    now = datetime.now(timezone.utc)
    cm = ChatMessage(
        message_id=950_000 + next(_chat_counter),
        chat_id=chat_id,
        user_id=uid,
        text="secret content",
        date=now - timedelta(hours=6),
        raw_json={"text": "secret content"},
        memory_policy="normal",
        is_redacted=False,
    )
    db_session.add(cm)
    await db_session.flush()
    mv = MessageVersion(
        chat_message_id=cm.id,
        version_seq=1,
        text="secret content",
        normalized_text="secret content",
        entities_json={"entities": []},
        content_hash=f"h-{cm.id}",
        is_redacted=False,
    )
    db_session.add(mv)
    await db_session.flush()
    cm.current_version_id = mv.id

    digest = Digest(
        type="daily",
        window_start=now - timedelta(days=1),
        window_end=now,
        body_markdown="TL;DR.\n\n- One bullet [[mv:" + str(mv.id) + "]]",
        citations=[{"kind": "message_version", "id": mv.id, "position": 0}],
        status="draft",
    )
    db_session.add(digest)
    await db_session.flush()
    did = digest.id

    # Insert forget_event targeting this chat_message.
    fe = ForgetEvent(
        target_type="message",
        target_id=str(cm.id),
        actor_user_id=None,
        authorized_by="self",
        tombstone_key=f"message:{chat_id}:{cm.id}",
        policy="forgotten",
        status="pending",
    )
    db_session.add(fe)
    await db_session.flush()

    # Run cascade worker. This DOES touch many tables; we test only the
    # contract: after the worker tick, the digest is status='redacted' and
    # the body contains the REDACTED marker.
    await run_cascade_worker_once(db_session, batch_size=10)

    row = (
        (
            await db_session.execute(
                text("SELECT status, body_markdown FROM digests WHERE id = :id"),
                {"id": did},
            )
        )
        .mappings()
        .one()
    )
    assert row["status"] in ("redacted", "redacted_edit_failed"), f"got status={row['status']}"
    assert "[REDACTED — забыто]" in row["body_markdown"]


@pytest.mark.parametrize(
    "terminal_status",
    ["failed", "cost_exceeded", "skipped"],
)
async def test_redactor_skips_terminal_no_body_states(db_session, terminal_status):
    """Terminal states without body content (failed/cost_exceeded/skipped)
    remain skipped by the redactor — these rows either have no body or
    are explicitly outside the §5.D scan filter."""
    from bot.db.models import Digest
    from bot.services.digest_redactor import redact_digest_for_forget

    digest = Digest(
        type="daily",
        window_start=datetime.now(timezone.utc) - timedelta(days=1),
        window_end=datetime.now(timezone.utc),
        body_markdown=None,
        citations=[],
        status=terminal_status,
        error_text="test",
    )
    db_session.add(digest)
    await db_session.flush()
    did = digest.id

    await redact_digest_for_forget(
        db_session,
        digest_id=did,
        affected_mvids={1},
        affected_card_source_ids=set(),
        bot=None,
    )

    row = (
        (await db_session.execute(text("SELECT status FROM digests WHERE id = :id"), {"id": did}))
        .mappings()
        .one()
    )
    assert row["status"] == terminal_status


async def test_publisher_invalid_state_has_structured_fields(db_session):
    """§5.L : DigestPublisherInvalidState exposes structured fields
    (digest_id, current_status, reason)."""
    from bot.db.models import Digest
    from bot.services.digest_publisher import (
        DigestPublisherInvalidState,
        publish_digest,
    )
    from bot.services.digests import DigestConfig

    digest = Digest(
        type="daily",
        window_start=datetime.now(timezone.utc) - timedelta(days=1),
        window_end=datetime.now(timezone.utc),
        body_markdown="TL;DR.\n\n- One [[mv:1]]",
        citations=[{"kind": "message_version", "id": 1, "position": 0}],
        status="posted",
        posted_chat_id=-42,
        posted_message_id=999,
        posted_at=datetime.now(timezone.utc),
    )
    db_session.add(digest)
    await db_session.flush()

    cfg = DigestConfig(destination_chat_id=-42)
    bot_mock = MagicMock()
    with pytest.raises(DigestPublisherInvalidState) as exc_info:
        await publish_digest(db_session, bot=bot_mock, digest=digest, digest_config=cfg)
    assert exc_info.value.digest_id == digest.id
    assert exc_info.value.current_status == "posted"
    assert "draft" in exc_info.value.reason
    assert "draft" in exc_info.value.reason


async def test_publisher_classifier_row_deleted_distinguishes_from_wrong_state(
    db_session,
):
    """§5.L canonical rowcount=0 classifier — deleted-row branch.

    Simulate the race window: between the FOR UPDATE NOWAIT lock attempt
    (which silently succeeds against a missing row) and the guarded UPDATE
    to 'posting' (which returns rowcount=0), the row is gone. Classifier
    must raise DigestPublisherInvalidState with ``current_status=None`` and
    ``reason='row_deleted_during_transition'``.
    """
    from bot.db.models import Digest
    from bot.services.digest_publisher import (
        DigestPublisherInvalidState,
        publish_digest,
    )
    from bot.services.digests import DigestConfig

    # Build a normal draft row, hold a detached reference, then delete it
    # so the publisher's guarded UPDATE finds no row.
    digest = Digest(
        type="daily",
        window_start=datetime.now(timezone.utc) - timedelta(days=1),
        window_end=datetime.now(timezone.utc),
        body_markdown="TL;DR.\n\n- One [[mv:1]]",
        citations=[],
        status="draft",
    )
    db_session.add(digest)
    await db_session.flush()
    did = digest.id

    # Race: row DELETEd before publish runs (operator manual cleanup or
    # --regenerate racing).
    await db_session.execute(text("DELETE FROM digests WHERE id=:id"), {"id": did})
    await db_session.flush()

    # The Python `digest` object still has status='draft' in memory so the
    # trigger guard passes; the FOR UPDATE NOWAIT against a missing row
    # returns zero rows but does not error; the guarded UPDATE to 'posting'
    # then returns rowcount=0 — classifier path.
    cfg = DigestConfig(destination_chat_id=-1001234567890)
    bot_mock = MagicMock()
    bot_mock.send_message = AsyncMock()
    with pytest.raises(DigestPublisherInvalidState) as exc_info:
        await publish_digest(db_session, bot=bot_mock, digest=digest, digest_config=cfg)
    assert exc_info.value.digest_id == did
    assert exc_info.value.current_status is None
    assert "row_deleted_during_transition" in exc_info.value.reason


async def test_publisher_classifier_wrong_state_after_guard_miss(db_session):
    """§5.L canonical rowcount=0 classifier — wrong-state branch.

    Simulate cascade-won race: row started as 'draft' (passes trigger
    guard) but is moved to 'redacted' before the guarded UPDATE to
    'posting' runs. Classifier raises with ``current_status='redacted'``.
    """
    from bot.db.models import Digest
    from bot.services.digest_publisher import (
        DigestPublisherInvalidState,
        publish_digest,
    )
    from bot.services.digests import DigestConfig

    digest = Digest(
        type="daily",
        window_start=datetime.now(timezone.utc) - timedelta(days=1),
        window_end=datetime.now(timezone.utc),
        body_markdown="TL;DR.\n\n- One [[mv:1]]",
        citations=[],
        status="draft",
    )
    db_session.add(digest)
    await db_session.flush()
    did = digest.id

    # Race: row moved to 'redacted' by cascade before publisher's guarded
    # UPDATE — but the Python `digest` object still says 'draft' so the
    # trigger guard passes.
    await db_session.execute(
        text("UPDATE digests SET status='redacted', updated_at=now() WHERE id=:id"),
        {"id": did},
    )
    await db_session.flush()

    cfg = DigestConfig(destination_chat_id=-1001234567890)
    bot_mock = MagicMock()
    bot_mock.send_message = AsyncMock()
    with pytest.raises(DigestPublisherInvalidState) as exc_info:
        await publish_digest(db_session, bot=bot_mock, digest=digest, digest_config=cfg)
    assert exc_info.value.digest_id == did
    assert exc_info.value.current_status == "redacted"
    assert "expected status 'draft'" in exc_info.value.reason
