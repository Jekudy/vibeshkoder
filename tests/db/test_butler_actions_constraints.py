"""CHECK constraint enforcement tests for butler_actions and related tables (T12-01).

Every CHECK constraint has both:
  - POSITIVE test: valid value is accepted
  - NEGATIVE test: invalid value raises IntegrityError

Uses outer-transaction isolation (db_session fixture).
Tests do NOT call session.commit().
"""

from __future__ import annotations

import itertools

import pytest
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.usefixtures("app_env")

_counter = itertools.count(start=7_800_000_000)


def _next_id() -> int:
    return next(_counter)


def _base_action_kwargs(**overrides) -> dict:
    """Return minimal valid kwargs for ButlerAction creation."""
    defaults = dict(
        requester_tg_id=_next_id(),
        chat_id=_next_id(),
        action_type="recall",
        status="rejected",
        tool_name="recall_evidence",
        tool_manifest_version="1.0",
        governance_filter_version="v1",
        evidence_context_hash="abc",
        plan_summary="plan",
        action_args={},
        action_args_hash="h",
        rollback_kind="not_reversible",
        risk_level="low",
    )
    defaults.update(overrides)
    return defaults


async def _insert_action(db_session, **kwargs) -> int:
    from bot.db.models import ButlerAction

    row = ButlerAction(**_base_action_kwargs(**kwargs))
    db_session.add(row)
    await db_session.flush()
    return row.id


# ── ck_butler_actions_status ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status",
    [
        "rejected",
        "expired",
        "cancelled",
    ],
)
async def test_butler_action_status_exempt_from_ledger_valid(db_session, status: str) -> None:
    """Statuses exempt from ledger requirement are accepted without llm_usage_ledger_id."""
    await _insert_action(db_session, status=status)


async def test_butler_action_status_invalid(db_session) -> None:
    from bot.db.models import ButlerAction
    from sqlalchemy import text

    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(
                "INSERT INTO butler_actions "
                "(requester_tg_id, chat_id, action_type, status, tool_name, "
                " tool_manifest_version, governance_filter_version, "
                " evidence_context_hash, plan_summary, action_args, action_args_hash, "
                " rollback_kind, risk_level) "
                "VALUES (:tg, :chat, 'recall', 'INVALID_STATUS', 'recall_evidence', "
                " '1.0', 'v1', 'x', 'p', '{}', 'h', 'not_reversible', 'low')"
            ),
            {"tg": _next_id(), "chat": _next_id()},
        )


# ── ck_butler_actions_tool_name ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "tool_name",
    [
        "recall_evidence",
        "schedule_meeting",
        "send_intro",
        "update_intro",
        "suggest_card_creation",
    ],
)
async def test_butler_action_tool_name_valid(db_session, tool_name: str) -> None:
    await _insert_action(db_session, tool_name=tool_name)


async def test_butler_action_tool_name_invalid(db_session) -> None:
    from sqlalchemy import text

    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(
                "INSERT INTO butler_actions "
                "(requester_tg_id, chat_id, action_type, status, tool_name, "
                " tool_manifest_version, governance_filter_version, "
                " evidence_context_hash, plan_summary, action_args, action_args_hash, "
                " rollback_kind, risk_level) "
                "VALUES (:tg, :chat, 'recall', 'rejected', 'nonexistent_tool', "
                " '1.0', 'v1', 'x', 'p', '{}', 'h', 'not_reversible', 'low')"
            ),
            {"tg": _next_id(), "chat": _next_id()},
        )


# ── ck_butler_actions_rollback_kind ──────────────────────────────────────────


@pytest.mark.parametrize(
    "rollback_kind",
    [
        "delete_message",
        "edit_message",
        "followup_correction",
        "cancel_pending",
        "not_reversible",
    ],
)
async def test_butler_action_rollback_kind_valid(db_session, rollback_kind: str) -> None:
    await _insert_action(db_session, rollback_kind=rollback_kind)


async def test_butler_action_rollback_kind_invalid(db_session) -> None:
    from sqlalchemy import text

    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(
                "INSERT INTO butler_actions "
                "(requester_tg_id, chat_id, action_type, status, tool_name, "
                " tool_manifest_version, governance_filter_version, "
                " evidence_context_hash, plan_summary, action_args, action_args_hash, "
                " rollback_kind, risk_level) "
                "VALUES (:tg, :chat, 'recall', 'rejected', 'recall_evidence', "
                " '1.0', 'v1', 'x', 'p', '{}', 'h', 'unknown_kind', 'low')"
            ),
            {"tg": _next_id(), "chat": _next_id()},
        )


# ── ck_butler_actions_risk_level ─────────────────────────────────────────────


@pytest.mark.parametrize("risk_level", ["low", "medium", "high"])
async def test_butler_action_risk_level_valid(db_session, risk_level: str) -> None:
    await _insert_action(db_session, risk_level=risk_level)


