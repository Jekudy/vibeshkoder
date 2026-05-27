"""Integration tests for butler undo real-producer → dispatcher contract (T12-07 M4).

M4 ROOT CAUSE FIX: prior tests hand-crafted inverse_op_payload keys that matched
what the dispatcher reads. Real producers emit different keys → silent mismatch in
production. These tests instantiate real tool classes, call build_inverse, and assert
the resulting payload is consumable by the matching _undo_* dispatcher.

Test inventory (one per (tool_name, rollback_kind) pair):
1. send_intro + delete_message         — chat_id emitted (C2 fix verified)
2. schedule_meeting + delete_message   — chat_id emitted (already correct)
3. update_intro + edit_message         — chat_id + message_id emitted (C3 path)
4. update_intro + followup_correction  — followup_message_id emitted (C4 fix verified)
5. suggest_card_creation + cancel_pending — payload shape consumed correctly
6. recall_evidence + not_reversible    — skipped cleanly

These tests FAIL on pre-fix state (where send_intro emitted target_user_id, not chat_id).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from bot.services.butler_tools import ToolResult
from bot.services.butler_tools.send_intro import SendIntroTool
from bot.services.butler_tools.schedule_meeting import ScheduleMeetingTool
from bot.services.butler_tools.update_intro import UpdateIntroTool
from bot.services.butler_tools.suggest_card_creation import SuggestCardCreationTool
from bot.services.butler_tools.recall_evidence import RecallEvidenceTool
from bot.services.butler import ButlerService

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REQUESTER_ID = 42
CHAT_ID = 123456789   # In DMs, user_id == chat_id
MESSAGE_ID = 9001
FOLLOWUP_MESSAGE_ID = 9002


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Test 1: send_intro.build_inverse → chat_id for delete_message dispatcher
# ---------------------------------------------------------------------------


def test_send_intro_build_inverse_emits_chat_id() -> None:
    """C2: send_intro.build_inverse must emit chat_id (not target_user_id).

    _undo_delete_message reads payload.get('chat_id'). Pre-fix, send_intro
    emitted target_user_id which is ignored → chat_id=None → missing_payload_fields.
    """
    tool = SendIntroTool()
    result = ToolResult(
        success=True,
        payload={
            "target_user_id": CHAT_ID,
            "message_id": MESSAGE_ID,
        },
    )

    inverse = asyncio.run(tool.build_inverse(result))

    assert inverse["rollback_kind"] == "delete_message"
    # chat_id must be present and == target_user_id for DMs
    assert inverse.get("chat_id") == CHAT_ID, (
        f"send_intro.build_inverse must emit chat_id (got {inverse})"
    )
    assert inverse.get("message_id") == MESSAGE_ID
    # target_user_id is no longer required in the dispatcher
    # (may still be present as extra field, but chat_id is what matters)


# ---------------------------------------------------------------------------
# Test 2: schedule_meeting.build_inverse → chat_id already correct
# ---------------------------------------------------------------------------


def test_schedule_meeting_build_inverse_emits_chat_id() -> None:
    """schedule_meeting.build_inverse already emits chat_id — verify stays correct."""
    tool = ScheduleMeetingTool()
    result = ToolResult(
        success=True,
        payload={
            "chat_id": CHAT_ID,
            "message_id": MESSAGE_ID,
        },
    )

    inverse = asyncio.run(tool.build_inverse(result))

    assert inverse["rollback_kind"] == "delete_message"
    assert inverse.get("chat_id") == CHAT_ID
    assert inverse.get("message_id") == MESSAGE_ID


# ---------------------------------------------------------------------------
# Test 3: update_intro.build_inverse (outcome=edited) → edit_message path
# _undo_edit_message consumes chat_id + message_id (+ optional prior_text)
# ---------------------------------------------------------------------------


def test_update_intro_build_inverse_edit_message_shape() -> None:
    """update_intro (edit path) build_inverse → edit_message with chat_id + message_id.

    C3: prior_text intentionally omitted. _undo_edit_message will query
    message_versions when prior_text is absent.
    """
    tool = UpdateIntroTool()
    result = ToolResult(
        success=True,
        payload={
            "outcome": "edited",
            "message_id": MESSAGE_ID,
            "chat_id": CHAT_ID,
        },
    )

    inverse = asyncio.run(tool.build_inverse(result))

    assert inverse["rollback_kind"] == "edit_message"
    assert inverse.get("chat_id") == CHAT_ID
    assert inverse.get("message_id") == MESSAGE_ID
    # prior_text intentionally absent (privacy) — dispatcher handles fallback
    assert "prior_text" not in inverse


# ---------------------------------------------------------------------------
# Test 4: update_intro.build_inverse (outcome=followup_reply) → followup_correction
# _undo_followup_correction reads followup_message_id (C4 fix)
# ---------------------------------------------------------------------------


def test_update_intro_build_inverse_followup_shape() -> None:
    """C4: update_intro (followup path) → followup_correction with followup_message_id.

    _undo_followup_correction reads payload.get('followup_message_id').
    Pre-fix, dispatcher read correction_text which was never emitted → failed.
    """
    tool = UpdateIntroTool()
    result = ToolResult(
        success=True,
        payload={
            "outcome": "followup_reply",
            "followup_message_id": FOLLOWUP_MESSAGE_ID,
            "chat_id": CHAT_ID,
        },
    )

    inverse = asyncio.run(tool.build_inverse(result))

    assert inverse["rollback_kind"] == "followup_correction"
    assert inverse.get("chat_id") == CHAT_ID
    assert inverse.get("followup_message_id") == FOLLOWUP_MESSAGE_ID
    # correction_text is not present (old field, dropped)
    assert "correction_text" not in inverse


# ---------------------------------------------------------------------------
# Test 5: suggest_card_creation.build_inverse → cancel_pending dispatcher
# ---------------------------------------------------------------------------


def test_suggest_card_creation_build_inverse_cancel_pending_shape() -> None:
    """suggest_card_creation.build_inverse → cancel_pending.

    _undo_cancel_pending uses butler_action_id (from execute_undo caller context),
    not from the payload. Verify rollback_kind is correct.
    """
    tool = SuggestCardCreationTool()
    result = ToolResult(
        success=True,
        payload={
            "butler_card_suggestion_id": 77,
            "candidate_id": "some-uuid",
        },
    )

    inverse = asyncio.run(tool.build_inverse(result))

    assert inverse["rollback_kind"] == "cancel_pending"


# ---------------------------------------------------------------------------
# Test 6: recall_evidence.build_inverse → not_reversible skipped cleanly
# ---------------------------------------------------------------------------


def test_recall_evidence_build_inverse_not_reversible() -> None:
    """recall_evidence.build_inverse → not_reversible → dispatcher skips cleanly."""
    tool = RecallEvidenceTool()
    result = ToolResult(success=True, payload={})

    inverse = asyncio.run(tool.build_inverse(result))

    assert inverse["rollback_kind"] == "not_reversible"


# ---------------------------------------------------------------------------
# Test 7: H2 — idempotency before TTL check
# An already-undone action past TTL window must return cached summary, not ttl_expired.
# ---------------------------------------------------------------------------


@dataclass
class _FakeActionH2:
    id: int = 1
    requester_tg_id: int = REQUESTER_ID
    status: str = "undone"
    executed_at: datetime = field(default_factory=lambda: _now() - __import__("datetime").timedelta(hours=5))
    chat_id: int = CHAT_ID
    plan_payload: dict = field(default_factory=dict)


@dataclass
class _FakeInvH2:
    id: int = 10
    action_id: int = 1
    tool_name: str = "recall_evidence"
    idempotency_key: str = "k1"
    request_payload: dict = field(default_factory=dict)
    request_payload_hash: str = ""
    invocation_seq: int = 1
    inverse_op_payload: dict = field(default_factory=lambda: {"rollback_kind": "not_reversible"})
    status: str = "succeeded"


@dataclass
class _FakeUndoRowH2:
    id: int = 200
    butler_action_id: int = 1
    butler_tool_invocation_id: int = 10
    requester_user_id: int = REQUESTER_ID
    rollback_kind: str = "not_reversible"
    status: str = "skipped_not_reversible"


class _FakeActionRepoH2:
    def __init__(self, action: Any) -> None:
        self._action = action
        self.updates: list[dict] = []

    async def get(self, session: Any, action_id: int) -> Any:
        return self._action

    async def get_for_update(self, session: Any, action_id: int) -> Any:
        return self._action

    async def update_status(self, session: Any, action_id: int, **kwargs: Any) -> int:
        self.updates.append(kwargs)
        return 1


class _FakeInvRepoH2:
    def __init__(self, invocations: list) -> None:
        self._invocations = invocations

    async def list_for_action(self, session: Any, action_id: int) -> list:
        return self._invocations


class _FakeUndoRepoH2:
    def __init__(self, existing_row: Any) -> None:
        self._row = existing_row

    async def find_by_action_and_invocation(self, session: Any, action_id: int, inv_id: int) -> Any:
        return self._row

    async def create(self, session: Any, **kwargs: Any) -> Any:
        raise AssertionError("Should not create new undo row on idempotent re-run")

    async def update_status(self, session: Any, undo_id: int, **kwargs: Any) -> int:
        return 1


def test_idempotency_before_ttl() -> None:
    """H2: action already fully undone + past TTL → return cached summary (not ttl_expired).

    Pre-fix order was: TTL check → idempotency. Past-TTL already-undone actions
    returned ttl_expired instead of the existing summary.
    """
    import datetime

    action = _FakeActionH2(
        status="undone",
        # executed_at 5 hours ago — way past default 60-min TTL
        executed_at=_now() - datetime.timedelta(hours=5),
    )
    inv = _FakeInvH2()
    existing_undo = _FakeUndoRowH2(status="skipped_not_reversible")

    action_repo = _FakeActionRepoH2(action)
    inv_repo = _FakeInvRepoH2([inv])
    undo_repo = _FakeUndoRepoH2(existing_undo)

    svc = ButlerService(
        session=AsyncMock(),
        ledger_repo=MagicMock(),
        butler_action_repo=action_repo,
        butler_action_confirmation_repo=MagicMock(),
        butler_tool_invocation_repo=inv_repo,
        butler_rate_bucket_repo=MagicMock(),
        user_repo=MagicMock(),
        llm_gateway=MagicMock(),
        evidence_builder=MagicMock(),
        settings=MagicMock(butler_undo_ttl_minutes=60),
        undo_invocation_repo=undo_repo,
        card_suggestion_repo=MagicMock(),
    )

    # Must NOT raise ttl_expired — must return existing summary
    summary = asyncio.run(
        svc.execute_undo(action_id=1, requester_user_id=REQUESTER_ID, bot=AsyncMock())
    )
    assert summary["status"] == "undone"
    assert len(summary["steps"]) == 1
    assert summary["steps"][0].status == "skipped_not_reversible"


# ---------------------------------------------------------------------------
# Test 8: C5 — dismiss_by_undo passes reviewer_user_id (CHECK constraint satisfied)
# ---------------------------------------------------------------------------


def test_cancel_pending_passes_reviewer_user_id() -> None:
    """C5: _undo_cancel_pending threads requester_user_id → dismiss_by_undo(reviewer_user_id).

    Without reviewer_user_id, DB CHECK ck_extraction_candidates_reviewer_consistency fails.
    """
    captured: list[dict] = []

    class _FakeCardRepo:
        async def dismiss_by_undo(self, session: Any, action_id: int, *, reviewer_user_id: int) -> int:
            captured.append({"action_id": action_id, "reviewer_user_id": reviewer_user_id})
            return 1

    @dataclass
    class _FakeAction:
        id: int = 1
        requester_tg_id: int = REQUESTER_ID
        status: str = "succeeded"
        executed_at: datetime = field(default_factory=_now)
        chat_id: int = CHAT_ID
        plan_payload: dict = field(default_factory=dict)

    @dataclass
    class _FakeInv:
        id: int = 10
        action_id: int = 1
        tool_name: str = "suggest_card_creation"
        idempotency_key: str = "k2"
        request_payload: dict = field(default_factory=dict)
        request_payload_hash: str = ""
        invocation_seq: int = 1
        inverse_op_payload: dict = field(default_factory=lambda: {"rollback_kind": "cancel_pending"})
        status: str = "succeeded"

    class _LocalActionRepo:
        async def get(self, s: Any, aid: int) -> Any:
            return None

        async def get_for_update(self, s: Any, aid: int) -> Any:
            return _FakeAction()

        async def update_status(self, s: Any, aid: int, **kw: Any) -> int:
            return 1

    class _LocalInvRepo:
        async def list_for_action(self, s: Any, aid: int) -> list:
            return [_FakeInv()]

    class _LocalUndoRepo:
        async def find_by_action_and_invocation(self, s: Any, aid: int, iid: int) -> Any:
            return None

        async def create(self, s: Any, **kw: Any) -> Any:
            @dataclass
            class _Row:
                id: int = 99
                status: str = "pending"

            return _Row()

        async def update_status(self, s: Any, uid: int, **kw: Any) -> int:
            return 1

    svc = ButlerService(
        session=AsyncMock(),
        ledger_repo=MagicMock(),
        butler_action_repo=_LocalActionRepo(),
        butler_action_confirmation_repo=MagicMock(),
        butler_tool_invocation_repo=_LocalInvRepo(),
        butler_rate_bucket_repo=MagicMock(),
        user_repo=MagicMock(),
        llm_gateway=MagicMock(),
        evidence_builder=MagicMock(),
        settings=MagicMock(butler_undo_ttl_minutes=60),
        undo_invocation_repo=_LocalUndoRepo(),
        card_suggestion_repo=_FakeCardRepo(),
    )

    asyncio.run(svc.execute_undo(action_id=1, requester_user_id=REQUESTER_ID, bot=AsyncMock()))

    assert len(captured) == 1
    assert captured[0]["reviewer_user_id"] == REQUESTER_ID, (
        f"dismiss_by_undo must receive reviewer_user_id={REQUESTER_ID}, got {captured}"
    )


# ---------------------------------------------------------------------------
# Test 9: H5 — error_message uses coded key, not raw exception text
# ---------------------------------------------------------------------------


def test_delete_message_error_message_no_raw_text() -> None:
    """H5: bot.delete_message failure → error_message is class name, not raw exc text.

    Raw exc text can contain user content (e.g. Telegram API echoes message text).
    After the fix, error_message = exc.__class__.__name__ only.
    """
    from dataclasses import dataclass as dc

    @dc
    class _FakeAction2:
        id: int = 1
        requester_tg_id: int = REQUESTER_ID
        status: str = "succeeded"
        executed_at: datetime = field(default_factory=_now)
        chat_id: int = CHAT_ID
        plan_payload: dict = field(default_factory=dict)

    @dc
    class _FakeInv2:
        id: int = 10
        action_id: int = 1
        tool_name: str = "send_intro"
        idempotency_key: str = "k3"
        request_payload: dict = field(default_factory=dict)
        request_payload_hash: str = ""
        invocation_seq: int = 1
        inverse_op_payload: dict = field(default_factory=lambda: {
            "rollback_kind": "delete_message",
            "chat_id": CHAT_ID,
            "message_id": MESSAGE_ID,
        })
        status: str = "succeeded"

    captured_error_msgs: list[str | None] = []

    class _LocalActionRepo2:
        async def get_for_update(self, s: Any, aid: int) -> Any:
            return _FakeAction2()

        async def update_status(self, s: Any, aid: int, **kw: Any) -> int:
            return 1

    class _LocalInvRepo2:
        async def list_for_action(self, s: Any, aid: int) -> list:
            return [_FakeInv2()]

    class _LocalUndoRepo2:
        async def find_by_action_and_invocation(self, s: Any, aid: int, iid: int) -> Any:
            return None

        async def create(self, s: Any, **kw: Any) -> Any:
            @dc
            class _Row:
                id: int = 99
                status: str = "pending"

            return _Row()

        async def update_status(self, s: Any, uid: int, *, status: str, error_kind: str | None = None, error_message: str | None = None) -> int:
            if error_message is not None:
                captured_error_msgs.append(error_message)
            return 1

    svc = ButlerService(
        session=AsyncMock(),
        ledger_repo=MagicMock(),
        butler_action_repo=_LocalActionRepo2(),
        butler_action_confirmation_repo=MagicMock(),
        butler_tool_invocation_repo=_LocalInvRepo2(),
        butler_rate_bucket_repo=MagicMock(),
        user_repo=MagicMock(),
        llm_gateway=MagicMock(),
        evidence_builder=MagicMock(),
        settings=MagicMock(butler_undo_ttl_minutes=60),
        undo_invocation_repo=_LocalUndoRepo2(),
        card_suggestion_repo=MagicMock(),
    )

    bot = AsyncMock()
    sensitive_text = "SECRET_USER_CONTENT: very private message"
    bot.delete_message.side_effect = ValueError(sensitive_text)

    asyncio.run(svc.execute_undo(action_id=1, requester_user_id=REQUESTER_ID, bot=bot))

    # error_message must NOT contain the sensitive exception text
    for msg in captured_error_msgs:
        if msg is not None:
            assert sensitive_text not in msg, (
                f"error_message leaked sensitive content: {msg!r}"
            )
            # Should be class name only
            assert "ValueError" in msg, f"Expected class name in error_message, got: {msg!r}"


# ---------------------------------------------------------------------------
# Test 10: M2 — skip terminal steps on partial-failure retry
# ---------------------------------------------------------------------------


def test_skip_terminal_steps_on_retry() -> None:
    """M2: on partial-failure retry, succeeded steps are NOT re-dispatched."""
    @dataclass
    class _FakeAction3:
        id: int = 1
        requester_tg_id: int = REQUESTER_ID
        status: str = "succeeded"
        executed_at: datetime = field(default_factory=_now)
        chat_id: int = CHAT_ID
        plan_payload: dict = field(default_factory=dict)

    @dataclass
    class _FakeInv3:
        id: int
        action_id: int = 1
        tool_name: str = "send_intro"
        idempotency_key: str = ""
        request_payload: dict = field(default_factory=dict)
        request_payload_hash: str = ""
        invocation_seq: int = 1
        status: str = "succeeded"
        inverse_op_payload: dict = field(default_factory=dict)

    @dataclass
    class _FakeUndoRow3:
        id: int
        status: str
        butler_action_id: int = 1
        butler_tool_invocation_id: int = 0
        requester_user_id: int = REQUESTER_ID
        rollback_kind: str = "delete_message"

    inv_succeeded = _FakeInv3(
        id=10, invocation_seq=1,
        inverse_op_payload={"rollback_kind": "delete_message", "chat_id": CHAT_ID, "message_id": 1001}
    )
    inv_pending = _FakeInv3(
        id=11, invocation_seq=2,
        inverse_op_payload={"rollback_kind": "delete_message", "chat_id": CHAT_ID, "message_id": 1002}
    )

    # inv_succeeded already has a terminal undo row; inv_pending does not
    existing_undo_succeeded = _FakeUndoRow3(id=200, status="succeeded", butler_tool_invocation_id=10)

    class _RetryUndoRepo:
        def __init__(self) -> None:
            self.creates: list[dict] = []
            self.updates: list[dict] = []

        async def find_by_action_and_invocation(self, s: Any, aid: int, iid: int) -> Any:
            if iid == 10:
                return existing_undo_succeeded
            return None

        async def create(self, s: Any, **kw: Any) -> Any:
            self.creates.append(kw)

            @dataclass
            class _Row2:
                id: int = 201
                status: str = "pending"
                butler_action_id: int = 1
                butler_tool_invocation_id: int = 11
                requester_user_id: int = REQUESTER_ID
                rollback_kind: str = "delete_message"

            return _Row2()

        async def update_status(self, s: Any, uid: int, **kw: Any) -> int:
            self.updates.append(kw)
            return 1

        async def list_by_action(self, s: Any, aid: int) -> list:
            return []

    class _LocalActionRepo3:
        async def get_for_update(self, s: Any, aid: int) -> Any:
            return _FakeAction3()

        async def update_status(self, s: Any, aid: int, **kw: Any) -> int:
            return 1

    class _LocalInvRepo3:
        async def list_for_action(self, s: Any, aid: int) -> list:
            return [inv_succeeded, inv_pending]

    retry_undo_repo = _RetryUndoRepo()

    svc = ButlerService(
        session=AsyncMock(),
        ledger_repo=MagicMock(),
        butler_action_repo=_LocalActionRepo3(),
        butler_action_confirmation_repo=MagicMock(),
        butler_tool_invocation_repo=_LocalInvRepo3(),
        butler_rate_bucket_repo=MagicMock(),
        user_repo=MagicMock(),
        llm_gateway=MagicMock(),
        evidence_builder=MagicMock(),
        settings=MagicMock(butler_undo_ttl_minutes=60),
        undo_invocation_repo=retry_undo_repo,
        card_suggestion_repo=MagicMock(),
    )

    bot = AsyncMock()
    asyncio.run(svc.execute_undo(action_id=1, requester_user_id=REQUESTER_ID, bot=bot))

    # Only 1 new undo row created (for inv_pending=11, not inv_succeeded=10)
    assert len(retry_undo_repo.creates) == 1
    # bot.delete_message called once for message_id=1002 (LIFO: 11 before 10)
    # but 10 is terminal so skipped → only 1002 dispatched
    bot.delete_message.assert_awaited_once_with(chat_id=CHAT_ID, message_id=1002)
