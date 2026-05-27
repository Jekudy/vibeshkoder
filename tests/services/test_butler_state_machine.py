"""Behaviour tests for ButlerService state machine (T12-04).

TDD — tests written and executed before/alongside the butler.py implementation.

Covers:
  - plan_action happy path (success → pending_confirmation)
  - plan_action rejection paths: tool_not_allowed, invalid_args, evidence_context_mismatch,
    orphan_evidence_ids, budget_exceeded, membership_revoked, rate_limit_exceeded
  - confirm_action happy path (single-confirmation completion → pending_execution)
  - confirm_action happy path (multi-confirmation — partial then full)
  - confirm_action rejection: already_confirmed_by_user, bad_token, expired,
    evidence_stale, cascade_in_flight
  - execute_action happy path (all tools succeed → succeeded)
  - execute_action tool failure (first failure → action.status='failed')
  - cancel_action happy (requester), happy (admin), auth failure (other user)
  - expire_action happy + idempotent
  - cascade-vs-callback race: FOR UPDATE on butler_actions in confirm_action
  - error_kind assertions on every raised ButlerActionError
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from bot.services.butler import (
    ButlerActionError,
    ButlerActionExpiredError,
    ButlerActionRejectedError,
    ButlerService,
    CascadeInFlightError,
    EvidenceStaleError,
    MembershipRevokedError,
)
from bot.services.butler_evidence import ButlerEvidenceContext, butler_context_hash
from bot.services.butler_tools import (
    ButlerActionStep,
    ButlerPlan,
    ButlerPlanError,
    InvalidToolArgsError,
    ToolNotAllowedError,
)
from bot.services.evidence import EvidenceBundle, EvidenceItem


# ---------------------------------------------------------------------------
# Test doubles — minimal fakes for repos + gateway + evidence builder
# ---------------------------------------------------------------------------

CHAT_ID = -100_999_888_777


def _make_item(mvid: int) -> EvidenceItem:
    return EvidenceItem(
        message_version_id=mvid,
        chat_message_id=mvid + 1000,
        chat_id=CHAT_ID,
        message_id=mvid + 2000,
        user_id=55,
        snippet="test snippet",
        ts_rank=0.7,
        captured_at=datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
        message_date=datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
        source_type="message",
        card_id=None,
        card_source_message_version_ids=(),
    )


def _make_context(
    mvids: tuple[int, ...] = (10, 11),
    *,
    visibility_scope: str = "member",
    gov_version: str = "test-v1",
    requester_user_id: int = 42,
) -> ButlerEvidenceContext:
    items = tuple(_make_item(mvid) for mvid in mvids)
    bundle = EvidenceBundle(
        query="who knows Rust?",
        chat_id=CHAT_ID,
        items=items,
        abstained=len(items) == 0,
        created_at=datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
    )
    ctx_hash = butler_context_hash(bundle, visibility_scope, gov_version)
    return ButlerEvidenceContext(
        bundle=bundle,
        visibility_scope=visibility_scope,
        context_hash=ctx_hash,
        governance_filter_version=gov_version,
        requester_user_id=requester_user_id,
        chat_id=CHAT_ID,
        query="who knows Rust?",
        snapshot_at=datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
        governance_excluded_count=0,
    )


def _valid_plan(
    context: ButlerEvidenceContext | None = None,
    *,
    tool_name: str = "recall_evidence",
    affected_user_ids: tuple[int, ...] = (),
    requester_user_id: int = 42,
) -> ButlerPlan:
    ctx = context or _make_context(requester_user_id=requester_user_id)
    step = ButlerActionStep(
        tool_name=tool_name,
        args={"query": "who knows Rust?"},
        requires_confirmation=True,
        affected_user_ids=affected_user_ids,
        risk_level="low",
        rollback_kind="not_reversible",
        inverse_op_payload=None,
    )
    return ButlerPlan(
        plan_summary="Recall members who know Rust",
        evidence_ids=ctx.bundle.evidence_ids or (10,),
        actions=(step,),
        evidence_context_hash=ctx.context_hash,
        requester_user_id=requester_user_id,
        chat_id=CHAT_ID,
        visibility_scope=ctx.visibility_scope,
        governance_filter_version=ctx.governance_filter_version,
    )


# ---------------------------------------------------------------------------
# Fake DB row classes
# ---------------------------------------------------------------------------

@dataclass
class FakeButlerAction:
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
    confirmed_at: datetime | None = None
    executed_at: datetime | None = None
    undone_at: datetime | None = None
    rejection_reason: str | None = None
    error_code: str | None = None
    error_context: dict | None = None
    llm_usage_ledger_id: int | None = None
    result_payload: dict | None = None
    result_payload_hash: str | None = None
    inverse_op_payload: dict | None = None
    action_uuid: uuid.UUID = field(default_factory=uuid.uuid4)
    parent_action_id: int | None = None
    plan_payload: dict | None = None  # serialized plan JSON (C3)
    approved_card_source_ids: list = field(default_factory=list)
    # migration 074 fields (C2): stored on row at plan time; used by confirm/execute
    query: str = ""
    visibility_scope: str = "member"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class FakeButlerActionConfirmation:
    id: int
    action_id: int
    confirmer_tg_id: int
    confirmation_role: str
    status: str
    preview_payload_hash: str
    expires_at: datetime
    confirmation_token: str = field(default_factory=lambda: str(uuid.uuid4()))
    confirmed_at: datetime | None = None
    rejected_at: datetime | None = None
    confirmation_message_chat_id: int | None = None
    confirmation_message_id: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class FakeButlerToolInvocation:
    id: int
    action_id: int
    tool_name: str
    idempotency_key: str
    request_payload: dict
    request_payload_hash: str
    status: str
    invocation_seq: int = 1
    response_payload: dict | None = None
    response_payload_hash: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    error_code: str | None = None
    error_context: dict | None = None


# ---------------------------------------------------------------------------
# Fake repositories
# ---------------------------------------------------------------------------

class FakeButlerActionRepo:
    def __init__(self) -> None:
        self._rows: dict[int, FakeButlerAction] = {}
        self._next_id = 1
        self.locked_ids: set[int] = set()  # simulate FOR UPDATE

    async def create(
        self,
        session: Any,
        *,
        requester_tg_id: int,
        chat_id: int,
        action_type: str,
        status: str,
        tool_name: str,
        tool_manifest_version: str,
        governance_filter_version: str,
        evidence_context_hash: str,
        plan_summary: str,
        action_args: dict,
        action_args_hash: str,
        rollback_kind: str,
        risk_level: str,
        evidence_ids: list | None = None,
        approved_card_source_ids: list | None = None,
        requires_confirmation: bool = True,
        confirmation_policy: str = "per_action",
        expires_at: datetime | None = None,
        llm_usage_ledger_id: int | None = None,
        rejection_reason: str | None = None,
        # migration 074 kwargs (C2/C3)
        query: str = "",
        visibility_scope: str = "member",
        plan_payload: dict | None = None,
    ) -> FakeButlerAction:
        row = FakeButlerAction(
            id=self._next_id,
            requester_tg_id=requester_tg_id,
            chat_id=chat_id,
            action_type=action_type,
            status=status,
            tool_name=tool_name,
            tool_manifest_version=tool_manifest_version,
            governance_filter_version=governance_filter_version,
            evidence_context_hash=evidence_context_hash,
            evidence_ids=evidence_ids or [],
            plan_summary=plan_summary,
            action_args=action_args,
            action_args_hash=action_args_hash,
            rollback_kind=rollback_kind,
            risk_level=risk_level,
            requires_confirmation=requires_confirmation,
            confirmation_policy=confirmation_policy,
            expires_at=expires_at,
            llm_usage_ledger_id=llm_usage_ledger_id,
            rejection_reason=rejection_reason,
            plan_payload=plan_payload if plan_payload is not None else {},
        )
        # Store query and visibility_scope as attributes (C2)
        row.query = query  # type: ignore[attr-defined]
        row.visibility_scope = visibility_scope  # type: ignore[attr-defined]
        self._rows[self._next_id] = row
        self._next_id += 1
        return row

    async def get(self, session: Any, action_id: int) -> FakeButlerAction | None:
        return self._rows.get(action_id)

    async def get_for_update(self, session: Any, action_id: int) -> FakeButlerAction | None:
        """Simulate SELECT FOR UPDATE — returns row or None if 'locked' by cascade."""
        if action_id in self.locked_ids:
            return None  # cascade holds the lock
        return self._rows.get(action_id)

    async def update_status(
        self,
        session: Any,
        action_id: int,
        *,
        status: str,
        rejection_reason: str | None = None,
        error_code: str | None = None,
        error_context: dict | None = None,
        result_payload: dict | None = None,
        result_payload_hash: str | None = None,
        inverse_op_payload: dict | None = None,
        confirmed_at: datetime | None = None,
        executed_at: datetime | None = None,
        undone_at: datetime | None = None,
        llm_usage_ledger_id: int | None = None,
    ) -> int:
        row = self._rows.get(action_id)
        if row is None:
            raise LookupError(f"ButlerAction(id={action_id}) not found")
        row.status = status
        row.updated_at = datetime.now(timezone.utc)
        if rejection_reason is not None:
            row.rejection_reason = rejection_reason
        if error_code is not None:
            row.error_code = error_code
        if error_context is not None:
            row.error_context = error_context
        if result_payload is not None:
            row.result_payload = result_payload
        if result_payload_hash is not None:
            row.result_payload_hash = result_payload_hash
        if inverse_op_payload is not None:
            row.inverse_op_payload = inverse_op_payload
        if confirmed_at is not None:
            row.confirmed_at = confirmed_at
        if executed_at is not None:
            row.executed_at = executed_at
        if undone_at is not None:
            row.undone_at = undone_at
        if llm_usage_ledger_id is not None:
            row.llm_usage_ledger_id = llm_usage_ledger_id
        return 1


class FakeButlerActionConfirmationRepo:
    def __init__(self) -> None:
        self._rows: dict[int, FakeButlerActionConfirmation] = {}
        self._next_id = 1

    async def create(
        self,
        session: Any,
        *,
        action_id: int,
        confirmer_tg_id: int,
        confirmation_role: str,
        status: str,
        preview_payload_hash: str,
        expires_at: datetime,
        confirmation_message_chat_id: int | None = None,
        confirmation_message_id: int | None = None,
        # C1: confirmation_token from service (secrets.token_urlsafe(32))
        confirmation_token: str = "",
    ) -> FakeButlerActionConfirmation:
        row = FakeButlerActionConfirmation(
            id=self._next_id,
            action_id=action_id,
            confirmer_tg_id=confirmer_tg_id,
            confirmation_role=confirmation_role,
            status=status,
            preview_payload_hash=preview_payload_hash,
            expires_at=expires_at,
            confirmation_token=confirmation_token,
        )
        self._rows[self._next_id] = row
        self._next_id += 1
        return row

    async def get_for_action_user(
        self,
        session: Any,
        action_id: int,
        confirmer_tg_id: int,
    ) -> FakeButlerActionConfirmation | None:
        for row in self._rows.values():
            if row.action_id == action_id and row.confirmer_tg_id == confirmer_tg_id:
                return row
        return None

    async def list_for_action(
        self,
        session: Any,
        action_id: int,
    ) -> list[FakeButlerActionConfirmation]:
        return [r for r in self._rows.values() if r.action_id == action_id]

    async def mark_resolved(
        self,
        session: Any,
        confirmation_id: int,
        *,
        status: str,
        resolved_at: datetime | None = None,
    ) -> int:
        row = self._rows.get(confirmation_id)
        if row is None:
            raise LookupError(f"FakeButlerActionConfirmation(id={confirmation_id}) not found")
        row.status = status
        if status == "confirmed":
            row.confirmed_at = resolved_at or datetime.now(timezone.utc)
        elif status == "rejected":
            row.rejected_at = resolved_at or datetime.now(timezone.utc)
        return 1

    async def mark_all_for_action(
        self,
        session: Any,
        action_id: int,
        *,
        status: str,
    ) -> int:
        count = 0
        for row in self._rows.values():
            if row.action_id == action_id and row.status == "pending":
                row.status = status
                count += 1
        return count


class FakeButlerToolInvocationRepo:
    def __init__(self) -> None:
        self._rows: dict[int, FakeButlerToolInvocation] = {}
        self._next_id = 1

    async def create(
        self,
        session: Any,
        *,
        action_id: int,
        tool_name: str,
        idempotency_key: str,
        request_payload: dict,
        request_payload_hash: str,
        status: str,
        invocation_seq: int = 1,
    ) -> FakeButlerToolInvocation:
        row = FakeButlerToolInvocation(
            id=self._next_id,
            action_id=action_id,
            tool_name=tool_name,
            idempotency_key=idempotency_key,
            request_payload=request_payload,
            request_payload_hash=request_payload_hash,
            status=status,
            invocation_seq=invocation_seq,
        )
        self._rows[self._next_id] = row
        self._next_id += 1
        return row

    async def update_invocation(
        self,
        session: Any,
        invocation_id: int,
        *,
        status: str,
        response_payload: dict | None = None,
        response_payload_hash: str | None = None,
        finished_at: datetime | None = None,
        error_code: str | None = None,
        error_context: dict | None = None,
    ) -> int:
        row = self._rows.get(invocation_id)
        if row is None:
            raise LookupError(f"FakeButlerToolInvocation(id={invocation_id}) not found")
        row.status = status
        if response_payload is not None:
            row.response_payload = response_payload
        if response_payload_hash is not None:
            row.response_payload_hash = response_payload_hash
        if finished_at is not None:
            row.finished_at = finished_at
        if error_code is not None:
            row.error_code = error_code
        if error_context is not None:
            row.error_context = error_context
        return 1

    def list_for_action(self, action_id: int) -> list[FakeButlerToolInvocation]:
        return [r for r in self._rows.values() if r.action_id == action_id]


class FakeButlerRateBucketRepo:
    def __init__(self, *, fail_on_tool: str | None = None) -> None:
        self._fail_on_tool = fail_on_tool

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
        # Return False (rate-limit exceeded) if this bucket_kind matches
        if self._fail_on_tool and self._fail_on_tool in bucket_kind:
            return False
        return True

    async def decrement(
        self,
        session: Any,
        *,
        bucket_kind: str,
        scope_id: int,
        bucket_key: str,
    ) -> None:
        # No-op in basic fake — tracking variant (TrackingFakeButlerRateBucketRepo) records calls
        pass


class FakeLedgerRepo:
    def __init__(self, *, over_budget: bool = False) -> None:
        self._over_budget = over_budget
        self._rows: list[Any] = []
        self._next_id = 1

    async def daily_cost_usd(self, session: Any, *, day: Any, call_type: str | None = None) -> Decimal:
        if self._over_budget:
            return Decimal("999.0")
        return Decimal("0")

    async def monthly_cost_usd(self, session: Any, *, year: int, month: int, call_type: str | None = None) -> Decimal:
        if self._over_budget:
            return Decimal("999.0")
        return Decimal("0")


@dataclass
class _FakeUser:
    """Minimal user object for H1/M2 tests."""
    telegram_id: int
    is_member: bool = True
    is_admin: bool = False


class FakeUserRepo:
    """Fake UserRepo for membership pre-check (H1) and admin verification (M2)."""

    def __init__(self, *, members: set[int] | None = None, admins: set[int] | None = None) -> None:
        self._members: set[int] = members if members is not None else set()
        self._admins: set[int] = admins if admins is not None else set()

    async def get(self, session: Any, user_id: int) -> _FakeUser | None:
        if user_id in self._admins:
            return _FakeUser(telegram_id=user_id, is_member=True, is_admin=True)
        if user_id in self._members:
            return _FakeUser(telegram_id=user_id, is_member=True, is_admin=False)
        return None


# ---------------------------------------------------------------------------
# Fake gateway + evidence builder
# ---------------------------------------------------------------------------

@dataclass
class FakeLLMGateway:
    """Fake for plan_butler_action returning a ButlerPlan + ledger_id + cost."""
    plan: ButlerPlan | None = None
    ledger_id: int = 1
    cost: Decimal = Decimal("0.01")
    should_raise: Exception | None = None

    async def plan_butler_action(self, **kwargs: Any) -> tuple[ButlerPlan, int, Decimal]:
        if self.should_raise is not None:
            raise self.should_raise
        assert self.plan is not None
        return self.plan, self.ledger_id, self.cost


@dataclass
class FakeEvidenceBuilder:
    """Fake for build_butler_evidence."""
    context: ButlerEvidenceContext | None = None
    should_raise: Exception | None = None

    async def build_butler_evidence(self, **kwargs: Any) -> ButlerEvidenceContext:
        if self.should_raise is not None:
            raise self.should_raise
        assert self.context is not None
        return self.context


# ---------------------------------------------------------------------------
# Fake session — records SQL calls for advisory lock inspection
# ---------------------------------------------------------------------------

class FakeSession:
    def __init__(self) -> None:
        self.executed: list[Any] = []
        self.flushed = 0

    async def execute(self, stmt: Any, params: Any = None) -> Any:
        self.executed.append((stmt, params))
        return _FakeResult()

    async def flush(self) -> None:
        self.flushed += 1

    def add(self, obj: Any) -> None:
        pass


class _FakeResult:
    def scalar_one_or_none(self) -> None:
        return None

    def scalars(self) -> "_FakeScalars":
        return _FakeScalars([])

    def mappings(self) -> list:
        return []


class _FakeScalars:
    def __init__(self, items: list) -> None:
        self._items = items

    def all(self) -> list:
        return self._items


# ---------------------------------------------------------------------------
# Settings stub
# ---------------------------------------------------------------------------

@dataclass
class FakeSettings:
    butler_plan_ttl_seconds: int = 900  # 15 min
    butler_confirmation_ttl_seconds: int = 300  # 5 min


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

@dataclass
class _ButlerServiceTestHarness:
    """Wires fake repos + gateway + evidence builder into ButlerService."""
    action_repo: FakeButlerActionRepo = field(default_factory=FakeButlerActionRepo)
    confirmation_repo: FakeButlerActionConfirmationRepo = field(
        default_factory=FakeButlerActionConfirmationRepo
    )
    invocation_repo: FakeButlerToolInvocationRepo = field(
        default_factory=FakeButlerToolInvocationRepo
    )
    rate_bucket_repo: FakeButlerRateBucketRepo = field(
        default_factory=FakeButlerRateBucketRepo
    )
    ledger_repo: FakeLedgerRepo = field(default_factory=FakeLedgerRepo)
    # user_repo=None means membership check is skipped (default for tests that
    # don't exercise H1/M2 paths).  Set to FakeUserRepo(...) to test those paths.
    user_repo: FakeUserRepo | None = None
    gateway: FakeLLMGateway = field(default_factory=FakeLLMGateway)
    evidence_builder: FakeEvidenceBuilder = field(default_factory=FakeEvidenceBuilder)
    settings: FakeSettings = field(default_factory=FakeSettings)
    session: FakeSession = field(default_factory=FakeSession)

    def make_service(self):
        return ButlerService(
            session=self.session,
            ledger_repo=self.ledger_repo,
            butler_action_repo=self.action_repo,
            butler_action_confirmation_repo=self.confirmation_repo,
            butler_tool_invocation_repo=self.invocation_repo,
            butler_rate_bucket_repo=self.rate_bucket_repo,
            user_repo=self.user_repo,
            llm_gateway=self.gateway,
            evidence_builder=self.evidence_builder,
            settings=self.settings,
        )


# ===========================================================================
# Tests — plan_action
# ===========================================================================


@pytest.mark.asyncio
async def test_plan_action_happy_path_returns_pending_confirmation() -> None:
    """plan_action with valid plan → butler_actions row with status='pending_confirmation'."""
    ctx = _make_context()
    plan = _valid_plan(ctx)
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan, ledger_id=99, cost=Decimal("0.01")),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="who knows Rust?",
        visibility_scope="member",
    )

    assert action.status == "pending_confirmation"
    assert action.llm_usage_ledger_id == 99
    assert action.requester_tg_id == 42


@pytest.mark.asyncio
async def test_plan_action_writes_confirmation_row_for_requester() -> None:
    """plan_action writes a butler_action_confirmations row for the requester."""
    ctx = _make_context()
    plan = _valid_plan(ctx)
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="who knows Rust?",
        visibility_scope="member",
    )

    confirmations = await harness.confirmation_repo.list_for_action(harness.session, action.id)
    requester_confirmation = [c for c in confirmations if c.confirmer_tg_id == 42]
    assert len(requester_confirmation) == 1
    assert requester_confirmation[0].status == "pending"


@pytest.mark.asyncio
async def test_plan_action_cross_user_writes_confirmation_for_affected_user() -> None:
    """plan_action with affected_user_ids writes confirmations for both requester + affected."""
    ctx = _make_context()
    plan = _valid_plan(ctx, tool_name="send_intro", affected_user_ids=(99,))
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="introduce me to user 99",
        visibility_scope="member",
    )

    confirmations = await harness.confirmation_repo.list_for_action(harness.session, action.id)
    confirmer_ids = {c.confirmer_tg_id for c in confirmations}
    assert 42 in confirmer_ids   # requester
    assert 99 in confirmer_ids   # affected user


@pytest.mark.asyncio
async def test_plan_action_rejection_tool_not_allowed_writes_rejected_row() -> None:
    """plan_action with tool_not_allowed ButlerPlanError writes status='rejected' row."""
    ctx = _make_context()
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(
            should_raise=ToolNotAllowedError(
                "tool_name='hack' not in ALLOWED_BUTLER_TOOLS",
                llm_usage_ledger_id=5,
                error_kind="tool_not_allowed",
            )
        ),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    with pytest.raises(ButlerActionError) as exc_info:
        await svc.plan_action(
            requester_user_id=42,
            chat_id=CHAT_ID,
            query="hack the mainframe",
            visibility_scope="member",
        )

    assert exc_info.value.error_kind == "tool_not_allowed"
    # A rejected row MUST have been written
    rows = list(harness.action_repo._rows.values())
    assert len(rows) == 1
    assert rows[0].status == "rejected"
    assert rows[0].rejection_reason == "tool_not_allowed"


@pytest.mark.asyncio
async def test_plan_action_rejection_tool_not_allowed_null_ledger_allowed() -> None:
    """plan_action with pre-planning failure → rejected row with NULL llm_usage_ledger_id allowed."""
    ctx = _make_context()
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(
            should_raise=ToolNotAllowedError(
                "tool not allowed",
                llm_usage_ledger_id=None,  # ledger not yet written (pre-plan failure)
                error_kind="tool_not_allowed",
            )
        ),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    with pytest.raises(ButlerActionError):
        await svc.plan_action(
            requester_user_id=42,
            chat_id=CHAT_ID,
            query="hack",
            visibility_scope="member",
        )

    rows = list(harness.action_repo._rows.values())
    assert rows[0].status == "rejected"
    assert rows[0].llm_usage_ledger_id is None  # pre-plan failure — NULL ledger OK per CHECK


@pytest.mark.asyncio
async def test_plan_action_rejection_invalid_args() -> None:
    """plan_action with invalid_args error → rejected row with error_kind='invalid_args'."""
    ctx = _make_context()
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(
            should_raise=InvalidToolArgsError(
                "args validation failed",
                llm_usage_ledger_id=7,
                error_kind="invalid_args",
            )
        ),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    with pytest.raises(ButlerActionError) as exc_info:
        await svc.plan_action(
            requester_user_id=42,
            chat_id=CHAT_ID,
            query="bad args",
            visibility_scope="member",
        )

    assert exc_info.value.error_kind == "invalid_args"
    rows = list(harness.action_repo._rows.values())
    assert rows[0].status == "rejected"
    assert rows[0].llm_usage_ledger_id == 7  # ledger written pre-validation


@pytest.mark.asyncio
async def test_plan_action_rejection_evidence_context_mismatch() -> None:
    """plan_action with evidence_context_mismatch → rejected row."""
    ctx = _make_context()
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(
            should_raise=ButlerPlanError(
                "context hash mismatch",
                llm_usage_ledger_id=8,
                error_kind="evidence_context_mismatch",
            )
        ),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    with pytest.raises(ButlerActionError) as exc_info:
        await svc.plan_action(
            requester_user_id=42,
            chat_id=CHAT_ID,
            query="test",
            visibility_scope="member",
        )

    assert exc_info.value.error_kind == "evidence_context_mismatch"
    rows = list(harness.action_repo._rows.values())
    assert rows[0].status == "rejected"


@pytest.mark.asyncio
async def test_plan_action_rejection_orphan_evidence_ids() -> None:
    """plan_action with orphan_evidence_ids error → rejected row."""
    ctx = _make_context()
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(
            should_raise=ButlerPlanError(
                "orphan evidence_ids",
                llm_usage_ledger_id=9,
                error_kind="orphan_evidence_ids",
            )
        ),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    with pytest.raises(ButlerActionError) as exc_info:
        await svc.plan_action(
            requester_user_id=42,
            chat_id=CHAT_ID,
            query="test",
            visibility_scope="member",
        )

    assert exc_info.value.error_kind == "orphan_evidence_ids"


@pytest.mark.asyncio
async def test_plan_action_rejection_budget_exceeded() -> None:
    """plan_action with budget_exceeded → rejected row."""
    ctx = _make_context()
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(
            should_raise=ButlerPlanError(
                "budget exceeded",
                llm_usage_ledger_id=10,
                error_kind="budget_exceeded",
            )
        ),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    with pytest.raises(ButlerActionError) as exc_info:
        await svc.plan_action(
            requester_user_id=42,
            chat_id=CHAT_ID,
            query="test",
            visibility_scope="member",
        )

    assert exc_info.value.error_kind == "budget_exceeded"
    rows = list(harness.action_repo._rows.values())
    assert rows[0].status == "rejected"
    assert rows[0].rejection_reason == "budget_exceeded"


@pytest.mark.asyncio
async def test_plan_action_rejection_rate_limit_exceeded() -> None:
    """plan_action with rate limit exceeded → rejected row with error_kind='rate_limit_exceeded'."""
    ctx = _make_context()
    plan = _valid_plan(ctx)
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
        rate_bucket_repo=FakeButlerRateBucketRepo(fail_on_tool="user_plans_day"),
    )
    svc = harness.make_service()

    with pytest.raises(ButlerActionError) as exc_info:
        await svc.plan_action(
            requester_user_id=42,
            chat_id=CHAT_ID,
            query="test",
            visibility_scope="member",
        )

    assert exc_info.value.error_kind == "rate_limit_exceeded"
    rows = list(harness.action_repo._rows.values())
    assert rows[0].status == "rejected"
    assert rows[0].rejection_reason == "rate_limit_exceeded"


@pytest.mark.asyncio
async def test_plan_action_rejection_membership_revoked() -> None:
    """plan_action with membership_revoked → rejected row."""
    ctx = _make_context()
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(
            should_raise=ButlerPlanError(
                "user is not a member",
                llm_usage_ledger_id=None,
                error_kind="membership_revoked",
            )
        ),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    with pytest.raises((ButlerActionError, MembershipRevokedError)) as exc_info:
        await svc.plan_action(
            requester_user_id=42,
            chat_id=CHAT_ID,
            query="test",
            visibility_scope="member",
        )

    assert exc_info.value.error_kind == "membership_revoked"


# ===========================================================================
# Tests — confirm_action
# ===========================================================================


@pytest.mark.asyncio
async def test_confirm_action_happy_single_confirmation() -> None:
    """confirm_action with single confirmer → action transitions to pending_execution."""
    ctx = _make_context()
    plan = _valid_plan(ctx)
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="who knows Rust?",
        visibility_scope="member",
    )
    assert action.status == "pending_confirmation"

    # Get the confirmation token
    confirmations = await harness.confirmation_repo.list_for_action(harness.session, action.id)
    conf = confirmations[0]

    result = await svc.confirm_action(
        action_id=action.id,
        confirming_user_id=42,
        confirmation_token=conf.confirmation_token,
    )

    # C5: 'confirmed' (matches DB CHECK enum), not 'pending_execution'
    assert result.status == "confirmed"


@pytest.mark.asyncio
async def test_confirm_action_multi_confirmation_partial_then_full() -> None:
    """Multi-confirmation: partial confirmation keeps pending_confirmation; all confirmed → 'confirmed'."""
    ctx = _make_context()
    plan = _valid_plan(ctx, tool_name="send_intro", affected_user_ids=(99,))
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="introduce me",
        visibility_scope="member",
    )

    # requester confirms first — still pending_confirmation (affected user not yet confirmed)
    all_confs = await harness.confirmation_repo.list_for_action(harness.session, action.id)
    requester_conf = next(c for c in all_confs if c.confirmer_tg_id == 42)

    result = await svc.confirm_action(
        action_id=action.id,
        confirming_user_id=42,
        confirmation_token=requester_conf.confirmation_token,
    )
    assert result.status == "pending_confirmation"  # affected user hasn't confirmed yet

    # affected user confirms — now all confirmed → 'confirmed' (C5: matches DB CHECK enum)
    affected_conf = next(c for c in all_confs if c.confirmer_tg_id == 99)
    result2 = await svc.confirm_action(
        action_id=action.id,
        confirming_user_id=99,
        confirmation_token=affected_conf.confirmation_token,
    )
    assert result2.status == "confirmed"


@pytest.mark.asyncio
async def test_confirm_action_reject_bad_token() -> None:
    """confirm_action with wrong token → ButlerActionError error_kind='bad_token'."""
    ctx = _make_context()
    plan = _valid_plan(ctx)
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="test",
        visibility_scope="member",
    )

    with pytest.raises(ButlerActionError) as exc_info:
        await svc.confirm_action(
            action_id=action.id,
            confirming_user_id=42,
            confirmation_token="wrong-token-abc",
        )

    assert exc_info.value.error_kind == "bad_token"


@pytest.mark.asyncio
async def test_confirm_action_reject_expired_action() -> None:
    """confirm_action on an expired action → ButlerActionExpiredError."""
    ctx = _make_context()
    plan = _valid_plan(ctx)
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="test",
        visibility_scope="member",
    )

    # Force-expire the action
    action.status = "expired"
    action.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)

    with pytest.raises(ButlerActionExpiredError) as exc_info:
        await svc.confirm_action(
            action_id=action.id,
            confirming_user_id=42,
            confirmation_token="any-token",
        )

    assert exc_info.value.error_kind == "expired"


@pytest.mark.asyncio
async def test_confirm_action_reject_already_confirmed() -> None:
    """confirm_action on already-confirmed confirmation → error_kind='already_confirmed_by_user'."""
    ctx = _make_context()
    plan = _valid_plan(ctx)
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="test",
        visibility_scope="member",
    )

    all_confs = await harness.confirmation_repo.list_for_action(harness.session, action.id)
    conf = all_confs[0]

    # First confirmation succeeds
    await svc.confirm_action(
        action_id=action.id,
        confirming_user_id=42,
        confirmation_token=conf.confirmation_token,
    )

    # Second attempt with same token → idempotency guard
    with pytest.raises(ButlerActionError) as exc_info:
        await svc.confirm_action(
            action_id=action.id,
            confirming_user_id=42,
            confirmation_token=conf.confirmation_token,
        )

    assert exc_info.value.error_kind == "already_confirmed_by_user"


@pytest.mark.asyncio
async def test_confirm_action_cascade_in_flight_raises() -> None:
    """confirm_action when cascade holds FOR UPDATE → CascadeInFlightError."""
    ctx = _make_context()
    plan = _valid_plan(ctx)
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="test",
        visibility_scope="member",
    )

    # Simulate cascade holding the lock on this action row
    harness.action_repo.locked_ids.add(action.id)

    with pytest.raises(CascadeInFlightError) as exc_info:
        await svc.confirm_action(
            action_id=action.id,
            confirming_user_id=42,
            confirmation_token="any-token",
        )

    assert exc_info.value.error_kind == "cascade_in_flight"


@pytest.mark.asyncio
async def test_confirm_action_evidence_stale() -> None:
    """confirm_action with stale evidence hash → EvidenceStaleError."""
    ctx = _make_context()
    plan = _valid_plan(ctx)
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="test",
        visibility_scope="member",
    )

    # Tamper: give evidence builder a DIFFERENT context (stale hash)
    different_ctx = _make_context(mvids=(999, 998))  # different mvids → different hash
    harness.evidence_builder.context = different_ctx

    all_confs = await harness.confirmation_repo.list_for_action(harness.session, action.id)
    conf = all_confs[0]

    with pytest.raises(EvidenceStaleError) as exc_info:
        await svc.confirm_action(
            action_id=action.id,
            confirming_user_id=42,
            confirmation_token=conf.confirmation_token,
        )

    assert exc_info.value.error_kind == "evidence_stale"
    # Action should now be rejected
    updated_action = await harness.action_repo.get(harness.session, action.id)
    assert updated_action is not None
    assert updated_action.status == "rejected"
    assert updated_action.rejection_reason == "evidence_stale"


# ===========================================================================
# Tests — execute_action
# ===========================================================================


@dataclass
class FakeToolResult:
    success: bool
    payload: dict | None = None
    error: str | None = None


@dataclass
class FakeButlerTool:
    """Fake ButlerTool implementation for execute_action tests."""
    name: str = "recall_evidence"
    schema_version: str = "v1.0.0"
    should_fail: bool = False
    fail_error: str = "tool_execution_failed"

    async def validate_policy(self, context: Any, args: Any) -> None:
        pass  # Always passes in happy path

    async def execute(self, plan: Any, ctx: Any, *, session: Any) -> FakeToolResult:
        if self.should_fail:
            return FakeToolResult(success=False, error=self.fail_error)
        return FakeToolResult(success=True, payload={"result": "recalled"})

    async def build_inverse(self, result: FakeToolResult) -> dict:
        return {"rollback": "noop"}


@pytest.mark.asyncio
async def test_execute_action_happy_path_succeeds() -> None:
    """execute_action with all tools succeeding → action.status='succeeded'."""
    ctx = _make_context()
    plan = _valid_plan(ctx)
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    # Plan + confirm
    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="test",
        visibility_scope="member",
    )
    all_confs = await harness.confirmation_repo.list_for_action(harness.session, action.id)
    await svc.confirm_action(
        action_id=action.id,
        confirming_user_id=42,
        confirmation_token=all_confs[0].confirmation_token,
    )

    # Execute with a fake tool registry
    fake_tool = FakeButlerTool()
    result = await svc.execute_action(
        action_id=action.id,
        tool_registry={"recall_evidence": fake_tool},
    )

    assert result.status == "succeeded"


@pytest.mark.asyncio
async def test_execute_action_tool_failure_marks_action_failed() -> None:
    """execute_action with tool failure → action.status='execution_failed' (DB enum, C5)."""
    ctx = _make_context()
    plan = _valid_plan(ctx)
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="test",
        visibility_scope="member",
    )
    all_confs = await harness.confirmation_repo.list_for_action(harness.session, action.id)
    await svc.confirm_action(
        action_id=action.id,
        confirming_user_id=42,
        confirmation_token=all_confs[0].confirmation_token,
    )

    failing_tool = FakeButlerTool(should_fail=True, fail_error="tool_error_xyz")
    result = await svc.execute_action(
        action_id=action.id,
        tool_registry={"recall_evidence": failing_tool},
    )

    # C5: 'execution_failed' matches DB CHECK enum, not 'failed'
    assert result.status == "execution_failed"
    assert result.error_code == "tool_error_xyz"


@pytest.mark.asyncio
async def test_execute_action_writes_tool_invocation_row() -> None:
    """execute_action writes a butler_tool_invocations row for each tool step."""
    ctx = _make_context()
    plan = _valid_plan(ctx)
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="test",
        visibility_scope="member",
    )
    all_confs = await harness.confirmation_repo.list_for_action(harness.session, action.id)
    await svc.confirm_action(
        action_id=action.id,
        confirming_user_id=42,
        confirmation_token=all_confs[0].confirmation_token,
    )

    fake_tool = FakeButlerTool()
    await svc.execute_action(
        action_id=action.id,
        tool_registry={"recall_evidence": fake_tool},
    )

    invocations = harness.invocation_repo.list_for_action(action.id)
    assert len(invocations) == 1
    assert invocations[0].tool_name == "recall_evidence"
    assert invocations[0].status == "succeeded"


# ===========================================================================
# Tests — cancel_action
# ===========================================================================


@pytest.mark.asyncio
async def test_cancel_action_by_requester() -> None:
    """cancel_action by the requester → action.status='cancelled'."""
    ctx = _make_context()
    plan = _valid_plan(ctx)
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="test",
        visibility_scope="member",
    )

    result = await svc.cancel_action(
        action_id=action.id,
        cancelling_user_id=42,  # requester
        is_admin=False,
    )

    assert result.status == "cancelled"


@pytest.mark.asyncio
async def test_cancel_action_by_admin() -> None:
    """cancel_action by an admin → action.status='cancelled'."""
    ctx = _make_context()
    plan = _valid_plan(ctx)
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="test",
        visibility_scope="member",
    )

    result = await svc.cancel_action(
        action_id=action.id,
        cancelling_user_id=999,  # admin user (different from requester)
        is_admin=True,
    )

    assert result.status == "cancelled"


@pytest.mark.asyncio
async def test_cancel_action_other_user_forbidden() -> None:
    """cancel_action by non-requester non-admin → ButlerActionError error_kind='forbidden'."""
    ctx = _make_context()
    plan = _valid_plan(ctx)
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="test",
        visibility_scope="member",
    )

    with pytest.raises(ButlerActionError) as exc_info:
        await svc.cancel_action(
            action_id=action.id,
            cancelling_user_id=888,  # some other user
            is_admin=False,
        )

    assert exc_info.value.error_kind == "forbidden"


@pytest.mark.asyncio
async def test_cancel_action_marks_pending_confirmations_rejected() -> None:
    """cancel_action marks pending confirmation rows as 'cancelled'."""
    ctx = _make_context()
    plan = _valid_plan(ctx, tool_name="send_intro", affected_user_ids=(99,))
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="introduce",
        visibility_scope="member",
    )

    await svc.cancel_action(
        action_id=action.id,
        cancelling_user_id=42,
        is_admin=False,
    )

    confirmations = await harness.confirmation_repo.list_for_action(harness.session, action.id)
    pending = [c for c in confirmations if c.status == "pending"]
    assert len(pending) == 0  # all should be cancelled/rejected


# ===========================================================================
# Tests — expire_action
# ===========================================================================


@pytest.mark.asyncio
async def test_expire_action_happy() -> None:
    """expire_action on pending_confirmation past expires_at → status='expired'."""
    ctx = _make_context()
    plan = _valid_plan(ctx)
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="test",
        visibility_scope="member",
    )

    # Force TTL to be in the past
    action.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)

    result = await svc.expire_action(action_id=action.id)

    assert result.status == "expired"


@pytest.mark.asyncio
async def test_expire_action_idempotent() -> None:
    """expire_action on already-expired action is idempotent (no error)."""
    ctx = _make_context()
    plan = _valid_plan(ctx)
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="test",
        visibility_scope="member",
    )
    action.status = "expired"
    action.expires_at = datetime.now(timezone.utc) - timedelta(hours=2)

    # Should not raise
    result = await svc.expire_action(action_id=action.id)
    assert result.status == "expired"


@pytest.mark.asyncio
async def test_expire_action_not_expired_yet_does_nothing() -> None:
    """expire_action on action not yet past TTL → returns action unchanged."""
    ctx = _make_context()
    plan = _valid_plan(ctx)
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="test",
        visibility_scope="member",
    )

    # TTL is in the future (set by plan_action)
    result = await svc.expire_action(action_id=action.id)

    # Should still be pending_confirmation since not expired
    assert result.status == "pending_confirmation"


# ===========================================================================
# Tests — exception hierarchy / error_kind
# ===========================================================================


def test_butler_action_error_carries_error_kind() -> None:
    """ButlerActionError carries error_kind attribute."""
    err = ButlerActionError("test", error_kind="tool_not_allowed")
    assert err.error_kind == "tool_not_allowed"


def test_butler_action_expired_error_is_subclass() -> None:
    """ButlerActionExpiredError is a subclass of ButlerActionError."""
    err = ButlerActionExpiredError("expired", error_kind="expired")
    assert isinstance(err, ButlerActionError)
    assert err.error_kind == "expired"


def test_butler_action_rejected_error_is_subclass() -> None:
    """ButlerActionRejectedError is a subclass of ButlerActionError."""
    err = ButlerActionRejectedError("rejected", error_kind="forbidden")
    assert isinstance(err, ButlerActionError)


def test_evidence_stale_error_is_subclass() -> None:
    """EvidenceStaleError is a subclass of ButlerActionError."""
    err = EvidenceStaleError("stale", error_kind="evidence_stale")
    assert isinstance(err, ButlerActionError)


def test_cascade_in_flight_error_is_subclass() -> None:
    """CascadeInFlightError is a subclass of ButlerActionError."""
    err = CascadeInFlightError("locked", error_kind="cascade_in_flight")
    assert isinstance(err, ButlerActionError)


def test_membership_revoked_error_is_subclass() -> None:
    """MembershipRevokedError is a subclass of ButlerActionError."""
    err = MembershipRevokedError("revoked", error_kind="membership_revoked")
    assert isinstance(err, ButlerActionError)


# ===========================================================================
# Tests — cascade-vs-callback race (advisory lock coordination)
# ===========================================================================


@pytest.mark.asyncio
async def test_cascade_in_flight_error_has_cascade_in_flight_kind() -> None:
    """The CascadeInFlightError raised when row is locked has error_kind='cascade_in_flight'."""
    ctx = _make_context()
    plan = _valid_plan(ctx)
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="test",
        visibility_scope="member",
    )

    # Simulate cascade lock
    harness.action_repo.locked_ids.add(action.id)

    with pytest.raises(CascadeInFlightError) as exc_info:
        await svc.confirm_action(
            action_id=action.id,
            confirming_user_id=42,
            confirmation_token="any-token",
        )

    assert exc_info.value.error_kind == "cascade_in_flight"
    assert exc_info.value.action_id == action.id


@pytest.mark.asyncio
async def test_plan_action_ledger_id_populated_post_plan() -> None:
    """Constraint #9: succeeded plan → llm_usage_ledger_id NOT NULL."""
    ctx = _make_context()
    plan = _valid_plan(ctx)
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan, ledger_id=42),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="test",
        visibility_scope="member",
    )

    assert action.llm_usage_ledger_id == 42
    assert action.status == "pending_confirmation"
    # Check the stored row directly
    stored = await harness.action_repo.get(harness.session, action.id)
    assert stored is not None
    assert stored.llm_usage_ledger_id == 42


