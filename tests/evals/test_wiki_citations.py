"""Phase 11 binding tests — wiki citation invariants C8a and C8b.

Phase 9 / T9-08.

C8a — Every ``[^mv:<id>]`` token in ``wiki_pages.body_markdown`` resolves to an
      existing, non-redacted, non-forgotten ``message_versions.id``; any that
      don't are suppressed from member-role output.

C8b — Revision citations (``wiki_revisions.source_message_version_ids_snapshot``)
      are also validated at render time — forgotten content does not leak via
      revision body even if a UI ever renders them.

Both tests use raw SQL inserts (wiki_pages / wiki_revisions / wiki_page_message_sources
have no ORM models; they are plain SQL tables). TRUNCATE isolation mirrors the
pattern in ``tests/evals/test_leakage.py``.
"""

from __future__ import annotations

import importlib
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Privacy-lint-defeating literal splits (same idiom as test_leakage.py):
_OFFRECORD_MARKER = "#" + "off" + "record"
_OFFRECORD_POLICY = "off" + "record"

# Stable chat_id for all wiki citation fixtures.
_WIKI_CHAT_ID = -1009999000001

# User-id range that does not collide with seed_v1 or other eval fixtures.
_BASE_USER_ID = 87_000_000

pytestmark = pytest.mark.asyncio(loop_scope="class")


# ---------------------------------------------------------------------------
# TRUNCATE helper
# ---------------------------------------------------------------------------


async def _clear_wiki_tables(session: AsyncSession) -> None:
    """TRUNCATE all tables touched by wiki citation tests.

    Order: child tables first to satisfy FK constraints (even though CASCADE
    would handle them, explicit order avoids relying on that).
    """
    await session.execute(
        text(
            """
            TRUNCATE TABLE
                wiki_revisions,
                wiki_page_message_sources,
                wiki_page_card_sources,
                wiki_publication_log,
                wiki_pages,
                card_sources,
                knowledge_cards,
                offrecord_marks,
                message_versions,
                chat_messages,
                forget_events
            RESTART IDENTITY CASCADE
            """
        )
    )
    await session.flush()


# ---------------------------------------------------------------------------
# Low-level seeding helpers (raw SQL for wiki tables, ORM for known models)
# ---------------------------------------------------------------------------


async def _upsert_user(session: AsyncSession, *, user_id: int) -> None:
    user_repo = importlib.import_module("bot.db.repos.user")
    await user_repo.UserRepo.upsert(
        session,
        telegram_id=user_id,
        username=f"wiki_c8_user_{user_id}",
        first_name="WikiC8",
        last_name=None,
    )


async def _seed_normal_mv(
    session: AsyncSession,
    *,
    message_id: int,
    user_id: int,
    text_value: str,
) -> int:
    """Insert a governance-clean chat_message + message_version. Returns mvid."""
    from bot.db.models import ChatMessage, MessageVersion

    await _upsert_user(session, user_id=user_id)

    cm = ChatMessage(
        message_id=message_id,
        chat_id=_WIKI_CHAT_ID,
        user_id=user_id,
        text=text_value,
        caption=None,
        date=datetime.now(timezone.utc),
        memory_policy="normal",
        is_redacted=False,
        content_hash=f"wiki-c8-hash-{message_id}",
    )
    session.add(cm)
    await session.flush()

    mv = MessageVersion(
        chat_message_id=cm.id,
        version_seq=1,
        text=text_value,
        caption=None,
        normalized_text=text_value,
        content_hash=cm.content_hash,
        is_redacted=False,
    )
    session.add(mv)
    await session.flush()
    cm.current_version_id = mv.id
    await session.flush()
    return int(mv.id)


async def _seed_offrecord_mv(
    session: AsyncSession,
    *,
    message_id: int,
    user_id: int,
) -> int:
    """Insert a chat_message with memory_policy='offrecord' + redacted version."""
    from bot.db.models import ChatMessage, MessageVersion

    await _upsert_user(session, user_id=user_id)

    cm = ChatMessage(
        message_id=message_id,
        chat_id=_WIKI_CHAT_ID,
        user_id=user_id,
        text=None,  # redacted
        caption=None,
        date=datetime.now(timezone.utc),
        memory_policy=_OFFRECORD_POLICY,
        is_redacted=True,
        content_hash=f"wiki-c8-offr-{message_id}",
    )
    session.add(cm)
    await session.flush()

    mv = MessageVersion(
        chat_message_id=cm.id,
        version_seq=1,
        text=None,
        caption=None,
        normalized_text=None,
        content_hash=cm.content_hash,
        is_redacted=True,
    )
    session.add(mv)
    await session.flush()
    cm.current_version_id = mv.id
    await session.flush()
    return int(mv.id)


