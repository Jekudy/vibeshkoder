"""TDD tests for suggest_card_creation tool (T12-06).

Spec: PHASE12_PLAN_REFRESH.md §10 T12-06
- Creates extraction_candidates row with status='pending_review' (→ 'pending' in schema)
- Creates butler_card_suggestions mapping row
- NEVER creates an active card (status must not be 'approved')
- inverse_op_payload: rollback_kind='cancel_pending' with suggestion_id
- idempotency: second call returns prior result (no double-write)
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pytest

from bot.db.models import ButlerCardSuggestion, ExtractionCandidate
from bot.services.butler_evidence import ButlerEvidenceContext, butler_context_hash
from bot.services.butler_tools import ButlerPlanError, SuggestCardCreationArgs
from bot.services.butler_tools.suggest_card_creation import SuggestCardCreationTool
from bot.services.evidence import EvidenceBundle

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CHAT_ID = -100_999_005
REQUESTER_UID = 42

_TOOL = SuggestCardCreationTool()


def _make_ctx() -> ButlerEvidenceContext:
    bundle = EvidenceBundle(
        query="card suggestion",
        chat_id=CHAT_ID,
        items=(),
        abstained=True,
        created_at=datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
    )
    ctx_hash = butler_context_hash(bundle, "member", "v1")
    return ButlerEvidenceContext(
        bundle=bundle,
        visibility_scope="member",
        context_hash=ctx_hash,
        governance_filter_version="v1",
        requester_user_id=REQUESTER_UID,
        chat_id=CHAT_ID,
        query="card suggestion",
        snapshot_at=datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
        governance_excluded_count=0,
    )


@dataclass
class FakeExtractionCandidate:
    id: uuid.UUID
    status: str
    candidate_json: dict
    source_message_version_ids: list


@dataclass
class FakeButlerCardSuggestion:
    id: int
    butler_action_id: int
    extraction_candidate_id: uuid.UUID | None
    suggested_card_payload: dict
    created_by_user_id: int


class FakeSession:
    """Minimal session fake that records add() calls."""

    def __init__(self) -> None:
        self.added: list[Any] = []
        self._flush_count = 0

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self._flush_count += 1
        # Assign fake IDs after flush
        for obj in self.added:
            if not getattr(obj, "id", None):
                if hasattr(obj, "id") and obj.id is None:
                    obj.id = uuid.uuid4() if isinstance(getattr(obj, "id", None), uuid.UUID) else 1

    async def execute(self, *a, **kw):
        # Should NOT be called directly (Hard Constraint #2)
        raise AssertionError("suggest_card_creation must not call session.execute() directly")


class _FakeSession:
    """Even simpler session that just tracks flush count."""
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, obj: Any) -> None:
        # Assign fake ID on add
        if hasattr(obj, "id") and obj.id is None:
            obj.id = 1
        self.added.append(obj)

    async def flush(self) -> None:
        pass

    async def execute(self, *a, **kw):
        raise AssertionError("suggest_card_creation must not call session.execute() directly")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_tool_name():
    from bot.services.butler_tools import ALLOWED_BUTLER_TOOLS
    assert _TOOL.name == "suggest_card_creation"
    assert _TOOL.name in ALLOWED_BUTLER_TOOLS


def test_schema_version():
    assert _TOOL.schema_version == "v1.0.0"


def test_args_model():
    assert _TOOL.args_model is SuggestCardCreationArgs


# ---------------------------------------------------------------------------
# validate_policy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_policy_passes_valid_args():
    ctx = _make_ctx()
    args = SuggestCardCreationArgs(title="Rust best practices", summary="A guide")
    await _TOOL.validate_policy(ctx, args)  # should not raise


@pytest.mark.asyncio
async def test_validate_policy_rejects_blank_title():
    ctx = _make_ctx()
    args = SuggestCardCreationArgs(title="   ")
    with pytest.raises(ButlerPlanError):
        await _TOOL.validate_policy(ctx, args)


# ---------------------------------------------------------------------------
# execute — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_creates_candidate_with_pending_status():
    """execute must write extraction_candidates with status='pending' (pending_review)."""
    ctx = _make_ctx()
    args = SuggestCardCreationArgs(title="Rust practices", summary="Summary", tags=["rust"])
    session = _FakeSession()

    result = await _TOOL.execute(ctx, args, session=session)

    assert result.success is True
    # Find the ExtractionCandidate in session.added
    candidates = [obj for obj in session.added if isinstance(obj, ExtractionCandidate)]
    assert len(candidates) == 1
    assert candidates[0].status == "pending"


@pytest.mark.asyncio
async def test_execute_never_creates_approved_card():
    """execute NEVER sets status to 'approved' on any created object."""
    ctx = _make_ctx()
    args = SuggestCardCreationArgs(title="Rust practices")
    session = _FakeSession()

    await _TOOL.execute(ctx, args, session=session)

    # No object in session.added should have status='approved' or 'active'
    for obj in session.added:
        status = getattr(obj, "status", None)
        assert status not in ("approved", "active"), (
            f"suggest_card_creation created object with forbidden status: {status!r}"
        )


@pytest.mark.asyncio
async def test_execute_creates_butler_card_suggestion_row():
    """execute writes a butler_card_suggestions mapping row."""
    ctx = _make_ctx()
    args = SuggestCardCreationArgs(title="Rust practices")
    session = _FakeSession()

    await _TOOL.execute(ctx, args, session=session)

    suggestions = [obj for obj in session.added if isinstance(obj, ButlerCardSuggestion)]
    assert len(suggestions) == 1


@pytest.mark.asyncio
async def test_execute_payload_has_suggestion_id():
    """Result payload carries butler_card_suggestion_id for rollback."""
    ctx = _make_ctx()
    args = SuggestCardCreationArgs(title="Rust practices")
    session = _FakeSession()

    result = await _TOOL.execute(ctx, args, session=session)

    assert result.success is True
    assert "butler_card_suggestion_id" in result.payload


@pytest.mark.asyncio
async def test_execute_no_direct_db_execute():
    """execute must NOT call session.execute() — only session.add() + flush()."""
    ctx = _make_ctx()
    args = SuggestCardCreationArgs(title="Test")

    class _StrictSession:
        added: list[Any] = []

        def add(self, obj: Any) -> None:
            self._added.append(obj)

        async def flush(self) -> None:
            pass

        async def execute(self, *a, **kw):
            raise AssertionError("session.execute must NOT be called")

        def __init__(self) -> None:
            self._added: list[Any] = []
            self.added = self._added

    session = _StrictSession()
    result = await _TOOL.execute(ctx, args, session=session)
    assert result.success is True


# ---------------------------------------------------------------------------
# build_inverse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_inverse_has_cancel_pending_kind():
    """build_inverse returns rollback_kind='cancel_pending'."""
    ctx = _make_ctx()
    args = SuggestCardCreationArgs(title="Test")
    session = _FakeSession()
    result = await _TOOL.execute(ctx, args, session=session)

    inverse = await _TOOL.build_inverse(result)
    assert inverse["rollback_kind"] == "cancel_pending"


@pytest.mark.asyncio
async def test_build_inverse_has_suggestion_id():
    """inverse_op_payload contains butler_card_suggestion_id."""
    ctx = _make_ctx()
    args = SuggestCardCreationArgs(title="Test")
    session = _FakeSession()
    result = await _TOOL.execute(ctx, args, session=session)

    inverse = await _TOOL.build_inverse(result)
    assert "butler_card_suggestion_id" in inverse


@pytest.mark.asyncio
async def test_build_inverse_deterministic():
    ctx = _make_ctx()
    args = SuggestCardCreationArgs(title="Test")
    session = _FakeSession()
    result = await _TOOL.execute(ctx, args, session=session)

    inv1 = await _TOOL.build_inverse(result)
    inv2 = await _TOOL.build_inverse(result)
    assert json.dumps(inv1, sort_keys=True) == json.dumps(inv2, sort_keys=True)