# ===========================================================================
# New tests for T12-04 fix cycle
# ===========================================================================


# ── C1: confirmation_token storage + verification ────────────────────────────


@pytest.mark.asyncio
async def test_plan_action_generates_distinct_confirmation_tokens() -> None:
    """plan_action generates a unique token per confirmation row (C1)."""
    ctx = _make_context()
    plan = _valid_plan(ctx, tool_name="send_intro", affected_user_ids=(99,))
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="introduce me",
        visibility_scope="member",
    )

    confs = await harness.confirmation_repo.list_for_action(harness.session, action.id)
    assert len(confs) == 2
    tokens = {c.confirmation_token for c in confs}
    # All tokens must be non-empty and distinct
    assert all(t != "" for t in tokens)
    assert len(tokens) == 2  # distinct tokens


@pytest.mark.asyncio
async def test_confirm_action_wrong_token_raises_bad_token() -> None:
    """confirm_action with wrong token → error_kind='bad_token' (C1)."""
    ctx = _make_context()
    plan = _valid_plan(ctx)
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="test",
        visibility_scope="member",
    )

    with pytest.raises(ButlerActionError) as exc_info:
        await svc.confirm_action(
            action_id=action.id,
            confirming_user_id=42,
            confirmation_token="WRONG-TOKEN-THAT-NEVER-MATCHES",
        )

    assert exc_info.value.error_kind == "bad_token"


