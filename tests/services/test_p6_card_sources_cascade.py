"""T6-01 Phase 6 ``_cascade_card_sources_on_forget`` tests.

PHASE6_PLAN.md §5.A.5 defines the cascade contract:

1. Apply lock-of-row on affected ``knowledge_cards`` rows
   (defence-in-depth — primary serialization with /approve happens at the
   ``apply_forget_event`` orchestrator level via
   ``pg_advisory_xact_lock(_p6_mvid_advisory_lock_id(mvid))``).
2. DELETE every ``card_sources`` row whose ``message_version_id`` matches
   the forgotten version.
3. For each affected ``card_id``, recount remaining ``card_sources`` rows.
4. If remaining count = 0, demote: ``card_status='archived'``,
   ``archived_reason='all sources forgotten via cascade <forget_event_id>'``,
   ``updated_at=now()``.
5. If remaining count > 0, leave the card alone (source unlinked, partial
   attribution; flag for later admin review — out of scope).

**Privacy invariant:** ``archived_reason`` MUST NOT contain quoted body
content from the forgotten message — only the ``forget_event_id``
reference. (PHASE6_PLAN.md §5.A.5 "Privacy invariant" paragraph.)

Order invariant: ``_cascade_qa_traces_llm`` runs BEFORE
``_cascade_card_sources_on_forget`` so qa_traces are NULL'd before card
sources are removed.
"""

from __future__ import annotations

import itertools
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text

pytestmark = pytest.mark.usefixtures("app_env")


_user_counter = itertools.count(start=9_700_000_000)
_msg_counter = itertools.count(start=970_000)
_chat_counter = itertools.count(start=1)
_key_counter = itertools.count(start=1)


def _next_user() -> int:
    return next(_user_counter)


def _next_msg_id() -> int:
    return next(_msg_counter)


def _next_chat_id() -> int:
    return -1_000_000_000_000 - next(_chat_counter)


def _next_key(prefix: str = "message") -> str:
    return f"{prefix}:p6:test:{next(_key_counter)}"


async def _make_user(db_session) -> int:
    from bot.db.repos.user import UserRepo

    uid = _next_user()
    await UserRepo.upsert(
        db_session,
        telegram_id=uid,
        username=f"u{uid}",
        first_name="Test",
        last_name=None,
    )
    return uid


async def _make_chat_message_with_v1(db_session) -> tuple[int, int, int, int]:
    from bot.db.models import ChatMessage, MessageVersion
    from sqlalchemy import update as sa_update

    uid = await _make_user(db_session)
    chat_id = _next_chat_id()
    message_id = _next_msg_id()
    when = datetime.now(timezone.utc)

    msg = ChatMessage(
        message_id=message_id,
        chat_id=chat_id,
        user_id=uid,
        text="source content",
        date=when,
        memory_policy="normal",
        is_redacted=False,
    )
    db_session.add(msg)
    await db_session.flush()

    v = MessageVersion(
        chat_message_id=msg.id,
        version_seq=1,
        text="source content",
        normalized_text="source content",
        entities_json={},
        content_hash=f"h{uuid.uuid4().hex[:16]}",
        is_redacted=False,
    )
    db_session.add(v)
    await db_session.flush()
    # Set current_version_id so the target_type='message' cascade resolution
    # path can map chat_message_id → message_version_id.
    await db_session.execute(
        sa_update(ChatMessage)
        .where(ChatMessage.id == msg.id)
        .values(current_version_id=v.id)
    )
    await db_session.flush()
    return msg.id, v.id, chat_id, message_id