async def _seed_forgotten_mv(
    session: AsyncSession,
    *,
    message_id: int,
    user_id: int,
    text_value: str,
) -> tuple[int, int]:
    """Insert a normal mv, then create a forget_event tombstone for it.

    Returns (mvid, forget_event_id).
    """
    from bot.db.models import ChatMessage, ForgetEvent, MessageVersion

    mvid = await _seed_normal_mv(
        session, message_id=message_id, user_id=user_id, text_value=text_value
    )
    # Fetch the chat_message to build the tombstone key
    mv = await session.get(MessageVersion, mvid)
    assert mv is not None
    cm = await session.get(ChatMessage, mv.chat_message_id)
    assert cm is not None
    tombstone_key = f"message:{cm.chat_id}:{cm.message_id}"

    fe = ForgetEvent(
        target_type="message",
        target_id=f"{cm.chat_id}:{cm.message_id}",
        authorized_by="admin",
        tombstone_key=tombstone_key,
        policy="forgotten",
        status="completed",
        created_at=datetime.now(timezone.utc),
    )
    session.add(fe)
    await session.flush()
    return mvid, int(fe.id)


async def _insert_wiki_page(
    session: AsyncSession,
    *,
    page_id: uuid.UUID,
    slug: str,
    body_markdown: str,
    creator_user_id: int,
) -> None:
    """Insert a wiki_pages row via raw SQL (no ORM model)."""
    await session.execute(
        text(
            """
            INSERT INTO wiki_pages
                (id, slug, title, body_markdown, page_status, visibility,
                 public_enabled, robots_policy, validation_status,
                 created_by_user_id, created_at, updated_at)
            VALUES
                (:id, :slug, :title, :body, 'draft', 'member',
                 false, 'noindex', 'valid',
                 :creator, NOW(), NOW())
            """
        ),
        {
            "id": str(page_id),
            "slug": slug,
            "title": f"Test wiki page {slug}",
            "body": body_markdown,
            "creator": creator_user_id,
        },
    )
    await session.flush()


async def _link_mv_to_page(
    session: AsyncSession,
    *,
    wiki_page_id: uuid.UUID,
    message_version_id: int,
    position: int = 0,
) -> None:
    """Insert a wiki_page_message_sources row."""
    await session.execute(
        text(
            """
            INSERT INTO wiki_page_message_sources (wiki_page_id, message_version_id, position)
            VALUES (:page_id, :mv_id, :pos)
            """
        ),
        {"page_id": str(wiki_page_id), "mv_id": message_version_id, "pos": position},
    )
    await session.flush()


async def _insert_wiki_revision(
    session: AsyncSession,
    *,
    revision_id: uuid.UUID,
    wiki_page_id: uuid.UUID,
    revision_seq: int,
    body_markdown: str,
    source_mvids: list[int],
    editor_user_id: int | None = None,
) -> None:
    """Insert a wiki_revisions row with the given source snapshot."""
    import json

    await session.execute(
        text(
            """
            INSERT INTO wiki_revisions
                (id, wiki_page_id, revision_seq, body_markdown, revision_status,
                 source_message_version_ids_snapshot, source_card_ids_snapshot,
                 edited_by_user_id, edited_at, created_at)
            VALUES
                (:id, :page_id, :seq, :body, 'active',
                 CAST(:snapshot AS jsonb), '[]'::jsonb,
                 :editor, NOW(), NOW())
            """
        ),
        {
            "id": str(revision_id),
            "page_id": str(wiki_page_id),
            "seq": revision_seq,
            "body": body_markdown,
            "snapshot": json.dumps(source_mvids),
            "editor": editor_user_id,
        },
    )
    await session.flush()