# ── C2/C3: query/visibility_scope/plan_payload stored + replayed ─────────────


@pytest.mark.asyncio
async def test_plan_action_stores_query_and_visibility_scope() -> None:
    """plan_action stores query and visibility_scope on the action row (C2)."""
    ctx = _make_context()
    plan = _valid_plan(ctx)
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="who knows Rust?",
        visibility_scope="admin",
    )

    assert action.query == "who knows Rust?"
    assert action.visibility_scope == "admin"


@pytest.mark.asyncio
async def test_plan_action_stores_plan_payload() -> None:
    """plan_action stores the serialized plan as plan_payload (C3)."""
    ctx = _make_context()
    plan = _valid_plan(ctx)
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="test",
        visibility_scope="member",
    )

    # plan_payload must be a non-empty dict (serialized plan)
    assert action.plan_payload is not None
    assert isinstance(action.plan_payload, dict)
    # Contains the serialized plan actions
    assert "actions" in action.plan_payload or len(action.plan_payload) > 0


# ── C6: execute_action re-checks affected confirmations ──────────────────────


@pytest.mark.asyncio
async def test_execute_action_rejects_if_affected_consent_revoked() -> None:
    """execute_action fails with affected_user_consent_revoked if consent was revoked (C6)."""
    ctx = _make_context()
    plan = _valid_plan(ctx, tool_name="send_intro", affected_user_ids=(99,))
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="introduce me",
        visibility_scope="member",
    )

    # Both requester (42) and affected user (99) confirm
    all_confs = await harness.confirmation_repo.list_for_action(harness.session, action.id)
    requester_conf = next(c for c in all_confs if c.confirmer_tg_id == 42)
    affected_conf = next(c for c in all_confs if c.confirmer_tg_id == 99)

    await svc.confirm_action(
        action_id=action.id,
        confirming_user_id=42,
        confirmation_token=requester_conf.confirmation_token,
    )
    await svc.confirm_action(
        action_id=action.id,
        confirming_user_id=99,
        confirmation_token=affected_conf.confirmation_token,
    )

    # Action is now 'confirmed'; simulate consent revocation by flipping affected_user back
    affected_conf.status = "rejected"  # cascade or cancel revoked consent

    with pytest.raises(ButlerActionRejectedError) as exc_info:
        await svc.execute_action(action_id=action.id)

    assert exc_info.value.error_kind == "affected_user_consent_revoked"


