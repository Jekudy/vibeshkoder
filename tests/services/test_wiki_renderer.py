"""T9-04 — wiki renderer tests.

Covers all 9 scenarios (8 ACs + AST lint) from PHASE9_PLAN.md §T9-04.

Isolation: every async test uses db_session (rollback fixture). Helper
functions are duplicated minimally from test_wiki_governance.py to keep
tests self-contained without importing from another test module.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.usefixtures("app_env")


# ── helpers (minimal, mirrored from test_wiki_governance.py) ──────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _make_user(session) -> int:
    from bot.db.models import User

    uid = int(uuid.uuid4().int & 0x7FFFFFFF)
    user = User(
        id=uid,
        username=f"u{uid}",
        first_name="Test",
        is_member=True,
        is_admin=False,
    )
    session.add(user)
    await session.flush()
    return uid


async def _make_chat_message(session, *, user_id: int, memory_policy: str = "normal"):
    from bot.db.models import ChatMessage

    cm = ChatMessage(
        message_id=int(uuid.uuid4().int & 0x7FFFFFFF),
        chat_id=-1001234567890,
        user_id=user_id,
        text="test content",
        date=_now(),
        raw_json={"text": "test content"},
        memory_policy=memory_policy,
        is_redacted=False,
    )
    session.add(cm)
    await session.flush()
    return cm


async def _make_message_version(
    session,
    *,
    chat_message_id: int,
    content_hash: str | None = None,
    is_redacted: bool = False,
    version_seq: int | None = None,
) -> int:
    """Create a MessageVersion. Auto-assigns version_seq when not supplied
    so callers can make multiple revisions of the same chat_message without
    hitting uq_message_versions_chat_message_seq.
    """
    from sqlalchemy import text as _text
    from bot.db.models import MessageVersion

    if content_hash is None:
        content_hash = f"h-{uuid.uuid4().hex[:16]}"

    if version_seq is None:
        # Next free version_seq for this chat_message_id.
        existing = (
            await session.execute(
                _text(
                    "SELECT COALESCE(MAX(version_seq), 0) + 1 FROM message_versions "
                    "WHERE chat_message_id = :cmid"
                ),
                {"cmid": chat_message_id},
            )
        ).scalar()
        version_seq = int(existing or 1)

    mv = MessageVersion(
        chat_message_id=chat_message_id,
        version_seq=version_seq,
        text="test content",
        normalized_text="test content",
        entities_json={},
        content_hash=content_hash,
        is_redacted=is_redacted,
    )
    session.add(mv)
    await session.flush()
    return mv.id


async def _make_knowledge_card(
    session,
    *,
    admin_user_id: int,
    card_status: str = "approved",
) -> uuid.UUID:
    from bot.db.models import KnowledgeCard

    card = KnowledgeCard(
        title="Test Card",
        body_markdown="test body",
        card_status=card_status,
        approved_by_user_id=admin_user_id if card_status == "approved" else None,
        approved_at=_now() if card_status == "approved" else None,
    )
    session.add(card)
    await session.flush()
    return card.id


async def _make_card_source(session, *, card_id: uuid.UUID, message_version_id: int) -> None:
    from bot.db.models import CardSource

    cs = CardSource(
        card_id=card_id,
        message_version_id=message_version_id,
        position=0,
    )
    session.add(cs)
    await session.flush()


async def _make_wiki_page(
    session,
    *,
    created_by_user_id: int | None = None,
    body_markdown: str = "body content",
) -> uuid.UUID:
    from sqlalchemy import text

    if created_by_user_id is None:
        created_by_user_id = await _make_user(session)

    page_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO wiki_pages "
            "(id, slug, title, body_markdown, page_status, public_enabled, robots_policy, "
            " created_by_user_id, created_at, updated_at) "
            "VALUES "
            "(:id, :slug, :title, :body, 'draft', false, 'noindex', "
            " :created_by, now(), now())"
        ),
        {
            "id": str(page_id),
            "slug": f"test-page-{uuid.uuid4().hex[:8]}",
            "title": "Test Page",
            "body": body_markdown,
            "created_by": created_by_user_id,
        },
    )
    return page_id


async def _link_card(session, *, page_id: uuid.UUID, card_id: uuid.UUID, position: int = 0) -> None:
    from sqlalchemy import text

    await session.execute(
        text(
            "INSERT INTO wiki_page_card_sources (wiki_page_id, card_id, position) "
            "VALUES (:page_id, :card_id, :pos)"
        ),
        {"page_id": str(page_id), "card_id": str(card_id), "pos": position},
    )


async def _link_mv(
    session, *, page_id: uuid.UUID, message_version_id: int, position: int = 0
) -> None:
    from sqlalchemy import text

    await session.execute(
        text(
            "INSERT INTO wiki_page_message_sources (wiki_page_id, message_version_id, position) "
            "VALUES (:page_id, :mvid, :pos)"
        ),
        {"page_id": str(page_id), "mvid": message_version_id, "pos": position},
    )


# ── AC1: valid page with approved cards → sanitized HTML, page_archived=False ──


async def test_valid_page_produces_sanitized_html(db_session) -> None:
    """AC1: a page with valid approved-card citations renders sanitized HTML."""
    from bot.services.wiki_renderer import render_wiki_page

    uid = await _make_user(db_session)
    cm = await _make_chat_message(db_session, user_id=uid)
    mv_id = await _make_message_version(db_session, chat_message_id=cm.id)
    card_id = await _make_knowledge_card(db_session, admin_user_id=uid)
    await _make_card_source(db_session, card_id=card_id, message_version_id=mv_id)

    page_id = await _make_wiki_page(db_session, body_markdown="Hello **world**.")
    await _link_card(db_session, page_id=page_id, card_id=card_id)
    await _link_mv(db_session, page_id=page_id, message_version_id=mv_id)

    result = await render_wiki_page(
        db_session, page_id=page_id, role="member", body_markdown="Hello **world**."
    )

    assert result.page_archived is False
    assert result.html_body != ""
    # CommonMark renders **world** as <strong>world</strong>
    assert "world" in result.html_body


# ── AC2 (C8): [^mv:N] valid mv → citation link ────────────────────────────────


async def test_valid_mv_token_renders_as_citation_link(db_session) -> None:
    """AC2 (C8): [^mv:42] referencing a valid non-redacted mv → citation anchor."""
    from bot.services.wiki_renderer import render_wiki_page

    uid = await _make_user(db_session)
    cm = await _make_chat_message(db_session, user_id=uid)
    mv_id = await _make_message_version(db_session, chat_message_id=cm.id)
    cm.current_version_id = mv_id
    await db_session.flush()

    page_id = await _make_wiki_page(db_session, body_markdown=f"See [^mv:{mv_id}] here.")
    await _link_mv(db_session, page_id=page_id, message_version_id=mv_id)

    result = await render_wiki_page(
        db_session,
        page_id=page_id,
        role="member",
        body_markdown=f"See [^mv:{mv_id}] here.",
    )

    assert result.page_archived is False
    assert f'href="#mv-{mv_id}"' in result.html_body
    assert "wiki-citation" in result.html_body
    assert f"[^{mv_id}]" in result.html_body


# ── AC3 member: invalid mv → suppressed ───────────────────────────────────────


async def test_invalid_mv_suppressed_for_member(db_session) -> None:
    """AC3 member: invalid [^mv:N] among valid sources → suppressed (not archived).

    Page has one valid mv (good_mv) AND one invalid mv (bad_mv, redacted).
    Per-citation suppression applies; page_archived stays False because at
    least one source is valid. (When every source is invalid, AC#7 escalates
    to page_archived=True — covered by test_all_sources_failing_*.)
    """
    from bot.services.wiki_renderer import render_wiki_page

    uid = await _make_user(db_session)
    good_cm = await _make_chat_message(db_session, user_id=uid)
    good_mv = await _make_message_version(db_session, chat_message_id=good_cm.id)
    good_cm.current_version_id = good_mv
    bad_cm = await _make_chat_message(db_session, user_id=uid)
    bad_mv = await _make_message_version(db_session, chat_message_id=bad_cm.id, is_redacted=True)
    bad_cm.current_version_id = bad_mv
    await db_session.flush()

    body = f"Good [^mv:{good_mv}] and bad [^mv:{bad_mv}] here."
    page_id = await _make_wiki_page(db_session, body_markdown=body)
    await _link_mv(db_session, page_id=page_id, message_version_id=good_mv, position=0)
    await _link_mv(db_session, page_id=page_id, message_version_id=bad_mv, position=1)

    result = await render_wiki_page(
        db_session,
        page_id=page_id,
        role="member",
        body_markdown=body,
    )

    assert result.page_archived is False
    assert bad_mv in result.suppressed_citations
    assert good_mv not in result.suppressed_citations
    assert f"[^mv:{bad_mv}]" not in result.html_body
    assert "SOURCE UNAVAILABLE" not in result.html_body
    # Member-suppress post-pass collapses "and bad ." → "and bad."
    assert "  " not in result.html_body  # no double-space gap


# ── AC3 admin: invalid mv → [⚠ SOURCE UNAVAILABLE] shown ─────────────────────


async def test_invalid_mv_shown_as_unavailable_for_admin(db_session) -> None:
    """AC3 admin: invalid [^mv:N] among valid sources → '[⚠ SOURCE UNAVAILABLE]'.

    Same fixture shape as the member test — one good mv + one bad mv. Admin
    sees a visible marker instead of silent suppression.
    """
    from bot.services.wiki_renderer import render_wiki_page

    uid = await _make_user(db_session)
    good_cm = await _make_chat_message(db_session, user_id=uid)
    good_mv = await _make_message_version(db_session, chat_message_id=good_cm.id)
    good_cm.current_version_id = good_mv
    bad_cm = await _make_chat_message(db_session, user_id=uid)
    bad_mv = await _make_message_version(db_session, chat_message_id=bad_cm.id, is_redacted=True)
    bad_cm.current_version_id = bad_mv
    await db_session.flush()

    body = f"Good [^mv:{good_mv}] and bad [^mv:{bad_mv}] here."
    page_id = await _make_wiki_page(db_session, body_markdown=body)
    await _link_mv(db_session, page_id=page_id, message_version_id=good_mv, position=0)
    await _link_mv(db_session, page_id=page_id, message_version_id=bad_mv, position=1)

    result = await render_wiki_page(
        db_session,
        page_id=page_id,
        role="admin",
        body_markdown=body,
    )

    assert result.page_archived is False
    assert bad_mv in result.admin_unavailable_markers
    assert good_mv not in result.admin_unavailable_markers
    assert "SOURCE UNAVAILABLE" in result.html_body
    assert f"[^mv:{bad_mv}]" not in result.html_body
    # good_mv still renders as citation
    assert f'href="#mv-{good_mv}"' in result.html_body


# ── AC4 (L9b): archived card → page_archived=True, html_body='' ───────────────


async def test_archived_card_sets_page_archived(db_session) -> None:
    """AC4 (L9b): [^card:UUID] referencing archived card → page_archived=True, html_body=''."""
    from bot.services.wiki_renderer import render_wiki_page

    uid = await _make_user(db_session)
    card_id = await _make_knowledge_card(db_session, admin_user_id=uid, card_status="archived")

    body = f"See [^card:{card_id}] here."
    page_id = await _make_wiki_page(db_session, body_markdown=body)
    await _link_card(db_session, page_id=page_id, card_id=card_id)

    result = await render_wiki_page(
        db_session,
        page_id=page_id,
        role="member",
        body_markdown=body,
    )

    assert result.page_archived is True
    assert result.html_body == ""


# ── AC5: raw <script> stripped ────────────────────────────────────────────────


async def test_script_tag_stripped_from_html(db_session) -> None:
    """AC5: raw <script>alert('x')</script> in body_markdown is stripped from output."""
    from bot.services.wiki_renderer import render_wiki_page

    page_id = await _make_wiki_page(db_session)

    body = "Hello <script>alert('x')</script> world."
    result = await render_wiki_page(
        db_session,
        page_id=page_id,
        role="member",
        body_markdown=body,
    )

    assert result.page_archived is False
    assert "<script>" not in result.html_body
    assert "alert" not in result.html_body


# ── AC6: raw <img> stripped (not in allowlist) ────────────────────────────────


async def test_img_tag_stripped_from_html(db_session) -> None:
    """AC6: raw <img src="..."> in body_markdown is stripped (img not in bleach allowlist)."""
    from bot.services.wiki_renderer import render_wiki_page

    page_id = await _make_wiki_page(db_session)

    body = 'Hello <img src="https://evil.example.com/track.gif"> world.'
    result = await render_wiki_page(
        db_session,
        page_id=page_id,
        role="member",
        body_markdown=body,
    )

    assert result.page_archived is False
    assert "<img" not in result.html_body
    assert "evil.example.com" not in result.html_body


# ── AC7: all cited sources failing governance → page_archived=True ─────────────


async def test_all_sources_failing_governance_sets_page_archived(db_session) -> None:
    """AC7: page with ALL cited sources failing governance → page_archived=True, html_body=''."""
    from bot.services.wiki_renderer import render_wiki_page

    uid = await _make_user(db_session)
    # Archived card — governance will flag it
    card_id = await _make_knowledge_card(db_session, admin_user_id=uid, card_status="archived")

    body = f"Content [^card:{card_id}]."
    page_id = await _make_wiki_page(db_session, body_markdown=body)
    await _link_card(db_session, page_id=page_id, card_id=card_id)

    result = await render_wiki_page(
        db_session,
        page_id=page_id,
        role="member",
        body_markdown=body,
    )

    assert result.page_archived is True
    assert result.html_body == ""


# ── Codex HIGH fix: unlinked mv token never rendered as citation ──────────────


async def test_unlinked_mv_token_treated_as_invalid(db_session) -> None:
    """Codex HIGH fix: body referencing an mv NOT linked to the page must
    NOT render as a valid citation. validate_sources returns invalid_mvids
    only for LINKED sources; the renderer must also block unknown tokens.
    """
    from bot.services.wiki_renderer import render_wiki_page

    uid = await _make_user(db_session)
    linked_cm = await _make_chat_message(db_session, user_id=uid)
    # Linked good source for the page (so the page isn't fully invalid).
    linked_mv = await _make_message_version(db_session, chat_message_id=linked_cm.id)
    linked_cm.current_version_id = linked_mv
    # Unlinked mv — exists in DB but never tied to this page.
    unlinked_cm = await _make_chat_message(db_session, user_id=uid)
    unlinked_mv = await _make_message_version(db_session, chat_message_id=unlinked_cm.id)
    unlinked_cm.current_version_id = unlinked_mv
    await db_session.flush()

    body = f"Linked [^mv:{linked_mv}] and unlinked [^mv:{unlinked_mv}]."
    page_id = await _make_wiki_page(db_session, body_markdown=body)
    await _link_mv(db_session, page_id=page_id, message_version_id=linked_mv)

    result = await render_wiki_page(
        db_session,
        page_id=page_id,
        role="admin",
        body_markdown=body,
    )

    # Unlinked mv should be in admin_unavailable_markers (treated as invalid).
    assert unlinked_mv in result.admin_unavailable_markers
    # Linked good mv still renders as a real citation.
    assert f'href="#mv-{linked_mv}"' in result.html_body
    # Unlinked mv must NOT appear as a wiki-citation anchor.
    assert f'href="#mv-{unlinked_mv}"' not in result.html_body


# ── Codex L9c integration: transitive offrecord mv blocked from rendering ────


async def test_transitive_offrecord_mv_token_not_rendered_as_citation(db_session) -> None:
    """Codex HIGH (T9-05 PAR): body [^mv:N] where N is a transitive source of an
    invalid card MUST NOT render as a valid citation.

    Setup: page cites card C via wiki_page_card_sources. C has card_source
    pointing to mv_offrecord (chat_message memory_policy='offrecord').
    Governance flags C as transitive_forget but does NOT add mv_offrecord
    to invalid_mvids. Renderer must still treat [^mv:mv_offrecord] as
    invalid/unknown because every parent card linking that mv is invalid.
    """
    from bot.services.wiki_renderer import render_wiki_page

    uid = await _make_user(db_session)
    cm = await _make_chat_message(db_session, user_id=uid, memory_policy="offrecord")
    mv_offrecord = await _make_message_version(db_session, chat_message_id=cm.id)
    cm.current_version_id = mv_offrecord
    card_id = await _make_knowledge_card(db_session, admin_user_id=uid, card_status="approved")
    await _make_card_source(db_session, card_id=card_id, message_version_id=mv_offrecord)

    # Direct valid mv so the page isn't fully invalid (would archive).
    cm_clean = await _make_chat_message(db_session, user_id=uid)
    mv_clean = await _make_message_version(db_session, chat_message_id=cm_clean.id)
    cm_clean.current_version_id = mv_clean
    await db_session.flush()

    body = f"Good [^mv:{mv_clean}] and offrecord [^mv:{mv_offrecord}] here."
    page_id = await _make_wiki_page(db_session, body_markdown=body)
    await _link_card(db_session, page_id=page_id, card_id=card_id)
    await _link_mv(db_session, page_id=page_id, message_version_id=mv_clean)

    # Admin role to make the marker observable (member role would suppress silently).
    result = await render_wiki_page(
        db_session,
        page_id=page_id,
        role="admin",
        body_markdown=body,
    )

    # The page isn't archived overall (mv_clean keeps a valid source AND the
    # card-token itself isn't in the body so AC#4 / AC#7 don't apply).
    assert result.page_archived is False
    # Clean mv renders as a citation; offrecord transitive mv does not.
    assert f'href="#mv-{mv_clean}"' in result.html_body
    assert f'href="#mv-{mv_offrecord}"' not in result.html_body
    # And the offrecord mv lands in admin_unavailable_markers.
    assert mv_offrecord in result.admin_unavailable_markers


# ── AC8 (G1 lint): no LLM/graph imports in wiki_renderer.py ──────────────────


def test_no_llm_or_graph_imports_in_wiki_renderer() -> None:
    """AC8 (G1 lint): wiki_renderer.py must not import neo4j, graph_*, or llm_*."""
    import ast
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2] / "bot" / "services" / "wiki_renderer.py"
    ).read_text()
    tree = ast.parse(source)
    forbidden_prefixes = ("neo4j",)
    forbidden_substrings = ("graph_", "llm_")

    def _check(name: str) -> None:
        for p in forbidden_prefixes:
            assert not name.startswith(p), f"Forbidden import: {name}"
        for s in forbidden_substrings:
            assert s not in name, f"Forbidden import: {name}"

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _check(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            _check(module)
            # Also catch `from bot.services import llm_gateway` style — alias name
            # carries the forbidden symbol.
            for alias in node.names:
                _check(alias.name)
                _check(f"{module}.{alias.name}")