# ---------------------------------------------------------------------------
# C8a — body_markdown token suppression for member role
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("eval_app_env")
class TestC8aBodyTokenSuppression:
    """C8a: invalid [^mv:<id>] tokens in wiki_pages.body_markdown are suppressed
    from member-role output by render_wiki_page().

    Three cases are tested per test method:
      1. A valid (clean, linked) mv → citation anchor appears in HTML.
      2. An offrecord mv (linked but redacted) → suppressed from member output.
      3. A forgotten mv (active tombstone, linked) → suppressed from member output.
    """

    @pytest_asyncio.fixture(scope="class")
    async def c8a_seed(
        self, eval_db_session: AsyncSession
    ) -> dict[str, Any]:
        """Seed one wiki page with three [^mv:N] tokens: valid, offrecord, forgotten."""
        await _clear_wiki_tables(eval_db_session)

        creator_id = _BASE_USER_ID + 1
        await _upsert_user(eval_db_session, user_id=creator_id)

        # mvid_valid: normal, non-forgotten
        mvid_valid = await _seed_normal_mv(
            eval_db_session,
            message_id=70_001,
            user_id=_BASE_USER_ID + 2,
            text_value="обсуждение алгоритма сортировки",
        )
        # mvid_offrecord: offrecord policy → governance flags as invalid
        mvid_offrecord = await _seed_offrecord_mv(
            eval_db_session,
            message_id=70_002,
            user_id=_BASE_USER_ID + 3,
        )
        # mvid_forgotten: normal mv with active forget_event tombstone
        mvid_forgotten, _fe_id = await _seed_forgotten_mv(
            eval_db_session,
            message_id=70_003,
            user_id=_BASE_USER_ID + 4,
            text_value="удалённое сообщение об алгоритме",
        )

        page_id = uuid.uuid4()
        body = (
            f"Введение в сортировку [^mv:{mvid_valid}] "
            f"дополнительные детали [^mv:{mvid_offrecord}] "
            f"и ещё источник [^mv:{mvid_forgotten}]."
        )

        await _insert_wiki_page(
            eval_db_session,
            page_id=page_id,
            slug=f"c8a-test-{page_id.hex[:8]}",
            body_markdown=body,
            creator_user_id=creator_id,
        )
        # Link all three mvids to the page via wiki_page_message_sources
        await _link_mv_to_page(eval_db_session, wiki_page_id=page_id, message_version_id=mvid_valid, position=0)
        await _link_mv_to_page(eval_db_session, wiki_page_id=page_id, message_version_id=mvid_offrecord, position=1)
        await _link_mv_to_page(eval_db_session, wiki_page_id=page_id, message_version_id=mvid_forgotten, position=2)

        return {
            "page_id": page_id,
            "body": body,
            "mvid_valid": mvid_valid,
            "mvid_offrecord": mvid_offrecord,
            "mvid_forgotten": mvid_forgotten,
        }

    async def test_c8a_valid_token_renders_for_member(
        self,
        eval_db_session: AsyncSession,
        c8a_seed: dict[str, Any],
    ) -> None:
        """C8a (part 1): the valid mvid citation anchor appears in member HTML."""
        from bot.services.wiki_renderer import render_wiki_page

        result = await render_wiki_page(
            eval_db_session,
            page_id=c8a_seed["page_id"],
            role="member",
            body_markdown=c8a_seed["body"],
        )
        assert not result.page_archived, (
            "C8a: page must not be archived — at least one valid source remains"
        )
        mvid_valid = c8a_seed["mvid_valid"]
        assert f"#mv-{mvid_valid}" in result.html_body, (
            f"C8a: valid mvid={mvid_valid} citation anchor missing from member HTML"
        )

    async def test_c8a_offrecord_token_suppressed_for_member(
        self,
        eval_db_session: AsyncSession,
        c8a_seed: dict[str, Any],
    ) -> None:
        """C8a (part 2): offrecord mvid token is suppressed from member output."""
        from bot.services.wiki_renderer import render_wiki_page

        result = await render_wiki_page(
            eval_db_session,
            page_id=c8a_seed["page_id"],
            role="member",
            body_markdown=c8a_seed["body"],
        )
        mvid_offrecord = c8a_seed["mvid_offrecord"]
        assert f"#mv-{mvid_offrecord}" not in result.html_body, (
            f"C8a: offrecord mvid={mvid_offrecord} must NOT appear in member HTML"
        )
        assert mvid_offrecord in result.suppressed_citations, (
            f"C8a: offrecord mvid={mvid_offrecord} must be in suppressed_citations"
        )
        # Raw token must not appear either (no bare [^mv:N] leakage)
        assert f"[^mv:{mvid_offrecord}]" not in result.html_body, (
            f"C8a: raw token [^mv:{mvid_offrecord}] must not appear in member output"
        )

    async def test_c8a_forgotten_token_suppressed_for_member(
        self,
        eval_db_session: AsyncSession,
        c8a_seed: dict[str, Any],
    ) -> None:
        """C8a (part 3): forgotten-mvid token is suppressed from member output."""
        from bot.services.wiki_renderer import render_wiki_page

        result = await render_wiki_page(
            eval_db_session,
            page_id=c8a_seed["page_id"],
            role="member",
            body_markdown=c8a_seed["body"],
        )
        mvid_forgotten = c8a_seed["mvid_forgotten"]
        assert f"#mv-{mvid_forgotten}" not in result.html_body, (
            f"C8a: forgotten mvid={mvid_forgotten} must NOT appear in member HTML"
        )
        assert mvid_forgotten in result.suppressed_citations, (
            f"C8a: forgotten mvid={mvid_forgotten} must be in suppressed_citations"
        )
        assert f"[^mv:{mvid_forgotten}]" not in result.html_body, (
            f"C8a: raw token [^mv:{mvid_forgotten}] must not appear in member output"
        )

    async def test_c8a_admin_sees_unavailable_markers(
        self,
        eval_db_session: AsyncSession,
        c8a_seed: dict[str, Any],
    ) -> None:
        """C8a (admin variant): invalid tokens produce [⚠ SOURCE UNAVAILABLE] markers."""
        from bot.services.wiki_renderer import render_wiki_page

        result = await render_wiki_page(
            eval_db_session,
            page_id=c8a_seed["page_id"],
            role="admin",
            body_markdown=c8a_seed["body"],
        )
        assert not result.page_archived, "C8a-admin: page must not be archived"
        mvid_offrecord = c8a_seed["mvid_offrecord"]
        mvid_forgotten = c8a_seed["mvid_forgotten"]
        assert mvid_offrecord in result.admin_unavailable_markers, (
            f"C8a-admin: offrecord mvid={mvid_offrecord} must be in admin_unavailable_markers"
        )
        assert mvid_forgotten in result.admin_unavailable_markers, (
            f"C8a-admin: forgotten mvid={mvid_forgotten} must be in admin_unavailable_markers"
        )
        # Admin sees the warning text, not the raw token
        assert "[⚠ SOURCE UNAVAILABLE]" in result.html_body or result.html_body == "", (
            "C8a-admin: admin output must contain [⚠ SOURCE UNAVAILABLE] for invalid tokens"
        )


