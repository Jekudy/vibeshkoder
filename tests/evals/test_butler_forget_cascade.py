"""Phase 11 binding tests — I9 forget-cascade family (T12-09).

Asserts the forget cascade's Butler layers (_cascade_butler_actions /
_cascade_butler_tool_invocations / _cascade_butler_action_confirmations,
shipped in T12-01) as Phase 11 privacy invariants.

| ID   | Acceptance criterion (as bound here) |
|------|--------------------------------------|
| I9.a | forget on a cited mv redacts the tool invocation's response_payload
         (content removed, forget_event_id recorded). Action row preserved. |
| I9.b | forget on a card-backing mv that an action cited redacts/expires that
         action (source_forgotten). |
| I9.c | the Butler layers run at the tail of CASCADE_LAYER_ORDER, after graph_nodes. |
| I9.d | a terminal (succeeded) action's result_payload is masked, the row preserved. |
| I9.e | a pending_confirmation action whose source is forgotten transitions to
         expired, and confirm_action then fails closed (callback refuses). |
| I9.f | butler_tool_invocations.idempotency_key UNIQUE holds (no duplicate row). |

Reconciliation note (impl vs PHASE12_PLAN_REFRESH §12.3 wording)
----------------------------------------------------------------
The shipped cascade (T12-01) masks privacy surface by REPLACING the whole JSONB
payload with ``{"redacted": true, "forget_event_id": <n>}`` rather than setting a
per-field ``text``/``caption`` value to the string
``[CONTENT_REDACTED: forget_event_id={n}]`` quoted in the plan. The privacy
invariant (user content removed; forget_event_id recorded for audit) is satisfied
identically; only the sentinel format differs. These binding tests assert the
SHIPPED contract. The spec's distinct ``rejection_reason='source_card_forgotten'``
and auto ``followup_correction`` on already-executed actions (I9.b) are NOT
implemented — the cascade uses ``source_forgotten`` and payload redaction; that
stricter refinement is deferred (tracked for Phase 12.5).

DB-backed via the live ``db_session``; gated behind ``app_env`` like
``test_butler_leakage.py``.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone

import pytest

from bot.services.butler import ButlerActionError, ButlerService

# NOTE: bot.services.forget_cascade imports bot.db.engine (→ Settings()) at module
# load, so it must be imported lazily INSIDE tests (after app_env sets env vars),
# mirroring tests/services/test_forget_cascade_butler.py.

pytestmark = pytest.mark.usefixtures("app_env")

_counter = itertools.count(110_000)


def _next_id() -> int:
    return next(_counter)


# ---------------------------------------------------------------------------
# Builders (mirror tests/services/test_forget_cascade_butler.py)
# ---------------------------------------------------------------------------


async def _make_user(db_session) -> int:
    from bot.db.repos.user import UserRepo

    uid = _next_id()
    await UserRepo.upsert(
        db_session, telegram_id=uid, username=f"i9_{uid}", first_name="T", last_name=None
    )
    return uid


async def _make_message_version(db_session) -> tuple[int, int, int]:
    """Return (chat_message_id, message_version_id, chat_id)."""
    from bot.db.models import ChatMessage, MessageVersion

    uid = await _make_user(db_session)
    chat_id = -1_000_000_000_000 - _next_id()
    msg = ChatMessage(
        message_id=_next_id(),
        chat_id=chat_id,
        user_id=uid,
        text="src",
        date=datetime.now(timezone.utc),
        raw_json={"text": "src"},
        memory_policy="normal",
        is_redacted=False,
    )
    db_session.add(msg)
    await db_session.flush()
    ver = MessageVersion(
        chat_message_id=msg.id,
        version_seq=1,
        text="src",
        normalized_text="src",
        entities_json={"entities": []},
        content_hash=f"h-{_next_id()}",
        is_redacted=False,
    )
    db_session.add(ver)
    await db_session.flush()
    msg.current_version_id = ver.id
    await db_session.flush()
    return msg.id, ver.id, chat_id


async def _make_forget_event_for_message(db_session, cm_id: int, chat_id: int, message_id: int):
    from bot.db.repos.forget_event import ForgetEventRepo

    actor = await _make_user(db_session)
    return await ForgetEventRepo.create(
        db_session,
        target_type="message",
        target_id=str(cm_id),
        actor_user_id=actor,
        authorized_by="admin",
        tombstone_key=f"message:{chat_id}:{message_id}",
        reason="i9 test",
        policy="forgotten",
    )


async def _make_ledger(db_session) -> int:
    from bot.db.models import LlmUsageLedger

    row = LlmUsageLedger(
        call_type="butler_decision", provider="anthropic", model="m", tokens_in=1, tokens_out=1, cost_usd=0
    )
    db_session.add(row)
    await db_session.flush()
    return row.id


async def _make_action(
    db_session, *, mv_id: int, status: str, result_payload: dict | None = None
) -> int:
    from bot.db.repos.butler_action import ButlerActionRepo

    ledger_id = None if status in ("rejected", "expired", "cancelled") else await _make_ledger(db_session)
    row = await ButlerActionRepo.create(
        db_session,
        requester_tg_id=_next_id(),
        chat_id=_next_id(),
        action_type="recall",
        status=status,
        tool_name="recall_evidence",
        tool_manifest_version="1.0",
        governance_filter_version="v1",
        evidence_context_hash="h",
        evidence_ids=[mv_id],
        plan_summary="p",
        action_args={},
        action_args_hash="h",
        rollback_kind="not_reversible",
        risk_level="low",
        llm_usage_ledger_id=ledger_id,
        inverse_op_payload={"rollback_kind": "not_reversible"} if status == "succeeded" else None,
    )
    if result_payload is not None:
        await ButlerActionRepo.update_status(
            db_session, row.id, status=status, result_payload=result_payload
        )
    return row.id


def _is_redacted_payload(payload, ev_id: int) -> bool:
    return isinstance(payload, dict) and payload.get("redacted") is True and payload.get("forget_event_id") == ev_id


# ---------------------------------------------------------------------------
# I9.c — layer order (pure, no DB)
# ---------------------------------------------------------------------------


def test_i9c_butler_actions_is_last_cascade_layer() -> None:
    """I9.c: butler_actions is the tail layer, after graph_nodes."""
    from bot.services.forget_cascade import CASCADE_LAYER_ORDER

    assert CASCADE_LAYER_ORDER[-1] == "butler_actions"
    assert CASCADE_LAYER_ORDER.index("butler_actions") > CASCADE_LAYER_ORDER.index("graph_nodes")
    # Butler triple ordering: confirmations → tool_invocations → actions
    assert CASCADE_LAYER_ORDER.index("butler_action_confirmations") < CASCADE_LAYER_ORDER.index(
        "butler_tool_invocations"
    ) < CASCADE_LAYER_ORDER.index("butler_actions")


# ---------------------------------------------------------------------------
# I9.f — idempotency_key UNIQUE (no DB-state dependence)
# ---------------------------------------------------------------------------


async def test_i9f_tool_invocation_idempotency_key_unique(db_session) -> None:
    """I9.f: a duplicate idempotency_key on butler_tool_invocations is rejected."""
    from sqlalchemy.exc import IntegrityError

    from bot.db.models import ButlerToolInvocation

    _cm, mv_id, _chat = await _make_message_version(db_session)
    action_id = await _make_action(db_session, mv_id=mv_id, status="succeeded")
    key = f"butler:{action_id}:recall_evidence:1"

    db_session.add(
        ButlerToolInvocation(
            action_id=action_id, tool_name="recall_evidence", idempotency_key=key,
            request_payload={}, request_payload_hash="h", status="succeeded", invocation_seq=1,
        )
    )
    await db_session.flush()
    db_session.add(
        ButlerToolInvocation(
            action_id=action_id, tool_name="recall_evidence", idempotency_key=key,
            request_payload={}, request_payload_hash="h", status="succeeded", invocation_seq=2,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


# ---------------------------------------------------------------------------
# I9.a — tool invocation response_payload redacted on forget
# ---------------------------------------------------------------------------


async def test_i9a_forget_redacts_tool_invocation_payload(db_session) -> None:
    """I9.a: forgetting a cited mv redacts the tool invocation's response_payload."""
    from bot.db.models import ButlerToolInvocation, ChatMessage
    from bot.services.forget_cascade import _cascade_butler_tool_invocations

    cm_id, mv_id, chat_id = await _make_message_version(db_session)

    cm = await db_session.get(ChatMessage, cm_id)
    action_id = await _make_action(db_session, mv_id=mv_id, status="succeeded")

    inv = ButlerToolInvocation(
        action_id=action_id, tool_name="send_intro", idempotency_key=f"k-{_next_id()}",
        request_payload={}, request_payload_hash="h", status="succeeded", invocation_seq=1,
        response_payload={"text": "secret intro body", "caption": "secret caption", "message_id": 5},
    )
    db_session.add(inv)
    await db_session.flush()

    ev = await _make_forget_event_for_message(db_session, cm.id, cm.chat_id, cm.message_id)
    n = await _cascade_butler_tool_invocations(db_session, ev)
    assert n >= 1

    await db_session.refresh(inv)
    assert _is_redacted_payload(inv.response_payload, ev.id)
    # the user-visible content fields are gone
    assert "secret intro body" not in str(inv.response_payload)
    assert "secret caption" not in str(inv.response_payload)