# ── H1: membership pre-check ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_plan_action_rejects_non_member_before_llm_call() -> None:
    """plan_action rejects a non-member before building evidence or calling LLM (H1)."""
    ctx = _make_context()
    plan = _valid_plan(ctx)
    # user_repo knows user 42 is NOT a member
    user_repo = FakeUserRepo(members=set(), admins=set())
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
        user_repo=user_repo,
    )
    svc = harness.make_service()

    with pytest.raises(MembershipRevokedError) as exc_info:
        await svc.plan_action(
            requester_user_id=42,
            chat_id=CHAT_ID,
            query="test",
            visibility_scope="member",
        )

    assert exc_info.value.error_kind == "membership_revoked"
    # An audit row must have been written (status='rejected')
    rows = [r for r in harness.action_repo._rows.values()]
    assert len(rows) == 1
    assert rows[0].status == "rejected"
    assert rows[0].rejection_reason == "membership_revoked"


@pytest.mark.asyncio
async def test_plan_action_member_proceeds_normally() -> None:
    """plan_action proceeds for a known member when user_repo is wired (H1)."""
    ctx = _make_context()
    plan = _valid_plan(ctx)
    user_repo = FakeUserRepo(members={42}, admins=set())
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
        user_repo=user_repo,
    )
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="test",
        visibility_scope="member",
    )

    assert action.status == "pending_confirmation"


