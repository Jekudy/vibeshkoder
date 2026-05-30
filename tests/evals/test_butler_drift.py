"""Phase 11 binding tests — G3 drift / invariant-binding family (T12-09).

G3.a lives in ``tests/evals/test_no_llm_imports.py`` (the AST per-path scan,
which needs no DB). G3.b/c/d are DB-backed and live here.

| ID   | Acceptance criterion |
|------|----------------------|
| G3.b | ``butler_actions.evidence_context_hash`` is stable across replays —
         recomputed via the canonical ``butler_context_hash(bundle,
         visibility_scope, governance_filter_version)`` helper, byte-equal to
         the stored hash. Input includes per-item source_type + message_version_id
         + card_id + sorted card_source_message_version_ids, NOT just the
         flattened evidence_ids list. |
| G3.c | NO ``butler_actions`` row exists in a post-plan status without a linked
         ``llm_usage_ledger`` row. The DB CHECK
         ``ck_butler_actions_ledger_required_post_plan`` enforces this at write
         time; G3.c asserts the constraint rejects a NULL-ledger post-plan insert. |
| G3.d | NO ``butler_tool_invocations`` row exists for a tool_name not in
         ``ALLOWED_BUTLER_TOOLS``. The DB CHECK
         ``ck_butler_tool_invocations_tool_name`` enforces this; G3.d asserts the
         constraint is present and active. |

Gated behind the eval harness (``app_env`` + ``db_session``) exactly like
``tests/evals/test_butler_leakage.py``. DB-backed sub-cases skip when postgres
is unreachable (handled by the ``db_session`` fixture).
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.butler_evidence import ButlerEvidenceContext, butler_context_hash
from bot.services.butler_tools import ALLOWED_BUTLER_TOOLS
from bot.services.evidence import EvidenceBundle, EvidenceItem

pytestmark = pytest.mark.usefixtures("app_env")

_counter = itertools.count(70_000)


def _next_id() -> int:
    return next(_counter)


# ---------------------------------------------------------------------------
# G3.b — evidence_context_hash stability across replay
# ---------------------------------------------------------------------------


def _make_item(mvid: int, *, card_id: int | None = None) -> EvidenceItem:
    return EvidenceItem(
        message_version_id=mvid,
        chat_message_id=mvid + 1000,
        chat_id=-100_500,
        message_id=mvid + 2000,
        user_id=55,
        snippet="snippet",
        ts_rank=0.5,
        captured_at=datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
        message_date=datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
        source_type="card" if card_id is not None else "message",
        card_id=card_id,
        card_source_message_version_ids=(mvid, mvid + 1) if card_id is not None else (),
    )


def test_g3b_evidence_context_hash_stable_across_replay() -> None:
    """G3.b: recomputing the canonical hash from the same bundle is byte-equal.

    Pure-function invariant — no DB needed. Mixes a message item and a card item
    so the per-item source_type / card_id / card_source_message_version_ids all
    participate in the hash (not just the flattened evidence_ids list).
    """
    items = (_make_item(10), _make_item(11, card_id=900))
    bundle = EvidenceBundle(
        query="who knows Rust?",
        chat_id=-100_500,
        items=items,
        abstained=False,
        created_at=datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
    )
    h1 = butler_context_hash(bundle, "member", "gov-v1")
    h2 = butler_context_hash(bundle, "member", "gov-v1")
    assert h1 == h2  # byte equality across replay


def test_g3b_hash_sensitive_to_card_source_ids() -> None:
    """G3.b corollary: the hash changes when card_source_message_version_ids differ.

    Guards against a hash that flattens to evidence_ids only — two bundles with
    the same top-level mvids but different card backing must hash differently.
    """
    base_card = _make_item(11, card_id=900)
    other_card = EvidenceItem(
        message_version_id=11,
        chat_message_id=1011,
        chat_id=-100_500,
        message_id=2011,
        user_id=55,
        snippet="snippet",
        ts_rank=0.5,
        captured_at=datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
        message_date=datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
        source_type="card",
        card_id=900,
        card_source_message_version_ids=(99, 98),  # different backing set
    )

    def _bundle(card_item: EvidenceItem) -> EvidenceBundle:
        return EvidenceBundle(
            query="q",
            chat_id=-100_500,
            items=(_make_item(10), card_item),
            abstained=False,
            created_at=datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
        )

    h_base = butler_context_hash(_bundle(base_card), "member", "gov-v1")
    h_other = butler_context_hash(_bundle(other_card), "member", "gov-v1")
    assert h_base != h_other


def test_g3b_stored_hash_matches_recompute() -> None:
    """G3.b: a hash stored at plan time recomputes byte-equal from the same context."""
    items = (_make_item(10), _make_item(11, card_id=900))
    bundle = EvidenceBundle(
        query="q",
        chat_id=-100_500,
        items=items,
        abstained=False,
        created_at=datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
    )
    stored = butler_context_hash(bundle, "admin", "gov-v2")
    ctx = ButlerEvidenceContext(
        bundle=bundle,
        visibility_scope="admin",
        context_hash=stored,
        governance_filter_version="gov-v2",
        requester_user_id=42,
        chat_id=-100_500,
        query="q",
        snapshot_at=datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
        governance_excluded_count=0,
    )
    recompute = butler_context_hash(
        ctx.bundle, ctx.visibility_scope, ctx.governance_filter_version
    )
    assert recompute == ctx.context_hash


# ---------------------------------------------------------------------------
# G3.c — ledger required for post-plan butler_actions (CHECK enforced)
# ---------------------------------------------------------------------------


async def _butler_action_kwargs(**overrides):
    base = dict(
        requester_tg_id=_next_id(),
        chat_id=_next_id(),
        action_type="recall",
        status="succeeded",
        tool_name="recall_evidence",
        tool_manifest_version="1.0",
        governance_filter_version="v1",
        evidence_context_hash="abc",
        evidence_ids=[1],
        approved_card_source_ids=[],
        plan_summary="plan",
        action_args={},
        action_args_hash="h",
        rollback_kind="not_reversible",
        risk_level="low",
    )
    base.update(overrides)
    return base


@pytest.mark.parametrize("status", ["planned", "pending_confirmation", "confirmed", "executing", "succeeded"])
async def test_g3c_post_plan_status_requires_ledger(db_session: AsyncSession, status: str) -> None:
    """G3.c: inserting a post-plan butler_actions row with NULL ledger is rejected."""
    from bot.db.models import ButlerAction

    row = ButlerAction(**await _butler_action_kwargs(status=status, llm_usage_ledger_id=None))
    db_session.add(row)
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.parametrize("status", ["rejected", "expired", "cancelled"])
async def test_g3c_terminal_status_allows_null_ledger(db_session: AsyncSession, status: str) -> None:
    """G3.c corollary: terminal (pre-plan failure) statuses may have NULL ledger."""
    from bot.db.models import ButlerAction

    row = ButlerAction(**await _butler_action_kwargs(status=status, llm_usage_ledger_id=None))
    db_session.add(row)
    await db_session.flush()  # must NOT raise
    assert row.id is not None


# ---------------------------------------------------------------------------
# G3.d — tool_name whitelist CHECK on butler_tool_invocations
# ---------------------------------------------------------------------------


async def test_g3d_tool_invocation_rejects_unknown_tool_name(db_session: AsyncSession) -> None:
    """G3.d: a butler_tool_invocations row with a non-whitelisted tool_name is rejected."""
    from bot.db.models import ButlerAction, ButlerToolInvocation

    # A valid parent action (succeeded → needs a ledger row).
    from bot.db.models import LlmUsageLedger

    ledger = LlmUsageLedger(
        call_type="butler_decision",
        provider="anthropic",
        model="claude-test",
        tokens_in=1,
        tokens_out=1,
        cost_usd=0,
    )
    db_session.add(ledger)
    await db_session.flush()

    action = ButlerAction(**await _butler_action_kwargs(llm_usage_ledger_id=ledger.id))
    db_session.add(action)
    await db_session.flush()

    bad = ButlerToolInvocation(
        action_id=action.id,
        tool_name="rm_minus_rf",  # NOT in ALLOWED_BUTLER_TOOLS
        idempotency_key=f"k-{_next_id()}",
        request_payload={},
        request_payload_hash="h",
        status="pending",
        invocation_seq=1,
    )
    db_session.add(bad)
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


def test_g3d_allowed_tools_is_the_five_whitelist() -> None:
    """G3.d: the Python whitelist matches the documented 5 tools (drift guard)."""
    assert ALLOWED_BUTLER_TOOLS == frozenset(
        {
            "recall_evidence",
            "schedule_meeting",
            "send_intro",
            "update_intro",
            "suggest_card_creation",
        }
    )