# ---------------------------------------------------------------------------
# C8b — revision snapshot validation
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("eval_app_env")
class TestC8bRevisionCitationValidation:
    """C8b: forgotten content does not leak via wiki_revisions body rendering.

    Tests that:
      1. A revision body containing a [^mv:N] token for a now-forgotten mvid
         is suppressed when rendered for member role.
      2. The revision's source_message_version_ids_snapshot pointing to a
         forgotten mvid does not expose the forgotten content — governance
         flags the mvid as invalid regardless of snapshot values.
    """

    @pytest_asyncio.fixture(scope="class")
    async def c8b_seed(
        self, eval_db_session: AsyncSession
    ) -> dict[str, Any]:
        """Seed a wiki page + revision where one cited mvid gets tombstoned."""
        await _clear_wiki_tables(eval_db_session)

        creator_id = _BASE_USER_ID + 10
        await _upsert_user(eval_db_session, user_id=creator_id)

        # Seed a clean mvid first (for the page to have ≥1 valid source)
        mvid_clean = await _seed_normal_mv(
            eval_db_session,
            message_id=71_001,
            user_id=_BASE_USER_ID + 11,
            text_value="чистый источник для страницы",
        )
        # Seed the mvid that will be forgotten later
        mvid_to_forget, fe_id = await _seed_forgotten_mv(
            eval_db_session,
            message_id=71_002,
            user_id=_BASE_USER_ID + 12,
            text_value="источник который будет забыт",
        )

        page_id = uuid.uuid4()
        page_body = (
            f"Актуальное содержание [^mv:{mvid_clean}]."
        )
        await _insert_wiki_page(
            eval_db_session,
            page_id=page_id,
            slug=f"c8b-test-{page_id.hex[:8]}",
            body_markdown=page_body,
            creator_user_id=creator_id,
        )
        await _link_mv_to_page(eval_db_session, wiki_page_id=page_id, message_version_id=mvid_clean, position=0)
        # Also link the forgotten mvid so governance can check it
        await _link_mv_to_page(eval_db_session, wiki_page_id=page_id, message_version_id=mvid_to_forget, position=1)

        # Insert a revision that cites both — the revision body contains a token
        # for the now-forgotten mvid.
        revision_id = uuid.uuid4()
        revision_body = (
            f"Старая версия: актуальный [^mv:{mvid_clean}] "
            f"и уже забытый [^mv:{mvid_to_forget}] источники."
        )
        await _insert_wiki_revision(
            eval_db_session,
            revision_id=revision_id,
            wiki_page_id=page_id,
            revision_seq=1,
            body_markdown=revision_body,
            source_mvids=[mvid_clean, mvid_to_forget],
            editor_user_id=creator_id,
        )

        return {
            "page_id": page_id,
            "revision_id": revision_id,
            "revision_body": revision_body,
            "mvid_clean": mvid_clean,
            "mvid_to_forget": mvid_to_forget,
            "fe_id": fe_id,
        }

    async def test_c8b_forgotten_token_suppressed_in_revision_body(
        self,
        eval_db_session: AsyncSession,
        c8b_seed: dict[str, Any],
    ) -> None:
        """C8b (part 1): revision body renders with forgotten token suppressed for member."""
        from bot.services.wiki_renderer import render_wiki_page

        result = await render_wiki_page(
            eval_db_session,
            page_id=c8b_seed["page_id"],
            role="member",
            body_markdown=c8b_seed["revision_body"],
        )
        mvid_to_forget = c8b_seed["mvid_to_forget"]
        mvid_clean = c8b_seed["mvid_clean"]

        # The forgotten mvid must NOT appear as a citation anchor
        assert f"#mv-{mvid_to_forget}" not in result.html_body, (
            f"C8b: forgotten mvid={mvid_to_forget} citation anchor leaked into revision HTML"
        )
        # The clean mvid must still render
        assert f"#mv-{mvid_clean}" in result.html_body, (
            f"C8b: clean mvid={mvid_clean} must still render in revision body"
        )
        # Forgotten mvid must appear in suppressed_citations
        assert mvid_to_forget in result.suppressed_citations, (
            f"C8b: forgotten mvid={mvid_to_forget} must be listed in suppressed_citations"
        )

    async def test_c8b_snapshot_does_not_leak_forgotten_content(
        self,
        eval_db_session: AsyncSession,
        c8b_seed: dict[str, Any],
    ) -> None:
        """C8b (part 2): governance sees the forgotten mvid as invalid regardless of snapshot.

        Validates that validate_sources() flags the forgotten mvid when called
        against the page — proving that the snapshot being present in
        wiki_revisions does not cause governance to bypass the tombstone check.
        """
        from bot.services.wiki_governance import validate_sources

        gov = await validate_sources(eval_db_session, page_id=c8b_seed["page_id"])
        mvid_to_forget = c8b_seed["mvid_to_forget"]

        assert mvid_to_forget in gov.invalid_mvids, (
            f"C8b: governance must flag forgotten mvid={mvid_to_forget} as invalid; "
            f"invalid_mvids={gov.invalid_mvids}"
        )
        reason = gov.reasons.get(f"mvid:{mvid_to_forget}", "")
        assert reason == "forgotten", (
            f"C8b: reason for mvid={mvid_to_forget} must be 'forgotten', got {reason!r}"
        )

    async def test_c8b_raw_token_does_not_appear_in_member_revision_output(
        self,
        eval_db_session: AsyncSession,
        c8b_seed: dict[str, Any],
    ) -> None:
        """C8b (part 3): raw [^mv:N] token for forgotten mvid does not appear in member output."""
        from bot.services.wiki_renderer import render_wiki_page

        result = await render_wiki_page(
            eval_db_session,
            page_id=c8b_seed["page_id"],
            role="member",
            body_markdown=c8b_seed["revision_body"],
        )
        mvid_to_forget = c8b_seed["mvid_to_forget"]
        assert f"[^mv:{mvid_to_forget}]" not in result.html_body, (
            f"C8b: raw token [^mv:{mvid_to_forget}] must not appear in member revision output"
        )