# ── H2: cancel_action uses FOR UPDATE ────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_action_cascade_in_flight_raises() -> None:
    """cancel_action when cascade holds FOR UPDATE → CascadeInFlightError (H2)."""
    ctx = _make_context()
    plan = _valid_plan(ctx)
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="test",
        visibility_scope="member",
    )

    # Simulate cascade holding the lock
    harness.action_repo.locked_ids.add(action.id)

    with pytest.raises(CascadeInFlightError) as exc_info:
        await svc.cancel_action(
            action_id=action.id,
            cancelling_user_id=42,
            is_admin=False,
        )

    assert exc_info.value.error_kind == "cascade_in_flight"


# ── H3: rate-bucket completeness ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_plan_action_checks_chat_actions_day_bucket() -> None:
    """plan_action checks chat_actions_day rate bucket (H3)."""
    ctx = _make_context()
    plan = _valid_plan(ctx)
    # Fail on chat_actions_day bucket
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
        rate_bucket_repo=FakeButlerRateBucketRepo(fail_on_tool="chat_actions_day"),
    )
    svc = harness.make_service()

    with pytest.raises(ButlerActionError) as exc_info:
        await svc.plan_action(
            requester_user_id=42,
            chat_id=CHAT_ID,
            query="test",
            visibility_scope="member",
        )

    assert exc_info.value.error_kind == "rate_limit_exceeded"


