"""Phase 11 binding tests — R8 refusal family (T12-09).

| ID   | Acceptance criterion (abridged) |
|------|---------------------------------|
| R8.a | Non-member /butler rejected at the handler/membership layer: NO evidence
         context, NO LLM call, NO butler_actions row. |
| R8.b | BLOCKED — pending orchestrator decision on empty_evidence behavior.
         Not built in this pass. |
| R8.c | Refuse to execute a cross-user action when the affected user's confirmation
         row is not 'confirmed'. Refusal before any Telegram side effect. |
| R8.d | Butler does not consume graph_query in baseline (transitive graph-purge
         read-block); asserted via the G3.a per-path import scan. |
| R8.e | Refuse to confirm/execute a pending_confirmation action past its TTL. |
| R8.f | Refuse a ButlerPlan whose tool_name is not in ALLOWED_BUTLER_TOOLS. |
| R8.g | Refuse a ButlerPlan whose args fail the tool's pydantic args model. |

R8.f/g and the membership/state-machine cases use in-memory fakes (no DB, no LLM).
Gated behind the eval harness (``app_env``) like ``test_butler_leakage.py``.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from bot.services.butler import (
    ButlerActionError,
    ButlerActionExpiredError,
    ButlerActionRejectedError,
    ButlerService,
    MembershipRevokedError,
)
from bot.services.butler_evidence import ButlerEvidenceContext, butler_context_hash
from bot.services.butler_tools import (
    ButlerActionStep,
    ButlerPlan,
    InvalidToolArgsError,
    ToolNotAllowedError,
    validate_butler_plan,
)
from bot.services.evidence import EvidenceBundle, EvidenceItem

pytestmark = pytest.mark.usefixtures("app_env")

_counter = itertools.count(70_000)


def _next_id() -> int:
    return next(_counter)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_evidence_item() -> EvidenceItem:
    """Build a minimal EvidenceItem with all required fields."""
    mvid = _next_id()
    return EvidenceItem(
        message_version_id=mvid,
        chat_message_id=mvid + 1000,
        chat_id=-100_500,
        message_id=mvid + 2000,
        user_id=55,
        snippet="snippet",
        ts_rank=0.9,
        captured_at=datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
        message_date=datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
        source_type="message",
        card_id=None,
        card_source_message_version_ids=(),
    )


def _bundle(*, abstained: bool) -> EvidenceBundle:
    items: tuple = () if abstained else (_make_evidence_item(),)
    return EvidenceBundle(
        query="who knows Rust?",
        chat_id=-100_500,
        items=items,
        abstained=abstained,
        created_at=datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
    )


def _context(*, abstained: bool, requester: int = 42) -> ButlerEvidenceContext:
    b = _bundle(abstained=abstained)
    return ButlerEvidenceContext(
        bundle=b,
        visibility_scope="member",
        context_hash=butler_context_hash(b, "member", "gov-v1"),
        governance_filter_version="gov-v1",
        requester_user_id=requester,
        chat_id=-100_500,
        query="who knows Rust?",
        snapshot_at=datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
        governance_excluded_count=0,
    )


class _FakeEvidenceBuilder:
    def __init__(self, ctx: ButlerEvidenceContext) -> None:
        self._ctx = ctx

    async def build_butler_evidence(self, **_kw: Any) -> ButlerEvidenceContext:
        return self._ctx


class _ExplodingGateway:
    """Gateway that fails the test if the Butler ever reaches the LLM call."""

    async def plan_butler_action(self, **_kw: Any) -> Any:
        raise AssertionError("plan_butler_action must NOT be called on refusal path")


class _FakeUserRepo:
    def __init__(self, user: Any) -> None:
        self._user = user

    async def get(self, _session: Any, _uid: int) -> Any:
        return self._user


@dataclass
class _FakeAction:
    id: int
    requester_tg_id: int
    chat_id: int
    status: str
    action_type: str = "recall"
    tool_name: str = "recall_evidence"
    rollback_kind: str = "not_reversible"
    evidence_context_hash: str = ""
    query: str = "q"
    visibility_scope: str = "member"
    plan_payload: dict = field(default_factory=dict)
    expires_at: datetime | None = None
    confirmed_at: datetime | None = None
    rejection_reason: str | None = None


class _FakeActionRepo:
    def __init__(self) -> None:
        self.rows: dict[int, _FakeAction] = {}
        self._id = 1

    async def create(self, _session: Any, **kw: Any) -> _FakeAction:
        row = _FakeAction(
            id=self._id,
            requester_tg_id=kw["requester_tg_id"],
            chat_id=kw["chat_id"],
            status=kw["status"],
            action_type=kw.get("action_type", "recall"),
            tool_name=kw.get("tool_name", "recall_evidence"),
            evidence_context_hash=kw.get("evidence_context_hash", ""),
            rejection_reason=kw.get("rejection_reason"),
            expires_at=kw.get("expires_at"),
            plan_payload=kw.get("plan_payload") or {},
        )
        self.rows[self._id] = row
        self._id += 1
        return row

    async def get(self, _session: Any, action_id: int) -> _FakeAction | None:
        return self.rows.get(action_id)

    async def get_for_update(self, _session: Any, action_id: int) -> _FakeAction | None:
        return self.rows.get(action_id)

    async def update_status(self, _session: Any, action_id: int, *, status: str, **kw: Any) -> int:
        row = self.rows[action_id]
        row.status = status
        if kw.get("rejection_reason") is not None:
            row.rejection_reason = kw["rejection_reason"]
        return 1


class _FakeConfirmationRepo:
    def __init__(self, confs: list[Any] | None = None) -> None:
        self._confs = confs or []

    async def list_for_action(self, _session: Any, action_id: int) -> list[Any]:
        return [c for c in self._confs if c.action_id == action_id]


class _FakeRateBucketRepo:
    def __init__(self) -> None:
        self.increments = 0
        self.decrements = 0

    async def try_increment(self, *_a: Any, **_kw: Any) -> bool:
        self.increments += 1
        return True

    async def decrement(self, *_a: Any, **_kw: Any) -> None:
        self.decrements += 1


class _Settings:
    butler_plan_ttl_seconds = 900
    butler_confirmation_ttl_seconds = 300
    user_plans_day_ceiling = 10
    user_execs_day_ceiling = 5
    chat_actions_day_ceiling = 50
    tool_hour_ceiling = 20
    # huge ceiling → budget never the cause of refusal in these tests
    butler_per_user_daily_usd_ceiling = 10_000


class _FakeResult:
    """Minimal result proxy for _FakeSession.execute()."""

    def scalar_one_or_none(self) -> None:
        return None  # SUM → NULL → Decimal("0") spend → budget always OK


class _FakeSession:
    """Minimal async session that satisfies the budget query path.

    Returns None from execute() scalar so the budget check sees zero spend
    and never triggers budget_exceeded — budget is not the subject of R8.b.
    """

    async def execute(self, stmt: Any, params: Any = None) -> "_FakeResult":
        return _FakeResult()


# ---------------------------------------------------------------------------
# R8.f / R8.g — plan validation (pure, no DB, no LLM)
# ---------------------------------------------------------------------------


def _plan_with_step(step: ButlerActionStep) -> ButlerPlan:
    return ButlerPlan(
        plan_summary="s",
        evidence_ids=(10,),
        actions=(step,),
        evidence_context_hash="h",
        requester_user_id=42,
        chat_id=-100_500,
        visibility_scope="member",
        governance_filter_version="gov-v1",
    )


def test_r8f_refuses_non_whitelisted_tool() -> None:
    """R8.f: a tool_name outside ALLOWED_BUTLER_TOOLS is rejected by validation."""
    step = ButlerActionStep(tool_name="rm_minus_rf", args={})
    with pytest.raises(ToolNotAllowedError) as ei:
        validate_butler_plan(_plan_with_step(step))
    assert ei.value.error_kind == "tool_not_allowed"


def test_r8g_refuses_bad_args() -> None:
    """R8.g: args that fail the tool's pydantic model are rejected by validation."""
    # send_intro requires target_user_id:int + intro_text:str; omit both.
    step = ButlerActionStep(tool_name="send_intro", args={"target_user_id": "not-an-int"})
    with pytest.raises(InvalidToolArgsError) as ei:
        validate_butler_plan(_plan_with_step(step))
    assert ei.value.error_kind == "invalid_args"


