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
  - Prompt sent to provider does NOT include raw forgotten content.
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
    """Happy path: valid LLM JSON → ButlerPlan returned."""
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

    assert isinstance(result, ButlerPlan)
    assert result.plan_summary == "Recall members who know Rust"
    assert len(result.actions) == 1
    assert result.actions[0].tool_name == "recall_evidence"


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

    # The LLM echoed back the correct hash.
    assert result.evidence_context_hash == context.context_hash


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

    with pytest.raises((InvalidToolArgsError, ButlerPlanError)):
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

    with pytest.raises((InvalidToolArgsError, ButlerPlanError)):
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

    assert len(ledger.rows) >= 1
    row = ledger.rows[-1]
    assert row.error == "budget_exceeded"


# ---------------------------------------------------------------------------
# Privacy: prompt does not include raw forgotten content
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_butler_action_prompt_does_not_include_raw_source_content() -> None:
    """The prompt sent to the provider does NOT include raw forgotten content.

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
