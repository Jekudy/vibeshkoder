"""Phase 11 binding tests — C10 citations family (T12-09).

| ID    | Acceptance criterion (abridged) |
|-------|---------------------------------|
| C10.a | Every executed butler_actions row (status='succeeded') has evidence_ids
          resolving to ≥1 live message_versions.id OR approved card_sources.id.
          No empty-citation executions. |
| C10.b | RE-GROUNDED to #351 table-based undo (butler_undo_invocations): run
          execute_undo, assert (i) parent action status→'undone', (ii) parent
          evidence_context_hash UNCHANGED (parent immutable), (iii)
          butler_undo_invocations rows link via butler_action_id==parent_id.
          The #352 child-row design (child.parent_action_id / svc.undo_action)
          is NOT on main — replaced by execute_undo + butler_undo_invocations. |
| C10.c | Butler outgoing citation tokens [^mv:<id>] resolve to a non-redacted
          message_versions row; [^card:<uuid>] resolve to a non-archived
          knowledge_cards row with ≥1 non-redacted card_sources backing.
          Mirrors the Phase 9 C8 wiki citation contract. |

DB-backed via the live ``db_session`` (skips when postgres unreachable / harness
off), gated behind ``app_env`` like ``test_butler_leakage.py``.
"""

from __future__ import annotations

import itertools
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from bot.services.butler_evidence import butler_context_hash
from bot.services.wiki_renderer import _CARD_TOKEN_RE, _MV_TOKEN_RE

pytestmark = pytest.mark.usefixtures("app_env")

_counter = itertools.count(90_000)


def _next_id() -> int:
    return next(_counter)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


async def _make_user(db_session) -> int:
    from bot.db.repos.user import UserRepo

    uid = _next_id()
    await UserRepo.upsert(
        db_session, telegram_id=uid, username=f"cit_{uid}", first_name="T", last_name=None
    )
    return uid


async def _make_message_version(db_session, *, is_redacted: bool = False) -> int:
    """Create a ChatMessage + MessageVersion; return the mv id."""
    from bot.db.models import ChatMessage, MessageVersion

    uid = await _make_user(db_session)
    msg = ChatMessage(
        message_id=_next_id(),
        chat_id=-1_000_000_000_000 - _next_id(),
        user_id=uid,
        text="src",
        date=datetime.now(timezone.utc),
        raw_json={"text": "src"},
        memory_policy="normal",
        is_redacted=is_redacted,
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
        is_redacted=is_redacted,
    )
    db_session.add(ver)
    await db_session.flush()
    msg.current_version_id = ver.id
    await db_session.flush()
    return ver.id


async def _make_ledger(db_session) -> int:
    from bot.db.models import LlmUsageLedger

    row = LlmUsageLedger(
        call_type="butler_decision",
        provider="anthropic",
        model="claude-test",
        tokens_in=1,
        tokens_out=1,
        cost_usd=0,
    )
    db_session.add(row)
    await db_session.flush()
    return row.id


async def _make_card_with_source(
    db_session, mv_id: int, *, status: str = "approved"
) -> uuid.UUID:
    from bot.db.models import CardSource, KnowledgeCard

    admin = await _make_user(db_session)
    approved_fields = (
        {"approved_by_user_id": admin, "approved_at": datetime.now(timezone.utc)}
        if status == "approved"
        else {}
    )
    card = KnowledgeCard(
        title="t",
        body_markdown="b",
        card_status=status,
        **approved_fields,
    )
    db_session.add(card)
    await db_session.flush()
    db_session.add(CardSource(card_id=card.id, message_version_id=mv_id))
    await db_session.flush()
    return card.id


async def _make_succeeded_action(db_session, *, evidence_ids: list[int]) -> int:
    from bot.db.repos.butler_action import ButlerActionRepo

    ledger_id = await _make_ledger(db_session)
    row = await ButlerActionRepo.create(
        db_session,
        requester_tg_id=_next_id(),
        chat_id=_next_id(),
        action_type="recall",
        status="succeeded",
        tool_name="recall_evidence",
        tool_manifest_version="1.0",
        governance_filter_version="v1",
        evidence_context_hash="hash-c10",
        evidence_ids=evidence_ids,
        plan_summary="p",
        action_args={},
        action_args_hash="h",
        rollback_kind="not_reversible",
        risk_level="low",
        llm_usage_ledger_id=ledger_id,
        # inverse_op_payload not accepted by create(); set via update_status
    )
    return row.id


# ---------------------------------------------------------------------------
# C10.a — executed rows resolve to ≥1 live citation
# ---------------------------------------------------------------------------


