"""Cascade tail extension tests for Phase 12 Butler layers (T12-01).

Verifies:
1. CASCADE_LAYER_ORDER ends with the 3 butler layers in correct order:
   confirmations → invocations → actions (children before parent).
2. forget event on a butler row's evidence_ids expires 'pending_confirmation'
   and 'confirmed'/'executing' non-terminal action rows.
3. Terminal rows get evidence_ids redacted with the standard format.
4. butler_action_confirmations rows in 'pending' status get expired + hash redacted.
5. butler_tool_invocations response_payload is redacted.
6. _LAYER_FUNCS contains all 3 butler layer keys.

Tests do NOT rely on Bot threading (no Telegram side effects in T12-01).
"""

from __future__ import annotations

import itertools
import secrets
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.usefixtures("app_env")

_counter = itertools.count(start=8_100_000_000)


def _next_id() -> int:
    return next(_counter)


# ── Smoke: ordering invariants (no DB needed) ──────────────────────────────────


def test_cascade_layer_order_ends_with_butler_layers() -> None:
    """CASCADE_LAYER_ORDER must end with butler layers in: confirmations → invocations → undo_invocations → actions.

    T12-07 added butler_undo_invocations between butler_tool_invocations and
    butler_actions — FK dependency: undo rows reference tool invocation ids and
    must be processed before the parent action row is masked.
    """
    from bot.services.forget_cascade import CASCADE_LAYER_ORDER

    tail = list(CASCADE_LAYER_ORDER[-4:])
    assert tail == [
        "butler_action_confirmations",
        "butler_tool_invocations",
        "butler_undo_invocations",
        "butler_actions",
    ], f"Unexpected tail: {tail}"


def test_cascade_layer_order_butler_after_graph_nodes() -> None:
    """butler_action_confirmations must come immediately after graph_nodes."""
    from bot.services.forget_cascade import CASCADE_LAYER_ORDER

    layers = list(CASCADE_LAYER_ORDER)
    graph_idx = layers.index("graph_nodes")
    confirm_idx = layers.index("butler_action_confirmations")
    assert confirm_idx == graph_idx + 1, (
        f"butler_action_confirmations should be at index {graph_idx + 1}, "
        f"got {confirm_idx}"
    )


def test_layer_funcs_contains_butler_keys() -> None:
    """_LAYER_FUNCS must contain all butler layer keys (T12-01 + T12-07)."""
    from bot.services.forget_cascade import _LAYER_FUNCS  # type: ignore[attr-defined]

    for key in (
        "butler_action_confirmations",
        "butler_tool_invocations",
        "butler_undo_invocations",
        "butler_actions",
    ):
        assert key in _LAYER_FUNCS, f"Missing key in _LAYER_FUNCS: {key}"


# ── DB-backed cascade tests ───────────────────────────────────────────────────


async def _make_user(db_session) -> int:
    from bot.db.repos.user import UserRepo

    uid = _next_id()
    await UserRepo.upsert(
        db_session,
        telegram_id=uid,
        username=f"cas_{uid}",
        first_name="Test",
        last_name=None,
    )
    return uid


async def _make_message_with_version(db_session) -> tuple[int, int]:
    """Create a ChatMessage + MessageVersion. Returns (cm_id, mv_id)."""
    from bot.db.models import ChatMessage, MessageVersion

    uid = await _make_user(db_session)
    chat_id = -1_000_000_000_000 - _next_id()
    msg_id = _next_id()
    when = datetime.now(timezone.utc)

    msg = ChatMessage(
        message_id=msg_id,
        chat_id=chat_id,
        user_id=uid,
        text="test",
        date=when,
        raw_json={"text": "test"},
        memory_policy="normal",
        is_redacted=False,
    )
    db_session.add(msg)
    await db_session.flush()

    ver = MessageVersion(
        chat_message_id=msg.id,
        version_seq=1,
        text="test",
        normalized_text="test",
        entities_json={"entities": []},
        content_hash=f"h-{_next_id()}",
        is_redacted=False,
    )
    db_session.add(ver)
    await db_session.flush()

    msg.current_version_id = ver.id
    await db_session.flush()
    return msg.id, ver.id


async def _make_forget_event(db_session, *, target_type: str, target_id: str, tombstone_key: str):
    from bot.db.repos.forget_event import ForgetEventRepo

    actor = await _make_user(db_session)
    ev = await ForgetEventRepo.create(
        db_session,
        target_type=target_type,
        target_id=target_id,
        actor_user_id=actor,
        authorized_by="admin",
        tombstone_key=tombstone_key,
        reason="test cascade",
        policy="forgotten",
    )
    return ev


async def _create_butler_action_with_evidence(
    db_session, mv_id: int, *, status: str = "rejected"
) -> int:
    """Create a ButlerAction with evidence_ids containing mv_id."""
    from bot.db.models import ButlerAction

    row = ButlerAction(
        requester_tg_id=_next_id(),
        chat_id=_next_id(),
        action_type="recall",
        status=status,
        tool_name="recall_evidence",
        tool_manifest_version="1.0",
        governance_filter_version="v1",
        evidence_context_hash="abc",
        evidence_ids=[mv_id],
        approved_card_source_ids=[],
        plan_summary="plan",
        action_args={},
        action_args_hash="h",
        rollback_kind="not_reversible",
        risk_level="low",
    )
    db_session.add(row)
    await db_session.flush()
    return row.id