# ---------------------------------------------------------------------------
# R8.a — membership refusal (no evidence, no LLM, no action row beyond audit)
# ---------------------------------------------------------------------------


async def test_r8a_non_member_refused_before_evidence_and_llm() -> None:
    """R8.a: a non-member never reaches evidence build or the LLM gateway."""
    builder_called = {"n": 0}

    class _SpyBuilder:
        async def build_butler_evidence(self, **_kw: Any) -> Any:
            builder_called["n"] += 1
            raise AssertionError("evidence must not be built for a non-member")

    svc = ButlerService(
        session=object(),
        ledger_repo=None,
        butler_action_repo=_FakeActionRepo(),
        butler_action_confirmation_repo=_FakeConfirmationRepo(),
        butler_tool_invocation_repo=None,
        butler_rate_bucket_repo=_FakeRateBucketRepo(),
        user_repo=_FakeUserRepo(SimpleNamespace(is_member=False, is_admin=False)),
        llm_gateway=_ExplodingGateway(),
        evidence_builder=_SpyBuilder(),
        settings=_Settings(),
    )

    with pytest.raises(MembershipRevokedError) as ei:
        await svc.plan_action(
            requester_user_id=_next_id(),
            chat_id=None,
            query="who knows Rust?",
            visibility_scope="member",
        )
    assert ei.value.error_kind == "membership_revoked"
    assert builder_called["n"] == 0


# ---------------------------------------------------------------------------
# R8.b — empty/abstained evidence bundle stops plan_action before the LLM call
# ---------------------------------------------------------------------------