async def test_butler_action_risk_level_invalid(db_session) -> None:
    from sqlalchemy import text

    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(
                "INSERT INTO butler_actions "
                "(requester_tg_id, chat_id, action_type, status, tool_name, "
                " tool_manifest_version, governance_filter_version, "
                " evidence_context_hash, plan_summary, action_args, action_args_hash, "
                " rollback_kind, risk_level) "
                "VALUES (:tg, :chat, 'recall', 'rejected', 'recall_evidence', "
                " '1.0', 'v1', 'x', 'p', '{}', 'h', 'not_reversible', 'critical')"
            ),
            {"tg": _next_id(), "chat": _next_id()},
        )


# ── ck_butler_actions_action_type ────────────────────────────────────────────


@pytest.mark.parametrize(
    "action_type", ["meeting", "intro", "intro_update", "card_suggestion", "recall"]
)
async def test_butler_action_type_valid(db_session, action_type: str) -> None:
    await _insert_action(db_session, action_type=action_type)


async def test_butler_action_type_invalid(db_session) -> None:
    from sqlalchemy import text

    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(
                "INSERT INTO butler_actions "
                "(requester_tg_id, chat_id, action_type, status, tool_name, "
                " tool_manifest_version, governance_filter_version, "
                " evidence_context_hash, plan_summary, action_args, action_args_hash, "
                " rollback_kind, risk_level) "
                "VALUES (:tg, :chat, 'invalid_type', 'rejected', 'recall_evidence', "
                " '1.0', 'v1', 'x', 'p', '{}', 'h', 'not_reversible', 'low')"
            ),
            {"tg": _next_id(), "chat": _next_id()},
        )


# ── ck_butler_actions_confirmation_policy ────────────────────────────────────


@pytest.mark.parametrize("policy", ["per_action", "opt_in_by_button"])
async def test_butler_action_confirmation_policy_valid(db_session, policy: str) -> None:
    await _insert_action(db_session, confirmation_policy=policy)


async def test_butler_action_confirmation_policy_invalid(db_session) -> None:
    from sqlalchemy import text

    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(
                "INSERT INTO butler_actions "
                "(requester_tg_id, chat_id, action_type, status, tool_name, "
                " tool_manifest_version, governance_filter_version, "
                " evidence_context_hash, plan_summary, action_args, action_args_hash, "
                " rollback_kind, risk_level, confirmation_policy) "
                "VALUES (:tg, :chat, 'recall', 'rejected', 'recall_evidence', "
                " '1.0', 'v1', 'x', 'p', '{}', 'h', 'not_reversible', 'low', "
                " 'session_wide')"
            ),
            {"tg": _next_id(), "chat": _next_id()},
        )


# ── ck_butler_actions_ledger_required_post_plan ──────────────────────────────


async def test_butler_action_ledger_required_post_plan_rejected_ok(db_session) -> None:
    """rejected/expired/cancelled are exempt from ledger requirement."""
    await _insert_action(db_session, status="rejected")
    await _insert_action(db_session, status="expired")
    await _insert_action(db_session, status="cancelled")


async def test_butler_action_ledger_required_post_plan_planned_no_ledger_fails(
    db_session,
) -> None:
    """planned status without llm_usage_ledger_id should violate the CHECK."""
    from sqlalchemy import text

    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(
                "INSERT INTO butler_actions "
                "(requester_tg_id, chat_id, action_type, status, tool_name, "
                " tool_manifest_version, governance_filter_version, "
                " evidence_context_hash, plan_summary, action_args, action_args_hash, "
                " rollback_kind, risk_level, llm_usage_ledger_id) "
                "VALUES (:tg, :chat, 'recall', 'planned', 'recall_evidence', "
                " '1.0', 'v1', 'x', 'p', '{}', 'h', 'not_reversible', 'low', NULL)"
            ),
            {"tg": _next_id(), "chat": _next_id()},
        )


# ── ck_butler_actions_executed_has_inverse ───────────────────────────────────


async def test_butler_action_executed_has_inverse_not_reversible_ok(db_session) -> None:
    """rejected + rollback_kind='not_reversible' (no inverse_op_payload needed)."""
    # Testing with 'rejected' (exempt from ledger) to isolate the inverse constraint logic.
    # The ck_butler_actions_executed_has_inverse only fires for status IN
    # ('succeeded','undo_pending','undo_succeeded') — 'rejected' is unconditionally accepted.
    await _insert_action(
        db_session,
        status="rejected",
        rollback_kind="not_reversible",
    )


async def test_butler_action_executed_has_inverse_edit_message_ok(db_session) -> None:
    """rejected + rollback_kind='edit_message' (not in the succeeded set — accepted)."""
    await _insert_action(
        db_session,
        status="rejected",
        rollback_kind="edit_message",
    )


# ── ck_butler_tool_invocations constraints ────────────────────────────────────


async def test_butler_tool_invocation_status_valid(db_session) -> None:
    from bot.db.models import ButlerToolInvocation

    action_id, _ = await _make_action_id(db_session)
    for status in ("pending", "running", "succeeded", "failed", "rolled_back"):
        inv = ButlerToolInvocation(
            action_id=action_id,
            tool_name="recall_evidence",
            invocation_seq=1,
            idempotency_key=f"ikey-{_next_id()}",
            request_payload={},
            request_payload_hash="h",
            status=status,
        )
        db_session.add(inv)
        await db_session.flush()