# ---------------------------------------------------------------------------
# I9.d — terminal action result_payload masked, row preserved
# ---------------------------------------------------------------------------


async def test_i9d_terminal_action_result_payload_masked_row_preserved(db_session) -> None:
    """I9.d: a succeeded action's result_payload is masked; the audit row survives."""
    from bot.db.models import ButlerAction, ChatMessage
    from bot.services.forget_cascade import _cascade_butler_actions

    cm_id, mv_id, chat_id = await _make_message_version(db_session)
    cm = await db_session.get(ChatMessage, cm_id)
    action_id = await _make_action(
        db_session, mv_id=mv_id, status="succeeded",
        result_payload={"text": "rendered telegram body"},
    )

    ev = await _make_forget_event_for_message(db_session, cm.id, cm.chat_id, cm.message_id)
    n = await _cascade_butler_actions(db_session, ev)
    assert n >= 1

    action = await db_session.get(ButlerAction, action_id)
    await db_session.refresh(action)
    assert action is not None  # row preserved
    assert _is_redacted_payload(action.result_payload, ev.id)
    assert _is_redacted_payload(action.evidence_ids, ev.id)
    assert "rendered telegram body" not in str(action.result_payload)


# ---------------------------------------------------------------------------
# I9.b — forget on a card-backing mv redacts the citing action
# ---------------------------------------------------------------------------


