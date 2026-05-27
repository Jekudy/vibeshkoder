"""T12-08 — butler controls: per-user-day budget cap, rate bucket exhaustiveness.

Scheduler / TTL worker tests live in test_butler_scheduler.py (requires app_env).
Module-level imports required (T12-06 CI lesson — class-identity).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from bot.services.butler import (
    ButlerActionError,
    ButlerService,
)
from bot.services.butler_tools import (
    ButlerActionStep,
)


# ---------------------------------------------------------------------------
# Minimal shared fakes (kept small — no duplication of the large
# test_butler_state_machine.py double set)
# ---------------------------------------------------------------------------

CHAT_ID = -100_111_222_333
USER_ID = 42


@dataclass
class _FakeAction:
    id: int
    requester_tg_id: int
    chat_id: int
    action_type: str
    status: str
    tool_name: str
    tool_manifest_version: str
    governance_filter_version: str
    evidence_context_hash: str
    evidence_ids: list = field(default_factory=list)
    plan_summary: str = ""
    action_args: dict = field(default_factory=dict)
    action_args_hash: str = ""
    rollback_kind: str = "not_reversible"
    risk_level: str = "low"
    requires_confirmation: bool = True
    confirmation_policy: str = "per_action"
    expires_at: datetime | None = None
    rejection_reason: str | None = None
    llm_usage_ledger_id: int | None = None
    plan_payload: dict = field(default_factory=dict)
    query: str = ""
    visibility_scope: str = "member"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    action_uuid: uuid.UUID = field(default_factory=uuid.uuid4)
    parent_action_id: int | None = None
    confirmed_at: datetime | None = None
    executed_at: datetime | None = None
    undone_at: datetime | None = None
    error_code: str | None = None
    error_context: dict | None = None
    result_payload: dict | None = None
    result_payload_hash: str | None = None
    inverse_op_payload: dict | None = None
    approved_card_source_ids: list = field(default_factory=list)


class _FakeActionRepo:
    def __init__(self) -> None:
        self._rows: dict[int, _FakeAction] = {}
        self._next_id = 1

    async def create(self, session: Any, **kwargs: Any) -> _FakeAction:
        row = _FakeAction(id=self._next_id, **kwargs)
        self._rows[self._next_id] = row
        self._next_id += 1
        return row

    async def get(self, session: Any, action_id: int) -> _FakeAction | None:
        return self._rows.get(action_id)

    async def get_for_update(self, session: Any, action_id: int) -> _FakeAction | None:
        return self._rows.get(action_id)

    async def update_status(
        self,
        session: Any,
        action_id: int,
        *,
        status: str,
        rejection_reason: str | None = None,
        **kwargs: Any,
    ) -> int:
        row = self._rows.get(action_id)
        if row is None:
            raise LookupError(f"not found: {action_id}")
        row.status = status
        if rejection_reason is not None:
            row.rejection_reason = rejection_reason
        return 1


class _FakeConfirmationRepo:
    async def create(self, session: Any, **kwargs: Any) -> Any:
        return None

    async def get_by_action(self, session: Any, action_id: int) -> list:
        return []


class _FakeInvocationRepo:
    async def create(self, session: Any, **kwargs: Any) -> Any:
        return None

    async def find_by_posted_message_id(self, session: Any, posted_message_id: int) -> Any:
        return None


class _FakeRateBucketRepo:
    """Rate bucket fake that optionally returns False on a specific bucket_kind."""

    def __init__(self, *, failing_kind: str | None = None) -> None:
        self._failing_kind = failing_kind
        self._incremented: list[dict] = []
        self._decremented: list[dict] = []

    async def try_increment(
        self,
        session: Any,
        *,
        bucket_kind: str,
        scope_id: int,
        bucket_key: str,
        window_start: datetime,
        window_end: datetime,
        ceiling: int,
    ) -> bool:
        if self._failing_kind is not None and bucket_kind == self._failing_kind:
            return False
        self._incremented.append({"bucket_kind": bucket_kind, "scope_id": scope_id})
        return True

    async def decrement(
        self,
        session: Any,
        *,
        bucket_kind: str,
        scope_id: int,
        bucket_key: str,
    ) -> None:
        self._decremented.append({"bucket_kind": bucket_kind, "scope_id": scope_id})


class _FakeLedgerRepo:
    """Ledger fake with configurable per-user daily spend."""

    def __init__(self, *, user_daily_spend: Decimal = Decimal("0")) -> None:
        self._user_daily_spend = user_daily_spend
        self._daily_costs: dict[str | None, Decimal] = {}

    async def daily_cost_usd(
        self,
        session: Any,
        *,
        day: Any,
        call_type: str | None = None,
        requester_user_id: int | None = None,
    ) -> Decimal:
        return self._daily_costs.get(call_type, Decimal("0"))

    async def monthly_cost_usd(
        self,
        session: Any,
        *,
        year: int,
        month: int,
        call_type: str | None = None,
    ) -> Decimal:
        return Decimal("0")


class _FakeUserRepo:
    def __init__(self, *, members: set[int] | None = None) -> None:
        self._members: set[int] = members or {USER_ID}

    async def get(self, session: Any, user_id: int) -> Any | None:
        if user_id in self._members:
            from dataclasses import dataclass as _dc

            @_dc
            class _U:
                is_member: bool = True
                is_admin: bool = False

            return _U()
        return None


@dataclass
class _FakeSettings:
    butler_plan_ttl_seconds: int = 900
    butler_confirmation_ttl_seconds: int = 300
    user_plans_day_ceiling: int = 10
    user_execs_day_ceiling: int = 5
    chat_actions_day_ceiling: int = 50
    tool_hour_ceiling: int = 20
    butler_per_user_daily_usd_ceiling: float = 0.20


@dataclass
class _FakePlan:
    """Minimal plan fake returned by gateway."""

    plan_summary: str = "test plan"
    evidence_ids: tuple = (10, 11)
    actions: tuple = field(default_factory=tuple)
    evidence_context_hash: str = "hash123"
    requester_user_id: int = USER_ID
    chat_id: int = CHAT_ID
    visibility_scope: str = "member"
    governance_filter_version: str = "test-v1"
    tool_manifest_version: str = "v1.0.0"

    def model_dump(self) -> dict:
        return {
            "plan_summary": self.plan_summary,
            "evidence_ids": list(self.evidence_ids),
        }


class _FakeGateway:
    def __init__(self, *, plan: Any | None = None, raise_exc: Exception | None = None) -> None:
        self._plan = plan
        self._raise = raise_exc

    async def plan_butler_action(self, **kwargs: Any) -> Any:
        if self._raise is not None:
            raise self._raise
        step = ButlerActionStep(
            tool_name="recall_evidence",
            args={"query": "test"},
            requires_confirmation=True,
            affected_user_ids=(),
            risk_level="low",
            rollback_kind="not_reversible",
            inverse_op_payload=None,
        )
        p = self._plan or _FakePlan(actions=(step,))
        return p, 1, Decimal("0.01")


class _FakeEvidenceBuilder:
    async def build_butler_evidence(self, **kwargs: Any) -> Any:
        from bot.services.butler_evidence import ButlerEvidenceContext, butler_context_hash
        from bot.services.evidence import EvidenceBundle

        bundle = EvidenceBundle(
            query="test",
            chat_id=CHAT_ID,
            items=(),
            abstained=True,
            created_at=datetime.now(timezone.utc),
        )
        ctx_hash = butler_context_hash(bundle, "member", "test-v1")
        return ButlerEvidenceContext(
            bundle=bundle,
            visibility_scope="member",
            context_hash=ctx_hash,
            governance_filter_version="test-v1",
            requester_user_id=USER_ID,
            chat_id=CHAT_ID,
            query="test",
            snapshot_at=datetime.now(timezone.utc),
            governance_excluded_count=0,
        )


def _make_service(
    *,
    rate_bucket_repo: _FakeRateBucketRepo | None = None,
    ledger_repo: _FakeLedgerRepo | None = None,
    gateway: _FakeGateway | None = None,
    user_repo: _FakeUserRepo | None = None,
    action_repo: _FakeActionRepo | None = None,
    settings: _FakeSettings | None = None,
) -> tuple[ButlerService, _FakeActionRepo]:
    ar = action_repo or _FakeActionRepo()
    svc = ButlerService(
        session=None,
        ledger_repo=ledger_repo or _FakeLedgerRepo(),
        butler_action_repo=ar,
        butler_action_confirmation_repo=_FakeConfirmationRepo(),
        butler_tool_invocation_repo=_FakeInvocationRepo(),
        butler_rate_bucket_repo=rate_bucket_repo or _FakeRateBucketRepo(),
        user_repo=user_repo or _FakeUserRepo(),
        llm_gateway=gateway or _FakeGateway(),
        evidence_builder=_FakeEvidenceBuilder(),
        settings=settings or _FakeSettings(),
    )
    return svc, ar


# ---------------------------------------------------------------------------
# Tests — rate bucket exhaustiveness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_plans_day_rate_limit_exceeded() -> None:
    """user_plans_day ceiling reached → plan_action raises rate_limit_exceeded."""
    svc, ar = _make_service(
        rate_bucket_repo=_FakeRateBucketRepo(failing_kind="user_plans_day"),
    )
    with pytest.raises(ButlerActionError) as exc_info:
        await svc.plan_action(
            requester_user_id=USER_ID,
            chat_id=CHAT_ID,
            query="test",
            visibility_scope="member",
        )
    assert exc_info.value.error_kind == "rate_limit_exceeded"


@pytest.mark.asyncio
async def test_chat_actions_day_rate_limit_exceeded() -> None:
    """chat_actions_day ceiling reached → plan_action raises rate_limit_exceeded."""
    svc, ar = _make_service(
        rate_bucket_repo=_FakeRateBucketRepo(failing_kind="chat_actions_day"),
    )
    with pytest.raises(ButlerActionError) as exc_info:
        await svc.plan_action(
            requester_user_id=USER_ID,
            chat_id=CHAT_ID,
            query="test",
            visibility_scope="member",
        )
    assert exc_info.value.error_kind == "rate_limit_exceeded"


@pytest.mark.asyncio
async def test_tool_hour_rate_limit_exceeded() -> None:
    """tool_hour:{tool_name} ceiling reached → plan_action raises rate_limit_exceeded."""
    svc, ar = _make_service(
        rate_bucket_repo=_FakeRateBucketRepo(failing_kind="tool_hour:recall_evidence"),
    )
    with pytest.raises(ButlerActionError) as exc_info:
        await svc.plan_action(
            requester_user_id=USER_ID,
            chat_id=CHAT_ID,
            query="test",
            visibility_scope="member",
        )
    assert exc_info.value.error_kind == "rate_limit_exceeded"


@pytest.mark.asyncio
async def test_rate_limit_rollback_on_chat_failure() -> None:
    """When chat_actions_day fails, already-incremented user_plans_day is decremented."""
    rate_repo = _FakeRateBucketRepo(failing_kind="chat_actions_day")
    svc, _ = _make_service(rate_bucket_repo=rate_repo)
    with pytest.raises(ButlerActionError):
        await svc.plan_action(
            requester_user_id=USER_ID,
            chat_id=CHAT_ID,
            query="test",
            visibility_scope="member",
        )
    # user_plans_day was incremented, then decremented on chat failure
    assert any(d["bucket_kind"] == "user_plans_day" for d in rate_repo._decremented)


@pytest.mark.asyncio
async def test_rate_limit_rollback_on_tool_hour_failure() -> None:
    """When tool_hour fails, prior increments (user_plans_day + chat_actions_day) are decremented."""
    rate_repo = _FakeRateBucketRepo(failing_kind="tool_hour:recall_evidence")
    svc, _ = _make_service(rate_bucket_repo=rate_repo)
    with pytest.raises(ButlerActionError):
        await svc.plan_action(
            requester_user_id=USER_ID,
            chat_id=CHAT_ID,
            query="test",
            visibility_scope="member",
        )
    decremented_kinds = {d["bucket_kind"] for d in rate_repo._decremented}
    assert "user_plans_day" in decremented_kinds
    assert "chat_actions_day" in decremented_kinds


# ---------------------------------------------------------------------------
# Tests — per-user-day budget cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_cap_raises_when_ceiling_exceeded() -> None:
    """When user has spent >= ceiling today, plan_action raises budget_exceeded."""
    from bot.services.butler_budget import ButlerBudgetChecker

    # Ledger reports $0.25 spent for this user today
    checker = ButlerBudgetChecker(per_user_daily_ceiling=Decimal("0.20"))

    async def _fake_user_daily_spend(session: Any, user_id: int) -> Decimal:
        return Decimal("0.25")

    exceeded = await checker.is_user_daily_exceeded(None, user_id=USER_ID, spend_fn=_fake_user_daily_spend)
    assert exceeded is True


@pytest.mark.asyncio
async def test_budget_cap_passes_when_below_ceiling() -> None:
    """When user has spent < ceiling today, budget check passes."""
    from bot.services.butler_budget import ButlerBudgetChecker

    checker = ButlerBudgetChecker(per_user_daily_ceiling=Decimal("0.20"))

    async def _fake_user_daily_spend(session: Any, user_id: int) -> Decimal:
        return Decimal("0.10")

    exceeded = await checker.is_user_daily_exceeded(None, user_id=USER_ID, spend_fn=_fake_user_daily_spend)
    assert exceeded is False


@pytest.mark.asyncio
async def test_budget_cap_at_exact_ceiling_is_exceeded() -> None:
    """Spend == ceiling → exceeded (boundary: >= not >)."""
    from bot.services.butler_budget import ButlerBudgetChecker

    checker = ButlerBudgetChecker(per_user_daily_ceiling=Decimal("0.20"))

    async def _fake_user_daily_spend(session: Any, user_id: int) -> Decimal:
        return Decimal("0.20")

    exceeded = await checker.is_user_daily_exceeded(None, user_id=USER_ID, spend_fn=_fake_user_daily_spend)
    assert exceeded is True


@pytest.mark.asyncio
async def test_compute_user_daily_budget_spent() -> None:
    """compute_user_daily_budget_spent delegates to _query_butler_daily_spend."""
    from bot.services import butler_budget as bb_mod
    from bot.services.butler_budget import compute_user_daily_budget_spent

    async def _fake_query(session: Any, user_id: int) -> Decimal:
        return Decimal("0.15")

    original = bb_mod._query_butler_daily_spend
    bb_mod._query_butler_daily_spend = _fake_query
    try:
        spent = await compute_user_daily_budget_spent(session=None, user_id=USER_ID)
    finally:
        bb_mod._query_butler_daily_spend = original

    assert spent == Decimal("0.15")


# ---------------------------------------------------------------------------
# Tests — C1: ButlerBudgetChecker wired into plan_action
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_action_rejects_when_user_daily_budget_exceeded() -> None:
    """When user daily spend >= ceiling, plan_action raises budget_exceeded."""
    from bot.services import butler_budget as bb_mod

    # Patch _query_butler_daily_spend to simulate the user having exceeded the ceiling
    async def _over_spend(session: Any, user_id: int) -> Decimal:
        return Decimal("0.25")  # > 0.20 ceiling

    rate_repo = _FakeRateBucketRepo()
    svc, ar = _make_service(rate_bucket_repo=rate_repo)

    # monkeypatch _query_butler_daily_spend at module level
    original = bb_mod._query_butler_daily_spend
    bb_mod._query_butler_daily_spend = _over_spend
    try:
        with pytest.raises(ButlerActionError) as exc_info:
            await svc.plan_action(
                requester_user_id=USER_ID,
                chat_id=CHAT_ID,
                query="test",
                visibility_scope="member",
            )
    finally:
        bb_mod._query_butler_daily_spend = original

    assert exc_info.value.error_kind == "budget_exceeded"
    # Rate buckets (user_plans_day, chat_actions_day) should be rolled back
    decremented_kinds = {d["bucket_kind"] for d in rate_repo._decremented}
    assert "user_plans_day" in decremented_kinds
    assert "chat_actions_day" in decremented_kinds


@pytest.mark.asyncio
async def test_plan_action_proceeds_when_budget_within_ceiling() -> None:
    """When user spend < ceiling, plan_action succeeds past budget check."""
    from bot.services import butler_budget as bb_mod

    async def _under_spend(session: Any, user_id: int) -> Decimal:
        return Decimal("0.10")  # < 0.20 ceiling

    original = bb_mod._query_butler_daily_spend
    bb_mod._query_butler_daily_spend = _under_spend
    try:
        svc, ar = _make_service()
        action = await svc.plan_action(
            requester_user_id=USER_ID,
            chat_id=CHAT_ID,
            query="test",
            visibility_scope="member",
        )
    finally:
        bb_mod._query_butler_daily_spend = original

    assert action.status == "pending_confirmation"