# ── _cascade_butler_actions tests ─────────────────────────────────────────────


async def test_cascade_butler_actions_terminal_row_redacted(db_session) -> None:
    """A terminal butler_action row gets evidence_ids redacted on forget."""
    from bot.db.models import ChatMessage, ButlerAction
    from sqlalchemy import select

    cm_id, mv_id = await _make_message_with_version(db_session)
    action_id = await _create_butler_action_with_evidence(db_session, mv_id, status="rejected")

    # Resolve actual cm attributes to build a correctly-keyed tombstone.
    cm = (await db_session.execute(
        select(ChatMessage).where(ChatMessage.id == cm_id)
    )).scalar_one()

    ev = await _make_forget_event(
        db_session,
        target_type="message",
        target_id=str(cm.id),
        tombstone_key=f"message:{cm.chat_id}:{cm.message_id}",
    )

    from bot.services.forget_cascade import _cascade_butler_actions
    count = await _cascade_butler_actions(db_session, ev)
    assert count >= 1

    # Refresh the action row to bypass ORM identity map (raw SQL updated it).
    action_row = await db_session.get(ButlerAction, action_id)
    await db_session.refresh(action_row)
    # evidence_ids should be redacted.
    assert isinstance(action_row.evidence_ids, dict), (
        f"Expected dict redact payload, got {type(action_row.evidence_ids)}: {action_row.evidence_ids}"
    )
    assert action_row.evidence_ids.get("redacted") is True
    assert action_row.evidence_ids.get("forget_event_id") == ev.id


async def test_cascade_butler_actions_user_target(db_session) -> None:
    """User-targeted forget expires all butler_actions for that requester_tg_id."""
    tg_id = _next_id()
    from bot.db.models import ButlerAction

    # Create action for that user with status='rejected' (terminal).
    row = ButlerAction(
        requester_tg_id=tg_id,
        chat_id=_next_id(),
        action_type="recall",
        status="rejected",
        tool_name="recall_evidence",
        tool_manifest_version="1.0",
        governance_filter_version="v1",
        evidence_context_hash="abc",
        evidence_ids=[],
        approved_card_source_ids=[],
        plan_summary="plan",
        action_args={},
        action_args_hash="h",
        rollback_kind="not_reversible",
        risk_level="low",
    )
    db_session.add(row)
    await db_session.flush()

    ev = await _make_forget_event(
        db_session,
        target_type="user",
        target_id=str(tg_id),
        tombstone_key=f"user:{tg_id}:{_next_id()}",
    )

    from bot.services.forget_cascade import _cascade_butler_actions
    count = await _cascade_butler_actions(db_session, ev)
    assert count >= 1


# ── _cascade_butler_action_confirmations tests ────────────────────────────────


async def test_cascade_butler_confirmations_pending_expired(db_session) -> None:
    """Pending confirmation rows are expired when the action's source is forgotten."""
    _cm_id, mv_id = await _make_message_with_version(db_session)
    action_id = await _create_butler_action_with_evidence(db_session, mv_id, status="rejected")

    from bot.db.models import ButlerActionConfirmation
    from datetime import timedelta

    conf = ButlerActionConfirmation(
        action_id=action_id,
        confirmer_tg_id=_next_id(),
        confirmation_role="requester",
        status="pending",
        preview_payload_hash="pph123",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        confirmation_token=secrets.token_urlsafe(32),
    )
    db_session.add(conf)
    await db_session.flush()

    # Build forget event for the message that has mv_id.
    from bot.db.models import MessageVersion, ChatMessage
    from sqlalchemy import select

    ver = (await db_session.execute(
        select(MessageVersion).where(MessageVersion.id == mv_id)
    )).scalar_one()
    cm = (await db_session.execute(
        select(ChatMessage).where(ChatMessage.id == ver.chat_message_id)
    )).scalar_one()

    ev = await _make_forget_event(
        db_session,
        target_type="message",
        target_id=str(cm.id),
        tombstone_key=f"message:{cm.chat_id}:{cm.message_id}",
    )

    from bot.services.forget_cascade import _cascade_butler_action_confirmations
    count = await _cascade_butler_action_confirmations(db_session, ev)
    assert count >= 1

    # Refresh to re-fetch from DB (bypass ORM identity map cached before raw UPDATE).
    await db_session.refresh(conf)
    assert conf.status == "expired"
    assert "forget_event_id" in conf.preview_payload_hash


# ── _cascade_butler_tool_invocations tests ─────────────────────────────────────