@pytest.mark.asyncio
async def test_plan_action_checks_tool_hour_bucket() -> None:
    """plan_action checks tool_hour rate bucket for the primary tool (H3)."""
    ctx = _make_context()
    plan = _valid_plan(ctx)
    # Fail on tool_hour:recall_evidence bucket
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
        rate_bucket_repo=FakeButlerRateBucketRepo(fail_on_tool="tool_hour:recall_evidence"),
    )
    svc = harness.make_service()

    with pytest.raises(ButlerActionError) as exc_info:
        await svc.plan_action(
            requester_user_id=42,
            chat_id=CHAT_ID,
            query="test",
            visibility_scope="member",
        )

    assert exc_info.value.error_kind == "rate_limit_exceeded"


@pytest.mark.asyncio
async def test_execute_action_checks_user_execs_day_bucket() -> None:
    """execute_action checks user_execs_day rate bucket before executing (H3)."""
    ctx = _make_context()
    plan = _valid_plan(ctx)
    # Fail only on user_execs_day (so plan + confirm succeed)
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
        rate_bucket_repo=FakeButlerRateBucketRepo(fail_on_tool="user_execs_day"),
    )
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="test",
        visibility_scope="member",
    )
    all_confs = await harness.confirmation_repo.list_for_action(harness.session, action.id)
    await svc.confirm_action(
        action_id=action.id,
        confirming_user_id=42,
        confirmation_token=all_confs[0].confirmation_token,
    )

    with pytest.raises(ButlerActionError) as exc_info:
        await svc.execute_action(action_id=action.id)

    assert exc_info.value.error_kind == "rate_limit_exceeded"