async def _make_approved_card_with_sources(
    db_session, *, source_version_ids: list[int]
) -> uuid.UUID:
    """Insert an approved knowledge_card linked to the given source mvids."""
    from bot.db.models import CardSource, KnowledgeCard

    approver = await _make_user(db_session)
    card = KnowledgeCard(
        title="t",
        body_markdown="b",
        card_status="approved",
        approved_by_user_id=approver,
        approved_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    await db_session.flush()

    for pos, mv_id in enumerate(source_version_ids):
        src = CardSource(
            card_id=card.id, message_version_id=mv_id, position=pos
        )
        db_session.add(src)
    await db_session.flush()
    return card.id


async def _make_pending_forget_event(db_session, target_type, target_id) -> int:
    from bot.db.repos.forget_event import ForgetEventRepo

    ev = await ForgetEventRepo.create(
        db_session,
        target_type=target_type,
        target_id=str(target_id),
        actor_user_id=None,
        authorized_by="admin",
        tombstone_key=_next_key(target_type),
    )
    return ev.id


# ─── Test 1: CASCADE_LAYER_ORDER — qa_traces_llm BEFORE card_sources ────────


def test_cascade_layer_order_qa_traces_llm_before_card_sources() -> None:
    """PHASE6_PLAN.md §5.A.5 lock-ordering invariant: qa_traces_llm is
    nulled before card_sources are deleted, so citation_ids lookups in
    qa_traces complete before the card-source rows disappear.
    """
    from bot.services.forget_cascade import CASCADE_LAYER_ORDER

    assert "qa_traces_llm" in CASCADE_LAYER_ORDER
    assert "card_sources" in CASCADE_LAYER_ORDER

    qa_index = CASCADE_LAYER_ORDER.index("qa_traces_llm")
    cs_index = CASCADE_LAYER_ORDER.index("card_sources")

    assert qa_index < cs_index, (
        f"qa_traces_llm (index {qa_index}) MUST come before card_sources "
        f"(index {cs_index}) — see PHASE6_PLAN.md §5.A.5"
    )


# ─── Test 2: card with only forgotten source → demote to archived ────────────


async def test_card_with_only_forgotten_source_demotes_to_archived(
    db_session,
) -> None:
    """An approved card whose only ``card_sources`` row points at the
    forgotten ``message_version_id`` MUST transition to
    ``card_status='archived'`` with a forget_event_id reference."""
    from bot.db.models import CardSource, ForgetEvent, KnowledgeCard
    from bot.services.forget_cascade import _cascade_card_sources_on_forget

    cm_id, ver_id, chat_id, msg_id = await _make_chat_message_with_v1(db_session)
    card_id = await _make_approved_card_with_sources(
        db_session, source_version_ids=[ver_id]
    )

    fe_id = await _make_pending_forget_event(
        db_session, target_type="message", target_id=cm_id
    )
    ev = await db_session.get(ForgetEvent, fe_id)
    rowcount = await _cascade_card_sources_on_forget(db_session, ev)
    # rowcount = card_sources rows deleted (1 row)
    assert rowcount >= 1

    # Card demoted to archived.
    card = await db_session.get(KnowledgeCard, card_id)
    assert card.card_status == "archived"
    assert card.archived_reason is not None
    assert str(fe_id) in card.archived_reason
    # Privacy invariant: archived_reason must NOT contain body text.
    assert "source content" not in card.archived_reason

    # card_sources row is gone.
    remaining = (
        await db_session.execute(
            select(CardSource).where(CardSource.card_id == card_id)
        )
    ).scalars().all()
    assert len(remaining) == 0


# ─── Test 3: card with partial sources → keep approved, just unlink ──────────


async def test_card_with_remaining_sources_stays_approved(db_session) -> None:
    """An approved card with multiple sources, only one of which is
    forgotten, MUST stay ``card_status='approved'``. Only the forgotten
    source link is removed; the others survive."""
    from bot.db.models import CardSource, ForgetEvent, KnowledgeCard
    from bot.services.forget_cascade import _cascade_card_sources_on_forget

    cm_id_1, ver_id_1, _, _ = await _make_chat_message_with_v1(db_session)
    cm_id_2, ver_id_2, _, _ = await _make_chat_message_with_v1(db_session)
    card_id = await _make_approved_card_with_sources(
        db_session, source_version_ids=[ver_id_1, ver_id_2]
    )

    fe_id = await _make_pending_forget_event(
        db_session, target_type="message", target_id=cm_id_1
    )
    ev = await db_session.get(ForgetEvent, fe_id)
    await _cascade_card_sources_on_forget(db_session, ev)

    card = await db_session.get(KnowledgeCard, card_id)
    assert card.card_status == "approved"
    assert card.archived_reason is None

    remaining = (
        await db_session.execute(
            select(CardSource).where(CardSource.card_id == card_id)
        )
    ).scalars().all()
    assert len(remaining) == 1
    assert remaining[0].message_version_id == ver_id_2


# ─── Test 4: card not linked to forgotten source → untouched ─────────────────


async def test_cascade_noops_for_card_with_no_matching_source(db_session) -> None:
    """Cards whose sources do NOT include the forgotten mvid are left
    untouched. Cascade rowcount is 0 for them."""
    from bot.db.models import CardSource, ForgetEvent, KnowledgeCard
    from bot.services.forget_cascade import _cascade_card_sources_on_forget

    cm_id_1, ver_id_1, _, _ = await _make_chat_message_with_v1(db_session)
    cm_id_2, ver_id_2, _, _ = await _make_chat_message_with_v1(db_session)
    # Card cites mvid_2 only.
    card_id = await _make_approved_card_with_sources(
        db_session, source_version_ids=[ver_id_2]
    )

    # Forget mvid_1 (NOT a card source).
    fe_id = await _make_pending_forget_event(
        db_session, target_type="message", target_id=cm_id_1
    )
    ev = await db_session.get(ForgetEvent, fe_id)
    rowcount = await _cascade_card_sources_on_forget(db_session, ev)
    assert rowcount == 0

    card = await db_session.get(KnowledgeCard, card_id)
    assert card.card_status == "approved"
    assert card.archived_reason is None


# ─── Test 5: target_type='message_hash' resolves multiple mvids ──────────────


async def test_cascade_handles_message_hash_target(db_session) -> None:
    """target_type='message_hash' applies to ALL message_versions sharing
    the content_hash. A card whose only source matches is demoted; a card
    citing only an unrelated source is untouched."""
    from bot.db.models import ChatMessage, ForgetEvent, KnowledgeCard, MessageVersion
    from bot.services.forget_cascade import _cascade_card_sources_on_forget

    # Two chat_messages sharing the same content_hash (e.g. duplicate post).
    shared_hash = f"hash{uuid.uuid4().hex[:12]}"

    uid = await _make_user(db_session)
    msgs = []
    versions = []
    for i in range(2):
        msg = ChatMessage(
            message_id=_next_msg_id(),
            chat_id=_next_chat_id(),
            user_id=uid,
            text="shared",
            date=datetime.now(timezone.utc),
            memory_policy="normal",
            is_redacted=False,
        )
        db_session.add(msg)
        await db_session.flush()
        msgs.append(msg)
        v = MessageVersion(
            chat_message_id=msg.id,
            version_seq=1,
            text="shared",
            normalized_text="shared",
            entities_json={},
            content_hash=shared_hash,
            is_redacted=False,
        )
        db_session.add(v)
        await db_session.flush()
        versions.append(v)

    # Card whose sources are exactly the two version_ids sharing the hash.
    card_demote_id = await _make_approved_card_with_sources(
        db_session, source_version_ids=[v.id for v in versions]
    )

    # Unrelated card with a different source.
    cm_other_id, ver_other_id, _, _ = await _make_chat_message_with_v1(db_session)
    card_keep_id = await _make_approved_card_with_sources(
        db_session, source_version_ids=[ver_other_id]
    )

    fe_id = await _make_pending_forget_event(
        db_session,
        target_type="message_hash",
        target_id=shared_hash,
    )
    ev = await db_session.get(ForgetEvent, fe_id)
    await _cascade_card_sources_on_forget(db_session, ev)

    # Card cited by both forgotten versions → archived.
    card_demote = await db_session.get(KnowledgeCard, card_demote_id)
    assert card_demote.card_status == "archived"
    assert str(fe_id) in card_demote.archived_reason

    # Unrelated card → approved.
    card_keep = await db_session.get(KnowledgeCard, card_keep_id)
    assert card_keep.card_status == "approved"


# ─── Test 6: archived_reason NEVER contains body content (privacy) ──────────


async def test_archived_reason_contains_no_body_content(db_session) -> None:
    """PHASE6_PLAN.md §5.A.5 privacy invariant: archived_reason carries only
    the forget_event_id reference, never quoted body content from the
    forgotten message."""
    from bot.db.models import ChatMessage, ForgetEvent, KnowledgeCard, MessageVersion
    from bot.services.forget_cascade import _cascade_card_sources_on_forget

    from sqlalchemy import update as sa_update

    secret = "SECRET_BODY_TEXT_THAT_MUST_NOT_LEAK"
    uid = await _make_user(db_session)
    msg = ChatMessage(
        message_id=_next_msg_id(),
        chat_id=_next_chat_id(),
        user_id=uid,
        text=secret,
        caption=secret,
        date=datetime.now(timezone.utc),
        memory_policy="normal",
        is_redacted=False,
    )
    db_session.add(msg)
    await db_session.flush()
    v = MessageVersion(
        chat_message_id=msg.id,
        version_seq=1,
        text=secret,
        normalized_text=secret,
        entities_json={"raw": secret},
        content_hash=f"h{uuid.uuid4().hex[:16]}",
        is_redacted=False,
    )
    db_session.add(v)
    await db_session.flush()
    await db_session.execute(
        sa_update(ChatMessage)
        .where(ChatMessage.id == msg.id)
        .values(current_version_id=v.id)
    )
    await db_session.flush()

    card_id = await _make_approved_card_with_sources(
        db_session, source_version_ids=[v.id]
    )

    fe_id = await _make_pending_forget_event(
        db_session, target_type="message", target_id=msg.id
    )
    ev = await db_session.get(ForgetEvent, fe_id)
    await _cascade_card_sources_on_forget(db_session, ev)

    card = await db_session.get(KnowledgeCard, card_id)
    assert card.card_status == "archived"
    assert secret not in (card.archived_reason or "")


# ─── Test 7: full cascade worker runs card_sources layer ─────────────────────


async def test_full_cascade_worker_runs_card_sources_layer(db_session) -> None:
    """End-to-end: a pending forget_event picked up by
    ``run_cascade_worker_once`` must complete the ``card_sources`` layer
    and (when all sources are forgotten) leave the card archived.

    This is integration-style: it verifies the new layer is wired into
    ``_LAYER_FUNCS`` and ``CASCADE_LAYER_ORDER`` correctly (i.e., the
    cascade worker actually invokes it without code-level bypass)."""
    from bot.db.models import KnowledgeCard
    from bot.db.repos.forget_event import ForgetEventRepo
    from bot.services.forget_cascade import run_cascade_worker_once

    cm_id, ver_id, chat_id, msg_id = await _make_chat_message_with_v1(db_session)
    card_id = await _make_approved_card_with_sources(
        db_session, source_version_ids=[ver_id]
    )

    fe_id = await _make_pending_forget_event(
        db_session, target_type="message", target_id=cm_id
    )

    stats = await run_cascade_worker_once(db_session)
    assert stats["claimed"] == 1
    assert stats["processed"] == 1
    assert stats["failed"] == 0

    # Cascade status records card_sources layer as completed.
    ev = await ForgetEventRepo.get_by_tombstone_key(
        db_session, await _tombstone_key_of(db_session, fe_id)
    )
    assert ev is not None
    assert ev.status == "completed"
    assert ev.cascade_status["card_sources"]["status"] == "completed"
    # rows >= 1 because at least one card_source row was deleted.
    assert ev.cascade_status["card_sources"]["rows"] >= 1

    # Card demoted.
    card = await db_session.get(KnowledgeCard, card_id)
    assert card.card_status == "archived"


async def _tombstone_key_of(db_session, fe_id: int) -> str:
    """Look up the tombstone_key of a freshly-created forget_event row."""
    from bot.db.models import ForgetEvent

    ev = await db_session.get(ForgetEvent, fe_id)
    return ev.tombstone_key