async def test_r8b_abstained_bundle_raises_empty_evidence_before_llm() -> None:
    """R8.b: plan_action with an abstained evidence bundle raises ButlerActionRejectedError
    (error_kind='empty_evidence') and never calls the LLM gateway."""
    action_repo = _FakeActionRepo()

    svc = ButlerService(
        session=_FakeSession(),
        ledger_repo=None,
        butler_action_repo=action_repo,
        butler_action_confirmation_repo=_FakeConfirmationRepo(),
        butler_tool_invocation_repo=None,
        butler_rate_bucket_repo=_FakeRateBucketRepo(),
        user_repo=_FakeUserRepo(SimpleNamespace(is_member=True, is_admin=False)),
        llm_gateway=_ExplodingGateway(),
        evidence_builder=_FakeEvidenceBuilder(_context(abstained=True)),
        settings=_Settings(),
    )

    with pytest.raises(ButlerActionError) as ei:
        await svc.plan_action(
            requester_user_id=_next_id(),
            chat_id=None,
            query="who knows Rust?",
            visibility_scope="member",
        )

    exc = ei.value
    assert exc.error_kind == "empty_evidence"

    # A rejected butler_actions row must have been written
    assert len(action_repo.rows) == 1
    row = next(iter(action_repo.rows.values()))
    assert row.status == "rejected"
    assert row.rejection_reason == "empty_evidence"


# ---------------------------------------------------------------------------
# R8.c — cross-user execution refused without affected-user consent
# ---------------------------------------------------------------------------


async def test_r8c_refuses_execute_without_affected_consent() -> None:
    """R8.c: execute refuses when an affected_user confirmation is not 'confirmed'."""
    repo = _FakeActionRepo()
    action = _FakeAction(id=1, requester_tg_id=42, chat_id=-100_500, status="confirmed")
    repo.rows[1] = action

    affected_pending = SimpleNamespace(
        action_id=1, confirmer_tg_id=99, confirmation_role="affected_user", status="pending"
    )
    confs = _FakeConfirmationRepo([affected_pending])

    svc = ButlerService(
        session=object(),
        ledger_repo=None,
        butler_action_repo=repo,
        butler_action_confirmation_repo=confs,
        butler_tool_invocation_repo=None,
        butler_rate_bucket_repo=_FakeRateBucketRepo(),
        user_repo=None,
        llm_gateway=_ExplodingGateway(),
        evidence_builder=_FakeEvidenceBuilder(_context(abstained=False)),
        settings=_Settings(),
    )

    with pytest.raises(ButlerActionRejectedError) as ei:
        await svc.execute_action(action_id=1, tool_registry={}, bot=None)
    assert ei.value.error_kind == "affected_user_consent_revoked"
    assert repo.rows[1].status == "rejected"


# ---------------------------------------------------------------------------
# R8.e — TTL expiry refusal on confirm
# ---------------------------------------------------------------------------


async def test_r8e_refuses_confirm_past_ttl() -> None:
    """R8.e: confirming a pending action past expires_at refuses + marks it expired."""
    repo = _FakeActionRepo()
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    action = _FakeAction(
        id=1,
        requester_tg_id=42,
        chat_id=-100_500,
        status="pending_confirmation",
        expires_at=past,
    )
    repo.rows[1] = action

    requester_conf = SimpleNamespace(
        id=10,
        action_id=1,
        confirmer_tg_id=42,
        confirmation_role="requester",
        status="pending",
        confirmation_token="tok",
        expires_at=past,
    )

    class _Confs(_FakeConfirmationRepo):
        async def get_for_action_user(self, _s: Any, _a: int, _u: int) -> Any:
            return requester_conf

    svc = ButlerService(
        session=object(),
        ledger_repo=None,
        butler_action_repo=repo,
        butler_action_confirmation_repo=_Confs([requester_conf]),
        butler_tool_invocation_repo=None,
        butler_rate_bucket_repo=_FakeRateBucketRepo(),
        user_repo=None,
        llm_gateway=_ExplodingGateway(),
        evidence_builder=_FakeEvidenceBuilder(_context(abstained=False)),
        settings=_Settings(),
    )

    with pytest.raises(ButlerActionExpiredError):
        await svc.confirm_action(action_id=1, confirming_user_id=42, confirmation_token="tok")
    assert repo.rows[1].status == "expired"


# ---------------------------------------------------------------------------
# R8.d — Butler does not consume graph_query in baseline (transitive read-block)
# ---------------------------------------------------------------------------


def test_r8d_butler_does_not_import_graph_query() -> None:
    """R8.d: the baseline Butler surface never imports bot.services.graph_query.

    Reuses the G3.a per-path AST scanner — the concrete enforcement of the
    transitive graph-purge read-block in baseline (Butler has no graph tool).
    """
    from tests.evals.test_no_llm_imports import assert_no_forbidden_imports_per_path

    assert_no_forbidden_imports_per_path(
        {
            "bot/services/butler*.py": frozenset({"bot.services.graph_query"}),
            "bot/handlers/butler.py": frozenset({"bot.services.graph_query"}),
            "bot/services/butler_tools/*.py": frozenset({"bot.services.graph_query"}),
        }
    )