# ── M2: cancel_action admin verification via user_repo ───────────────────────


@pytest.mark.asyncio
async def test_cancel_action_admin_verified_via_user_repo() -> None:
    """cancel_action verifies admin status via user_repo when available (M2)."""
    ctx = _make_context()
    plan = _valid_plan(ctx)
    # user 999 is an admin in the user_repo
    user_repo = FakeUserRepo(members={42}, admins={999})
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
        user_repo=user_repo,
    )
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="test",
        visibility_scope="member",
    )

    # Admin (999) cancels — should succeed
    result = await svc.cancel_action(
        action_id=action.id,
        cancelling_user_id=999,
        is_admin=False,  # service must verify via user_repo, not this flag
    )
    assert result.status == "cancelled"


@pytest.mark.asyncio
async def test_cancel_action_non_admin_forbidden_via_user_repo() -> None:
    """cancel_action with user_repo: non-admin, non-requester → forbidden (M2)."""
    ctx = _make_context()
    plan = _valid_plan(ctx)
    # user 888 is just a member, not an admin
    user_repo = FakeUserRepo(members={42, 888}, admins=set())
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
        user_repo=user_repo,
    )
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="test",
        visibility_scope="member",
    )

    with pytest.raises(ButlerActionRejectedError) as exc_info:
        await svc.cancel_action(
            action_id=action.id,
            cancelling_user_id=888,  # member but not admin or requester
            is_admin=True,  # caller passes True, but user_repo says they're not admin
        )

    assert exc_info.value.error_kind == "forbidden"


# ===========================================================================
# Tests — Fix3: invariant_broken on missing query/visibility_scope (F1)
# ===========================================================================


@pytest.mark.asyncio
async def test_confirm_action_raises_invariant_broken_when_query_is_none() -> None:
    """confirm_action raises ButlerActionError(invariant_broken) when action.query is None.

    After migration 074, query/visibility_scope are NOT NULL. A None value means
    schema invariant is broken — getattr default would silently mask this.
    """
    ctx = _make_context()
    plan = _valid_plan(ctx)
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="test query",
        visibility_scope="member",
    )

    # Get the real confirmation token before corrupting state
    all_confs = await harness.confirmation_repo.list_for_action(harness.session, action.id)
    conf = all_confs[0]
    real_token = conf.confirmation_token

    # Simulate a broken schema invariant: set query to None directly on the stored row
    stored = harness.action_repo._rows[action.id]
    stored.query = None  # type: ignore[assignment]

    # confirm_action navigates token validation (real token) then hits query=None
    with pytest.raises(ButlerActionError) as exc_info:
        await svc.confirm_action(
            action_id=action.id,
            confirming_user_id=42,
            confirmation_token=real_token,
        )

    assert exc_info.value.error_kind == "invariant_broken"


@pytest.mark.asyncio
async def test_confirm_action_raises_invariant_broken_when_visibility_scope_is_none() -> None:
    """confirm_action raises ButlerActionError(invariant_broken) when action.visibility_scope is None."""
    ctx = _make_context()
    plan = _valid_plan(ctx)
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="test query",
        visibility_scope="member",
    )

    # Get the real token before corrupting state
    all_confs = await harness.confirmation_repo.list_for_action(harness.session, action.id)
    conf = all_confs[0]
    real_token = conf.confirmation_token

    # Simulate broken schema: set visibility_scope to None
    stored = harness.action_repo._rows[action.id]
    stored.visibility_scope = None  # type: ignore[assignment]

    with pytest.raises(ButlerActionError) as exc_info:
        await svc.confirm_action(
            action_id=action.id,
            confirming_user_id=42,
            confirmation_token=real_token,
        )

    assert exc_info.value.error_kind == "invariant_broken"


# ===========================================================================
# Tests — Fix3: invariant_broken on invalid plan_payload (F2)
# ===========================================================================


@pytest.mark.asyncio
async def test_execute_action_raises_invariant_broken_on_empty_plan_payload() -> None:
    """execute_action raises ButlerActionError(invariant_broken) when plan_payload is empty.

    plan_payload must contain 'actions' key per plan.model_dump() shape.
    Empty dict or missing 'actions' key = invariant broken — synthesis fallback removed.
    """
    ctx = _make_context()
    plan = _valid_plan(ctx)
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="test",
        visibility_scope="member",
    )
    all_confs = await harness.confirmation_repo.list_for_action(harness.session, action.id)
    await svc.confirm_action(
        action_id=action.id,
        confirming_user_id=42,
        confirmation_token=all_confs[0].confirmation_token,
    )

    # Simulate broken invariant: clear plan_payload
    stored = harness.action_repo._rows[action.id]
    stored.plan_payload = {}  # missing 'actions' key

    with pytest.raises(ButlerActionError) as exc_info:
        await svc.execute_action(action_id=action.id)

    assert exc_info.value.error_kind == "invariant_broken"


@pytest.mark.asyncio
async def test_execute_action_raises_invariant_broken_on_none_plan_payload() -> None:
    """execute_action raises ButlerActionError(invariant_broken) when plan_payload is None."""
    ctx = _make_context()
    plan = _valid_plan(ctx)
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="test",
        visibility_scope="member",
    )
    all_confs = await harness.confirmation_repo.list_for_action(harness.session, action.id)
    await svc.confirm_action(
        action_id=action.id,
        confirming_user_id=42,
        confirmation_token=all_confs[0].confirmation_token,
    )

    # Simulate broken invariant: None plan_payload
    stored = harness.action_repo._rows[action.id]
    stored.plan_payload = None  # type: ignore[assignment]

    with pytest.raises(ButlerActionError) as exc_info:
        await svc.execute_action(action_id=action.id)

    assert exc_info.value.error_kind == "invariant_broken"


# ===========================================================================
# Tests — Fix3: rate-bucket rollback on partial failure (H1)
# ===========================================================================


class TrackingFakeButlerRateBucketRepo:
    """Rate bucket fake that tracks increments and decrements for H1 rollback testing."""

    def __init__(self, *, fail_on_kind: str | None = None) -> None:
        self._fail_on_kind = fail_on_kind
        # Track (bucket_kind, scope_id, bucket_key) tuples
        self.incremented: list[tuple[str, int, str]] = []
        self.decremented: list[tuple[str, int, str]] = []

    async def try_increment(
        self,
        session: Any,
        *,
        bucket_kind: str,
        scope_id: int,
        bucket_key: str,
        window_start: Any,
        window_end: Any,
        ceiling: int,
    ) -> bool:
        if self._fail_on_kind and self._fail_on_kind in bucket_kind:
            return False
        self.incremented.append((bucket_kind, scope_id, bucket_key))
        return True

    async def decrement(
        self,
        session: Any,
        *,
        bucket_kind: str,
        scope_id: int,
        bucket_key: str,
    ) -> None:
        self.decremented.append((bucket_kind, scope_id, bucket_key))


@pytest.mark.asyncio
async def test_plan_action_tool_hour_failure_decrements_prior_buckets() -> None:
    """When tool_hour rate-bucket fails, prior increments (user_plans_day, chat_actions_day)
    are rolled back via decrement calls — H1 fix.
    """
    ctx = _make_context()
    plan = _valid_plan(ctx)
    rate_repo = TrackingFakeButlerRateBucketRepo(fail_on_kind="tool_hour")
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
        rate_bucket_repo=rate_repo,
    )
    svc = harness.make_service()

    with pytest.raises(ButlerActionError) as exc_info:
        await svc.plan_action(
            requester_user_id=42,
            chat_id=CHAT_ID,
            query="test",
            visibility_scope="member",
        )

    assert exc_info.value.error_kind == "rate_limit_exceeded"
    # Two prior increments (user_plans_day + chat_actions_day) must have been decremented
    assert len(rate_repo.decremented) == 2
    decremented_kinds = {k for k, _, _ in rate_repo.decremented}
    assert "user_plans_day" in decremented_kinds
    assert "chat_actions_day" in decremented_kinds


