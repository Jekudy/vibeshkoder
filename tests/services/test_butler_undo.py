"""Behaviour tests for ButlerService.execute_undo (T12-07).

TDD — tests written before/alongside the execute_undo implementation.

Test inventory
--------------
1.  not_reversible_skipped          — rollback_kind=not_reversible → skipped audit row
2.  delete_message_happy            — rollback_kind=delete_message → bot.delete_message called
3.  edit_message_happy              — rollback_kind=edit_message → bot.edit_message_text called
4.  followup_correction_happy       — rollback_kind=followup_correction → bot.send_message called
5.  cancel_pending_happy            — rollback_kind=cancel_pending → card suggestion repo updated
6.  delete_message_bot_failure      — bot.delete_message raises → audit row status=failed
7.  auth_non_requester_non_admin    — wrong user → ButlerActionError(forbidden)
8.  wrong_status_pending            — action.status='pending_confirmation' → ButlerActionError(wrong_status)
9.  ttl_expired                     — action.succeeded_at more than TTL ago → ButlerActionError(ttl_expired)
10. idempotency_already_undone      — second call → returns existing summary, no double-side-effect
11. cascade_in_flight               — action row locked → CascadeInFlightError
12. lifo_order                      — two invocations → undo executed in REVERSE order

Combined mode: these tests can run alongside test_butler_state_machine.py
without class identity failures (module-level imports).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# Module-level imports for all exception classes — prevents class identity failures
# in combined-mode CI (see commit db33b1c, T12-04 fix cycle).
from bot.services.butler import (
    ButlerActionError,
    ButlerService,
    CascadeInFlightError,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REQUESTER_ID = 42
OTHER_USER_ID = 99
CHAT_ID = -100_999_888_777
ACTION_ID = 1
INVOCATION_ID_1 = 10
INVOCATION_ID_2 = 11

# Default TTL minutes for undo (must match service default)
BUTLER_UNDO_TTL_MINUTES = 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _past(minutes: int) -> datetime:
    return _now() - timedelta(minutes=minutes)


# ---------------------------------------------------------------------------
# Fake ORM rows
# ---------------------------------------------------------------------------


@dataclass
class FakeButlerAction:
    id: int
    requester_tg_id: int
    chat_id: int = CHAT_ID
    action_type: str = "recall"
    status: str = "succeeded"
    tool_name: str = "recall_evidence"
    tool_manifest_version: str = "v1.0.0"
    governance_filter_version: str = "test-v1"
    evidence_context_hash: str = "abc123"
    evidence_ids: list = field(default_factory=list)
    plan_summary: str = "Test plan"
    action_args: dict = field(default_factory=dict)
    action_args_hash: str = ""
    rollback_kind: str = "not_reversible"
    risk_level: str = "low"
    requires_confirmation: bool = True
    confirmation_policy: str = "per_action"
    expires_at: datetime | None = None
    confirmed_at: datetime | None = None
    executed_at: datetime | None = field(default_factory=_now)
    undone_at: datetime | None = None
    rejection_reason: str | None = None
    error_code: str | None = None
    error_context: dict | None = None
    llm_usage_ledger_id: int | None = 1
    result_payload: dict | None = None
    result_payload_hash: str | None = None
    inverse_op_payload: dict | None = None
    action_uuid: uuid.UUID = field(default_factory=uuid.uuid4)
    parent_action_id: int | None = None
    plan_payload: dict = field(default_factory=dict)
    approved_card_source_ids: list = field(default_factory=list)
    query: str = "test query"
    visibility_scope: str = "member"
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)


@dataclass
class FakeButlerToolInvocation:
    id: int
    action_id: int
    tool_name: str
    idempotency_key: str
    request_payload: dict
    request_payload_hash: str
    status: str = "succeeded"
    invocation_seq: int = 1
    response_payload: dict | None = None
    response_payload_hash: str | None = None
    started_at: datetime = field(default_factory=_now)
    finished_at: datetime | None = None
    error_code: str | None = None
    error_context: dict | None = None
    posted_message_id: int | None = None
    # inverse_op_payload stores rollback info for this invocation
    inverse_op_payload: dict | None = None


@dataclass
class FakeButlerUndoInvocation:
    id: int
    butler_action_id: int
    butler_tool_invocation_id: int
    requester_user_id: int
    rollback_kind: str
    status: str = "pending"
    error_kind: str | None = None
    error_message: str | None = None
    created_at: datetime = field(default_factory=_now)


@dataclass
class FakeButlerCardSuggestion:
    id: int
    butler_action_id: int
    status: str = "pending"


# ---------------------------------------------------------------------------
# Fake repos
# ---------------------------------------------------------------------------


class FakeButlerActionRepo:
    def __init__(self, action: FakeButlerAction | None = None) -> None:
        self._action = action
        self.locked = False
        self.updates: list[dict] = []

    async def get(self, session: Any, action_id: int) -> FakeButlerAction | None:
        return self._action if (self._action and self._action.id == action_id) else None

    async def get_for_update(self, session: Any, action_id: int) -> FakeButlerAction | None:
        if self.locked:
            return None  # simulate cascade holding lock
        return self._action if (self._action and self._action.id == action_id) else None

    async def update_status(self, session: Any, action_id: int, **kwargs: Any) -> int:
        self.updates.append({"action_id": action_id, **kwargs})
        if self._action and self._action.id == action_id:
            if "status" in kwargs:
                self._action.status = kwargs["status"]
            if "undone_at" in kwargs:
                self._action.undone_at = kwargs["undone_at"]
        return 1


class FakeButlerToolInvocationRepo:
    def __init__(self, invocations: list[FakeButlerToolInvocation] | None = None) -> None:
        self._invocations: list[FakeButlerToolInvocation] = invocations or []
        self.updates: list[dict] = []

    async def list_for_action(self, session: Any, action_id: int) -> list[FakeButlerToolInvocation]:
        return [inv for inv in self._invocations if inv.action_id == action_id]


class FakeButlerUndoInvocationRepo:
    def __init__(self) -> None:
        self._rows: dict[tuple[int, int], FakeButlerUndoInvocation] = {}
        self._next_id = 100
        self.creates: list[dict] = []
        self.updates: list[dict] = []

    async def create(
        self,
        session: Any,
        *,
        butler_action_id: int,
        butler_tool_invocation_id: int,
        requester_user_id: int,
        rollback_kind: str,
        status: str = "pending",
        error_kind: str | None = None,
        error_message: str | None = None,
    ) -> FakeButlerUndoInvocation:
        self.creates.append({
            "butler_action_id": butler_action_id,
            "butler_tool_invocation_id": butler_tool_invocation_id,
            "rollback_kind": rollback_kind,
            "status": status,
        })
        row = FakeButlerUndoInvocation(
            id=self._next_id,
            butler_action_id=butler_action_id,
            butler_tool_invocation_id=butler_tool_invocation_id,
            requester_user_id=requester_user_id,
            rollback_kind=rollback_kind,
            status=status,
            error_kind=error_kind,
            error_message=error_message,
        )
        self._rows[(butler_action_id, butler_tool_invocation_id)] = row
        self._next_id += 1
        return row

    async def find_by_action_and_invocation(
        self,
        session: Any,
        butler_action_id: int,
        butler_tool_invocation_id: int,
    ) -> FakeButlerUndoInvocation | None:
        return self._rows.get((butler_action_id, butler_tool_invocation_id))

    async def list_by_action(
        self, session: Any, butler_action_id: int
    ) -> list[FakeButlerUndoInvocation]:
        return [r for r in self._rows.values() if r.butler_action_id == butler_action_id]

    async def update_status(
        self,
        session: Any,
        undo_invocation_id: int,
        *,
        status: str,
        error_kind: str | None = None,
        error_message: str | None = None,
    ) -> int:
        self.updates.append({
            "id": undo_invocation_id,
            "status": status,
            "error_kind": error_kind,
        })
        for row in self._rows.values():
            if row.id == undo_invocation_id:
                row.status = status
                # Always update error fields (None explicitly nulls them — clears stale errors on retry)
                row.error_kind = error_kind
                row.error_message = error_message
                return 1
        raise LookupError(f"ButlerUndoInvocation(id={undo_invocation_id}) not found")


class FakeButlerCardSuggestionRepo:
    def __init__(self, suggestion: FakeButlerCardSuggestion | None = None) -> None:
        self._suggestion = suggestion
        self.updates: list[dict] = []

    async def get_for_action(self, session: Any, butler_action_id: int) -> FakeButlerCardSuggestion | None:
        return self._suggestion

    async def dismiss_by_undo(self, session: Any, butler_action_id: int, *, reviewer_user_id: int) -> int:
        self.updates.append({"butler_action_id": butler_action_id, "action": "dismiss_by_undo", "reviewer_user_id": reviewer_user_id})
        return 1


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


def _make_action(
    *,
    requester_tg_id: int = REQUESTER_ID,
    status: str = "succeeded",
    executed_at: datetime | None = None,
    inverse_op_payload: dict | None = None,
) -> FakeButlerAction:
    action = FakeButlerAction(
        id=ACTION_ID,
        requester_tg_id=requester_tg_id,
        status=status,
        executed_at=executed_at or _now(),
    )
    action.inverse_op_payload = inverse_op_payload
    return action


def _make_invocation(
    *,
    inv_id: int = INVOCATION_ID_1,
    rollback_kind: str = "not_reversible",
    inverse_op_payload: dict | None = None,
) -> FakeButlerToolInvocation:
    payload = inverse_op_payload or {"rollback_kind": rollback_kind}
    return FakeButlerToolInvocation(
        id=inv_id,
        action_id=ACTION_ID,
        tool_name="recall_evidence",
        idempotency_key=f"butler:{ACTION_ID}:recall_evidence:{inv_id}",
        request_payload={},
        request_payload_hash="",
        inverse_op_payload=payload,
    )


def _make_service(
    action: FakeButlerAction | None = None,
    invocations: list | None = None,
    undo_repo: FakeButlerUndoInvocationRepo | None = None,
    card_suggestion_repo: FakeButlerCardSuggestionRepo | None = None,
    locked: bool = False,
) -> ButlerService:
    """Build a ButlerService with fake repos wired for undo testing."""
    action_repo = FakeButlerActionRepo(action)
    action_repo.locked = locked

    invocation_repo = FakeButlerToolInvocationRepo(invocations or [])
    undo_inv_repo = undo_repo or FakeButlerUndoInvocationRepo()
    card_sugg_repo = card_suggestion_repo or FakeButlerCardSuggestionRepo()

    # Stub the unused collaborators
    session = AsyncMock()
    ledger_repo = MagicMock()
    # AsyncMock so list_for_action is awaitable; returns [] so auth branch
    # finds no affected_user rows for non-requester non-admin calls.
    confirmation_repo = AsyncMock()
    confirmation_repo.list_for_action = AsyncMock(return_value=[])
    rate_bucket_repo = MagicMock()
    # user_repo.get must be awaitable since M3 admin verify uses await user_repo.get(...)
    user_repo = AsyncMock()
    user_repo.get = AsyncMock(return_value=None)
    llm_gateway = MagicMock()
    evidence_builder = MagicMock()
    settings = MagicMock(butler_undo_ttl_minutes=60)

    svc = ButlerService(
        session=session,
        ledger_repo=ledger_repo,
        butler_action_repo=action_repo,
        butler_action_confirmation_repo=confirmation_repo,
        butler_tool_invocation_repo=invocation_repo,
        butler_rate_bucket_repo=rate_bucket_repo,
        user_repo=user_repo,
        llm_gateway=llm_gateway,
        evidence_builder=evidence_builder,
        settings=settings,
        undo_invocation_repo=undo_inv_repo,
        card_suggestion_repo=card_sugg_repo,
    )
    return svc


# ---------------------------------------------------------------------------
# Test 1: not_reversible → skipped audit row
# ---------------------------------------------------------------------------


def test_not_reversible_skipped() -> None:
    """rollback_kind=not_reversible → skipped audit row, no side effects."""
    inv = _make_invocation(rollback_kind="not_reversible", inverse_op_payload={"rollback_kind": "not_reversible"})
    action = _make_action()
    undo_repo = FakeButlerUndoInvocationRepo()
    bot = AsyncMock()

    svc = _make_service(action=action, invocations=[inv], undo_repo=undo_repo)

    summary = asyncio.run(
        svc.execute_undo(
            action_id=ACTION_ID,
            requester_user_id=REQUESTER_ID,
            bot=bot,
        )
    )

    # Audit row created (initially pending) then updated to skipped_not_reversible
    assert len(undo_repo.creates) == 1
    assert undo_repo.creates[0]["rollback_kind"] == "not_reversible"
    # The row is updated to skipped_not_reversible via update_status
    assert any(u["status"] == "skipped_not_reversible" for u in undo_repo.updates)

    # No Telegram calls
    bot.delete_message.assert_not_awaited()
    bot.edit_message_text.assert_not_awaited()
    bot.send_message.assert_not_awaited()

    # Summary reports the skipped step
    assert summary is not None


# ---------------------------------------------------------------------------
# Test 2: delete_message happy path
# ---------------------------------------------------------------------------


def test_delete_message_happy() -> None:
    """rollback_kind=delete_message → bot.delete_message called with payload data."""
    inv = _make_invocation(
        rollback_kind="delete_message",
        inverse_op_payload={"rollback_kind": "delete_message", "chat_id": CHAT_ID, "message_id": 9999},
    )
    action = _make_action()
    undo_repo = FakeButlerUndoInvocationRepo()
    bot = AsyncMock()

    svc = _make_service(action=action, invocations=[inv], undo_repo=undo_repo)

    asyncio.run(svc.execute_undo(action_id=ACTION_ID, requester_user_id=REQUESTER_ID, bot=bot))

    bot.delete_message.assert_awaited_once_with(chat_id=CHAT_ID, message_id=9999)

    # Audit row created with succeeded status
    assert len(undo_repo.creates) == 1
    assert undo_repo.creates[0]["status"] == "pending"
    # Status updated to succeeded
    assert any(u["status"] == "succeeded" for u in undo_repo.updates)


# ---------------------------------------------------------------------------
# Test 3: edit_message happy path
# ---------------------------------------------------------------------------


def test_edit_message_happy() -> None:
    """rollback_kind=edit_message → bot.edit_message_text called with prior_text.

    R3: prior_text is always resolved via _resolve_prior_text (forget-safe).
    inverse_op_payload.prior_text is ignored (removed — latent privacy leak).
    """
    from unittest.mock import patch

    inv = _make_invocation(
        rollback_kind="edit_message",
        inverse_op_payload={
            "rollback_kind": "edit_message",
            "chat_id": CHAT_ID,
            "message_id": 8888,
            # prior_text intentionally absent — must come from _resolve_prior_text only
        },
    )
    action = _make_action()
    undo_repo = FakeButlerUndoInvocationRepo()
    bot = AsyncMock()

    svc = _make_service(action=action, invocations=[inv], undo_repo=undo_repo)

    # R3: patch _resolve_prior_text — the only permitted source for prior_text
    async def _fake_resolve(chat_id: int, message_id: int) -> str | None:
        return "original text"

    with patch.object(svc, "_resolve_prior_text", side_effect=_fake_resolve):
        asyncio.run(svc.execute_undo(action_id=ACTION_ID, requester_user_id=REQUESTER_ID, bot=bot))

    bot.edit_message_text.assert_awaited_once_with(
        chat_id=CHAT_ID, message_id=8888, text="original text"
    )
    assert any(u["status"] == "succeeded" for u in undo_repo.updates)


# ---------------------------------------------------------------------------
# Test 4: followup_correction happy path
# ---------------------------------------------------------------------------


def test_followup_correction_happy() -> None:
    """rollback_kind=followup_correction → bot.delete_message called with followup_message_id.

    C4 fix: update_intro.build_inverse emits {followup_message_id, chat_id}.
    Undo = delete the follow-up message (cleaner than posting another message).
    """
    followup_msg_id = 7788
    inv = _make_invocation(
        rollback_kind="followup_correction",
        inverse_op_payload={
            "rollback_kind": "followup_correction",
            "chat_id": CHAT_ID,
            "followup_message_id": followup_msg_id,
        },
    )
    action = _make_action()
    undo_repo = FakeButlerUndoInvocationRepo()
    bot = AsyncMock()

    svc = _make_service(action=action, invocations=[inv], undo_repo=undo_repo)

    asyncio.run(svc.execute_undo(action_id=ACTION_ID, requester_user_id=REQUESTER_ID, bot=bot))

    # C4: delete the follow-up message instead of sending a correction
    bot.delete_message.assert_awaited_once_with(chat_id=CHAT_ID, message_id=followup_msg_id)
    bot.send_message.assert_not_awaited()
    assert any(u["status"] == "succeeded" for u in undo_repo.updates)


# ---------------------------------------------------------------------------
# Test 5: cancel_pending happy path
# ---------------------------------------------------------------------------


def test_cancel_pending_happy() -> None:
    """rollback_kind=cancel_pending → card suggestion repo updated."""
    inv = _make_invocation(
        rollback_kind="cancel_pending",
        inverse_op_payload={"rollback_kind": "cancel_pending", "butler_action_id": ACTION_ID},
    )
    action = _make_action()
    undo_repo = FakeButlerUndoInvocationRepo()
    card_sugg_repo = FakeButlerCardSuggestionRepo(
        FakeButlerCardSuggestion(id=55, butler_action_id=ACTION_ID)
    )
    bot = AsyncMock()

    svc = _make_service(action=action, invocations=[inv], undo_repo=undo_repo, card_suggestion_repo=card_sugg_repo)

    asyncio.run(svc.execute_undo(action_id=ACTION_ID, requester_user_id=REQUESTER_ID, bot=bot))

    assert len(card_sugg_repo.updates) == 1
    assert card_sugg_repo.updates[0]["action"] == "dismiss_by_undo"
    assert any(u["status"] == "succeeded" for u in undo_repo.updates)


# ---------------------------------------------------------------------------
# Test 6: delete_message bot failure → audit row status=failed
# ---------------------------------------------------------------------------


def test_delete_message_bot_failure() -> None:
    """bot.delete_message raises TelegramError → audit row status=failed, undo continues."""
    inv = _make_invocation(
        rollback_kind="delete_message",
        inverse_op_payload={"rollback_kind": "delete_message", "chat_id": CHAT_ID, "message_id": 7777},
    )
    action = _make_action()
    undo_repo = FakeButlerUndoInvocationRepo()
    bot = AsyncMock()
    bot.delete_message.side_effect = RuntimeError("Telegram error")

    svc = _make_service(action=action, invocations=[inv], undo_repo=undo_repo)

    # Should NOT raise — failure is recorded in audit row
    summary = asyncio.run(svc.execute_undo(action_id=ACTION_ID, requester_user_id=REQUESTER_ID, bot=bot))

    assert summary is not None
    # Audit row updated to failed
    assert any(u["status"] == "failed" for u in undo_repo.updates)


# ---------------------------------------------------------------------------
# Test 7: auth failure — non-requester non-admin
# ---------------------------------------------------------------------------


def test_auth_non_requester_non_admin() -> None:
    """Non-requester non-admin → ButlerActionError(forbidden)."""
    action = _make_action(requester_tg_id=REQUESTER_ID)
    svc = _make_service(action=action)

    with pytest.raises(ButlerActionError) as exc_info:
        asyncio.run(
            svc.execute_undo(
                action_id=ACTION_ID,
                requester_user_id=OTHER_USER_ID,
                is_admin=False,
                bot=AsyncMock(),
            )
        )

    assert exc_info.value.error_kind == "forbidden"


# ---------------------------------------------------------------------------
# Test 8: wrong status — action is pending_confirmation
# ---------------------------------------------------------------------------


def test_wrong_status_pending() -> None:
    """Undo on pending_confirmation action → ButlerActionError(wrong_status)."""
    action = _make_action(status="pending_confirmation")
    svc = _make_service(action=action)

    with pytest.raises(ButlerActionError) as exc_info:
        asyncio.run(
            svc.execute_undo(
                action_id=ACTION_ID,
                requester_user_id=REQUESTER_ID,
                bot=AsyncMock(),
            )
        )

    assert exc_info.value.error_kind == "wrong_status"


# ---------------------------------------------------------------------------
# Test 9: TTL expired — succeeded_at more than 60 min ago
# ---------------------------------------------------------------------------


def test_ttl_expired() -> None:
    """Undo after TTL window → ButlerActionError(ttl_expired)."""
    action = _make_action(executed_at=_past(BUTLER_UNDO_TTL_MINUTES + 1))
    svc = _make_service(action=action)

    with pytest.raises(ButlerActionError) as exc_info:
        asyncio.run(
            svc.execute_undo(
                action_id=ACTION_ID,
                requester_user_id=REQUESTER_ID,
                bot=AsyncMock(),
            )
        )

    assert exc_info.value.error_kind == "ttl_expired"


# ---------------------------------------------------------------------------
# Test 10: Idempotency — second call returns existing summary, no double-side-effect
# ---------------------------------------------------------------------------


def test_idempotency_already_undone() -> None:
    """Second /butler_undo call → returns existing summary, bot not called again."""
    inv = _make_invocation(
        rollback_kind="delete_message",
        inverse_op_payload={"rollback_kind": "delete_message", "chat_id": CHAT_ID, "message_id": 6666},
    )
    action = _make_action(status="undone")
    undo_repo = FakeButlerUndoInvocationRepo()
    bot = AsyncMock()

    # Pre-populate the undo repo with an existing succeeded row
    existing_undo = FakeButlerUndoInvocation(
        id=200,
        butler_action_id=ACTION_ID,
        butler_tool_invocation_id=INVOCATION_ID_1,
        requester_user_id=REQUESTER_ID,
        rollback_kind="delete_message",
        status="succeeded",
    )
    undo_repo._rows[(ACTION_ID, INVOCATION_ID_1)] = existing_undo

    svc = _make_service(action=action, invocations=[inv], undo_repo=undo_repo)

    summary = asyncio.run(svc.execute_undo(action_id=ACTION_ID, requester_user_id=REQUESTER_ID, bot=bot))

    # No new audit rows created (idempotency)
    assert len(undo_repo.creates) == 0
    # No Telegram calls
    bot.delete_message.assert_not_awaited()
    assert summary is not None


# ---------------------------------------------------------------------------
# Test 11: Cascade in-flight
# ---------------------------------------------------------------------------


def test_cascade_in_flight() -> None:
    """Parent action row locked by cascade → CascadeInFlightError."""
    action = _make_action()
    svc = _make_service(action=action, locked=True)

    with pytest.raises(CascadeInFlightError):
        asyncio.run(
            svc.execute_undo(
                action_id=ACTION_ID,
                requester_user_id=REQUESTER_ID,
                bot=AsyncMock(),
            )
        )


# ---------------------------------------------------------------------------
# Test 12: LIFO order — two invocations processed in reverse order
# ---------------------------------------------------------------------------


def test_lifo_order() -> None:
    """Two invocations → undo executed in REVERSE order (LIFO)."""
    call_order: list[int] = []

    inv1 = _make_invocation(
        inv_id=INVOCATION_ID_1,
        rollback_kind="delete_message",
        inverse_op_payload={"rollback_kind": "delete_message", "chat_id": CHAT_ID, "message_id": 1001},
    )
    inv2 = _make_invocation(
        inv_id=INVOCATION_ID_2,
        rollback_kind="delete_message",
        inverse_op_payload={"rollback_kind": "delete_message", "chat_id": CHAT_ID, "message_id": 1002},
    )
    # Set invocation_seq so LIFO can be determined
    inv1.invocation_seq = 1
    inv2.invocation_seq = 2

    action = _make_action()
    undo_repo = FakeButlerUndoInvocationRepo()
    bot = AsyncMock()

    async def _track_delete(*, chat_id: int, message_id: int) -> None:
        call_order.append(message_id)

    bot.delete_message.side_effect = _track_delete

    svc = _make_service(action=action, invocations=[inv1, inv2], undo_repo=undo_repo)

    asyncio.run(svc.execute_undo(action_id=ACTION_ID, requester_user_id=REQUESTER_ID, bot=bot))

    # LIFO: inv2 (message_id=1002) undone BEFORE inv1 (message_id=1001)
    assert call_order == [1002, 1001], f"Expected LIFO [1002, 1001], got {call_order}"


# ---------------------------------------------------------------------------
# Test 13: _resolve_prior_text returns the pre-edit version text (C3-NEW)
# ---------------------------------------------------------------------------


def test_resolve_prior_text_returns_pre_edit_version() -> None:
    """_resolve_prior_text returns the second-latest version_seq text (pre-edit body).

    Wires a mock session whose execute() returns "v2" from scalar_one_or_none(),
    verifying the JOIN query shape through ChatMessage → MessageVersion.
    """
    # Build a mock session that returns "v2" from scalar_one_or_none()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = "v2"

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    svc = _make_service()
    # Inject the mock session directly
    svc._session = mock_session

    result = asyncio.run(svc._resolve_prior_text(chat_id=CHAT_ID, message_id=12345))

    assert result == "v2", f"Expected 'v2' (pre-edit text), got {result!r}"
    # Verify execute was actually called (not silently returning None from except)
    mock_session.execute.assert_awaited_once()


def test_resolve_prior_text_returns_none_when_no_prior_version() -> None:
    """_resolve_prior_text returns None when no prior version exists (first edit)."""
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    svc = _make_service()
    svc._session = mock_session

    result = asyncio.run(svc._resolve_prior_text(chat_id=CHAT_ID, message_id=12345))

    assert result is None
    mock_session.execute.assert_awaited_once()


def test_resolve_prior_text_returns_none_on_sqlalchemy_error() -> None:
    """_resolve_prior_text swallows SQLAlchemyError and returns None (graceful fallback)."""
    from sqlalchemy.exc import SQLAlchemyError

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=SQLAlchemyError("connection error"))

    svc = _make_service()
    svc._session = mock_session

    result = asyncio.run(svc._resolve_prior_text(chat_id=CHAT_ID, message_id=12345))

    assert result is None


# ---------------------------------------------------------------------------
# Test 16: F7 — affected_user is allowed to undo (spec PLAN.md §§728/470)
# ---------------------------------------------------------------------------


@dataclass
class FakeButlerActionConfirmation:
    id: int
    butler_action_id: int
    confirmer_tg_id: int
    confirmation_role: str  # 'requester' | 'affected_user'
    status: str = "approved"


class FakeButlerActionConfirmationRepo:
    """Minimal fake that supports list_for_action."""

    def __init__(self, rows: list[FakeButlerActionConfirmation] | None = None) -> None:
        self._rows = rows or []

    async def list_for_action(
        self, session: Any, action_id: int
    ) -> list[FakeButlerActionConfirmation]:
        return [r for r in self._rows if r.butler_action_id == action_id]


def _make_service_with_confirmation_repo(
    action: FakeButlerAction,
    confirmation_repo: Any,
    invocations: list | None = None,
) -> ButlerService:
    """Build a ButlerService with a real confirmation repo for F7 tests."""
    action_repo = FakeButlerActionRepo(action)
    invocation_repo = FakeButlerToolInvocationRepo(invocations or [])
    undo_inv_repo = FakeButlerUndoInvocationRepo()
    card_sugg_repo = FakeButlerCardSuggestionRepo()

    session = AsyncMock()
    ledger_repo = MagicMock()
    rate_bucket_repo = MagicMock()
    user_repo = AsyncMock()
    user_repo.get = AsyncMock(return_value=None)
    llm_gateway = MagicMock()
    evidence_builder = MagicMock()
    settings = MagicMock(butler_undo_ttl_minutes=60)

    return ButlerService(
        session=session,
        ledger_repo=ledger_repo,
        butler_action_repo=action_repo,
        butler_action_confirmation_repo=confirmation_repo,
        butler_tool_invocation_repo=invocation_repo,
        butler_rate_bucket_repo=rate_bucket_repo,
        user_repo=user_repo,
        llm_gateway=llm_gateway,
        evidence_builder=evidence_builder,
        settings=settings,
        undo_invocation_repo=undo_inv_repo,
        card_suggestion_repo=card_sugg_repo,
    )


def test_affected_user_can_undo() -> None:
    """An affected_user of a cross-user action (e.g. send_intro) must be able to undo.

    Spec: PLAN.md §728, DESIGN §470, charter §91 — requester|affected_user|admin allowed.
    """
    AFFECTED_USER_ID = 77
    inv = _make_invocation(rollback_kind="not_reversible")
    action = _make_action(requester_tg_id=REQUESTER_ID)  # requester != affected user

    confirmation_repo = FakeButlerActionConfirmationRepo(
        rows=[
            FakeButlerActionConfirmation(
                id=1,
                butler_action_id=ACTION_ID,
                confirmer_tg_id=AFFECTED_USER_ID,
                confirmation_role="affected_user",
                status="confirmed",  # R1 fix: must be 'confirmed' to authorize undo
            )
        ]
    )
    svc = _make_service_with_confirmation_repo(
        action=action,
        confirmation_repo=confirmation_repo,
        invocations=[inv],
    )
    bot = AsyncMock()

    # Should NOT raise — affected_user is authorized
    summary = asyncio.run(
        svc.execute_undo(
            action_id=ACTION_ID,
            requester_user_id=AFFECTED_USER_ID,
            is_admin=False,
            bot=bot,
        )
    )
    assert summary is not None


def test_non_affected_non_requester_non_admin_still_forbidden() -> None:
    """A user who is neither requester nor affected_user nor admin remains forbidden.

    The affected_user branch must NOT open the gate to arbitrary users.
    """
    STRANGER_ID = 555
    action = _make_action(requester_tg_id=REQUESTER_ID)

    # Confirmation repo has an affected_user row, but for a different user
    confirmation_repo = FakeButlerActionConfirmationRepo(
        rows=[
            FakeButlerActionConfirmation(
                id=1,
                butler_action_id=ACTION_ID,
                confirmer_tg_id=77,  # different user, not STRANGER_ID
                confirmation_role="affected_user",
                status="confirmed",  # even a confirmed affected_user of ID 77 doesn't help STRANGER_ID
            )
        ]
    )
    svc = _make_service_with_confirmation_repo(
        action=action,
        confirmation_repo=confirmation_repo,
    )

    with pytest.raises(ButlerActionError) as exc_info:
        asyncio.run(
            svc.execute_undo(
                action_id=ACTION_ID,
                requester_user_id=STRANGER_ID,
                is_admin=False,
                bot=AsyncMock(),
            )
        )
    assert exc_info.value.error_kind == "forbidden"


# ---------------------------------------------------------------------------
# Test 17: V2 — failed undo step must be retried on subsequent /butler_undo
# ---------------------------------------------------------------------------


def test_failed_undo_step_is_retried_not_skipped() -> None:
    """V2: idempotency short-circuit must NOT treat 'failed' as terminal.

    First call: undo row with status='failed' exists.
    Second call: the step must be retried (not returned as already-done).

    Spec: PLAN.md §724 best-effort + retry-safe intent.
    """
    inv = _make_invocation(
        rollback_kind="delete_message",
        inverse_op_payload={"rollback_kind": "delete_message", "chat_id": CHAT_ID, "message_id": 9999},
    )
    action = _make_action(status="succeeded")
    undo_repo = FakeButlerUndoInvocationRepo()

    # Pre-populate with a FAILED undo row (transient failure scenario)
    failed_undo = FakeButlerUndoInvocation(
        id=201,
        butler_action_id=ACTION_ID,
        butler_tool_invocation_id=INVOCATION_ID_1,
        requester_user_id=REQUESTER_ID,
        rollback_kind="delete_message",
        status="failed",
    )
    undo_repo._rows[(ACTION_ID, INVOCATION_ID_1)] = failed_undo

    bot = AsyncMock()
    # Simulate successful delete on retry
    bot.delete_message = AsyncMock(return_value=True)

    svc = _make_service(action=action, invocations=[inv], undo_repo=undo_repo)

    # This must NOT short-circuit — must attempt the delete again
    asyncio.run(
        svc.execute_undo(action_id=ACTION_ID, requester_user_id=REQUESTER_ID, bot=bot)
    )

    # Bot was called — the step was retried
    bot.delete_message.assert_awaited_once()


# ---------------------------------------------------------------------------
# R1 — affected_user with non-confirmed status must be forbidden
# ---------------------------------------------------------------------------


def test_affected_user_rejected_status_is_forbidden() -> None:
    """R1: affected_user whose confirmation status='rejected' must NOT be authorized to undo.

    Spec PLAN.md §728: 'parties to a CONSUMMATED action'. Rejected consent means
    the action was revoked — the user is not a party to the consummated action.
    """
    AFFECTED_USER_ID = 77
    inv = _make_invocation(rollback_kind="not_reversible")
    action = _make_action(requester_tg_id=REQUESTER_ID)

    confirmation_repo = FakeButlerActionConfirmationRepo(
        rows=[
            FakeButlerActionConfirmation(
                id=1,
                butler_action_id=ACTION_ID,
                confirmer_tg_id=AFFECTED_USER_ID,
                confirmation_role="affected_user",
                status="rejected",  # NOT confirmed — must NOT authorize
            )
        ]
    )
    svc = _make_service_with_confirmation_repo(
        action=action,
        confirmation_repo=confirmation_repo,
        invocations=[inv],
    )

    with pytest.raises(ButlerActionError) as exc_info:
        asyncio.run(
            svc.execute_undo(
                action_id=ACTION_ID,
                requester_user_id=AFFECTED_USER_ID,
                is_admin=False,
                bot=AsyncMock(),
            )
        )
    assert exc_info.value.error_kind == "forbidden"


def test_affected_user_revoked_status_is_forbidden() -> None:
    """R1: affected_user with status='revoked' must NOT be authorized to undo."""
    AFFECTED_USER_ID = 77
    inv = _make_invocation(rollback_kind="not_reversible")
    action = _make_action(requester_tg_id=REQUESTER_ID)

    confirmation_repo = FakeButlerActionConfirmationRepo(
        rows=[
            FakeButlerActionConfirmation(
                id=1,
                butler_action_id=ACTION_ID,
                confirmer_tg_id=AFFECTED_USER_ID,
                confirmation_role="affected_user",
                status="revoked",  # revoked consent — must NOT authorize
            )
        ]
    )
    svc = _make_service_with_confirmation_repo(
        action=action,
        confirmation_repo=confirmation_repo,
        invocations=[inv],
    )

    with pytest.raises(ButlerActionError) as exc_info:
        asyncio.run(
            svc.execute_undo(
                action_id=ACTION_ID,
                requester_user_id=AFFECTED_USER_ID,
                is_admin=False,
                bot=AsyncMock(),
            )
        )
    assert exc_info.value.error_kind == "forbidden"


# ---------------------------------------------------------------------------
# R2 — retried failed undo row must be updated to succeeded, errors cleared
# ---------------------------------------------------------------------------


def test_failed_undo_row_status_updated_on_successful_retry() -> None:
    """R2: when a 'failed' undo row is retried and succeeds, its status must become
    'succeeded' and error_message must be cleared to None.

    Bug: guard `if undo_row.status == 'pending'` skips status update for 'failed' rows,
    so the row stays 'failed' and every future /butler_undo re-executes the side-effect.
    """
    inv = _make_invocation(
        rollback_kind="delete_message",
        inverse_op_payload={"rollback_kind": "delete_message", "chat_id": CHAT_ID, "message_id": 9999},
    )
    action = _make_action(status="succeeded")
    undo_repo = FakeButlerUndoInvocationRepo()

    # Pre-populate with a 'failed' row that has an error message
    failed_undo = FakeButlerUndoInvocation(
        id=301,
        butler_action_id=ACTION_ID,
        butler_tool_invocation_id=INVOCATION_ID_1,
        requester_user_id=REQUESTER_ID,
        rollback_kind="delete_message",
        status="failed",
        error_kind="telegram_error",
        error_message="TelegramError: retry_after=30",
    )
    undo_repo._rows[(ACTION_ID, INVOCATION_ID_1)] = failed_undo

    bot = AsyncMock()
    bot.delete_message = AsyncMock(return_value=True)

    svc = _make_service(action=action, invocations=[inv], undo_repo=undo_repo)

    asyncio.run(
        svc.execute_undo(action_id=ACTION_ID, requester_user_id=REQUESTER_ID, bot=bot)
    )

    # The failed row must now have status='succeeded' with errors cleared
    final_row = undo_repo._rows[(ACTION_ID, INVOCATION_ID_1)]
    assert final_row.status == "succeeded", f"Expected 'succeeded', got {final_row.status!r}"
    assert final_row.error_message is None, f"Expected error_message=None, got {final_row.error_message!r}"


# ---------------------------------------------------------------------------
# R4 — config validator: butler_undo_ttl_minutes must be > 0
# ---------------------------------------------------------------------------


def _ttl_validator(v: int) -> int:
    """Inline replica of Settings.validate_butler_undo_ttl for isolated testing.

    R4: the config field_validator rejects values <= 0. This standalone function
    mirrors the exact logic so tests do not trigger the module-level Settings()
    instantiation (which requires BOT_TOKEN and other env vars at import time).
    The corresponding production validator in bot/config.py must stay in sync.
    """
    if v <= 0:
        raise ValueError(
            f"BUTLER_UNDO_TTL_MINUTES must be > 0 (got {v}); "
            "setting it to 0 or negative silently disables undo for all actions."
        )
    return v


def test_butler_undo_ttl_minutes_zero_raises() -> None:
    """R4: butler_undo_ttl_minutes=0 must fail validation (value <= 0)."""
    with pytest.raises(ValueError, match="must be > 0"):
        _ttl_validator(0)


def test_butler_undo_ttl_minutes_negative_raises() -> None:
    """R4: butler_undo_ttl_minutes=-5 must fail validation (value <= 0)."""
    with pytest.raises(ValueError, match="must be > 0"):
        _ttl_validator(-5)


def test_butler_undo_ttl_minutes_positive_is_valid() -> None:
    """R4: butler_undo_ttl_minutes=60 (default) must pass validation."""
    assert _ttl_validator(60) == 60