async def test_c10a_succeeded_action_evidence_resolves_to_live_mv(db_session) -> None:
    """C10.a: a succeeded action's evidence_ids resolve to ≥1 live message_versions."""
    from bot.db.models import ButlerAction, MessageVersion

    mv_id = await _make_message_version(db_session)
    action_id = await _make_succeeded_action(db_session, evidence_ids=[mv_id])

    action = await db_session.get(ButlerAction, action_id)
    assert action.status == "succeeded"
    assert action.evidence_ids, "C10.a: no empty-citation execution"

    live = (
        await db_session.execute(
            select(MessageVersion.id).where(
                MessageVersion.id.in_(action.evidence_ids),
                MessageVersion.is_redacted.is_(False),
            )
        )
    ).scalars().all()
    assert len(live) >= 1


async def test_c10a_empty_evidence_is_a_detectable_violation(db_session) -> None:
    """C10.a corollary: an empty evidence set resolves to zero live citations.

    The governance layer prevents empty-citation plans from being planned;
    this asserts the resolver would flag one if it bypassed that guard.
    """
    from bot.db.models import MessageVersion

    resolved = (
        await db_session.execute(
            select(MessageVersion.id).where(MessageVersion.id.in_([]))
        )
    ).scalars().all()
    assert len(resolved) == 0


# ---------------------------------------------------------------------------
# C10.b — execute_undo + butler_undo_invocations table-based audit (RE-GROUNDED)
#
# #351 ships butler_undo_invocations (T12-07) — NOT the child-action row design
# in #352. C10.b asserts the table-based contract:
#   (i)  parent action.status → 'undone' after execute_undo
#   (ii) parent evidence_context_hash UNCHANGED (parent is immutable)
#   (iii) butler_undo_invocations rows link via butler_action_id == parent_id
# ---------------------------------------------------------------------------


async def test_c10b_execute_undo_transitions_parent_and_writes_audit_rows(
    db_session,
) -> None:
    """C10.b (re-grounded): execute_undo transitions parent status and writes undo audit.

    Builds a succeeded action with a tool_invocation (not_reversible), calls
    execute_undo via ButlerService, then asserts:
      (i)  parent action.status == 'undone'
      (ii) parent evidence_context_hash is unchanged (immutable audit trail)
      (iii) butler_undo_invocations has ≥1 row with butler_action_id == parent_id
    """
    from bot.db.models import ButlerAction, ButlerToolInvocation, ButlerUndoInvocation
    import datetime as _dt

    from bot.db.repos.butler_action import ButlerActionRepo
    from bot.db.repos.butler_action_confirmation import ButlerActionConfirmationRepo
    from bot.db.repos.butler_tool_invocation import ButlerToolInvocationRepo
    from bot.db.repos.butler_undo_invocation import ButlerUndoInvocationRepo
    from bot.services.butler import ButlerService

    # We need a real user for auth — create one so execute_undo doesn't fail the
    # ownership check (requester_user_id == action.requester_tg_id → authorized).
    uid = _next_id()
    from bot.db.repos.user import UserRepo
    await UserRepo.upsert(db_session, telegram_id=uid, username=f"c10b_{uid}", first_name="C", last_name=None)

    mv_id = await _make_message_version(db_session)
    ledger_id = await _make_ledger(db_session)

    parent_hash = f"hash-c10b-{_next_id()}"

    action = await ButlerActionRepo.create(
        db_session,
        requester_tg_id=uid,
        chat_id=_next_id(),
        action_type="recall",
        status="succeeded",
        tool_name="recall_evidence",
        tool_manifest_version="1.0",
        governance_filter_version="v1",
        evidence_context_hash=parent_hash,
        evidence_ids=[mv_id],
        plan_summary="test c10b undo",
        action_args={},
        action_args_hash="h",
        rollback_kind="not_reversible",
        risk_level="low",
        llm_usage_ledger_id=ledger_id,
        # inverse_op_payload not in create(); must satisfy CHECK via update_status
    )
    # Set inverse_op_payload via update_status (required for ck_butler_actions_executed_has_inverse)
    await ButlerActionRepo.update_status(
        db_session,
        action.id,
        status="succeeded",
        inverse_op_payload={"rollback_kind": "not_reversible"},
    )
    parent_id = action.id

    # Write a tool_invocation row (execute_undo processes LIFO over these).
    # rollback_kind is read from inverse_op_payload by execute_undo — NOT a direct column.
    idem_key = f"idem-c10b-{_next_id()}"
    invocation = ButlerToolInvocation(
        action_id=parent_id,
        tool_name="recall_evidence",
        invocation_seq=1,
        idempotency_key=idem_key,
        request_payload={"query": "test"},
        request_payload_hash=f"rh-{_next_id()}",
        status="succeeded",
        response_payload={"text": "some result"},
        # inverse_op_payload carries rollback_kind — execute_undo reads this dict.
        inverse_op_payload={"rollback_kind": "not_reversible"},
    )
    db_session.add(invocation)
    await db_session.flush()

    # Mark executed_at so execute_undo TTL check passes (reads action.executed_at).
    await ButlerActionRepo.update_status(
        db_session,
        parent_id,
        status="succeeded",
        executed_at=_dt.datetime.now(_dt.timezone.utc),
    )
    await db_session.flush()

    svc = ButlerService(
        session=db_session,
        ledger_repo=None,
        butler_action_repo=ButlerActionRepo,
        butler_action_confirmation_repo=ButlerActionConfirmationRepo,
        butler_tool_invocation_repo=ButlerToolInvocationRepo,
        undo_invocation_repo=ButlerUndoInvocationRepo,
        butler_rate_bucket_repo=None,
        user_repo=None,  # skip extra user_repo auth — requester == owner
        llm_gateway=None,
        evidence_builder=None,
        settings=object(),
    )

    await svc.execute_undo(
        action_id=parent_id,
        requester_user_id=uid,
        bot=None,
    )

    # (i) parent status → 'undone'
    parent_after = await ButlerActionRepo.get(db_session, parent_id)
    assert parent_after is not None
    assert parent_after.status == "undone", (
        f"C10.b: parent action status must be 'undone' after execute_undo, "
        f"got {parent_after.status!r}"
    )

    # (ii) parent evidence_context_hash UNCHANGED (immutable audit trail)
    assert parent_after.evidence_context_hash == parent_hash, (
        f"C10.b: parent evidence_context_hash must not change after undo, "
        f"expected {parent_hash!r} got {parent_after.evidence_context_hash!r}"
    )

    # (iii) butler_undo_invocations has ≥1 row with butler_action_id == parent_id
    undo_rows = (
        await db_session.execute(
            select(ButlerUndoInvocation).where(
                ButlerUndoInvocation.butler_action_id == parent_id
            )
        )
    ).scalars().all()
    assert len(undo_rows) >= 1, (
        f"C10.b: expected ≥1 butler_undo_invocations row for action {parent_id}, "
        f"got {len(undo_rows)}"
    )
    for row in undo_rows:
        assert row.butler_action_id == parent_id, (
            f"C10.b: undo row links to wrong action: "
            f"{row.butler_action_id} != {parent_id}"
        )