# ===========================================================================
# Tests — Fix3: F3 NOWAIT OperationalError → CascadeInFlightError mapping
# ===========================================================================


class RaisingButlerActionRepo(FakeButlerActionRepo):
    """Fake that raises OperationalError on get_for_update (simulates NOWAIT contention)."""

    def __init__(self) -> None:
        super().__init__()
        self.raise_operational_error_ids: set[int] = set()

    async def get_for_update(self, session: Any, action_id: int) -> Any:
        if action_id in self.raise_operational_error_ids:
            # Simulate psycopg LockNotAvailable wrapped in SQLAlchemy OperationalError
            from sqlalchemy.exc import OperationalError
            raise OperationalError("statement", {}, Exception("LockNotAvailable"))
        return await super().get_for_update(session, action_id)


@pytest.mark.asyncio
async def test_confirm_action_operational_error_maps_to_cascade_in_flight() -> None:
    """OperationalError from NOWAIT get_for_update → CascadeInFlightError (F3).

    Real Postgres raises OperationalError wrapping psycopg.errors.LockNotAvailable
    when NOWAIT lock is contended. The service must catch it and raise CascadeInFlightError.
    """
    ctx = _make_context()
    plan = _valid_plan(ctx)
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    # Replace the action_repo with the raising variant
    raising_repo = RaisingButlerActionRepo()
    harness.action_repo = raising_repo
    svc = harness.make_service()

    # Create an action via the raising repo (not locked yet during plan)
    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="test",
        visibility_scope="member",
    )

    # Now simulate NOWAIT contention on confirm
    raising_repo.raise_operational_error_ids.add(action.id)

    all_confs = await harness.confirmation_repo.list_for_action(harness.session, action.id)

    with pytest.raises(CascadeInFlightError) as exc_info:
        await svc.confirm_action(
            action_id=action.id,
            confirming_user_id=42,
            confirmation_token=all_confs[0].confirmation_token,
        )

    assert exc_info.value.error_kind == "cascade_in_flight"


@pytest.mark.asyncio
async def test_cancel_action_operational_error_maps_to_cascade_in_flight() -> None:
    """OperationalError from NOWAIT get_for_update in cancel_action → CascadeInFlightError (F3)."""
    ctx = _make_context()
    plan = _valid_plan(ctx)
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    raising_repo = RaisingButlerActionRepo()
    harness.action_repo = raising_repo
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="test",
        visibility_scope="member",
    )

    raising_repo.raise_operational_error_ids.add(action.id)

    with pytest.raises(CascadeInFlightError) as exc_info:
        await svc.cancel_action(
            action_id=action.id,
            cancelling_user_id=42,
            is_admin=False,
        )

    assert exc_info.value.error_kind == "cascade_in_flight"


@pytest.mark.asyncio
async def test_execute_action_operational_error_maps_to_cascade_in_flight() -> None:
    """OperationalError from NOWAIT get_for_update in execute_action → CascadeInFlightError (F3).

    Real Postgres raises OperationalError wrapping psycopg.errors.LockNotAvailable
    when NOWAIT lock is contended during execute_action. The service must catch it and
    raise CascadeInFlightError(error_kind='cascade_in_flight').
    """
    ctx = _make_context()
    plan = _valid_plan(ctx)
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    raising_repo = RaisingButlerActionRepo()
    harness.action_repo = raising_repo
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="test",
        visibility_scope="member",
    )

    # Confirm without contention first
    all_confs = await harness.confirmation_repo.list_for_action(harness.session, action.id)
    await svc.confirm_action(
        action_id=action.id,
        confirming_user_id=42,
        confirmation_token=all_confs[0].confirmation_token,
    )

    # Now simulate NOWAIT contention on execute
    raising_repo.raise_operational_error_ids.add(action.id)

    with pytest.raises(CascadeInFlightError) as exc_info:
        await svc.execute_action(action_id=action.id)

    assert exc_info.value.error_kind == "cascade_in_flight"


# ===========================================================================
# Tests — revoke_affected_user_consent (C1 T12-05-fix)
# ===========================================================================


@pytest.mark.asyncio
async def test_revoke_affected_user_consent_happy_path() -> None:
    """Affected user revokes consent → confirmation row 'revoked', action 'cancelled'."""
    ctx = _make_context()
    plan = _valid_plan(ctx, tool_name="send_intro", affected_user_ids=(99,))
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="send intro to @alice",
        visibility_scope="member",
    )

    # Invoke the new method as the affected user (user 99)
    result = await svc.revoke_affected_user_consent(
        action_id=action.id,
        affected_user_id=99,
    )

    # Action should be cancelled
    assert result.status == "cancelled"

    # Confirmation row should be 'revoked'
    confs = await harness.confirmation_repo.list_for_action(harness.session, action.id)
    affected_conf = next(c for c in confs if c.confirmer_tg_id == 99)
    assert affected_conf.status == "revoked"


@pytest.mark.asyncio
async def test_revoke_affected_user_consent_wrong_user_raises_not_found() -> None:
    """revoke_affected_user_consent by non-affected user → ButlerActionNotFoundError
    (or ButlerActionRejectedError with not_found kind)."""
    ctx = _make_context()
    plan = _valid_plan(ctx, tool_name="send_intro", affected_user_ids=(99,))
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="send intro",
        visibility_scope="member",
    )

    # User 777 is NOT an affected user on this action
    with pytest.raises(ButlerActionError) as exc_info:
        await svc.revoke_affected_user_consent(
            action_id=action.id,
            affected_user_id=777,
        )

    assert exc_info.value.error_kind in ("not_found", "forbidden")


@pytest.mark.asyncio
async def test_revoke_affected_user_consent_already_terminal_raises_wrong_status() -> None:
    """revoke_affected_user_consent when confirmation already confirmed → wrong_status."""
    ctx = _make_context()
    plan = _valid_plan(ctx, tool_name="send_intro", affected_user_ids=(99,))
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
    )
    svc = harness.make_service()

    action = await svc.plan_action(
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="send intro",
        visibility_scope="member",
    )

    # Manually confirm the affected user's row
    confs = await harness.confirmation_repo.list_for_action(harness.session, action.id)
    affected_conf = next(c for c in confs if c.confirmer_tg_id == 99)
    affected_conf.status = "confirmed"

    with pytest.raises(ButlerActionError) as exc_info:
        await svc.revoke_affected_user_consent(
            action_id=action.id,
            affected_user_id=99,
        )

    assert exc_info.value.error_kind == "wrong_status"


@pytest.mark.asyncio
async def test_plan_action_chat_rate_limit_rolls_back_user_plans_day() -> None:
    """When chat_actions_day is exceeded, user_plans_day increment is rolled back (Fix 1).

    user_plans_day is successfully incremented before chat_actions_day is checked.
    If chat_actions_day fails, the earlier user_plans_day increment must be decremented
    so the user's daily quota is not permanently consumed.
    """
    ctx = _make_context()
    plan = _valid_plan(ctx)
    rate_repo = TrackingFakeButlerRateBucketRepo(fail_on_kind="chat_actions_day")
    harness = _ButlerServiceTestHarness(
        gateway=FakeLLMGateway(plan=plan),
        evidence_builder=FakeEvidenceBuilder(context=ctx),
        rate_bucket_repo=rate_repo,
    )
    svc = harness.make_service()

    with pytest.raises(ButlerActionError) as exc_info:
        await svc.plan_action(
            requester_user_id=42,
            chat_id=CHAT_ID,
            query="test",
            visibility_scope="member",
        )

    assert exc_info.value.error_kind == "rate_limit_exceeded"
    # user_plans_day must have been incremented first, then rolled back via decrement
    assert ("user_plans_day", 42, rate_repo.decremented[0][2]) in [
        (b, s, k) for b, s, k in rate_repo.decremented
    ] or any(b == "user_plans_day" for b, _, _ in rate_repo.decremented), (
        f"Expected user_plans_day rollback decrement, got decremented={rate_repo.decremented}"
    )
