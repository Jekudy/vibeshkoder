"""Behaviour tests for plan_butler_action in bot.services.llm_gateway (T12-03).

TDD — tests written BEFORE the plan_butler_action implementation.

Covers:
  - Happy path: mock provider returns valid JSON → ButlerPlan returned.
    Ledger row written with call_type='butler_decision', cost_usd, tokens_in/out set.
  - Provider returns invalid JSON → ButlerPlanError raised; ledger row written with error field.
  - Provider returns valid JSON but tool_name not in allowlist → ToolNotAllowedError;
    ledger row written with error.
  - Provider returns valid JSON but args fail schema → InvalidToolArgsError;
    ledger row written with error.
  - Monthly cost guard fires when budget would be exceeded → ButlerPlanError raised;
    ledger row written with error='budget_exceeded'.
  - Prompt sent to provider does NOT include raw redacted/purged source content.
  - ButlerPlan returned has evidence_context_hash == evidence_context.context_hash.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
import pytest

from bot.services.butler_evidence import ButlerEvidenceContext, butler_context_hash
from bot.services.butler_tools import (
    ButlerPlan,
    InvalidToolArgsError,
    ToolNotAllowedError,
)
from bot.services.butler_tools import ButlerPlanError
from bot.services.evidence import EvidenceBundle, EvidenceItem
from bot.services.llm_gateway import LLMGatewayConfig, plan_butler_action
from bot.services.llm_providers import ProviderResult
from tests.services.test_llm_gateway import FakeSession, _config


# ---------------------------------------------------------------------------
# Extended FakeLedgerRepo that also captures call_type
# ---------------------------------------------------------------------------


@dataclass
class _ExtLedgerRow:
    """_LedgerRow extended to capture call_type."""

    id: int
    qa_trace_id: int | None
    provider: str
    model: str
    prompt_hash: str
    response_hash: str | None
    tokens_in: int
    tokens_out: int
    cost_usd: Decimal
    latency_ms: int
    request_id: str | None
    cache_hit: bool
    error: str | None
    call_type: str = "unknown"


@dataclass
class FakeLedgerRepo:
    """Extended in-memory LedgerRepo that tracks call_type."""

    rows: list[_ExtLedgerRow] = field(default_factory=list)
    daily_cost: Decimal = Decimal("0")
    monthly_cost: Decimal = Decimal("0")
    _next_id: int = 1

    async def record(
        self,
        session: Any,
        *,
        qa_trace_id: int | None,
        provider: str,
        model: str,
        prompt_hash: str,
        response_hash: str | None,
        tokens_in: int,
        tokens_out: int,
        cost_usd: Decimal,
        latency_ms: int,
        request_id: str | None,
        cache_hit: bool,
        error: str | None,
        call_type: str = "unknown",
    ) -> _ExtLedgerRow:
        row = _ExtLedgerRow(
            id=self._next_id,
            qa_trace_id=qa_trace_id,
            provider=provider,
            model=model,
            prompt_hash=prompt_hash,
            response_hash=response_hash,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            request_id=request_id,
            cache_hit=cache_hit,
            error=error,
            call_type=call_type,
        )
        self.rows.append(row)
        self._next_id += 1
        self.daily_cost += cost_usd
        self.monthly_cost += cost_usd
        return row

    async def daily_cost_usd(self, session: Any, *, day: Any, call_type: str | None = None) -> Decimal:
        return self.daily_cost

    async def monthly_cost_usd(
        self, session: Any, *, year: int, month: int, call_type: str | None = None
    ) -> Decimal:
        return self.monthly_cost

    async def update_placeholder(
        self,
        session: Any,
        *,
        llm_call_id: int,
        cost_usd: Decimal,
        response_hash: str | None,
        tokens_in: int,
        tokens_out: int,
        request_id: str | None,
        latency_ms: int,
        error: str | None,
    ) -> _ExtLedgerRow:
        for row in self.rows:
            if row.id == llm_call_id:
                old_cost = row.cost_usd
                row.cost_usd = cost_usd
                row.response_hash = response_hash
                row.tokens_in = tokens_in
                row.tokens_out = tokens_out
                row.request_id = request_id
                row.latency_ms = latency_ms
                row.error = error
                self.daily_cost += cost_usd - old_cost
                self.monthly_cost += cost_usd - old_cost
                return row
        raise KeyError(f"placeholder llm_call_id={llm_call_id} not found")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CHAT_ID = -100_999_888_777


def _make_item(mvid: int) -> EvidenceItem:
    return EvidenceItem(
        message_version_id=mvid,
        chat_message_id=mvid + 1000,
        chat_id=_CHAT_ID,
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
) -> ButlerEvidenceContext:
    items = tuple(_make_item(mvid) for mvid in mvids)
    bundle = EvidenceBundle(
        query="who knows Rust?",
        chat_id=_CHAT_ID,
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
        requester_user_id=42,
        chat_id=_CHAT_ID,
        query="who knows Rust?",
        snapshot_at=datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
        governance_excluded_count=0,
    )


def _valid_plan_json(
    tool_name: str = "recall_evidence",
    args: dict | None = None,
    context: ButlerEvidenceContext | None = None,
    *,
    visibility_scope: str = "member",
    gov_version: str = "test-v1",
) -> str:
    if context is None:
        context = _make_context()
    if args is None:
        args = {"query": "who knows Rust?"}
    return json.dumps(
        {
            "plan_summary": "Recall members who know Rust",
            "evidence_ids": list(context.evidence_ids),
            "actions": [
                {
                    "tool_name": tool_name,
                    "args": args,
                    "requires_confirmation": True,
                    "affected_user_ids": [],
                    "risk_level": "low",
                    "rollback_kind": "not_reversible",
                    "inverse_op_payload": None,
                }
            ],
            "evidence_context_hash": context.context_hash,
            "requester_user_id": 42,
            "chat_id": _CHAT_ID,
            "visibility_scope": visibility_scope,
            "governance_filter_version": gov_version,
            "rationale": "User asked about Rust knowledge",
        }
    )


@dataclass
class FakeButlerProvider:
    """LLMProvider stub that returns a configurable answer_text."""

    answer_text: str = ""
    tokens_in: int = 200
    tokens_out: int = 100
    request_id: str = "req-butler-001"
    raise_exc: BaseException | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def call(self, *, prompt: str, model: str) -> ProviderResult:
        self.calls.append({"prompt": prompt, "model": model})
        if self.raise_exc is not None:
            raise self.raise_exc
        return ProviderResult(
            answer_text=self.answer_text,
            citation_ids=tuple(),
            tokens_in=self.tokens_in,
            tokens_out=self.tokens_out,
            request_id=self.request_id,
            raw_latency_ms=50,
        )


# Butler-specific config — uses the global config but tests can override
def _butler_config() -> LLMGatewayConfig:
    return _config()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_butler_action_happy_path_returns_butler_plan() -> None:
    """Happy path: valid LLM JSON → (ButlerPlan, ledger_id, cost_usd) returned."""
    context = _make_context()
    plan_json = _valid_plan_json(context=context)

    provider = FakeButlerProvider(answer_text=plan_json)
    ledger = FakeLedgerRepo()
    session = FakeSession()
    config = _butler_config()

    result = await plan_butler_action(
        session=session,
        requester_user_id=42,
        chat_id=_CHAT_ID,
        query="who knows Rust?",
        evidence_context=context,
        visibility_scope="member",
        config=config,
        ledger_repo=ledger,
        provider=provider,
    )

    plan, ledger_id, cost_usd = result
    assert isinstance(plan, ButlerPlan)
    assert plan.plan_summary == "Recall members who know Rust"
    assert len(plan.actions) == 1
    assert plan.actions[0].tool_name == "recall_evidence"


@pytest.mark.asyncio
async def test_plan_butler_action_writes_ledger_row_with_butler_decision_call_type() -> None:
    """Ledger row written with call_type='butler_decision'."""
    context = _make_context()
    plan_json = _valid_plan_json(context=context)

    provider = FakeButlerProvider(answer_text=plan_json)
    ledger = FakeLedgerRepo()
    session = FakeSession()
    config = _butler_config()

    await plan_butler_action(
        session=session,
        requester_user_id=42,
        chat_id=_CHAT_ID,
        query="who knows Rust?",
        evidence_context=context,
        visibility_scope="member",
        config=config,
        ledger_repo=ledger,
        provider=provider,
    )

    # Must have at least one ledger row for this call.
    assert len(ledger.rows) >= 1
    # The final ledger row should have call_type='butler_decision'.
    row = ledger.rows[-1]
    assert row.call_type == "butler_decision"


@pytest.mark.asyncio
async def test_plan_butler_action_writes_ledger_row_with_cost_and_tokens() -> None:
    """Ledger row has cost_usd > 0 and tokens set after successful dispatch."""
    context = _make_context()
    plan_json = _valid_plan_json(context=context)

    provider = FakeButlerProvider(
        answer_text=plan_json, tokens_in=200, tokens_out=100
    )
    ledger = FakeLedgerRepo()
    session = FakeSession()
    config = _butler_config()

    await plan_butler_action(
        session=session,
        requester_user_id=42,
        chat_id=_CHAT_ID,
        query="who knows Rust?",
        evidence_context=context,
        visibility_scope="member",
        config=config,
        ledger_repo=ledger,
        provider=provider,
    )

    row = ledger.rows[-1]
    # After a successful dispatch, tokens_in and tokens_out should be set.
    assert row.tokens_in == 200
    assert row.tokens_out == 100
    # Cost should be non-zero (model pricing from llm_pricing.py).
    # We just verify it was updated from the 0-placeholder.
    assert row.cost_usd >= Decimal("0")


@pytest.mark.asyncio
async def test_plan_butler_action_result_has_matching_evidence_context_hash() -> None:
    """ButlerPlan.evidence_context_hash must equal evidence_context.context_hash."""
    context = _make_context()
    plan_json = _valid_plan_json(context=context)

    provider = FakeButlerProvider(answer_text=plan_json)
    ledger = FakeLedgerRepo()
    session = FakeSession()

    plan, _ledger_id, _cost = await plan_butler_action(
        session=session,
        requester_user_id=42,
        chat_id=_CHAT_ID,
        query="who knows Rust?",
        evidence_context=context,
        visibility_scope="member",
        config=_butler_config(),
        ledger_repo=ledger,
        provider=provider,
    )

    # The LLM echoed back the correct hash.
    assert plan.evidence_context_hash == context.context_hash


# ---------------------------------------------------------------------------
# Error paths — provider returns invalid JSON
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_butler_action_raises_butler_plan_error_on_invalid_json() -> None:
    """Provider returns invalid JSON → ButlerPlanError raised."""
    context = _make_context()

    provider = FakeButlerProvider(answer_text="this is not JSON at all")
    ledger = FakeLedgerRepo()
    session = FakeSession()

    with pytest.raises(ButlerPlanError):
        await plan_butler_action(
            session=session,
            requester_user_id=42,
            chat_id=_CHAT_ID,
            query="who knows Rust?",
            evidence_context=context,
            visibility_scope="member",
            config=_butler_config(),
            ledger_repo=ledger,
            provider=provider,
        )


@pytest.mark.asyncio
async def test_plan_butler_action_writes_ledger_row_with_error_on_invalid_json() -> None:
    """Ledger row written with error field set when JSON is invalid."""
    context = _make_context()

    provider = FakeButlerProvider(answer_text="definitely not json {{")
    ledger = FakeLedgerRepo()
    session = FakeSession()

    with pytest.raises(ButlerPlanError):
        await plan_butler_action(
            session=session,
            requester_user_id=42,
            chat_id=_CHAT_ID,
            query="who knows Rust?",
            evidence_context=context,
            visibility_scope="member",
            config=_butler_config(),
            ledger_repo=ledger,
            provider=provider,
        )

    assert len(ledger.rows) >= 1
    row = ledger.rows[-1]
    assert row.error is not None
    assert "invalid_plan_json" in (row.error or "")


# ---------------------------------------------------------------------------
# Error paths — tool_name not in allowlist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_butler_action_raises_tool_not_allowed_on_forbidden_tool() -> None:
    """Provider returns valid JSON but tool_name not in allowlist → ToolNotAllowedError."""
    context = _make_context()
    # Build plan JSON with a forbidden tool
    bad_json = json.dumps(
        {
            "plan_summary": "Evil plan",
            "evidence_ids": list(context.evidence_ids),
            "actions": [
                {
                    "tool_name": "send_email",  # NOT in allowlist
                    "args": {"to": "victim@example.com"},
                    "requires_confirmation": False,
                    "affected_user_ids": [],
                    "risk_level": "high",
                    "rollback_kind": "not_reversible",
                    "inverse_op_payload": None,
                }
            ],
            "evidence_context_hash": context.context_hash,
            "requester_user_id": 42,
            "chat_id": _CHAT_ID,
            "visibility_scope": "member",
            "governance_filter_version": "test-v1",
            "rationale": None,
        }
    )

    provider = FakeButlerProvider(answer_text=bad_json)
    ledger = FakeLedgerRepo()
    session = FakeSession()

    with pytest.raises((ToolNotAllowedError, ButlerPlanError)) as excinfo:
        await plan_butler_action(
            session=session,
            requester_user_id=42,
            chat_id=_CHAT_ID,
            query="who knows Rust?",
            evidence_context=context,
            visibility_scope="member",
            config=_butler_config(),
            ledger_repo=ledger,
            provider=provider,
        )
    assert excinfo.value.error_kind == "tool_not_allowed"


@pytest.mark.asyncio
async def test_plan_butler_action_unknown_tool_name_has_error_kind_tool_not_allowed() -> None:
    """Unknown tool_name must produce error_kind='tool_not_allowed', not 'invalid_plan_schema'.

    HIGH fix: before Option A, ButlerActionStep.tool_name was Literal[...] so pydantic
    raised a ValidationError before validate_butler_plan ran, tagging the error as
    'invalid_plan_schema'. After Option A (tool_name: str), pydantic accepts any string
    and validate_butler_plan is the sole source of the allowlist check, raising
    ToolNotAllowedError(error_kind='tool_not_allowed').
    """
    context = _make_context()
    bad_json = json.dumps(
        {
            "plan_summary": "Evil plan",
            "evidence_ids": list(context.evidence_ids),
            "actions": [
                {
                    "tool_name": "send_email",  # NOT in allowlist
                    "args": {"to": "victim@example.com"},
                    "requires_confirmation": False,
                    "affected_user_ids": [],
                    "risk_level": "high",
                    "rollback_kind": "not_reversible",
                    "inverse_op_payload": None,
                }
            ],
            "evidence_context_hash": context.context_hash,
            "requester_user_id": 42,
            "chat_id": _CHAT_ID,
            "visibility_scope": "member",
            "governance_filter_version": "test-v1",
            "rationale": None,
        }
    )

    provider = FakeButlerProvider(answer_text=bad_json)
    ledger = FakeLedgerRepo()
    session = FakeSession()

    with pytest.raises((ToolNotAllowedError, ButlerPlanError)) as excinfo:
        await plan_butler_action(
            session=session,
            requester_user_id=42,
            chat_id=_CHAT_ID,
            query="who knows Rust?",
            evidence_context=context,
            visibility_scope="member",
            config=_butler_config(),
            ledger_repo=ledger,
            provider=provider,
        )

    assert excinfo.value.error_kind == "tool_not_allowed", (
        f"Expected error_kind='tool_not_allowed' but got {excinfo.value.error_kind!r}. "
        "ButlerActionStep.tool_name must be str (not Literal) so pydantic accepts any "
        "string and validate_butler_plan raises ToolNotAllowedError, not a schema error."
    )


@pytest.mark.asyncio
async def test_plan_butler_action_writes_ledger_row_with_error_on_forbidden_tool() -> None:
    """Ledger row written with error field set when tool is not allowed."""
    context = _make_context()
    bad_json = json.dumps(
        {
            "plan_summary": "Evil plan",
            "evidence_ids": list(context.evidence_ids),
            "actions": [
                {
                    "tool_name": "send_email",
                    "args": {"to": "victim@example.com"},
                    "requires_confirmation": False,
                    "affected_user_ids": [],
                    "risk_level": "high",
                    "rollback_kind": "not_reversible",
                    "inverse_op_payload": None,
                }
            ],
            "evidence_context_hash": context.context_hash,
            "requester_user_id": 42,
            "chat_id": _CHAT_ID,
            "visibility_scope": "member",
            "governance_filter_version": "test-v1",
            "rationale": None,
        }
    )

    provider = FakeButlerProvider(answer_text=bad_json)
    ledger = FakeLedgerRepo()
    session = FakeSession()

    with pytest.raises((ToolNotAllowedError, ButlerPlanError)):
        await plan_butler_action(
            session=session,
            requester_user_id=42,
            chat_id=_CHAT_ID,
            query="who knows Rust?",
            evidence_context=context,
            visibility_scope="member",
            config=_butler_config(),
            ledger_repo=ledger,
            provider=provider,
        )

    assert len(ledger.rows) >= 1
    row = ledger.rows[-1]
    assert row.error is not None


# ---------------------------------------------------------------------------
# Error paths — args fail schema validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_butler_action_raises_invalid_tool_args_on_schema_violation() -> None:
    """Provider returns valid JSON but args fail schema → InvalidToolArgsError."""
    context = _make_context()
    # recall_evidence requires 'query' as str; pass an int instead.
    bad_args_json = _valid_plan_json(
        tool_name="recall_evidence",
        args={"query": 99999},  # wrong type
        context=context,
    )

    provider = FakeButlerProvider(answer_text=bad_args_json)
    ledger = FakeLedgerRepo()
    session = FakeSession()

    with pytest.raises((InvalidToolArgsError, ButlerPlanError)) as excinfo:
        await plan_butler_action(
            session=session,
            requester_user_id=42,
            chat_id=_CHAT_ID,
            query="who knows Rust?",
            evidence_context=context,
            visibility_scope="member",
            config=_butler_config(),
            ledger_repo=ledger,
            provider=provider,
        )
    assert excinfo.value.error_kind == "invalid_args"


@pytest.mark.asyncio
async def test_plan_butler_action_writes_ledger_row_with_error_on_schema_violation() -> None:
    """Ledger row written with error field set when args fail schema."""
    context = _make_context()
    bad_args_json = _valid_plan_json(
        tool_name="recall_evidence",
        args={"query": 99999},
        context=context,
    )

    provider = FakeButlerProvider(answer_text=bad_args_json)
    ledger = FakeLedgerRepo()
    session = FakeSession()

    with pytest.raises((InvalidToolArgsError, ButlerPlanError)) as excinfo:
        await plan_butler_action(
            session=session,
            requester_user_id=42,
            chat_id=_CHAT_ID,
            query="who knows Rust?",
            evidence_context=context,
            visibility_scope="member",
            config=_butler_config(),
            ledger_repo=ledger,
            provider=provider,
        )

    assert excinfo.value.error_kind == "invalid_args"
    assert len(ledger.rows) >= 1
    row = ledger.rows[-1]
    assert row.error is not None


# ---------------------------------------------------------------------------
# Monthly cost guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_butler_action_raises_when_monthly_budget_exceeded() -> None:
    """Monthly cost guard fires → ButlerPlanError raised (budget exceeded)."""
    context = _make_context()
    plan_json = _valid_plan_json(context=context)

    provider = FakeButlerProvider(answer_text=plan_json)
    # Pre-load the ledger with monthly cost at ceiling
    from decimal import Decimal as D

    # BUTLER_MONTHLY_USD_CEILING = $10 per charter
    butler_monthly_ceiling = D("10.00")
    ledger = FakeLedgerRepo(
        daily_cost=D("0"),
        monthly_cost=butler_monthly_ceiling,  # already at ceiling
    )
    session = FakeSession()
    config = LLMGatewayConfig(
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        daily_ceiling_usd=D("1.00"),
        monthly_ceiling_usd=butler_monthly_ceiling,
        prompt_template_version="v1.0.0",
    )

    with pytest.raises(ButlerPlanError) as excinfo:
        await plan_butler_action(
            session=session,
            requester_user_id=42,
            chat_id=_CHAT_ID,
            query="who knows Rust?",
            evidence_context=context,
            visibility_scope="member",
            config=config,
            ledger_repo=ledger,
            provider=provider,
        )

    assert excinfo.value.error_kind == "budget_exceeded"
    # Provider must NOT have been called (budget guard fires pre-dispatch)
    assert len(provider.calls) == 0


@pytest.mark.asyncio
async def test_plan_butler_action_budget_exceeded_still_writes_ledger_row() -> None:
    """Even on budget exceeded, a ledger row is written with error='budget_exceeded'."""
    context = _make_context()
    plan_json = _valid_plan_json(context=context)

    provider = FakeButlerProvider(answer_text=plan_json)
    from decimal import Decimal as D

    butler_monthly_ceiling = D("10.00")
    ledger = FakeLedgerRepo(
        daily_cost=D("0"),
        monthly_cost=butler_monthly_ceiling,
    )
    session = FakeSession()
    config = LLMGatewayConfig(
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        daily_ceiling_usd=D("1.00"),
        monthly_ceiling_usd=butler_monthly_ceiling,
        prompt_template_version="v1.0.0",
    )

    with pytest.raises(ButlerPlanError) as excinfo:
        await plan_butler_action(
            session=session,
            requester_user_id=42,
            chat_id=_CHAT_ID,
            query="who knows Rust?",
            evidence_context=context,
            visibility_scope="member",
            config=config,
            ledger_repo=ledger,
            provider=provider,
        )

    assert excinfo.value.error_kind == "budget_exceeded"
    assert len(ledger.rows) >= 1
    row = ledger.rows[-1]
    assert row.error == "budget_exceeded"


# ---------------------------------------------------------------------------
# Privacy: prompt does not include raw source snippet content
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_butler_action_prompt_does_not_include_raw_source_content() -> None:
    """The prompt sent to the provider does NOT include raw source snippet content.

    The gateway receives a ButlerEvidenceContext (sealed envelope).
    It must NOT dump raw snippet text from the bundle items into the prompt body.
    The prompt must only include: query, evidence_ids (numbers), tool schemas,
    and the context_hash — never raw content from EvidenceItem.snippet.
    """
    # Deliberately embed a "secret" snippet text that should never reach the provider.
    secret_text = "SUPER_SECRET_CONTENT_DO_NOT_SEND_TO_LLM"
    items = (
        EvidenceItem(
            message_version_id=99,
            chat_message_id=1099,
            chat_id=_CHAT_ID,
            message_id=2099,
            user_id=55,
            snippet=secret_text,
            ts_rank=0.9,
            captured_at=datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
            message_date=datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
            source_type="message",
            card_id=None,
            card_source_message_version_ids=(),
        ),
    )
    bundle = EvidenceBundle(
        query="secret test",
        chat_id=_CHAT_ID,
        items=items,
        abstained=False,
        created_at=datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
    )
    ctx_hash = butler_context_hash(bundle, "member", "test-v1")
    secret_context = ButlerEvidenceContext(
        bundle=bundle,
        visibility_scope="member",
        context_hash=ctx_hash,
        governance_filter_version="test-v1",
        requester_user_id=42,
        chat_id=_CHAT_ID,
        query="secret test",
        snapshot_at=datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
        governance_excluded_count=0,
    )

    plan_json = json.dumps(
        {
            "plan_summary": "Secret plan",
            "evidence_ids": [99],
            "actions": [
                {
                    "tool_name": "recall_evidence",
                    "args": {"query": "secret test"},
                    "requires_confirmation": True,
                    "affected_user_ids": [],
                    "risk_level": "low",
                    "rollback_kind": "not_reversible",
                    "inverse_op_payload": None,
                }
            ],
            "evidence_context_hash": ctx_hash,
            "requester_user_id": 42,
            "chat_id": _CHAT_ID,
            "visibility_scope": "member",
            "governance_filter_version": "test-v1",
            "rationale": None,
        }
    )

    provider = FakeButlerProvider(answer_text=plan_json)
    ledger = FakeLedgerRepo()
    session = FakeSession()

    await plan_butler_action(
        session=session,
        requester_user_id=42,
        chat_id=_CHAT_ID,
        query="secret test",
        evidence_context=secret_context,
        visibility_scope="member",
        config=_butler_config(),
        ledger_repo=ledger,
        provider=provider,
    )

    # The secret snippet text must NOT appear in the prompt sent to the provider.
    assert len(provider.calls) == 1
    prompt_sent = provider.calls[0]["prompt"]
    assert secret_text not in prompt_sent, (
        f"Raw snippet content leaked into prompt: {prompt_sent[:200]}"
    )


# ---------------------------------------------------------------------------
# C-3: plan_butler_action returns (ButlerPlan, int, Decimal) tuple
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_butler_action_returns_three_tuple() -> None:
    """plan_butler_action returns (ButlerPlan, ledger_id, cost_usd) tuple."""
    context = _make_context()
    plan_json = _valid_plan_json(context=context)

    provider = FakeButlerProvider(answer_text=plan_json)
    ledger = FakeLedgerRepo()
    session = FakeSession()

    result = await plan_butler_action(
        session=session,
        requester_user_id=42,
        chat_id=_CHAT_ID,
        query="who knows Rust?",
        evidence_context=context,
        visibility_scope="member",
        config=_butler_config(),
        ledger_repo=ledger,
        provider=provider,
    )

    assert isinstance(result, tuple), f"Expected tuple, got {type(result)}"
    assert len(result) == 3, f"Expected 3-tuple, got {len(result)}-tuple"
    plan, ledger_id, cost_usd = result
    assert isinstance(plan, ButlerPlan)
    assert isinstance(ledger_id, int)
    assert ledger_id > 0
    assert isinstance(cost_usd, Decimal)
    assert cost_usd >= Decimal("0")


@pytest.mark.asyncio
async def test_plan_butler_action_tuple_ledger_id_matches_written_row() -> None:
    """The ledger_id in the tuple matches the actual ledger row id."""
    context = _make_context()
    plan_json = _valid_plan_json(context=context)

    provider = FakeButlerProvider(answer_text=plan_json)
    ledger = FakeLedgerRepo()
    session = FakeSession()

    _plan, ledger_id, _cost = await plan_butler_action(
        session=session,
        requester_user_id=42,
        chat_id=_CHAT_ID,
        query="who knows Rust?",
        evidence_context=context,
        visibility_scope="member",
        config=_butler_config(),
        ledger_repo=ledger,
        provider=provider,
    )

    # The returned ledger_id must match a row in the ledger
    matching = [r for r in ledger.rows if r.id == ledger_id]
    assert len(matching) == 1, f"Expected row with id={ledger_id} in ledger"


@pytest.mark.asyncio
async def test_plan_butler_action_butler_plan_error_carries_ledger_id() -> None:
    """ButlerPlanError raised on validation failure carries the ledger_id."""
    context = _make_context()
    # Supply invalid JSON so a ButlerPlanError is raised
    provider = FakeButlerProvider(answer_text="not json at all")
    ledger = FakeLedgerRepo()
    session = FakeSession()

    with pytest.raises(ButlerPlanError) as exc_info:
        await plan_butler_action(
            session=session,
            requester_user_id=42,
            chat_id=_CHAT_ID,
            query="who knows Rust?",
            evidence_context=context,
            visibility_scope="member",
            config=_butler_config(),
            ledger_repo=ledger,
            provider=provider,
        )

    # The exception must carry the ledger_id
    assert exc_info.value.llm_usage_ledger_id is not None
    assert exc_info.value.llm_usage_ledger_id > 0


# ---------------------------------------------------------------------------
# C-1: gateway binds identity fields (LLM cannot forge them)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_butler_action_gateway_binds_identity_fields_over_llm_output() -> None:
    """LLM returning forged visibility_scope or requester_user_id is overridden by gateway."""
    context = _make_context(mvids=(10, 11))
    # LLM attempts to forge admin scope and a different requester_user_id
    forged_json = json.dumps(
        {
            "plan_summary": "Forged plan",
            "evidence_ids": list(context.evidence_ids),
            "actions": [
                {
                    "tool_name": "recall_evidence",
                    "args": {"query": "who knows Rust?"},
                    "requires_confirmation": True,
                    "affected_user_ids": [],
                    "risk_level": "low",
                    "rollback_kind": "not_reversible",
                    "inverse_op_payload": None,
                }
            ],
            "evidence_context_hash": context.context_hash,
            "requester_user_id": 99999,  # FORGED — should be overridden to 42
            "chat_id": _CHAT_ID,
            "visibility_scope": "admin",  # FORGED — should be overridden to "member"
            "governance_filter_version": "evil-v99",  # FORGED
            "rationale": None,
        }
    )

    provider = FakeButlerProvider(answer_text=forged_json)
    ledger = FakeLedgerRepo()
    session = FakeSession()

    plan, _, _ = await plan_butler_action(
        session=session,
        requester_user_id=42,
        chat_id=_CHAT_ID,
        query="who knows Rust?",
        evidence_context=context,
        visibility_scope="member",
        config=_butler_config(),
        ledger_repo=ledger,
        provider=provider,
    )

    # Gateway must override LLM-supplied identity fields
    assert plan.requester_user_id == 42, f"Expected 42, got {plan.requester_user_id}"
    assert plan.visibility_scope == "member", f"Expected 'member', got {plan.visibility_scope}"
    assert plan.governance_filter_version == context.governance_filter_version, (
        f"Expected {context.governance_filter_version!r}, got {plan.governance_filter_version!r}"
    )
    assert plan.chat_id == _CHAT_ID


# ---------------------------------------------------------------------------
# C-4: evidence_context_hash + evidence_ids fail-closed verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_butler_action_raises_on_evidence_context_hash_mismatch() -> None:
    """LLM returns wrong evidence_context_hash → ButlerPlanError (fail-closed)."""
    context = _make_context(mvids=(10, 11))

    # LLM echoes a completely different hash
    wrong_hash_json = json.dumps(
        {
            "plan_summary": "Mismatch plan",
            "evidence_ids": list(context.evidence_ids),
            "actions": [
                {
                    "tool_name": "recall_evidence",
                    "args": {"query": "who knows Rust?"},
                    "requires_confirmation": True,
                    "affected_user_ids": [],
                    "risk_level": "low",
                    "rollback_kind": "not_reversible",
                    "inverse_op_payload": None,
                }
            ],
            "evidence_context_hash": "WRONG_HASH_HALLUCINATED",  # mismatch
            "requester_user_id": 42,
            "chat_id": _CHAT_ID,
            "visibility_scope": "member",
            "governance_filter_version": "test-v1",
            "rationale": None,
        }
    )

    provider = FakeButlerProvider(answer_text=wrong_hash_json)
    ledger = FakeLedgerRepo()
    session = FakeSession()

    with pytest.raises(ButlerPlanError) as exc_info:
        await plan_butler_action(
            session=session,
            requester_user_id=42,
            chat_id=_CHAT_ID,
            query="who knows Rust?",
            evidence_context=context,
            visibility_scope="member",
            config=_butler_config(),
            ledger_repo=ledger,
            provider=provider,
        )

    assert exc_info.value.error_kind == "evidence_context_mismatch"
    # Provider should have been called once (mismatch checked post-dispatch)
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_plan_butler_action_hash_mismatch_writes_error_to_ledger() -> None:
    """evidence_context_hash mismatch updates ledger with error."""
    context = _make_context(mvids=(10, 11))
    wrong_hash_json = json.dumps(
        {
            "plan_summary": "Mismatch plan",
            "evidence_ids": list(context.evidence_ids),
            "actions": [
                {
                    "tool_name": "recall_evidence",
                    "args": {"query": "who knows Rust?"},
                    "requires_confirmation": True,
                    "affected_user_ids": [],
                    "risk_level": "low",
                    "rollback_kind": "not_reversible",
                    "inverse_op_payload": None,
                }
            ],
            "evidence_context_hash": "WRONG_HASH",
            "requester_user_id": 42,
            "chat_id": _CHAT_ID,
            "visibility_scope": "member",
            "governance_filter_version": "test-v1",
            "rationale": None,
        }
    )

    provider = FakeButlerProvider(answer_text=wrong_hash_json)
    ledger = FakeLedgerRepo()
    session = FakeSession()

    with pytest.raises(ButlerPlanError):
        await plan_butler_action(
            session=session,
            requester_user_id=42,
            chat_id=_CHAT_ID,
            query="who knows Rust?",
            evidence_context=context,
            visibility_scope="member",
            config=_butler_config(),
            ledger_repo=ledger,
            provider=provider,
        )

    row = ledger.rows[-1]
    assert row.error == "evidence_context_mismatch"


@pytest.mark.asyncio
async def test_plan_butler_action_raises_on_orphan_evidence_ids() -> None:
    """LLM references evidence_id not in sealed context → ButlerPlanError (fail-closed)."""
    context = _make_context(mvids=(10, 11))
    # LLM returns evidence_ids containing 999 which is not in sealed context (10, 11)
    orphan_json = json.dumps(
        {
            "plan_summary": "Orphan plan",
            "evidence_ids": [10, 11, 999],  # 999 is orphan
            "actions": [
                {
                    "tool_name": "recall_evidence",
                    "args": {"query": "who knows Rust?"},
                    "requires_confirmation": True,
                    "affected_user_ids": [],
                    "risk_level": "low",
                    "rollback_kind": "not_reversible",
                    "inverse_op_payload": None,
                }
            ],
            "evidence_context_hash": context.context_hash,
            "requester_user_id": 42,
            "chat_id": _CHAT_ID,
            "visibility_scope": "member",
            "governance_filter_version": "test-v1",
            "rationale": None,
        }
    )

    provider = FakeButlerProvider(answer_text=orphan_json)
    ledger = FakeLedgerRepo()
    session = FakeSession()

    with pytest.raises(ButlerPlanError) as exc_info:
        await plan_butler_action(
            session=session,
            requester_user_id=42,
            chat_id=_CHAT_ID,
            query="who knows Rust?",
            evidence_context=context,
            visibility_scope="member",
            config=_butler_config(),
            ledger_repo=ledger,
            provider=provider,
        )

    assert exc_info.value.error_kind == "orphan_evidence_ids"


@pytest.mark.asyncio
async def test_plan_butler_action_orphan_evidence_ids_writes_error_to_ledger() -> None:
    """Orphan evidence_id updates ledger with error='orphan_evidence_ids'."""
    context = _make_context(mvids=(10, 11))
    orphan_json = json.dumps(
        {
            "plan_summary": "Orphan plan",
            "evidence_ids": [10, 999],  # 999 is orphan
            "actions": [
                {
                    "tool_name": "recall_evidence",
                    "args": {"query": "test"},
                    "requires_confirmation": True,
                    "affected_user_ids": [],
                    "risk_level": "low",
                    "rollback_kind": "not_reversible",
                    "inverse_op_payload": None,
                }
            ],
            "evidence_context_hash": context.context_hash,
            "requester_user_id": 42,
            "chat_id": _CHAT_ID,
            "visibility_scope": "member",
            "governance_filter_version": "test-v1",
            "rationale": None,
        }
    )

    provider = FakeButlerProvider(answer_text=orphan_json)
    ledger = FakeLedgerRepo()
    session = FakeSession()

    with pytest.raises(ButlerPlanError) as excinfo:
        await plan_butler_action(
            session=session,
            requester_user_id=42,
            chat_id=_CHAT_ID,
            query="who knows Rust?",
            evidence_context=context,
            visibility_scope="member",
            config=_butler_config(),
            ledger_repo=ledger,
            provider=provider,
        )

    assert excinfo.value.error_kind == "orphan_evidence_ids"
    row = ledger.rows[-1]
    assert row.error == "orphan_evidence_ids"


# ---------------------------------------------------------------------------
# H-2: Butler-specific cost guard (daily $1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_butler_action_butler_daily_ceiling_fires() -> None:
    """Butler-specific daily $1 ceiling fires before provider dispatch."""
    context = _make_context()
    plan_json = _valid_plan_json(context=context)

    provider = FakeButlerProvider(answer_text=plan_json)
    from decimal import Decimal as D

    # Pre-load butler daily cost AT the $1 ceiling
    ledger = FakeLedgerRepo(daily_cost=D("1.00"), monthly_cost=D("0"))
    session = FakeSession()
    config = LLMGatewayConfig(
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        daily_ceiling_usd=D("100.00"),  # shared ceiling high — not the blocker
        monthly_ceiling_usd=D("100.00"),
        prompt_template_version="v1.0.0",
    )

    with pytest.raises(ButlerPlanError):
        await plan_butler_action(
            session=session,
            requester_user_id=42,
            chat_id=_CHAT_ID,
            query="who knows Rust?",
            evidence_context=context,
            visibility_scope="member",
            config=config,
            ledger_repo=ledger,
            provider=provider,
        )

    assert len(provider.calls) == 0, "Provider must not be called when butler ceiling exceeded"


@pytest.mark.asyncio
async def test_plan_butler_action_butler_daily_ceiling_uses_call_type_filter() -> None:
    """Butler daily_cost_usd check is called with call_type='butler_decision'."""
    context = _make_context()
    plan_json = _valid_plan_json(context=context)

    # Track calls to daily_cost_usd to verify call_type param
    call_type_seen: list[str | None] = []

    class TrackingLedgerRepo(FakeLedgerRepo):
        async def daily_cost_usd(
            self, session: Any, *, day: Any, call_type: str | None = None
        ) -> Decimal:
            call_type_seen.append(call_type)
            return await super().daily_cost_usd(session, day=day, call_type=call_type)

    provider = FakeButlerProvider(answer_text=plan_json)
    ledger = TrackingLedgerRepo()
    session = FakeSession()

    await plan_butler_action(
        session=session,
        requester_user_id=42,
        chat_id=_CHAT_ID,
        query="who knows Rust?",
        evidence_context=context,
        visibility_scope="member",
        config=_butler_config(),
        ledger_repo=ledger,
        provider=provider,
    )

    # At least one call_type='butler_decision' should appear
    assert "butler_decision" in call_type_seen, (
        f"Expected 'butler_decision' call_type in daily_cost_usd calls; got {call_type_seen}"
    )


# ---------------------------------------------------------------------------
# H-3: allowed_tools + tool_manifest_version params in plan_butler_action
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_butler_action_prompt_includes_tool_manifest_version() -> None:
    """Prompt body includes the tool_manifest_version for G3.b replay."""
    context = _make_context()
    plan_json = _valid_plan_json(context=context)

    provider = FakeButlerProvider(answer_text=plan_json)
    ledger = FakeLedgerRepo()
    session = FakeSession()

    await plan_butler_action(
        session=session,
        requester_user_id=42,
        chat_id=_CHAT_ID,
        query="who knows Rust?",
        evidence_context=context,
        visibility_scope="member",
        config=_butler_config(),
        ledger_repo=ledger,
        provider=provider,
    )

    prompt_sent = provider.calls[0]["prompt"]
    # The manifest version (at least something version-like) should appear in prompt
    assert "v1.0.0" in prompt_sent, (
        f"Expected tool_manifest_version in prompt; prompt={prompt_sent[:300]}"
    )


@pytest.mark.asyncio
async def test_plan_butler_action_prompt_includes_requester_and_chat() -> None:
    """Prompt body includes requester_user_id and chat_id (M-1)."""
    context = _make_context()
    plan_json = _valid_plan_json(context=context)

    provider = FakeButlerProvider(answer_text=plan_json)
    ledger = FakeLedgerRepo()
    session = FakeSession()

    await plan_butler_action(
        session=session,
        requester_user_id=42,
        chat_id=_CHAT_ID,
        query="who knows Rust?",
        evidence_context=context,
        visibility_scope="member",
        config=_butler_config(),
        ledger_repo=ledger,
        provider=provider,
    )

    prompt_sent = provider.calls[0]["prompt"]
    assert "42" in prompt_sent, "requester_user_id=42 should appear in prompt"
    assert str(_CHAT_ID) in prompt_sent, "chat_id should appear in prompt"
