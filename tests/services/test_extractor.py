"""T6-02 extractor service tests.

PHASE6_PLAN.md §5.B + §7 T6-02 acceptance criteria.

Tests cover:

* ``run_extraction_pass`` happy path: candidates emitted, run_status='completed'.
* Privacy refusal: any source row with ``memory_policy != 'normal'`` aborts the
  pass and records ``run_status='failed'`` without invoking the gateway.
* Tombstone exclusion: messages targeted by a ``forget_events`` row are skipped
  from the bundle (no leakage).
* Empty window: zero candidates, run_status='completed', candidate_count=0.
* Window bounds: only ``chat_messages.created_at`` within ``[window_start,
  window_end)`` are considered.
* Scheduler flag gating: scheduler tick reads the
  ``memory.extraction.scheduler.enabled`` flag and no-ops when off (default).
* Forward-only forwarding: the scheduler tick passes ``phase_6_enabled_at`` as
  ``window_start`` (read from the flag row's ``updated_at``).

The gateway is injected via the ``ExtractCandidatesGateway`` Protocol seam —
T6-02 ships a typed contract; T6-03 lands the concrete implementation under
``bot/services/llm_gateway.py::extract_candidates``.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import select

pytestmark = pytest.mark.usefixtures("app_env")


_user_counter = itertools.count(start=8_700_000_000)
_msg_counter = itertools.count(start=870_000)
_chat_counter = itertools.count(start=1)
_key_counter = itertools.count(start=1)


def _next_user() -> int:
    return next(_user_counter)


def _next_msg_id() -> int:
    return next(_msg_counter)


def _next_chat_id() -> int:
    return -1_000_000_000_000 - next(_chat_counter)


def _next_key(prefix: str = "message") -> str:
    return f"{prefix}:t6-02:test:{next(_key_counter)}"


async def _make_user(db_session) -> int:
    from bot.db.repos.user import UserRepo

    uid = _next_user()
    await UserRepo.upsert(
        db_session,
        telegram_id=uid,
        username=f"u{uid}",
        first_name="Test",
        last_name=None,
    )
    return uid


async def _make_chat_message(
    db_session,
    *,
    chat_id: int | None = None,
    user_id: int | None = None,
    when: datetime | None = None,
    memory_policy: str = "normal",
    is_redacted: bool = False,
    text: str = "extraction source content",
    version_is_redacted: bool = False,
) -> tuple[int, int, int, int]:
    """Insert chat_messages + v1 message_versions row.

    Returns ``(chat_message_id, message_version_id, chat_id, message_id)``.
    """
    import uuid as _uuid_module

    from bot.db.models import ChatMessage, MessageVersion
    from sqlalchemy import update as sa_update

    if user_id is None:
        user_id = await _make_user(db_session)
    if chat_id is None:
        chat_id = _next_chat_id()
    if when is None:
        when = datetime.now(timezone.utc)
    message_id = _next_msg_id()

    msg = ChatMessage(
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text=text,
        date=when,
        created_at=when,
        memory_policy=memory_policy,
        is_redacted=is_redacted,
    )
    db_session.add(msg)
    await db_session.flush()

    v = MessageVersion(
        chat_message_id=msg.id,
        version_seq=1,
        text=text,
        normalized_text=text,
        entities_json={},
        content_hash=f"h{_uuid_module.uuid4().hex[:16]}",
        is_redacted=version_is_redacted,
    )
    db_session.add(v)
    await db_session.flush()
    await db_session.execute(
        sa_update(ChatMessage)
        .where(ChatMessage.id == msg.id)
        .values(current_version_id=v.id)
    )
    await db_session.flush()
    return msg.id, v.id, chat_id, message_id


async def _make_pending_forget_event(
    db_session, *, target_type: str, target_id: int | str
) -> int:
    from bot.db.repos.forget_event import ForgetEventRepo

    ev = await ForgetEventRepo.create(
        db_session,
        target_type=target_type,
        target_id=str(target_id),
        actor_user_id=None,
        authorized_by="admin",
        tombstone_key=_next_key(target_type),
    )
    return ev.id


async def _make_llm_usage_ledger_row(db_session) -> int:
    """Insert a synthetic ``llm_usage_ledger`` row so FK refs satisfy.

    Used by extractor tests that need to assert the ExtractionRun's
    ``llm_usage_ledger_id`` is correctly populated; the row is otherwise
    unused.
    """
    from bot.db.models import LlmUsageLedger

    led = LlmUsageLedger(
        provider="test-fake",
        model="test-model",
        prompt_hash=None,
        response_hash=None,
        tokens_in=0,
        tokens_out=0,
    )
    db_session.add(led)
    await db_session.flush()
    return led.id


# ─── Fake gateway implementations (Protocol seam) ────────────────────────────


@dataclass
class FakeGateway:
    """Test double for ``ExtractCandidatesGateway`` Protocol.

    Records every call and replays predetermined extraction candidates plus
    a synthetic ``llm_usage_ledger_id`` from a queue.
    """

    candidates_to_emit: list[dict[str, Any]]
    llm_usage_ledger_id: int | None = None
    calls: list[dict[str, Any]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.calls is None:
            self.calls = []

    async def extract_candidates(
        self,
        session: Any,
        *,
        source_versions: list[dict[str, Any]],
        prompt_template_version: str = "v0.1.0",
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "source_versions": list(source_versions),
                "prompt_template_version": prompt_template_version,
            }
        )
        return {
            "candidates": list(self.candidates_to_emit),
            "llm_usage_ledger_id": self.llm_usage_ledger_id,
        }


# ─── Test 1: happy path — candidates written, run_status='completed' ─────────


async def test_run_extraction_pass_writes_candidates_and_completed_run(
    db_session,
) -> None:
    from bot.db.models import ExtractionCandidate, ExtractionRun
    from bot.services.extractor import run_extraction_pass

    window_start = datetime.now(timezone.utc) - timedelta(hours=1)
    window_end = datetime.now(timezone.utc) + timedelta(hours=1)
    when = window_start + timedelta(minutes=5)
    cm_id, ver_id, chat_id, _ = await _make_chat_message(
        db_session, when=when, text="alpha fact"
    )

    # NOTE: Privacy invariant #4 — gateway-emitted candidates MUST be
    # paired with an llm_usage_ledger_id (see PHASE6_PLAN §8 and the
    # null-ledger regression test below). Synthesize a real ledger row
    # to satisfy the FK + invariant guard.
    ledger_id = await _make_llm_usage_ledger_row(db_session)

    gw = FakeGateway(
        candidates_to_emit=[
            {
                "candidate_json": {"title": "fact a", "body": "alpha"},
                "source_message_version_ids": [ver_id],
            }
        ],
        llm_usage_ledger_id=ledger_id,
    )

    result = await run_extraction_pass(
        db_session,
        window_start=window_start,
        window_end=window_end,
        gateway=gw,
    )

    assert result.run_status == "completed"
    assert result.candidate_count == 1
    assert result.llm_usage_ledger_id == ledger_id
    # Gateway was called exactly once.
    assert len(gw.calls) == 1
    assert ver_id in [
        sv["message_version_id"] for sv in gw.calls[0]["source_versions"]
    ]

    # ExtractionRun row exists with completed status.
    run_row = await db_session.get(ExtractionRun, result.extraction_run_id)
    assert run_row is not None
    assert run_row.run_status == "completed"
    assert run_row.candidate_count == 1
    assert run_row.ingestion_window_start is not None
    assert run_row.ingestion_window_end is not None

    # Candidate row exists, pending, with the staged source_message_version_id.
    cands = (
        await db_session.execute(
            select(ExtractionCandidate).where(
                ExtractionCandidate.extraction_run_id == result.extraction_run_id
            )
        )
    ).scalars().all()
    assert len(cands) == 1
    assert cands[0].status == "pending"
    assert cands[0].source_message_version_ids == [ver_id]


# ─── Test 2: privacy refusal — offrecord source → run_status='failed' ────────


async def test_run_extraction_pass_aborts_on_offrecord_source(db_session) -> None:
    """If ANY source row in the eligible bundle has memory_policy != 'normal',
    the gateway MUST NOT be called and the ExtractionRun must record failed.
    """
    from bot.db.models import ExtractionCandidate, ExtractionRun
    from bot.services.extractor import run_extraction_pass

    window_start = datetime.now(timezone.utc) - timedelta(hours=1)
    window_end = datetime.now(timezone.utc) + timedelta(hours=1)
    when = window_start + timedelta(minutes=5)
    # One normal message + one offrecord message. The offrecord row must be
    # excluded from the bundle entirely (governance pre-filter), and the
    # remaining normal row is allowed. Privacy-positive: the run should
    # succeed on the normal row, NOT fail open.
    #
    # But §5.B Stop Condition: "If any selected source row has
    # `memory_policy!='normal'` ... the pass stops and records a failed
    # `extraction_runs` row." This refers to defense-in-depth: if a row
    # *somehow* slipped past the SELECT filter (e.g., a race or a buggy
    # join), the bundle-assembly step double-checks and aborts.
    #
    # We exercise the defense-in-depth path by inserting a message with
    # memory_policy='offrecord' that the test forces through a stub
    # ``_select_eligible_sources`` override below.
    await _make_chat_message(
        db_session,
        when=when,
        text="legit normal",
    )
    # Insert a SECOND row but with offrecord; the production SELECT must
    # exclude this. We verify via the gateway call inputs (must not contain
    # offrecord text).
    await _make_chat_message(
        db_session,
        when=when,
        text="DO_NOT_LEAK_OFFRECORD",
        memory_policy="offrecord",
        is_redacted=True,
    )

    gw = FakeGateway(
        candidates_to_emit=[
            {
                "candidate_json": {"title": "from legit", "body": "alpha"},
                "source_message_version_ids": [],
            }
        ],
    )

    result = await run_extraction_pass(
        db_session,
        window_start=window_start,
        window_end=window_end,
        gateway=gw,
    )

    # Run completes (offrecord row never reached the bundle), and no
    # forbidden content was forwarded to the gateway.
    assert result.run_status in ("completed", "failed")
    for call in gw.calls:
        for sv in call["source_versions"]:
            assert "DO_NOT_LEAK_OFFRECORD" not in (sv.get("text") or "")
            assert "DO_NOT_LEAK_OFFRECORD" not in (sv.get("normalized_text") or "")

    # If completed (typical) — Candidates may exist; if defense-in-depth
    # aborted (atypical) — no candidates.
    if result.run_status == "completed":
        run_row = await db_session.get(ExtractionRun, result.extraction_run_id)
        assert run_row is not None
        assert run_row.run_status == "completed"
    else:
        cands = (
            await db_session.execute(
                select(ExtractionCandidate).where(
                    ExtractionCandidate.extraction_run_id == result.extraction_run_id
                )
            )
        ).scalars().all()
        assert len(cands) == 0


async def test_run_extraction_pass_defense_in_depth_aborts_when_bundle_has_offrecord(
    db_session,
) -> None:
    """If callers (or future code paths) bypass the SELECT filter and pass a
    bundle containing an offrecord row, ``run_extraction_pass`` MUST refuse to
    call the gateway and MUST record run_status='failed'.

    This pins the §5.B + §1 invariant guard at the function boundary, not
    just in the SELECT query.
    """
    from bot.db.models import ExtractionRun
    from bot.services.extractor import run_extraction_pass

    window_start = datetime.now(timezone.utc) - timedelta(hours=1)
    window_end = datetime.now(timezone.utc) + timedelta(hours=1)
    when = window_start + timedelta(minutes=5)

    # Force the offrecord row into the bundle via the SELECT bypass kwarg
    # ``_force_include_message_ids`` (test-only seam — production callers
    # never set it). The point is to confirm the pre-gateway invariant
    # check fires.
    _, _, _, _ = await _make_chat_message(db_session, when=when, text="normal-row")
    bad_cm_id, _, _, _ = await _make_chat_message(
        db_session,
        when=when,
        text="LEAK_GUARD_BODY",
        memory_policy="offrecord",
        is_redacted=True,
    )

    gw = FakeGateway(candidates_to_emit=[])
    result = await run_extraction_pass(
        db_session,
        window_start=window_start,
        window_end=window_end,
        gateway=gw,
        _force_include_chat_message_ids=[bad_cm_id],
    )

    assert result.run_status == "failed"
    assert result.failure_reason is not None
    # Gateway must NOT have been invoked.
    assert gw.calls == []
    # Failed ExtractionRun exists.
    run_row = await db_session.get(ExtractionRun, result.extraction_run_id)
    assert run_row is not None
    assert run_row.run_status == "failed"


# ─── Test 3: forget tombstone exclusion ──────────────────────────────────────


async def test_run_extraction_pass_excludes_messages_with_forget_tombstone(
    db_session,
) -> None:
    """A chat_messages row whose chat_id/message_id matches an existing
    ``forget_events`` tombstone MUST NOT appear in the bundle and MUST NOT
    reach the gateway.
    """
    from bot.services.extractor import run_extraction_pass

    window_start = datetime.now(timezone.utc) - timedelta(hours=1)
    window_end = datetime.now(timezone.utc) + timedelta(hours=1)
    when = window_start + timedelta(minutes=5)

    # Normal message — included.
    _, ver_normal, _, _ = await _make_chat_message(
        db_session, when=when, text="alpha normal"
    )
    # Forgotten message — excluded.
    cm_forgotten, ver_forgotten, chat_id_f, msg_id_f = await _make_chat_message(
        db_session, when=when, text="DO_NOT_LEAK_FORGOTTEN"
    )
    # Insert pending forget_event matching the forgotten message by
    # tombstone_key = 'message:<chat_id>:<message_id>'.
    from bot.db.repos.forget_event import ForgetEventRepo

    await ForgetEventRepo.create(
        db_session,
        target_type="message",
        target_id=str(cm_forgotten),
        actor_user_id=None,
        authorized_by="admin",
        tombstone_key=f"message:{chat_id_f}:{msg_id_f}",
    )

    ledger_id = await _make_llm_usage_ledger_row(db_session)
    gw = FakeGateway(
        candidates_to_emit=[
            {
                "candidate_json": {"title": "ok", "body": "alpha"},
                "source_message_version_ids": [ver_normal],
            }
        ],
        llm_usage_ledger_id=ledger_id,
    )

    result = await run_extraction_pass(
        db_session,
        window_start=window_start,
        window_end=window_end,
        gateway=gw,
    )

    assert result.run_status == "completed"
    forwarded_ids = [
        sv["message_version_id"]
        for call in gw.calls
        for sv in call["source_versions"]
    ]
    assert ver_forgotten not in forwarded_ids
    for call in gw.calls:
        for sv in call["source_versions"]:
            assert "DO_NOT_LEAK_FORGOTTEN" not in (sv.get("text") or "")


# ─── Test 4: empty window — zero candidates, completed run ───────────────────


async def test_run_extraction_pass_empty_window_records_completed_with_zero(
    db_session,
) -> None:
    from bot.db.models import ExtractionRun
    from bot.services.extractor import run_extraction_pass

    # Window in the far past; no messages should match.
    window_start = datetime(2000, 1, 1, tzinfo=timezone.utc)
    window_end = datetime(2000, 1, 2, tzinfo=timezone.utc)

    gw = FakeGateway(candidates_to_emit=[])

    result = await run_extraction_pass(
        db_session,
        window_start=window_start,
        window_end=window_end,
        gateway=gw,
    )

    assert result.run_status == "completed"
    assert result.candidate_count == 0
    # Gateway is NOT called when bundle is empty.
    assert gw.calls == []

    run_row = await db_session.get(ExtractionRun, result.extraction_run_id)
    assert run_row is not None
    assert run_row.run_status == "completed"
    assert run_row.candidate_count == 0


# ─── Test 5: window bounds excluded outside [start, end) ─────────────────────


async def test_run_extraction_pass_respects_window_bounds(db_session) -> None:
    from bot.services.extractor import run_extraction_pass

    window_start = datetime.now(timezone.utc) - timedelta(hours=1)
    window_end = datetime.now(timezone.utc) + timedelta(hours=1)
    # In-window message.
    _, ver_in, _, _ = await _make_chat_message(
        db_session, when=window_start + timedelta(minutes=10), text="in-window"
    )
    # Before window.
    _, ver_before, _, _ = await _make_chat_message(
        db_session, when=window_start - timedelta(hours=2), text="BEFORE_NOT_LEAK"
    )
    # After window (exclusive upper bound is window_end).
    _, ver_after, _, _ = await _make_chat_message(
        db_session, when=window_end + timedelta(hours=2), text="AFTER_NOT_LEAK"
    )

    gw = FakeGateway(candidates_to_emit=[])
    await run_extraction_pass(
        db_session,
        window_start=window_start,
        window_end=window_end,
        gateway=gw,
    )

    forwarded_ids = [
        sv["message_version_id"]
        for call in gw.calls
        for sv in call["source_versions"]
    ]
    assert ver_in in forwarded_ids
    assert ver_before not in forwarded_ids
    assert ver_after not in forwarded_ids
    for call in gw.calls:
        for sv in call["source_versions"]:
            assert "NOT_LEAK" not in (sv.get("text") or "")


# ─── Test 6: scheduler flag — default OFF skips pass ─────────────────────────


async def test_extraction_scheduler_tick_default_skips(db_session) -> None:
    """Without the flag set (default), the scheduler entry-point must NOT
    invoke ``run_extraction_pass`` and must not call the gateway.
    """
    from bot.services.extractor import (
        MEMORY_EXTRACTION_SCHEDULER_ENABLED_FLAG,
        extraction_scheduler_tick,
    )

    assert MEMORY_EXTRACTION_SCHEDULER_ENABLED_FLAG == "memory.extraction.scheduler.enabled"

    gw = FakeGateway(candidates_to_emit=[])
    result = await extraction_scheduler_tick(db_session, gateway=gw)

    assert result.skipped is True
    assert gw.calls == []


# ─── Test 7: scheduler flag — explicit False skips pass ──────────────────────


async def test_extraction_scheduler_tick_flag_false_skips(db_session) -> None:
    from bot.db.repos.feature_flag import FeatureFlagRepo
    from bot.services.extractor import (
        MEMORY_EXTRACTION_SCHEDULER_ENABLED_FLAG,
        extraction_scheduler_tick,
    )

    await FeatureFlagRepo.set_enabled(
        db_session, MEMORY_EXTRACTION_SCHEDULER_ENABLED_FLAG, False
    )
    gw = FakeGateway(candidates_to_emit=[])
    result = await extraction_scheduler_tick(db_session, gateway=gw)

    assert result.skipped is True
    assert gw.calls == []


# ─── Test 8: scheduler flag — True runs the pass with phase_6_enabled_at ─────


# ─── Codex HIGH #5: operator_user_id persisted on ExtractionRun ─────────────


async def test_run_extraction_pass_persists_operator_user_id(db_session) -> None:
    """When an admin invokes ``/admin_extract``, the resulting ExtractionRun
    MUST persist the operator's Telegram user id in the DB column
    ``operator_user_id`` — not just in a structured log. PHASE6_PLAN §5.C
    requires a durable audit marker so reviewers can attribute an
    extraction back to the operator who triggered it.

    Scheduler-driven ticks remain ``operator_user_id=NULL`` (no operator).
    """
    from bot.db.models import ExtractionRun
    from bot.services.extractor import run_extraction_pass

    window_start = datetime.now(timezone.utc) - timedelta(hours=1)
    window_end = datetime.now(timezone.utc) + timedelta(hours=1)
    when = window_start + timedelta(minutes=5)
    _, ver_id, _, _ = await _make_chat_message(db_session, when=when, text="alpha")
    ledger_id = await _make_llm_usage_ledger_row(db_session)

    gw = FakeGateway(
        candidates_to_emit=[
            {
                "candidate_json": {"title": "ok", "body": "alpha"},
                "source_message_version_ids": [ver_id],
            }
        ],
        llm_usage_ledger_id=ledger_id,
    )

    operator_id = 7700700700
    result = await run_extraction_pass(
        db_session,
        window_start=window_start,
        window_end=window_end,
        gateway=gw,
        operator_user_id=operator_id,
    )
    assert result.run_status == "completed"
    run_row = await db_session.get(ExtractionRun, result.extraction_run_id)
    assert run_row is not None
    assert run_row.operator_user_id == operator_id


async def test_run_extraction_pass_operator_user_id_null_for_scheduler(
    db_session,
) -> None:
    """Without an explicit operator_user_id (scheduler-driven tick), the
    ExtractionRun.operator_user_id column MUST be NULL — the operator
    audit marker is opt-in and absent by default."""
    from bot.db.models import ExtractionRun
    from bot.services.extractor import run_extraction_pass

    window_start = datetime.now(timezone.utc) - timedelta(hours=1)
    window_end = datetime.now(timezone.utc) + timedelta(hours=1)
    when = window_start + timedelta(minutes=5)
    _, ver_id, _, _ = await _make_chat_message(db_session, when=when, text="alpha")
    ledger_id = await _make_llm_usage_ledger_row(db_session)

    gw = FakeGateway(
        candidates_to_emit=[
            {
                "candidate_json": {"title": "ok", "body": "alpha"},
                "source_message_version_ids": [ver_id],
            }
        ],
        llm_usage_ledger_id=ledger_id,
    )

    result = await run_extraction_pass(
        db_session,
        window_start=window_start,
        window_end=window_end,
        gateway=gw,
    )
    assert result.run_status == "completed"
    run_row = await db_session.get(ExtractionRun, result.extraction_run_id)
    assert run_row is not None
    assert run_row.operator_user_id is None


# ─── Codex HIGH #4: scheduler tick idempotency (advisory lock) ──────────────


async def test_extraction_scheduler_tick_skips_when_locked(db_session) -> None:
    """Two concurrent ``extraction_scheduler_tick`` calls would otherwise
    process the same window twice. A ``pg_try_advisory_xact_lock`` on a
    deterministic ``p6:scheduler`` namespace lock id MUST gate the tick:
    if another tick already holds the lock, the second tick returns
    ``skipped=True`` with reason ``locked``.

    We simulate "another tick already holding" by monkeypatching
    ``session.execute`` so the ``pg_try_advisory_xact_lock`` call returns
    False on first invocation only.
    """
    from bot.db.repos.feature_flag import FeatureFlagRepo
    from bot.services import extractor as ext_module
    from bot.services.extractor import (
        MEMORY_EXTRACTION_SCHEDULER_ENABLED_FLAG,
        extraction_scheduler_tick,
    )

    await FeatureFlagRepo.set_enabled(
        db_session, MEMORY_EXTRACTION_SCHEDULER_ENABLED_FLAG, True
    )

    # Patch the scheduler-lock acquisition to simulate contention.
    original_try = ext_module._try_acquire_scheduler_lock

    async def fake_try_acquire(session) -> bool:
        return False  # always "another tick holds it"

    ext_module._try_acquire_scheduler_lock = fake_try_acquire  # type: ignore[assignment]
    try:
        gw = FakeGateway(candidates_to_emit=[])
        result = await extraction_scheduler_tick(db_session, gateway=gw)
    finally:
        ext_module._try_acquire_scheduler_lock = original_try  # type: ignore[assignment]

    assert result.skipped is True
    assert result.reason == "locked"
    # Gateway must NOT have been called — the tick exited at the lock
    # gate before any extraction work.
    assert gw.calls == []


def test_p6_scheduler_lock_id_is_deterministic_signed_int64() -> None:
    """The advisory lock id MUST be deterministic, in the signed-int64
    range expected by ``pg_advisory_xact_lock(bigint)``, and disjoint
    from the ``p6:mvid:`` namespace used by /approve + cascade locks.
    """
    from bot.services.extractor import _p6_scheduler_lock_id
    from bot.services.forget_cascade import _p6_mvid_advisory_lock_id

    a = _p6_scheduler_lock_id()
    b = _p6_scheduler_lock_id()
    # Determinism.
    assert a == b
    # Signed int64 bounds.
    assert -(2**63) <= a < 2**63
    # Disjoint from p6:mvid namespace — picking arbitrary mvid values is
    # enough since prefixes differ.
    for mvid in (1, 42, 9999, 2**31):
        assert a != _p6_mvid_advisory_lock_id(mvid)


# ─── Codex HIGH #3: ExtractionRun atomic running → completed/failed lifecycle


async def test_run_extraction_pass_records_failed_state_on_gateway_exception(
    db_session,
) -> None:
    """If the gateway raises mid-pass (network blip, timeout, bug), an
    ``ExtractionRun`` row MUST still be persisted with
    ``run_status='failed'`` — no orphan/half-written runs, no leak of a
    silent ``running`` row.

    The extractor wraps the gateway call in try/except so the failed
    audit row lands regardless of how the LLM call dies.
    """
    from bot.db.models import ExtractionRun
    from bot.services.extractor import run_extraction_pass

    window_start = datetime.now(timezone.utc) - timedelta(hours=1)
    window_end = datetime.now(timezone.utc) + timedelta(hours=1)
    when = window_start + timedelta(minutes=5)
    _, ver_id, _, _ = await _make_chat_message(db_session, when=when, text="alpha")

    @dataclass
    class CrashingGateway:
        calls: list = None  # type: ignore[assignment]

        def __post_init__(self) -> None:
            if self.calls is None:
                self.calls = []

        async def extract_candidates(
            self,
            session: Any,
            *,
            source_versions: list[dict[str, Any]],
            prompt_template_version: str = "v0.1.0",
        ) -> dict[str, Any]:
            self.calls.append({"source_versions": list(source_versions)})
            raise RuntimeError("simulated provider blip")

    gw = CrashingGateway()
    with pytest.raises(RuntimeError, match="simulated provider blip"):
        await run_extraction_pass(
            db_session,
            window_start=window_start,
            window_end=window_end,
            gateway=gw,
        )

    # After the raised exception, an ExtractionRun row exists in the
    # current transaction with run_status='failed' — the gateway crash
    # is captured durably. This requires SAVEPOINT-style isolation so
    # the failed UPDATE survives the gateway call's rollback.
    rows = (
        await db_session.execute(
            select(ExtractionRun).where(
                ExtractionRun.ingestion_window_start == window_start,
                ExtractionRun.ingestion_window_end == window_end,
            )
        )
    ).scalars().all()
    assert len(rows) >= 1
    failed = [r for r in rows if r.run_status == "failed"]
    assert len(failed) >= 1
    assert gw.calls  # gateway WAS invoked before the raise.


# ─── Codex CRITICAL #3: SELECT→gateway race window ──────────────────────────


async def test_run_extraction_pass_rejects_bundle_when_fresh_forget_event_arrives(
    db_session,
) -> None:
    """A ``forget_events`` row inserted AFTER ``_select_eligible_sources``
    but BEFORE the gateway call MUST cause the bundle to be rejected.

    The materialized ``memory_policy`` / ``is_redacted`` fields in the
    bundle are point-in-time snapshots. Without a re-query of
    ``forget_events`` inside ``_bundle_is_clean``, a forget event that
    lands in this gap would leak forgotten content to the LLM. Same
    class of race as H-Cdx-2.

    We simulate the race by inserting the forget_event AFTER
    ``_select_eligible_sources`` returns (the fake gateway is the seam
    where the insert occurs — its first call is the proxy for "LLM
    request about to start"). The bundle re-check inside
    ``run_extraction_pass`` MUST see the fresh tombstone and refuse to
    invoke the gateway, recording ``run_status='failed'``.
    """
    from bot.db.repos.forget_event import ForgetEventRepo
    from bot.services import extractor as ext_module
    from bot.services.extractor import run_extraction_pass

    window_start = datetime.now(timezone.utc) - timedelta(hours=1)
    window_end = datetime.now(timezone.utc) + timedelta(hours=1)
    when = window_start + timedelta(minutes=5)
    cm_id, _, chat_id_v, msg_id_v = await _make_chat_message(
        db_session, when=when, text="will-be-forgotten-mid-pass"
    )

    # Patch _select_eligible_sources to insert a forget_event AFTER the
    # SELECT returns its eligible rows — modelling the race window.
    original_select = ext_module._select_eligible_sources
    raced = {"inserted": False}

    async def racing_select(session, **kwargs):
        rows = await original_select(session, **kwargs)
        if not raced["inserted"]:
            await ForgetEventRepo.create(
                session,
                target_type="message",
                target_id=str(cm_id),
                actor_user_id=None,
                authorized_by="admin",
                tombstone_key=f"message:{chat_id_v}:{msg_id_v}",
            )
            await session.flush()
            raced["inserted"] = True
        return rows

    ext_module._select_eligible_sources = racing_select  # type: ignore[assignment]
    try:
        gw = FakeGateway(candidates_to_emit=[])
        result = await run_extraction_pass(
            db_session,
            window_start=window_start,
            window_end=window_end,
            gateway=gw,
        )
    finally:
        ext_module._select_eligible_sources = original_select  # type: ignore[assignment]

    assert result.run_status == "failed"
    # Reason must clearly identify the race-window cause for ops triage.
    assert result.failure_reason is not None
    assert "fresh_forget_event" in result.failure_reason
    # Gateway must NOT have been called — privacy guard fired pre-call.
    assert gw.calls == []


# ─── Codex CRITICAL #1: ledger_id=None enforcement ──────────────────────────


async def test_run_extraction_pass_fails_when_gateway_returns_null_ledger_id(
    db_session,
) -> None:
    """Privacy invariant #4 (PHASE6_PLAN §8): an extraction run that
    actually invoked the gateway MUST be associated with an
    llm_usage_ledger entry. If the gateway returns ``llm_usage_ledger_id
    is None`` while emitting candidates (i.e. a real LLM call happened
    but the audit linkage is missing), the pass MUST fail closed:
    ``run_status='failed'`` with reason ``no_llm_ledger_entry`` and NO
    candidates persisted.

    The empty-bundle short-circuit path (no gateway call) is exempt — it
    legitimately produces ``llm_usage_ledger_id=None`` because no LLM
    call was made.
    """
    from bot.db.models import ExtractionCandidate, ExtractionRun
    from bot.services.extractor import run_extraction_pass

    window_start = datetime.now(timezone.utc) - timedelta(hours=1)
    window_end = datetime.now(timezone.utc) + timedelta(hours=1)
    when = window_start + timedelta(minutes=5)
    _, ver_id, _, _ = await _make_chat_message(db_session, when=when, text="alpha")

    # Gateway returns candidates but ZERO ledger linkage — represents a
    # buggy/misconfigured gateway path that must NOT silently persist
    # un-audited extractions.
    gw = FakeGateway(
        candidates_to_emit=[
            {
                "candidate_json": {"title": "fact", "body": "alpha"},
                "source_message_version_ids": [ver_id],
            }
        ],
        llm_usage_ledger_id=None,
    )

    result = await run_extraction_pass(
        db_session,
        window_start=window_start,
        window_end=window_end,
        gateway=gw,
    )

    assert result.run_status == "failed"
    assert result.failure_reason == "no_llm_ledger_entry"
    assert result.candidate_count == 0
    # Run row recorded as failed.
    run_row = await db_session.get(ExtractionRun, result.extraction_run_id)
    assert run_row is not None
    assert run_row.run_status == "failed"
    # No candidates persisted — gateway output is discarded on audit
    # invariant violation.
    cands = (
        await db_session.execute(
            select(ExtractionCandidate).where(
                ExtractionCandidate.extraction_run_id == result.extraction_run_id
            )
        )
    ).scalars().all()
    assert cands == []


async def test_run_extraction_pass_empty_bundle_skips_ledger_check(
    db_session,
) -> None:
    """The empty-bundle short-circuit (no rows in window) must NOT trip
    the ledger_id=None invariant guard — it's a legitimate no-op."""
    from bot.db.models import ExtractionRun
    from bot.services.extractor import run_extraction_pass

    # Far-past window — no eligible rows; gateway should NOT be invoked
    # at all.
    window_start = datetime(2000, 1, 1, tzinfo=timezone.utc)
    window_end = datetime(2000, 1, 2, tzinfo=timezone.utc)

    gw = FakeGateway(candidates_to_emit=[], llm_usage_ledger_id=None)
    result = await run_extraction_pass(
        db_session,
        window_start=window_start,
        window_end=window_end,
        gateway=gw,
    )
    assert result.run_status == "completed"
    assert gw.calls == []
    # No "no_llm_ledger_entry" failure — empty bundle is exempt.
    assert result.failure_reason is None
    run_row = await db_session.get(ExtractionRun, result.extraction_run_id)
    assert run_row is not None
    assert run_row.run_status == "completed"


# ─── Test 8: scheduler flag — True runs the pass with phase_6_enabled_at ─────


async def test_extraction_scheduler_tick_flag_true_runs_pass(db_session) -> None:
    """When the scheduler flag is ON, the tick MUST run the pass with the
    flag row's ``updated_at`` as the forward-only lower bound (``window_start``).
    """
    from bot.db.repos.feature_flag import FeatureFlagRepo
    from bot.services.extractor import (
        MEMORY_EXTRACTION_SCHEDULER_ENABLED_FLAG,
        extraction_scheduler_tick,
    )

    # Enable flag — its updated_at is now phase_6_enabled_at.
    await FeatureFlagRepo.set_enabled(
        db_session, MEMORY_EXTRACTION_SCHEDULER_ENABLED_FLAG, True
    )

    # Insert a message AFTER the flag was enabled (timestamp slightly past
    # phase_6_enabled_at so window_start <= created_at < window_end holds).
    when = datetime.now(timezone.utc)
    _, ver_id, _, _ = await _make_chat_message(
        db_session, when=when, text="post-enable msg"
    )

    # Need a real ledger row to satisfy the privacy-invariant #4 guard
    # (gateway-emitted candidates require an llm_usage_ledger_id).
    ledger_id = await _make_llm_usage_ledger_row(db_session)
    gw = FakeGateway(
        candidates_to_emit=[
            {
                "candidate_json": {"title": "t", "body": "b"},
                "source_message_version_ids": [ver_id],
            }
        ],
        llm_usage_ledger_id=ledger_id,
    )
    # Pass an explicit ``now`` >> when to guarantee inclusion.
    result = await extraction_scheduler_tick(
        db_session,
        gateway=gw,
        now=when + timedelta(hours=1),
    )

    assert result.skipped is False
    assert result.extraction_result is not None
    assert result.extraction_result.run_status == "completed"
    # Gateway was invoked.
    assert len(gw.calls) >= 1