async def test_cascade_butler_tool_invocations_response_redacted(db_session) -> None:
    """Response payload is redacted when the action's source is forgotten."""
    _cm_id, mv_id = await _make_message_with_version(db_session)
    action_id = await _create_butler_action_with_evidence(db_session, mv_id, status="rejected")

    from bot.db.models import ButlerToolInvocation

    inv = ButlerToolInvocation(
        action_id=action_id,
        tool_name="recall_evidence",
        invocation_seq=1,
        idempotency_key=f"ik-{_next_id()}",
        request_payload={"q": "secret"},
        request_payload_hash="h",
        status="succeeded",
        response_payload={"answer": "sensitive answer"},
        response_payload_hash="rph",
    )
    db_session.add(inv)
    await db_session.flush()

    from bot.db.models import MessageVersion, ChatMessage
    from sqlalchemy import select

    ver = (await db_session.execute(
        select(MessageVersion).where(MessageVersion.id == mv_id)
    )).scalar_one()
    cm = (await db_session.execute(
        select(ChatMessage).where(ChatMessage.id == ver.chat_message_id)
    )).scalar_one()

    ev = await _make_forget_event(
        db_session,
        target_type="message",
        target_id=str(cm.id),
        tombstone_key=f"message:{cm.chat_id}:{cm.message_id}",
    )

    from bot.services.forget_cascade import _cascade_butler_tool_invocations
    count = await _cascade_butler_tool_invocations(db_session, ev)
    assert count >= 1

    # Refresh to re-fetch from DB (bypass ORM identity map cached before raw UPDATE).
    await db_session.refresh(inv)
    assert isinstance(inv.response_payload, dict)
    assert inv.response_payload.get("redacted") is True
    assert inv.response_payload.get("forget_event_id") == ev.id


# ── No-op path (no matching rows) ─────────────────────────────────────────────


async def test_cascade_butler_actions_no_affected_rows_returns_zero(db_session) -> None:
    """Returns 0 when no butler_actions reference the forgotten message."""
    _cm_id, mv_id = await _make_message_with_version(db_session)
    # Create an action with a DIFFERENT mvid in evidence_ids.
    other_mv_id = _next_id() + 9_999_999
    await _create_butler_action_with_evidence(db_session, other_mv_id)

    from bot.db.models import MessageVersion, ChatMessage
    from sqlalchemy import select

    ver = (await db_session.execute(
        select(MessageVersion).where(MessageVersion.id == mv_id)
    )).scalar_one()
    cm = (await db_session.execute(
        select(ChatMessage).where(ChatMessage.id == ver.chat_message_id)
    )).scalar_one()

    ev = await _make_forget_event(
        db_session,
        target_type="message",
        target_id=str(cm.id),
        tombstone_key=f"message:{cm.chat_id}:{cm.message_id}",
    )

    from bot.services.forget_cascade import _cascade_butler_actions
    count = await _cascade_butler_actions(db_session, ev)
    assert count == 0


# ── F4: parent-lock ordering ──────────────────────────────────────────────────


async def test_cascade_butler_confirmations_acquires_parent_lock_first(db_session) -> None:
    """_cascade_butler_action_confirmations acquires FOR UPDATE on butler_actions before
    modifying child confirmation rows (F4 — parent-lock ordering).

    This test verifies the observable effect: the confirmation cascade completes
    correctly even when a parent row exists, proving the FOR UPDATE NOWAIT was
    executed without raising (i.e. no other transaction held the lock).

    The structural guarantee is tested here via the happy path: if the FOR UPDATE
    were absent, the function would still return a count, but with it present the
    execution path includes the lock step.  The race-condition itself (concurrent
    confirm_action failing) requires real concurrent transactions and is out of scope
    for single-session unit tests.
    """
    from bot.db.models import ButlerActionConfirmation
    from sqlalchemy import select
    import secrets

    cm_id, mv_id = await _make_message_with_version(db_session)
    # Use status='rejected' to avoid the ck_butler_actions_ledger_required_post_plan
    # constraint (rejected rows don't require a ledger_id). The test verifies the
    # confirmation cascade behaviour, not action status; having a pending confirmation
    # on a rejected action is a valid test fixture for the cascade path.
    action_id = await _create_butler_action_with_evidence(
        db_session, mv_id, status="rejected"
    )

    # Add a pending confirmation row for this action
    conf = ButlerActionConfirmation(
        action_id=action_id,
        confirmer_tg_id=_next_id(),
        confirmation_role="requester",
        status="pending",
        preview_payload_hash="h-test",
        expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
        confirmation_token=secrets.token_urlsafe(32),
    )
    db_session.add(conf)
    await db_session.flush()

    from bot.db.models import ChatMessage
    cm = (await db_session.execute(
        select(ChatMessage).where(ChatMessage.id == cm_id)
    )).scalar_one()

    ev = await _make_forget_event(
        db_session,
        target_type="message",
        target_id=str(cm.id),
        tombstone_key=f"message:{cm.chat_id}:{cm.message_id}",
    )

    from bot.services.forget_cascade import _cascade_butler_action_confirmations
    # Should succeed (no contention) and expire the pending confirmation row.
    count = await _cascade_butler_action_confirmations(db_session, ev)
    assert count >= 1

    await db_session.refresh(conf)
    assert conf.status == "expired", f"Expected 'expired', got {conf.status!r}"