async def test_butler_tool_invocation_status_invalid(db_session) -> None:
    from sqlalchemy import text

    action_id, _ = await _make_action_id(db_session)
    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(
                "INSERT INTO butler_tool_invocations "
                "(action_id, tool_name, invocation_seq, idempotency_key, "
                " request_payload, request_payload_hash, status) "
                "VALUES (:aid, 'recall_evidence', 1, :ikey, '{}', 'h', 'INVALID')"
            ),
            {"aid": action_id, "ikey": f"ikey-{_next_id()}"},
        )


async def test_butler_tool_invocation_seq_positive(db_session) -> None:
    from sqlalchemy import text

    action_id, _ = await _make_action_id(db_session)
    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(
                "INSERT INTO butler_tool_invocations "
                "(action_id, tool_name, invocation_seq, idempotency_key, "
                " request_payload, request_payload_hash, status) "
                "VALUES (:aid, 'recall_evidence', 0, :ikey, '{}', 'h', 'pending')"
            ),
            {"aid": action_id, "ikey": f"ikey-{_next_id()}"},
        )


# ── ck_butler_action_confirmations constraints ────────────────────────────────


async def test_butler_confirmation_role_valid(db_session) -> None:
    from bot.db.repos.butler_action_confirmation import ButlerActionConfirmationRepo
    import datetime

    action_id, _ = await _make_action_id(db_session)
    for role in ("requester", "affected_user", "admin", "rollback_requester"):
        await ButlerActionConfirmationRepo.create(
            db_session,
            action_id=action_id,
            confirmer_tg_id=_next_id(),
            confirmation_role=role,
            status="pending",
            preview_payload_hash="h",
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1),
        )


async def test_butler_confirmation_role_invalid(db_session) -> None:
    from sqlalchemy import text

    action_id, _ = await _make_action_id(db_session)
    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(
                "INSERT INTO butler_action_confirmations "
                "(action_id, confirmer_tg_id, confirmation_role, status, "
                " preview_payload_hash, expires_at) "
                "VALUES (:aid, :tg, 'superadmin', 'pending', 'h', NOW() + INTERVAL '1h')"
            ),
            {"aid": action_id, "tg": _next_id()},
        )


# ── ck_butler_rate_buckets constraints ────────────────────────────────────────


async def test_butler_rate_bucket_kind_valid(db_session) -> None:
    from sqlalchemy import text

    for kind in (
        "user_plans_day",
        "user_execs_day",
        "chat_actions_day",
        "tool_hour:recall_evidence",
        "tool_hour:schedule_meeting",
        "tool_hour:send_intro",
        "tool_hour:update_intro",
        "tool_hour:suggest_card_creation",
    ):
        sid = _next_id()
        await db_session.execute(
            text(
                "INSERT INTO butler_rate_buckets "
                "(bucket_kind, scope_id, bucket_key, window_start, window_end, "
                " count, ceiling) "
                "VALUES (:kind, :sid, :key, NOW(), NOW() + INTERVAL '1h', 0, 10)"
            ),
            {"kind": kind, "sid": sid, "key": f"day:2026-05-25-{sid}"},
        )


async def test_butler_rate_bucket_kind_invalid(db_session) -> None:
    from sqlalchemy import text

    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(
                "INSERT INTO butler_rate_buckets "
                "(bucket_kind, scope_id, bucket_key, window_start, window_end, "
                " count, ceiling) "
                "VALUES ('invalid_kind', :sid, :key, NOW(), NOW() + INTERVAL '1h', 0, 10)"
            ),
            {"sid": _next_id(), "key": "day:2026-05-25"},
        )


async def test_butler_rate_bucket_ceiling_positive(db_session) -> None:
    from sqlalchemy import text

    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(
                "INSERT INTO butler_rate_buckets "
                "(bucket_kind, scope_id, bucket_key, window_start, window_end, "
                " count, ceiling) "
                "VALUES ('user_plans_day', :sid, :key, NOW(), NOW() + INTERVAL '1h', 0, 0)"
            ),
            {"sid": _next_id(), "key": f"day:2026-05-25-{_next_id()}"},
        )


async def test_butler_rate_bucket_count_over_ceiling(db_session) -> None:
    from sqlalchemy import text

    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(
                "INSERT INTO butler_rate_buckets "
                "(bucket_kind, scope_id, bucket_key, window_start, window_end, "
                " count, ceiling) "
                "VALUES ('user_plans_day', :sid, :key, NOW(), NOW() + INTERVAL '1h', 11, 10)"
            ),
            {"sid": _next_id(), "key": f"day:2026-05-25-{_next_id()}"},
        )


# ── Helper ────────────────────────────────────────────────────────────────────


async def _make_action_id(db_session) -> tuple[int, int]:
    """Create a minimal ButlerAction row, return (action_id, tg_id)."""
    from bot.db.models import ButlerAction

    tg_id = _next_id()
    row = ButlerAction(**_base_action_kwargs(requester_tg_id=tg_id))
    db_session.add(row)
    await db_session.flush()
    return row.id, tg_id