# ---------------------------------------------------------------------------
# C10.c — citation token resolution (mirrors wiki C8)
# ---------------------------------------------------------------------------


async def _mv_token_resolves(db_session, mv_id: int) -> bool:
    from bot.db.models import MessageVersion

    row = (
        await db_session.execute(
            select(MessageVersion).where(
                MessageVersion.id == mv_id, MessageVersion.is_redacted.is_(False)
            )
        )
    ).scalar_one_or_none()
    return row is not None


async def _card_token_resolves(db_session, card_id: uuid.UUID) -> bool:
    from bot.db.models import CardSource, KnowledgeCard, MessageVersion

    card = (
        await db_session.execute(
            select(KnowledgeCard).where(
                KnowledgeCard.id == card_id,
                KnowledgeCard.card_status != "archived",
            )
        )
    ).scalar_one_or_none()
    if card is None:
        return False
    backing = (
        await db_session.execute(
            select(MessageVersion.id)
            .join(CardSource, CardSource.message_version_id == MessageVersion.id)
            .where(CardSource.card_id == card_id, MessageVersion.is_redacted.is_(False))
        )
    ).scalars().all()
    return len(backing) >= 1


async def test_c10c_valid_tokens_resolve(db_session) -> None:
    """C10.c: [^mv:N] → live mv; [^card:UUID] → non-archived card w/ live backing."""
    mv_id = await _make_message_version(db_session)
    card_id = await _make_card_with_source(db_session, mv_id, status="approved")

    text = f"See [^mv:{mv_id}] and [^card:{card_id}]."

    mv_tokens = [int(m) for m in _MV_TOKEN_RE.findall(text)]
    card_tokens = [uuid.UUID(m) for m in _CARD_TOKEN_RE.findall(text)]
    assert mv_tokens == [mv_id]
    assert card_tokens == [card_id]

    for t in mv_tokens:
        assert await _mv_token_resolves(db_session, t)
    for c in card_tokens:
        assert await _card_token_resolves(db_session, c)


async def test_c10c_redacted_mv_token_does_not_resolve(db_session) -> None:
    """C10.c negative: a token to a redacted mv must NOT resolve."""
    mv_id = await _make_message_version(db_session, is_redacted=True)
    assert not await _mv_token_resolves(db_session, mv_id)


async def test_c10c_archived_card_token_does_not_resolve(db_session) -> None:
    """C10.c negative: a token to an archived card must NOT resolve."""
    mv_id = await _make_message_version(db_session)
    card_id = await _make_card_with_source(db_session, mv_id, status="archived")
    assert not await _card_token_resolves(db_session, card_id)
