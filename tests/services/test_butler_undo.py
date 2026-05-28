"""Behaviour tests for ButlerService.undo_action — T12-07 (Wave 3 Stream G).

TDD coverage for the 6 acceptance criteria of PHASE12_PLAN §T12-07:

  1. undo creates a linked butler_actions.parent_action_id child row;
  2. the original action audit row remains immutable;
  3. delete_message / edit_message / followup_correction / cancel_pending
     inverse kinds are executed;
  4. irreversible actions report not_reversible and write audit;
  5. failed undo records undo_failed with structured error_context;
  6. actor authorization (requester / affected_user / admin) is enforced.

Plus C10.b (citations preserved across undo — child inherits
evidence_context_hash + evidence_ids + llm_usage_ledger_id), the double-undo
guard, wrong_status, and cascade-in-flight (FOR UPDATE NOWAIT) paths.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest

from bot.services.butler import (
    ButlerActionError,
    ButlerActionRejectedError,
    ButlerService,
    ButlerUndoError,
    CascadeInFlightError,
)

REQUESTER = 42
ADMIN = 7
AFFECTED = 99
STRANGER = 1234
CHAT_ID = -100_999_888_777
LEDGER_ID = 5050
EVIDENCE_HASH = "ctxhash-abc123"
EVIDENCE_IDS = [10, 11]


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeButlerAction:
    id: int
    requester_tg_id: int
    chat_id: int
    action_type: str
    status: str
    tool_name: str
    tool_manifest_version: str = "v1.0.0"
    governance_filter_version: str = "gov-v1"
    evidence_context_hash: str = EVIDENCE_HASH
    evidence_ids: list = field(default_factory=lambda: list(EVIDENCE_IDS))
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
    llm_usage_ledger_id: int | None = LEDGER_ID
    result_payload: dict | None = None
    result_payload_hash: str | None = None
    inverse_op_payload: dict | None = None
    parent_action_id: int | None = None
    plan_payload: dict = field(default_factory=dict)
    query: str = "who knows Rust?"
    visibility_scope: str = "member"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class FakeConfirmation:
    action_id: int
    confirmer_tg_id: int
    confirmation_role: str
    status: str = "confirmed"


class FakeButlerActionRepo:
    def __init__(self) -> None:
        self._rows: dict[int, FakeButlerAction] = {}
        self._next_id = 1
        self.locked_ids: set[int] = set()

    def _add(self, row: FakeButlerAction) -> FakeButlerAction:
        self._rows[row.id] = row
        self._next_id = max(self._next_id, row.id + 1)
        return row

    async def create(self, session: Any, **kwargs: Any) -> FakeButlerAction:
        row = FakeButlerAction(
            id=self._next_id,
            requester_tg_id=kwargs["requester_tg_id"],
            chat_id=kwargs["chat_id"],
            action_type=kwargs["action_type"],
            status=kwargs["status"],
            tool_name=kwargs["tool_name"],
            tool_manifest_version=kwargs.get("tool_manifest_version", "v1.0.0"),
            governance_filter_version=kwargs.get("governance_filter_version", "gov-v1"),
            evidence_context_hash=kwargs.get("evidence_context_hash", ""),
            evidence_ids=list(kwargs.get("evidence_ids") or []),
            plan_summary=kwargs.get("plan_summary", ""),
            action_args=kwargs.get("action_args", {}),
            action_args_hash=kwargs.get("action_args_hash", ""),
            rollback_kind=kwargs.get("rollback_kind", "not_reversible"),
            risk_level=kwargs.get("risk_level", "low"),
            requires_confirmation=kwargs.get("requires_confirmation", True),
            llm_usage_ledger_id=kwargs.get("llm_usage_ledger_id"),
            query=kwargs.get("query", ""),
            visibility_scope=kwargs.get("visibility_scope", "member"),
            plan_payload=kwargs.get("plan_payload") or {},
            parent_action_id=kwargs.get("parent_action_id"),
            inverse_op_payload=kwargs.get("inverse_op_payload"),
            undone_at=kwargs.get("undone_at"),
        )
        return self._add(row)

    async def get(self, session: Any, action_id: int) -> FakeButlerAction | None:
        return self._rows.get(action_id)

    async def get_for_update(self, session: Any, action_id: int) -> FakeButlerAction | None:
        if action_id in self.locked_ids:
            return None
        return self._rows.get(action_id)

    async def list_children(self, session: Any, parent_action_id: int) -> list[FakeButlerAction]:
        return [r for r in self._rows.values() if r.parent_action_id == parent_action_id]

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
        row = self._rows[action_id]
        row.status = status
        if error_code is not None:
            row.error_code = error_code
        if error_context is not None:
            row.error_context = error_context
        if result_payload is not None:
            row.result_payload = result_payload
        if undone_at is not None:
            row.undone_at = undone_at
        return 1


class FakeConfirmationRepo:
    def __init__(self, confirmations: list[FakeConfirmation] | None = None) -> None:
        self._rows = confirmations or []

    async def list_for_action(self, session: Any, action_id: int) -> list[FakeConfirmation]:
        return [c for c in self._rows if c.action_id == action_id]


@dataclass
class FakeUser:
    is_admin: bool = False
    is_member: bool = True


class FakeUserRepo:
    def __init__(self, users: dict[int, FakeUser]) -> None:
        self._users = users

    async def get(self, session: Any, user_id: int) -> FakeUser | None:
        return self._users.get(user_id)


class FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


class FakeBot:
    def __init__(self, *, fail: str | None = None) -> None:
        self.deleted: list[dict] = []
        self.edited: list[dict] = []
        self.sent: list[dict] = []
        self._fail = fail  # name of the method that should raise

    async def delete_message(self, *, chat_id: int, message_id: int) -> None:
        if self._fail == "delete_message":
            raise RuntimeError("message to delete not found")
        self.deleted.append({"chat_id": chat_id, "message_id": message_id})

    async def edit_message_text(self, *, chat_id: int, message_id: int, text: str, parse_mode: Any) -> None:
        if self._fail == "edit_message_text":
            raise RuntimeError("message can't be edited")
        self.edited.append(
            {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": parse_mode}
        )

    async def send_message(self, *, chat_id: int, text: str, parse_mode: Any) -> FakeMessage:
        if self._fail == "send_message":
            raise RuntimeError("chat not found")
        self.sent.append({"chat_id": chat_id, "text": text, "parse_mode": parse_mode})
        return FakeMessage(message_id=7777)


@dataclass
class FakeCandidate:
    id: uuid.UUID
    status: str = "pending"


class FakeExtractionCandidateRepo:
    def __init__(self, candidate: FakeCandidate | None = None) -> None:
        self.calls: list[dict] = []
        self._candidate = candidate

    async def get_by_id_for_update(
        self, session: Any, candidate_id: uuid.UUID
    ) -> FakeCandidate | None:
        if self._candidate is not None and self._candidate.id == candidate_id:
            return self._candidate
        return None

    async def mark_status(
        self, session: Any, *, candidate_id: uuid.UUID, status: str, reviewed_by: int
    ) -> None:
        self.calls.append(
            {"candidate_id": candidate_id, "status": status, "reviewed_by": reviewed_by}
        )


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_service(
    *,
    action_repo: FakeButlerActionRepo,
    confirmations: list[FakeConfirmation] | None = None,
    users: dict[int, FakeUser] | None = None,
    candidate_repo: Any = None,
) -> ButlerService:
    return ButlerService(
        session=object(),
        ledger_repo=None,
        butler_action_repo=action_repo,
        butler_action_confirmation_repo=FakeConfirmationRepo(confirmations),
        butler_tool_invocation_repo=None,
        butler_rate_bucket_repo=None,
        user_repo=FakeUserRepo(users) if users is not None else None,
        llm_gateway=None,
        evidence_builder=None,
        settings=object(),
        extraction_candidate_repo=candidate_repo,
    )


def _succeeded(
    repo: FakeButlerActionRepo,
    *,
    tool_name: str,
    rollback_kind: str,
    inverse: dict | None,
    action_type: str = "intro",
) -> FakeButlerAction:
    row = FakeButlerAction(
        id=repo._next_id,
        requester_tg_id=REQUESTER,
        chat_id=CHAT_ID,
        action_type=action_type,
        status="succeeded",
        tool_name=tool_name,
        rollback_kind=rollback_kind,
        inverse_op_payload=inverse,
    )
    repo._add(row)
    return row


# ---------------------------------------------------------------------------
# Criterion 3 — inverse kinds executed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_undo_delete_message_group_post() -> None:
    repo = FakeButlerActionRepo()
    orig = _succeeded(
        repo,
        tool_name="schedule_meeting",
        rollback_kind="delete_message",
        inverse={"rollback_kind": "delete_message", "chat_id": CHAT_ID, "message_id": 555},
        action_type="meeting",
    )
    bot = FakeBot()
    svc = _make_service(action_repo=repo)

    child = await svc.undo_action(action_id=orig.id, requester_user_id=REQUESTER, bot=bot)

    assert child.status == "undo_succeeded"
    assert child.parent_action_id == orig.id  # criterion 1
    assert bot.deleted == [{"chat_id": CHAT_ID, "message_id": 555}]
    # criterion 2 — original immutable
    assert orig.status == "succeeded"
    assert orig.undone_at is None


@pytest.mark.asyncio
async def test_undo_delete_message_dm_intro_uses_target_user_id() -> None:
    """send_intro inverse carries target_user_id (DM), not chat_id."""
    repo = FakeButlerActionRepo()
    orig = _succeeded(
        repo,
        tool_name="send_intro",
        rollback_kind="delete_message",
        inverse={"rollback_kind": "delete_message", "target_user_id": AFFECTED, "message_id": 888},
    )
    bot = FakeBot()
    svc = _make_service(action_repo=repo)

    await svc.undo_action(action_id=orig.id, requester_user_id=REQUESTER, bot=bot)

    assert bot.deleted == [{"chat_id": AFFECTED, "message_id": 888}]


@pytest.mark.asyncio
async def test_undo_edit_message_uses_retraction_notice() -> None:
    from bot.services.butler import _UNDO_RETRACTION_NOTICE

    repo = FakeButlerActionRepo()
    orig = _succeeded(
        repo,
        tool_name="update_intro",
        rollback_kind="edit_message",
        inverse={"rollback_kind": "edit_message", "chat_id": CHAT_ID, "message_id": 321},
        action_type="intro_update",
    )
    bot = FakeBot()
    svc = _make_service(action_repo=repo)

    await svc.undo_action(action_id=orig.id, requester_user_id=REQUESTER, bot=bot)

    assert len(bot.edited) == 1
    assert bot.edited[0]["text"] == _UNDO_RETRACTION_NOTICE
    assert bot.edited[0]["parse_mode"] is None  # never parse user-ish content


@pytest.mark.asyncio
async def test_undo_followup_correction_posts_notice() -> None:
    repo = FakeButlerActionRepo()
    orig = _succeeded(
        repo,
        tool_name="update_intro",
        rollback_kind="followup_correction",
        inverse={
            "rollback_kind": "followup_correction",
            "chat_id": CHAT_ID,
            "followup_message_id": 444,
        },
        action_type="intro_update",
    )
    bot = FakeBot()
    svc = _make_service(action_repo=repo)

    child = await svc.undo_action(action_id=orig.id, requester_user_id=REQUESTER, bot=bot)

    assert len(bot.sent) == 1
    assert bot.sent[0]["chat_id"] == CHAT_ID
    assert child.status == "undo_succeeded"


@pytest.mark.asyncio
async def test_undo_cancel_pending_rejects_candidate() -> None:
    repo = FakeButlerActionRepo()
    candidate_uuid = uuid.uuid4()
    orig = _succeeded(
        repo,
        tool_name="suggest_card_creation",
        rollback_kind="cancel_pending",
        inverse={
            "rollback_kind": "cancel_pending",
            "butler_card_suggestion_id": 12,
            "candidate_id": str(candidate_uuid),
        },
        action_type="card_suggestion",
    )
    cand_repo = FakeExtractionCandidateRepo(FakeCandidate(id=candidate_uuid, status="pending"))
    svc = _make_service(action_repo=repo, candidate_repo=cand_repo)

    await svc.undo_action(action_id=orig.id, requester_user_id=REQUESTER, bot=None)

    assert cand_repo.calls == [
        {"candidate_id": candidate_uuid, "status": "rejected", "reviewed_by": REQUESTER}
    ]


@pytest.mark.asyncio
async def test_undo_cancel_pending_skips_non_pending_candidate() -> None:
    """If an admin already acted on the candidate, undo must NOT clobber it."""
    repo = FakeButlerActionRepo()
    candidate_uuid = uuid.uuid4()
    orig = _succeeded(
        repo,
        tool_name="suggest_card_creation",
        rollback_kind="cancel_pending",
        inverse={
            "rollback_kind": "cancel_pending",
            "butler_card_suggestion_id": 12,
            "candidate_id": str(candidate_uuid),
        },
        action_type="card_suggestion",
    )
    cand_repo = FakeExtractionCandidateRepo(FakeCandidate(id=candidate_uuid, status="approved"))
    svc = _make_service(action_repo=repo, candidate_repo=cand_repo)

    child = await svc.undo_action(action_id=orig.id, requester_user_id=REQUESTER, bot=None)

    assert cand_repo.calls == []  # never rejected an already-approved candidate
    assert child.status == "undo_succeeded"
    assert child.result_payload["cancelled"] is False


# ---------------------------------------------------------------------------
# Criterion 4 — not_reversible
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_undo_not_reversible_records_audit_no_side_effect() -> None:
    repo = FakeButlerActionRepo()
    orig = _succeeded(
        repo,
        tool_name="recall_evidence",
        rollback_kind="not_reversible",
        inverse={"rollback_kind": "not_reversible"},
        action_type="recall",
    )
    bot = FakeBot()
    svc = _make_service(action_repo=repo)

    child = await svc.undo_action(action_id=orig.id, requester_user_id=REQUESTER, bot=bot)

    assert child.status == "undo_succeeded"
    assert child.parent_action_id == orig.id
    assert child.result_payload == {"rollback_kind": "not_reversible", "undone": False}
    assert bot.deleted == [] and bot.edited == [] and bot.sent == []


# ---------------------------------------------------------------------------
# Criterion 5 — failed undo records undo_failed + structured error_context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_undo_failed_records_error_context() -> None:
    repo = FakeButlerActionRepo()
    orig = _succeeded(
        repo,
        tool_name="send_intro",
        rollback_kind="delete_message",
        inverse={"rollback_kind": "delete_message", "target_user_id": AFFECTED, "message_id": 888},
    )
    bot = FakeBot(fail="delete_message")
    svc = _make_service(action_repo=repo)

    with pytest.raises(ButlerUndoError) as ei:
        await svc.undo_action(action_id=orig.id, requester_user_id=REQUESTER, bot=bot)

    assert ei.value.error_kind == "undo_failed"
    # child row is the one carrying the failure
    child = repo._rows[ei.value.action_id]
    assert child.status == "undo_failed"
    assert child.error_context["rollback_kind"] == "delete_message"
    assert child.error_context["exception_type"] == "RuntimeError"
    assert child.error_context["message_id"] == 888
    # criterion 2 — original still immutable
    assert orig.status == "succeeded"


# ---------------------------------------------------------------------------
# Criterion 6 — authorization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_undo_forbidden_for_stranger() -> None:
    repo = FakeButlerActionRepo()
    orig = _succeeded(
        repo,
        tool_name="send_intro",
        rollback_kind="delete_message",
        inverse={"rollback_kind": "delete_message", "target_user_id": AFFECTED, "message_id": 1},
    )
    svc = _make_service(action_repo=repo, users={STRANGER: FakeUser(is_admin=False)})

    with pytest.raises(ButlerActionRejectedError) as ei:
        await svc.undo_action(action_id=orig.id, requester_user_id=STRANGER, bot=FakeBot())

    assert ei.value.error_kind == "forbidden"
    # no child row created
    assert [r for r in repo._rows.values() if r.parent_action_id == orig.id] == []


@pytest.mark.asyncio
async def test_undo_allowed_for_affected_user() -> None:
    repo = FakeButlerActionRepo()
    orig = _succeeded(
        repo,
        tool_name="send_intro",
        rollback_kind="delete_message",
        inverse={"rollback_kind": "delete_message", "target_user_id": AFFECTED, "message_id": 2},
    )
    confs = [
        FakeConfirmation(action_id=orig.id, confirmer_tg_id=REQUESTER, confirmation_role="requester"),
        FakeConfirmation(action_id=orig.id, confirmer_tg_id=AFFECTED, confirmation_role="affected_user"),
    ]
    svc = _make_service(action_repo=repo, confirmations=confs, users={AFFECTED: FakeUser(is_admin=False)})

    child = await svc.undo_action(action_id=orig.id, requester_user_id=AFFECTED, bot=FakeBot())

    assert child.status == "undo_succeeded"


@pytest.mark.asyncio
async def test_undo_allowed_for_admin() -> None:
    repo = FakeButlerActionRepo()
    orig = _succeeded(
        repo,
        tool_name="schedule_meeting",
        rollback_kind="delete_message",
        inverse={"rollback_kind": "delete_message", "chat_id": CHAT_ID, "message_id": 3},
        action_type="meeting",
    )
    svc = _make_service(action_repo=repo, users={ADMIN: FakeUser(is_admin=True)})

    child = await svc.undo_action(action_id=orig.id, requester_user_id=ADMIN, bot=FakeBot())

    assert child.status == "undo_succeeded"


# ---------------------------------------------------------------------------
# Guards: wrong_status, double-undo, cascade-in-flight, C10.b
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_undo_wrong_status_for_non_succeeded() -> None:
    repo = FakeButlerActionRepo()
    orig = _succeeded(
        repo,
        tool_name="send_intro",
        rollback_kind="delete_message",
        inverse={"rollback_kind": "delete_message", "target_user_id": AFFECTED, "message_id": 4},
    )
    orig.status = "pending_confirmation"
    svc = _make_service(action_repo=repo)

    with pytest.raises(ButlerActionError) as ei:
        await svc.undo_action(action_id=orig.id, requester_user_id=REQUESTER, bot=FakeBot())

    assert ei.value.error_kind == "wrong_status"


@pytest.mark.asyncio
async def test_undo_double_undo_guard() -> None:
    repo = FakeButlerActionRepo()
    orig = _succeeded(
        repo,
        tool_name="send_intro",
        rollback_kind="delete_message",
        inverse={"rollback_kind": "delete_message", "target_user_id": AFFECTED, "message_id": 5},
    )
    svc = _make_service(action_repo=repo)
    bot = FakeBot()

    await svc.undo_action(action_id=orig.id, requester_user_id=REQUESTER, bot=bot)
    with pytest.raises(ButlerActionError) as ei:
        await svc.undo_action(action_id=orig.id, requester_user_id=REQUESTER, bot=bot)

    assert ei.value.error_kind == "wrong_status"
    # second attempt issued no further side effect
    assert len(bot.deleted) == 1


@pytest.mark.asyncio
async def test_undo_cascade_in_flight() -> None:
    repo = FakeButlerActionRepo()
    orig = _succeeded(
        repo,
        tool_name="send_intro",
        rollback_kind="delete_message",
        inverse={"rollback_kind": "delete_message", "target_user_id": AFFECTED, "message_id": 6},
    )
    repo.locked_ids.add(orig.id)
    svc = _make_service(action_repo=repo)

    with pytest.raises(CascadeInFlightError):
        await svc.undo_action(action_id=orig.id, requester_user_id=REQUESTER, bot=FakeBot())


@pytest.mark.asyncio
async def test_undo_child_inherits_citations_and_ledger() -> None:
    """C10.b — undo child inherits evidence_context_hash + evidence_ids + ledger."""
    repo = FakeButlerActionRepo()
    orig = _succeeded(
        repo,
        tool_name="schedule_meeting",
        rollback_kind="delete_message",
        inverse={"rollback_kind": "delete_message", "chat_id": CHAT_ID, "message_id": 9},
        action_type="meeting",
    )
    svc = _make_service(action_repo=repo)

    child = await svc.undo_action(action_id=orig.id, requester_user_id=REQUESTER, bot=FakeBot())

    assert child.evidence_context_hash == orig.evidence_context_hash == EVIDENCE_HASH
    assert child.evidence_ids == EVIDENCE_IDS
    assert child.llm_usage_ledger_id == LEDGER_ID
    assert child.rollback_kind == "not_reversible"  # undo is not itself undoable
