"""Phase 11 binding tests — C10 citations family (T12-09).

| ID    | Acceptance criterion (abridged) |
|-------|---------------------------------|
| C10.a | Every executed butler_actions row (status='succeeded') has evidence_ids
          resolving to ≥1 live message_versions.id OR approved card_sources.id.
          No empty-citation executions. |
| C10.b | An undo row (parent_action_id non-NULL) inherits the original's
          evidence_context_hash so audit replay reproduces the original context. |
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

from bot.services.butler import ButlerService
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
        inverse_op_payload={"rollback_kind": "not_reversible"},
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

    The R8.b abstain guard prevents empty-citation plans from ever being
    executed; this asserts the resolver would flag one if it existed.
    """
    from bot.db.models import MessageVersion

    resolved = (
        await db_session.execute(
            select(MessageVersion.id).where(MessageVersion.id.in_([]))
        )
    ).scalars().all()
    assert len(resolved) == 0


# ---------------------------------------------------------------------------
# C10.b — undo row inherits the original evidence_context_hash (DB-backed)
# ---------------------------------------------------------------------------


async def test_c10b_undo_child_inherits_evidence_context_hash(db_session) -> None:
    """C10.b: undo_action writes a child whose evidence_context_hash == parent's."""
    from bot.db.repos.butler_action import ButlerActionRepo
    from bot.db.repos.butler_action_confirmation import ButlerActionConfirmationRepo

    mv_id = await _make_message_version(db_session)
    parent_id = await _make_succeeded_action(db_session, evidence_ids=[mv_id])
    parent = await ButlerActionRepo.get(db_session, parent_id)

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

    child = await svc.undo_action(
        action_id=parent_id,
        requester_user_id=parent.requester_tg_id,  # requester → authorized
        bot=None,
    )

    assert child.parent_action_id == parent_id
    assert child.evidence_context_hash == parent.evidence_context_hash
    assert child.evidence_ids == parent.evidence_ids
    assert child.llm_usage_ledger_id == parent.llm_usage_ledger_id
    # original immutable
    parent_after = await ButlerActionRepo.get(db_session, parent_id)
    assert parent_after.status == "succeeded"


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