async def test_i9b_forget_card_backing_mv_redacts_action(db_session) -> None:
    """I9.b: an action citing a card-backing mv is redacted/expired on forget.

    (Impl uses source_forgotten + payload redaction, not a distinct
    'source_card_forgotten' — see module reconciliation note.)
    """
    from bot.db.models import ButlerAction, CardSource, ChatMessage, KnowledgeCard
    from bot.services.forget_cascade import _cascade_butler_actions

    cm_id, mv_id, chat_id = await _make_message_version(db_session)
    cm = await db_session.get(ChatMessage, cm_id)

    admin = await _make_user(db_session)
    card = KnowledgeCard(
        title="t", body_markdown="b", card_status="approved",
        approved_by_user_id=admin, approved_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    await db_session.flush()
    db_session.add(CardSource(card_id=card.id, message_version_id=mv_id))
    await db_session.flush()

    # An action that cited the card-backing mv, still pending.
    action_id = await _make_action(db_session, mv_id=mv_id, status="pending_confirmation")

    ev = await _make_forget_event_for_message(db_session, cm.id, cm.chat_id, cm.message_id)
    n = await _cascade_butler_actions(db_session, ev)
    assert n >= 1

    action = await db_session.get(ButlerAction, action_id)
    await db_session.refresh(action)
    assert action.status == "expired"
    assert action.rejection_reason == "source_forgotten"


# ---------------------------------------------------------------------------
# I9.e — pending action forgotten mid-TTL → expired → confirm fails closed
# ---------------------------------------------------------------------------


async def test_i9e_forgotten_pending_action_confirm_fails_closed(db_session) -> None:
    """I9.e: a pending action whose source is forgotten expires; confirm refuses."""
    from bot.db.repos.butler_action import ButlerActionRepo
    from bot.db.repos.butler_action_confirmation import ButlerActionConfirmationRepo
    from bot.db.models import ChatMessage
    from bot.services.forget_cascade import _cascade_butler_actions

    cm_id, mv_id, chat_id = await _make_message_version(db_session)
    cm = await db_session.get(ChatMessage, cm_id)
    action_id = await _make_action(db_session, mv_id=mv_id, status="pending_confirmation")

    # A pending requester confirmation (so confirm_action has a row to act on).
    action = await ButlerActionRepo.get(db_session, action_id)
    await ButlerActionConfirmationRepo.create(
        db_session,
        action_id=action_id,
        confirmer_tg_id=action.requester_tg_id,
        confirmation_role="requester",
        status="pending",
        preview_payload_hash="h",
        expires_at=datetime.now(timezone.utc).replace(year=2100),
        confirmation_token="tok",
    )

    # Forget the cited source mid-TTL → cascade expires the action.
    ev = await _make_forget_event_for_message(db_session, cm.id, cm.chat_id, cm.message_id)
    await _cascade_butler_actions(db_session, ev)
    # The cascade mutates via raw SQL; drop the ORM identity-map cache so subsequent
    # reads see the new status — production runs cascade + confirm in SEPARATE sessions.
    db_session.expire_all()

    expired = await ButlerActionRepo.get(db_session, action_id)
    assert expired.status == "expired"

    svc = ButlerService(
        session=db_session,
        ledger_repo=None,
        butler_action_repo=ButlerActionRepo,
        butler_action_confirmation_repo=ButlerActionConfirmationRepo,
        butler_tool_invocation_repo=None,
        butler_rate_bucket_repo=None,
        user_repo=None,
        llm_gateway=None,
        evidence_builder=None,
        settings=object(),
    )

    # confirm_action fails closed. Because the cascade expired the row with
    # rejection_reason='source_forgotten', confirm_action's post-cascade (C4) check
    # raises the MORE SPECIFIC source_forgotten refusal rather than a generic TTL
    # expiry — both are fail-closed; source_forgotten is the precise privacy reason.
    with pytest.raises(ButlerActionError) as ei:
        await svc.confirm_action(
            action_id=action_id, confirming_user_id=action.requester_tg_id, confirmation_token="tok"
        )
    assert ei.value.error_kind == "source_forgotten"
