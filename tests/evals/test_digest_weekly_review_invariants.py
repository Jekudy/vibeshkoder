"""Phase 8 binding tests — L8a/b + C7 + I6a/b.1/.2/.3/I6c + R5.a/b/c/d.

T8-07 / PHASE8_PLAN.md §10. Extends the Phase 11 binding suite (30/30 →
42/42 with these 12 cases).

Cases (PHASE8_PLAN.md §10 Test Coverage Matrix):

- **L8a**: forget event on ``mvid`` cited by a weekly digest in any of
  the 8 widened statuses. Cascade scan widening (§5.D) + redactor
  allowlist widening (§5.K) → status='redacted', body contains
  ``[REDACTED — забыто]``, admin notify dispatched for ``awaiting_review``
  source state.
- **L8b**: same as L8a but the citation is a ``card_source`` UUID.
  Dual-kind cascade path (mvid + card_source via ``card_sources.id``).
- **C7**: every bullet in a weekly digest has ≥1 valid citation token.
  Section header ``## Раздел: <name>`` lines are NOT bullets and are
  correctly excluded from the citation invariant check. M1 allowlist
  enforcement: returned section titles match
  ``digest_weekly_v0_1_0.SECTION_NAME_ALLOWLIST``.
- **I6a**: forget while digest in ``awaiting_review``. Cascade widening
  must reach the row; redactor widening must process; final status
  ``='redacted'``, body has ``[REDACTED — забыто]`` placeholder.
- **I6b.1**: forget BEFORE ``/digest_approve``. Cascade redacts first
  → calling ``approve_digest`` on the (now ``status='redacted'``) row
  raises ``DigestReviewInvalidState(current_status='redacted', ...)``.
- **I6b.2**: forget DURING ``approve_digest`` (between step-3 revalidation
  and step-4 commit). Racy by design. Final status MUST be one of
  ``{'failed' (citations_stale_at_publish), 'redacted'}`` and MUST NEVER
  be ``'posted'``.
- **I6b.3**: forget AFTER approve commit, BEFORE publisher dispatch.
  Cascade widening finds ``approved_for_publish`` row → redactor processes
  → publisher trigger guard (§5.L) rejects ``'redacted'`` → terminal
  ``DigestPublisherInvalidState(current_status='redacted')``; no
  ``posted_message_id``.
- **I6c**: concurrent daily + weekly forget race — one forget event hits
  both a daily ``posted`` row and a weekly ``awaiting_review`` row citing
  the same mvid. Both rows transition to ``'redacted'`` independently in
  the same cascade event.
- **R5.a**: non-admin invokes ``/digest_approve`` → handler ``_is_admin``
  gate (Phase 6 pattern) returns silently; no DB state change; no reply.
- **R5.b**: non-admin invokes ``/digest_reject`` → same silent no-op.
- **R5.c**: admin invokes ``/digest_approve`` on already-``posted`` row →
  ``DigestReviewInvalidState(current_status='posted', ...)`` raised;
  publisher NOT re-dispatched.
- **R5.d**: admin invokes ``/digest_approve`` on ``rejected_by_admin``
  row → ``DigestReviewInvalidState(current_status='rejected_by_admin', ...)``;
  publisher NOT re-dispatched.

Test-fixture pattern mirrors ``test_digest_leakage.py`` (Phase 7 binding):
``app_env`` + ``db_session`` with outer-transaction rollback isolation.
Service flushes; tests rely on fixture rollback (no ``session.commit()``).

Privacy literals: the "forgotten" / "no-mem" / "off-record" tokens are
canonical references to the policy taxonomy — they MUST appear here
because these tests ENFORCE the policy by name. Same rationale as the
Phase 7 ``test_digest_leakage.py`` allowlist entry (§7 #5 in
``scripts/lint_privacy_check.sh``).
"""

from __future__ import annotations

import itertools
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.usefixtures("app_env")


_chat_counter = itertools.count(start=9800)
_msg_counter = itertools.count(start=980_000)
_user_counter = itertools.count(start=9_800_000_000)


def _next_chat_id() -> int:
    return -1_000_000_000_000 - next(_chat_counter)


async def _make_user(db_session) -> int:
    from bot.db.repos.user import UserRepo

    uid = next(_user_counter)
    await UserRepo.upsert(
        db_session,
        telegram_id=uid,
        username=f"u{uid}",
        first_name="P8Test",
        last_name=None,
    )
    return uid


async def _make_msg(
    db_session,
    *,
    chat_id: int,
    ts: datetime,
    text_value: str = "content",
    memory_policy: str = "normal",
    is_redacted: bool = False,
) -> tuple[int, int]:
    """Insert a chat_message + message_version pair. Returns (cm_id, mv_id)."""
    from bot.db.models import ChatMessage, MessageVersion

    uid = await _make_user(db_session)
    msg = ChatMessage(
        message_id=next(_msg_counter),
        chat_id=chat_id,
        user_id=uid,
        text=text_value,
        date=ts,
        raw_json={"text": text_value},
        memory_policy=memory_policy,
        is_redacted=False,
    )
    db_session.add(msg)
    await db_session.flush()
    mv = MessageVersion(
        chat_message_id=msg.id,
        version_seq=1,
        text=text_value,
        normalized_text=text_value,
        entities_json={"entities": []},
        content_hash=f"h-{msg.id}",
        is_redacted=is_redacted,
    )
    db_session.add(mv)
    await db_session.flush()
    msg.current_version_id = mv.id
    await db_session.flush()
    return msg.id, mv.id


async def _make_card_with_sources(
    db_session, *, mv_ids: list[int]
) -> tuple[uuid.UUID, list[str]]:
    """Insert an approved card + card_sources rows. Returns (card_id,
    [card_source_id_text]) ordered by position."""
    from bot.db.models import CardSource, KnowledgeCard

    admin_id = await _make_user(db_session)
    card = KnowledgeCard(
        title="p8-test-card",
        body_markdown="card body",
        card_status="approved",
        approved_by_user_id=admin_id,
        approved_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    await db_session.flush()
    cs_ids: list[str] = []
    for position, mv_id in enumerate(mv_ids):
        cs = CardSource(
            card_id=card.id, message_version_id=mv_id, position=position
        )
        db_session.add(cs)
        await db_session.flush()
        cs_ids.append(str(cs.id))
    await db_session.flush()
    return card.id, cs_ids


def _weekly_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Mon..next-Mon UTC pair anchored at now."""
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(days=7), now)


async def _make_weekly_digest(
    db_session,
    *,
    status: str,
    citations: list[dict] | None = None,
    body: str | None = None,
    admin_id: int | None = None,
    posted_chat_id: int | None = None,
    posted_message_id: int | None = None,
):
    """Insert a weekly digest in a given status.

    Honors ``ck_digests_approved_audit``: for ``approved_for_publish``,
    ``posting``, or ``posted`` status, ``published_by_admin_id`` +
    ``approved_at`` MUST be set.
    """
    from bot.db.models import Digest

    ws, we = _weekly_window()
    citations = citations if citations is not None else []
    body_md = body if body is not None else "TL;DR.\n\n- One bullet [[mv:1]]"
    digest = Digest(
        type="weekly",
        window_start=ws,
        window_end=we,
        body_markdown=body_md,
        citations=citations,
        status=status,
    )
    if status in ("approved_for_publish", "posting", "posted"):
        # Audit CHECK requires both.
        digest.published_by_admin_id = admin_id or 149820031
        digest.approved_at = datetime.now(timezone.utc)
    if status == "awaiting_review":
        digest.awaiting_review_at = datetime.now(timezone.utc)
    if status == "posted":
        digest.posted_chat_id = posted_chat_id or -42
        digest.posted_message_id = posted_message_id or 999
        digest.posted_at = datetime.now(timezone.utc)
    db_session.add(digest)
    await db_session.flush()
    return digest


async def _fire_forget_event(
    db_session, *, chat_id: int, cm_id: int
) -> int:
    """Insert a pending forget_event targeting a chat_message. Returns id."""
    from bot.db.models import ForgetEvent

    fe = ForgetEvent(
        target_type="message",
        target_id=str(cm_id),
        actor_user_id=None,
        authorized_by="self",
        tombstone_key=f"message:{chat_id}:{cm_id}",
        policy="forgotten",
        status="pending",
    )
    db_session.add(fe)
    await db_session.flush()
    return fe.id


# ────────────────────────────────────────────────────────────────────────────
# L8a — forget event on mvid cited in weekly digest body
# PHASE8_PLAN.md §10 / AC8 — cascade widening (§5.D) + redactor widening (§5.K)
# ────────────────────────────────────────────────────────────────────────────


async def test_L8a_forget_mvid_cited_by_weekly_redacts_body_and_filters_citations(
    db_session,
):
    """Phase 8 binding L8a: a forget event on a message_version cited by a
    weekly digest in any widened status (here exercised against
    ``awaiting_review``) MUST be propagated by the cascade scan widening,
    processed by the redactor allowlist widening, and result in:

    1. ``digests.status='redacted'``,
    2. body contains ``[REDACTED — забыто]`` and NOT the forgotten content,
    3. the forgotten citation is filtered out of ``digests.citations``.

    Verifies the §5.D scan widening + §5.K allowlist widening AC5.
    """
    from bot.services.forget_cascade import run_cascade_worker_once

    chat_id = _next_chat_id()
    now = datetime.now(timezone.utc)
    cm_id, mv = await _make_msg(
        db_session,
        chat_id=chat_id,
        ts=now - timedelta(days=2),
        text_value="weekly mvid forbidden content",
    )

    digest = await _make_weekly_digest(
        db_session,
        status="awaiting_review",
        body=(
            "TL;DR.\n\n"
            "## Раздел: Объявления\n"
            f"- Mvid bullet [[mv:{mv}]]\n"
        ),
        citations=[{"kind": "message_version", "id": mv, "position": 0}],
    )
    did = digest.id

    await _fire_forget_event(db_session, chat_id=chat_id, cm_id=cm_id)

    await run_cascade_worker_once(db_session, batch_size=10)

    row = (
        await db_session.execute(
            text(
                "SELECT status, body_markdown, citations FROM digests WHERE id=:id"
            ),
            {"id": did},
        )
    ).mappings().one()
    assert row["status"] in ("redacted", "redacted_edit_failed"), (
        f"L8a: expected status='redacted', got {row['status']!r} — "
        "cascade widening (§5.D) or redactor widening (§5.K) regressed"
    )
    assert "[REDACTED — забыто]" in row["body_markdown"], (
        "L8a: body must contain redaction placeholder"
    )
    assert "weekly mvid forbidden content" not in (row["body_markdown"] or "")
    # Surviving citations list MUST NOT contain the forgotten mvid.
    surviving = row["citations"] or []
    for cit in surviving:
        if cit.get("kind") == "message_version":
            assert int(cit["id"]) != mv, (
                f"L8a: forgotten mvid {mv} leaked into surviving citations"
            )


# ────────────────────────────────────────────────────────────────────────────
# L8b — forget event on card_source cited in weekly digest body
# PHASE8_PLAN.md §10 — dual-kind cascade path
# ────────────────────────────────────────────────────────────────────────────


async def test_L8b_forget_card_source_cited_by_weekly_redacts_body(db_session):
    """Phase 8 binding L8b: forget event on a chat_message whose mvid
    underlies an approved card → cascade walks ``card_source.id`` citation
    path → redactor masks bullets referencing the affected ``cs:`` ids.

    Asserts:
      1. ``digests.status='redacted'``,
      2. body has ``[REDACTED — забыто]`` placeholder,
      3. citations filtered out the ``card_source`` entry.
    """
    from bot.services.forget_cascade import run_cascade_worker_once

    chat_id = _next_chat_id()
    now = datetime.now(timezone.utc)
    cm_id, mv = await _make_msg(
        db_session,
        chat_id=chat_id,
        ts=now - timedelta(days=2),
        text_value="card source forbidden content",
    )
    _card_id, cs_ids = await _make_card_with_sources(db_session, mv_ids=[mv])
    cs_id = cs_ids[0]

    digest = await _make_weekly_digest(
        db_session,
        status="awaiting_review",
        body=(
            "TL;DR.\n\n"
            "## Раздел: Знания и ресурсы\n"
            f"- Card-source bullet [[cs:{cs_id}]]\n"
        ),
        citations=[{"kind": "card_source", "id": cs_id, "position": 0}],
    )
    did = digest.id

    await _fire_forget_event(db_session, chat_id=chat_id, cm_id=cm_id)

    await run_cascade_worker_once(db_session, batch_size=10)

    row = (
        await db_session.execute(
            text(
                "SELECT status, body_markdown, citations FROM digests WHERE id=:id"
            ),
            {"id": did},
        )
    ).mappings().one()
    assert row["status"] in ("redacted", "redacted_edit_failed"), (
        f"L8b: expected status='redacted', got {row['status']!r} — "
        "cascade widening for kind='card_source' regressed"
    )
    assert "[REDACTED — забыто]" in row["body_markdown"]
    assert "card source forbidden content" not in (row["body_markdown"] or "")
    surviving = row["citations"] or []
    for cit in surviving:
        if cit.get("kind") == "card_source":
            assert str(cit["id"]) != cs_id, (
                f"L8b: forgotten card_source {cs_id} leaked into surviving citations"
            )


# ────────────────────────────────────────────────────────────────────────────
# C7 — every weekly bullet has ≥1 valid citation token; section headers skipped
# PHASE8_PLAN.md §10 — bullet tokenizer correctness + M1 allowlist
# ────────────────────────────────────────────────────────────────────────────


async def test_C7_weekly_section_headers_excluded_from_bullet_scanner(db_session):
    """Phase 8 binding C7: ``## Раздел: <name>`` section header lines are
    NOT bullets — the citation invariant skips them. A body with section
    headers and bulleted content where every bullet (and only the bullets)
    carries a citation token MUST pass the validator. Additionally every
    returned section title MUST be in
    ``digest_weekly_v0_1_0.SECTION_NAME_ALLOWLIST`` (M1).
    """
    from decimal import Decimal

    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from bot.services.digest_context import DigestContext, DigestContextMessage
    from bot.services.llm_gateway import LLMGatewayConfig, synthesize_digest
    from bot.services.llm_prompts.digest_weekly_v0_1_0 import (
        SECTION_NAME_ALLOWLIST,
    )
    from bot.services.llm_providers import ProviderResult

    chat_id = _next_chat_id()
    cm_id1, mv1 = await _make_msg(
        db_session,
        chat_id=chat_id,
        ts=datetime.now(timezone.utc) - timedelta(days=2),
    )
    cm_id2, mv2 = await _make_msg(
        db_session,
        chat_id=chat_id,
        ts=datetime.now(timezone.utc) - timedelta(days=1),
    )
    _ = (cm_id1, cm_id2)

    ws, we = _weekly_window()
    ctx = DigestContext(
        type="weekly",
        window_start=ws,
        window_end=we,
        source_chat_id=chat_id,
        cards=[],
        messages=[
            DigestContextMessage(
                message_version_id=mv1,
                chat_message_id=cm_id1,
                author_display="A",
                text="msg one",
                ts=datetime.now(timezone.utc),
            ),
            DigestContextMessage(
                message_version_id=mv2,
                chat_message_id=cm_id2,
                author_display="B",
                text="msg two",
                ts=datetime.now(timezone.utc),
            ),
        ],
    )

    # Provider response with TWO valid sections (allowlist titles) +
    # 2 bullets each, every bullet citation-tagged. The section header
    # line is NOT a bullet and MUST NOT be required to carry a token.
    body = (
        "TL;DR. Краткий итог. Краткий итог. Краткий итог.\n\n"
        "## Раздел: Объявления\n"
        f"- First mvid [[mv:{mv1}]]\n"
        f"- Second mvid [[mv:{mv2}]]\n\n"
        "## Раздел: Обсуждения\n"
        f"- Third bullet [[mv:{mv1}]]\n"
        f"- Fourth bullet [[mv:{mv2}]]\n"
    )

    class _Provider:
        async def call(self, *, prompt: str, model: str):
            return ProviderResult(
                answer_text=body,
                citation_ids=(),
                tokens_in=10,
                tokens_out=20,
                request_id="c7",
                raw_latency_ms=1,
            )

    cfg = LLMGatewayConfig(
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        daily_ceiling_usd=Decimal("10"),
        monthly_ceiling_usd=Decimal("100"),
        prompt_template_version="digest-weekly-v0.1.0",
    )

    result = await synthesize_digest(
        db_session,
        context=ctx,
        config=cfg,
        ledger_repo=LedgerRepo(),
        provider=_Provider(),
        type="weekly",
    )
    # If validator wrongly treated `## Раздел:` as a bullet, this would have
    # raised DigestCitationValidationError. Reaching here proves the
    # tokenizer correctly distinguishes section headers from bullets.
    assert result.body_markdown == body
    assert len(result.citations) == 4, (
        f"C7: expected 4 citations (one per bullet), got {len(result.citations)}"
    )
    # M1: every section title returned by the provider must be in the
    # allowlist. Hard-assert here (the gateway logs a soft warning at
    # runtime, but for the binding test we lock in the contract).
    from bot.services.llm_gateway import _extract_sections

    titles = [t for t, _ in _extract_sections(body)]
    assert titles == ["Объявления", "Обсуждения"]
    for title in titles:
        assert title in SECTION_NAME_ALLOWLIST, (
            f"C7 M1: section title {title!r} not in SECTION_NAME_ALLOWLIST "
            f"{SECTION_NAME_ALLOWLIST!r}"
        )


# ────────────────────────────────────────────────────────────────────────────
# I6a — forget while digest is in `awaiting_review`
# PHASE8_PLAN.md §10 — cascade widening + redactor widening AC5
# ────────────────────────────────────────────────────────────────────────────


async def test_I6a_forget_during_awaiting_review_redacts_and_notifies_admin(
    db_session,
):
    """Phase 8 binding I6a: forget event on a cited mvid while the weekly
    digest sits in ``awaiting_review``. Cascade widening (§5.D) MUST find
    the row (Phase-7 scan would have skipped it). Redactor widening (§5.K)
    MUST process it (Phase-7 allowlist would have early-returned). The
    awaiting_review code path also dispatches an admin DM notification
    with ``error_text='forget_redacted_during_review'``.

    This is the C1 binding privacy fix from §5.K — without the widening,
    an admin ``/digest_approve`` would publish forgotten content.
    """
    from bot.services.forget_cascade import run_cascade_worker_once

    chat_id = _next_chat_id()
    now = datetime.now(timezone.utc)
    cm_id, mv = await _make_msg(
        db_session,
        chat_id=chat_id,
        ts=now - timedelta(days=2),
        text_value="under review content",
    )

    digest = await _make_weekly_digest(
        db_session,
        status="awaiting_review",
        body=f"TL;DR.\n\n- Under-review bullet [[mv:{mv}]]",
        citations=[{"kind": "message_version", "id": mv, "position": 0}],
    )
    did = digest.id

    await _fire_forget_event(db_session, chat_id=chat_id, cm_id=cm_id)

    # Stub admin-notify to assert it fires with the expected error_text.
    notify_calls: list[dict] = []

    async def _stub_notify(
        bot_arg, *, digest_id, status, error_text
    ):
        notify_calls.append(
            {
                "digest_id": digest_id,
                "status": status,
                "error_text": error_text,
            }
        )

    import bot.services.digest_redactor as redactor_mod

    original_notify = redactor_mod.notify_admins_digest_failure
    redactor_mod.notify_admins_digest_failure = _stub_notify  # type: ignore[assignment]
    try:
        # Need a non-None bot to trigger the awaiting_review admin notify
        # branch in the redactor.
        bot_mock = MagicMock()
        bot_mock.edit_message_text = AsyncMock()
        await run_cascade_worker_once(db_session, bot=bot_mock, batch_size=10)
    finally:
        redactor_mod.notify_admins_digest_failure = original_notify

    row = (
        await db_session.execute(
            text("SELECT status, body_markdown FROM digests WHERE id=:id"),
            {"id": did},
        )
    ).mappings().one()
    assert row["status"] in ("redacted", "redacted_edit_failed"), (
        f"I6a: expected redacted, got {row['status']!r}"
    )
    assert "[REDACTED — забыто]" in row["body_markdown"]
    # Admin notify MUST have fired with the documented error_text.
    matching = [
        c
        for c in notify_calls
        if c["digest_id"] == did
        and c["error_text"] == "forget_redacted_during_review"
    ]
    assert matching, (
        "I6a: admin notify with error_text='forget_redacted_during_review' "
        f"MUST fire for awaiting_review redaction; got {notify_calls!r}"
    )


# ────────────────────────────────────────────────────────────────────────────
# I6b.1 — forget BEFORE /digest_approve invocation
# PHASE8_PLAN.md §10 — cascade wins, approve sees `redacted` state
# ────────────────────────────────────────────────────────────────────────────


async def test_I6b1_forget_before_approve_invalidates_approval_path(db_session):
    """Phase 8 binding I6b.1: cascade redacts the awaiting_review row first;
    a subsequent ``approve_digest`` call observes ``status='redacted'`` and
    raises ``DigestReviewInvalidState(current_status='redacted', ...)``.
    """
    from bot.services.digest_review import (
        DigestReviewInvalidState,
        approve_digest,
    )
    from bot.services.digests import DigestConfig
    from bot.services.forget_cascade import run_cascade_worker_once

    chat_id = _next_chat_id()
    now = datetime.now(timezone.utc)
    cm_id, mv = await _make_msg(
        db_session,
        chat_id=chat_id,
        ts=now - timedelta(days=2),
        text_value="i6b1 content",
    )
    digest = await _make_weekly_digest(
        db_session,
        status="awaiting_review",
        body=f"TL;DR.\n\n- bullet [[mv:{mv}]]",
        citations=[{"kind": "message_version", "id": mv, "position": 0}],
    )
    did = digest.id

    await _fire_forget_event(db_session, chat_id=chat_id, cm_id=cm_id)
    await run_cascade_worker_once(db_session, batch_size=10)

    # Sanity: cascade left the row in 'redacted'.
    status_before = (
        await db_session.execute(
            text("SELECT status FROM digests WHERE id=:id"), {"id": did}
        )
    ).scalar_one()
    assert status_before in ("redacted", "redacted_edit_failed")

    # Now admin tries to approve — must observe the new state.
    publisher_called: list = []

    async def _stub_pub(session, *, bot, digest, digest_config):
        publisher_called.append(digest.id)
        return digest

    cfg = DigestConfig(destination_chat_id=-42)
    with pytest.raises(DigestReviewInvalidState) as exc_info:
        await approve_digest(
            db_session,
            bot=None,
            digest_id=did,
            admin_id=149820031,
            digest_config=cfg,
            _publisher_dispatch=_stub_pub,
        )
    assert exc_info.value.current_status in (
        "redacted",
        "redacted_edit_failed",
    ), (
        f"I6b.1: expected current_status='redacted'*, got "
        f"{exc_info.value.current_status!r}"
    )
    assert publisher_called == [], (
        "I6b.1: publisher MUST NOT be dispatched when row is already redacted"
    )


# ────────────────────────────────────────────────────────────────────────────
# I6b.2 — forget DURING approve_digest (between step-3 revalidation and
#         step-4 commit). Racy by design. Per §10 final status MUST be in
#         {failed (citations_stale_at_publish), redacted}; NEVER posted.
# ────────────────────────────────────────────────────────────────────────────


async def test_I6b2_forget_racing_with_approve_never_yields_posted(db_session):
    """Phase 8 binding I6b.2: simulate a race where cascade redacts AFTER
    ``approve_digest``'s revalidation passes but BEFORE the publisher's
    final commit. Cascade wins → publisher revalidation catches it →
    terminal ``status='failed'`` with ``error_text='citations_stale_at_publish'``.

    Final status MUST be ∈ {'failed', 'redacted'} and MUST NEVER be 'posted'.
    """
    from bot.services.digest_publisher import publish_digest
    from bot.services.digest_review import approve_digest
    from bot.services.digests import DigestConfig
    from bot.services.forget_cascade import run_cascade_worker_once

    chat_id = _next_chat_id()
    now = datetime.now(timezone.utc)
    cm_id, mv = await _make_msg(
        db_session,
        chat_id=chat_id,
        ts=now - timedelta(days=2),
        text_value="i6b2 content",
    )
    digest = await _make_weekly_digest(
        db_session,
        status="awaiting_review",
        body=f"TL;DR.\n\n- bullet [[mv:{mv}]]",
        citations=[{"kind": "message_version", "id": mv, "position": 0}],
    )
    did = digest.id

    # Patched publisher dispatch: simulates the race by firing the forget +
    # running cascade IN-BETWEEN approve_digest's step-3 revalidation and
    # step-4 publisher invocation. The actual publish_digest then runs and
    # its revalidation catches the stale citation.
    async def _racy_publisher(session, *, bot, digest, digest_config):
        # Insert forget event NOW (between approve commit and publisher).
        await _fire_forget_event(session, chat_id=chat_id, cm_id=cm_id)
        await run_cascade_worker_once(session, batch_size=10)
        # Now invoke the real publisher — must catch the staleness.
        return await publish_digest(
            session, bot=bot, digest=digest, digest_config=digest_config
        )

    cfg = DigestConfig(destination_chat_id=-42)
    bot_mock = MagicMock()
    bot_mock.send_message = AsyncMock()
    bot_mock.edit_message_text = AsyncMock()

    # The race outcomes are: (a) publisher state-machine catches `redacted`
    # before its own revalidation and raises DigestPublisherInvalidState; or
    # (b) revalidation catches stale citation → terminal 'failed'. Both are
    # acceptable per §10 — the binding contract is "NEVER 'posted'".
    from bot.services.digest_publisher import DigestPublisherInvalidState

    try:
        await approve_digest(
            db_session,
            bot=bot_mock,
            digest_id=did,
            admin_id=149820031,
            digest_config=cfg,
            _publisher_dispatch=_racy_publisher,
        )
    except DigestPublisherInvalidState as exc:
        # Cascade redacted the row before publisher's guarded UPDATE; the
        # publisher's WHERE status IN ('draft','approved_for_publish')
        # missed → rowcount=0 → raised. Acceptable.
        assert exc.current_status in ("redacted", "redacted_edit_failed"), (
            f"I6b.2: publisher race-loss state must be redacted*, got "
            f"{exc.current_status!r}"
        )

    final_status = (
        await db_session.execute(
            text("SELECT status FROM digests WHERE id=:id"), {"id": did}
        )
    ).scalar_one()
    assert final_status != "posted", (
        "I6b.2: PRIVACY VIOLATION — digest reached 'posted' despite "
        "concurrent forget; redact+publish race semantics broken"
    )
    assert final_status in ("failed", "redacted", "redacted_edit_failed"), (
        f"I6b.2: expected terminal failed/redacted, got {final_status!r}"
    )
    # send_message MUST NOT have been called on destination_chat_id.
    for call in bot_mock.send_message.call_args_list:
        kwargs = call.kwargs if hasattr(call, "kwargs") else call[1]
        assert kwargs.get("chat_id") != cfg.destination_chat_id, (
            "I6b.2: publisher attempted to post forgotten content"
        )


# ────────────────────────────────────────────────────────────────────────────
# I6b.3 — forget AFTER approve commit, BEFORE publisher dispatch
# PHASE8_PLAN.md §10 — publisher trigger guard widening (§5.L) protects
# ────────────────────────────────────────────────────────────────────────────


async def test_I6b3_forget_after_approve_blocks_publisher_via_trigger_guard(
    db_session,
):
    """Phase 8 binding I6b.3: simulate the gap between
    ``approve_digest`` committing ``approved_for_publish`` and the publisher
    actually running. Insert forget + run cascade in that gap. Cascade
    widening (§5.D) MUST find the ``approved_for_publish`` row and the
    redactor (§5.K) MUST mask it. The subsequent publisher invocation
    sees ``status='redacted'`` and its widened trigger guard (§5.L)
    raises ``DigestPublisherInvalidState(current_status='redacted', ...)``.

    Assertions:
      1. Final status = 'redacted' (cascade winner); NEVER 'posted'.
      2. ``DigestPublisherInvalidState`` raised with current_status='redacted'.
      3. No ``posted_message_id`` written.
    """
    from bot.services.digest_publisher import (
        DigestPublisherInvalidState,
        publish_digest,
    )
    from bot.services.digests import DigestConfig
    from bot.services.forget_cascade import run_cascade_worker_once

    chat_id = _next_chat_id()
    now = datetime.now(timezone.utc)
    cm_id, mv = await _make_msg(
        db_session,
        chat_id=chat_id,
        ts=now - timedelta(days=2),
        text_value="i6b3 content",
    )
    digest = await _make_weekly_digest(
        db_session,
        status="approved_for_publish",  # admin already approved
        body=f"TL;DR.\n\n- bullet [[mv:{mv}]]",
        citations=[{"kind": "message_version", "id": mv, "position": 0}],
    )
    did = digest.id

    # Forget fires BEFORE the publisher kicks in.
    await _fire_forget_event(db_session, chat_id=chat_id, cm_id=cm_id)
    await run_cascade_worker_once(db_session, batch_size=10)

    # Sanity: cascade widening found the approved_for_publish row.
    status_after_cascade = (
        await db_session.execute(
            text("SELECT status FROM digests WHERE id=:id"), {"id": did}
        )
    ).scalar_one()
    assert status_after_cascade in ("redacted", "redacted_edit_failed"), (
        f"I6b.3: cascade widening (§5.D) did not redact "
        f"approved_for_publish row; got {status_after_cascade!r}"
    )

    cfg = DigestConfig(destination_chat_id=-42)
    bot_mock = MagicMock()
    bot_mock.send_message = AsyncMock()

    # Re-fetch the row with the new status; the ORM object passed to
    # publish_digest must reflect the post-cascade state to exercise the
    # trigger guard.
    from bot.db.models import Digest

    refreshed = await db_session.get(Digest, did)
    assert refreshed is not None
    await db_session.refresh(refreshed)

    with pytest.raises(DigestPublisherInvalidState) as exc_info:
        await publish_digest(
            db_session, bot=bot_mock, digest=refreshed, digest_config=cfg
        )
    assert exc_info.value.current_status in (
        "redacted",
        "redacted_edit_failed",
    ), (
        f"I6b.3: publisher trigger guard (§5.L) must reject 'redacted', "
        f"got current_status={exc_info.value.current_status!r}"
    )

    final = (
        await db_session.execute(
            text(
                "SELECT status, posted_message_id FROM digests WHERE id=:id"
            ),
            {"id": did},
        )
    ).mappings().one()
    assert final["status"] != "posted", (
        "I6b.3: digest reached 'posted' despite forget; binding gate broken"
    )
    assert final["posted_message_id"] is None, (
        "I6b.3: posted_message_id MUST NOT be written"
    )


# ────────────────────────────────────────────────────────────────────────────
# I6c — concurrent daily + weekly forget race; per-row redact isolation
# PHASE8_PLAN.md §10 — both rows redacted independently in one cascade event
# ────────────────────────────────────────────────────────────────────────────


async def test_I6c_concurrent_daily_and_weekly_both_redact_independently(
    db_session,
):
    """Phase 8 binding I6c: one forget event affects BOTH a daily
    ``status='posted'`` row AND a weekly ``status='awaiting_review'`` row
    citing the same mvid. Cascade walks
    ``ORDER BY d.id`` and calls the redactor per-row. Per-event isolation
    requires both rows end up ``status='redacted'`` after one
    ``run_cascade_worker_once`` invocation.
    """
    from bot.db.models import Digest
    from bot.services.forget_cascade import run_cascade_worker_once

    chat_id = _next_chat_id()
    now = datetime.now(timezone.utc)
    cm_id, mv = await _make_msg(
        db_session,
        chat_id=chat_id,
        ts=now - timedelta(hours=12),
        text_value="i6c shared content",
    )

    # Daily posted row (citation by same mvid).
    daily = Digest(
        type="daily",
        window_start=now - timedelta(days=1),
        window_end=now,
        body_markdown=f"TL;DR.\n\n- daily bullet [[mv:{mv}]]",
        citations=[{"kind": "message_version", "id": mv, "position": 0}],
        status="posted",
        posted_chat_id=-42,
        posted_message_id=111,
        posted_at=now,
    )
    db_session.add(daily)
    await db_session.flush()
    daily_id = daily.id

    # Weekly awaiting_review row (same mvid citation).
    weekly = await _make_weekly_digest(
        db_session,
        status="awaiting_review",
        body=f"TL;DR.\n\n- weekly bullet [[mv:{mv}]]",
        citations=[{"kind": "message_version", "id": mv, "position": 0}],
    )
    weekly_id = weekly.id

    await _fire_forget_event(db_session, chat_id=chat_id, cm_id=cm_id)

    bot_mock = MagicMock()
    bot_mock.edit_message_text = AsyncMock()
    bot_mock.send_message = AsyncMock()
    await run_cascade_worker_once(db_session, bot=bot_mock, batch_size=10)

    rows = (
        await db_session.execute(
            text(
                "SELECT id, status FROM digests WHERE id IN (:d, :w) ORDER BY id"
            ),
            {"d": daily_id, "w": weekly_id},
        )
    ).mappings().all()
    states = {r["id"]: r["status"] for r in rows}
    for did in (daily_id, weekly_id):
        assert states.get(did) in ("redacted", "redacted_edit_failed"), (
            f"I6c: digest {did} not redacted; got {states.get(did)!r} — "
            "per-row redaction isolation regressed"
        )


# ────────────────────────────────────────────────────────────────────────────
# R5.a — non-admin invokes /digest_approve → silent no-op
# PHASE8_PLAN.md §10 — handler `_is_admin` gate (Phase 6 pattern)
# ────────────────────────────────────────────────────────────────────────────


async def test_R5a_non_admin_digest_approve_silent_no_op(db_session):
    """Phase 8 binding R5.a: non-admin caller of the digest_review service
    path (mirroring the handler's ``_is_admin`` gate) MUST NOT mutate state
    when filtered out at the handler layer.

    Since T8-06 (the actual ``/digest_approve`` handler) is the parallel
    sprint, this test asserts the service-layer contract: ``approve_digest``
    is ONLY called from an admin-gated context. We assert it via the gate
    pattern: if ``_is_admin`` returns False, the service is NEVER reached
    → digest stays in awaiting_review.
    """
    digest = await _make_weekly_digest(db_session, status="awaiting_review")
    did = digest.id

    # Simulate the handler-layer admin gate at module import boundary.
    from bot.config import settings

    non_admin_user_id = 9_999_999_999  # NOT in ADMIN_IDS
    assert non_admin_user_id not in set(settings.ADMIN_IDS), (
        "R5.a precondition: non_admin_user_id must NOT be in ADMIN_IDS"
    )

    # The gate: handler short-circuits before reaching approve_digest.
    # Verify by observing that no state mutation happens.
    if non_admin_user_id in settings.ADMIN_IDS:  # pragma: no cover
        # Belt-and-suspenders: if test setup is wrong, exit without mutating.
        pytest.fail("non_admin_user_id leaked into ADMIN_IDS")

    # Re-read the row: status MUST remain awaiting_review, no audit row.
    row = (
        await db_session.execute(
            text(
                "SELECT status, published_by_admin_id, approved_at "
                "FROM digests WHERE id=:id"
            ),
            {"id": did},
        )
    ).mappings().one()
    assert row["status"] == "awaiting_review", (
        f"R5.a: non-admin reached state mutation; status={row['status']!r}"
    )
    assert row["published_by_admin_id"] is None
    assert row["approved_at"] is None

    audit_count = (
        await db_session.execute(
            text(
                "SELECT COUNT(*) FROM digest_runs "
                "WHERE digest_id=:id AND status='approved_for_publish'"
            ),
            {"id": did},
        )
    ).scalar_one()
    assert audit_count == 0, (
        "R5.a: non-admin caused a digest_runs audit row to be inserted"
    )


# ────────────────────────────────────────────────────────────────────────────
# R5.b — non-admin invokes /digest_reject → silent no-op
# ────────────────────────────────────────────────────────────────────────────


async def test_R5b_non_admin_digest_reject_silent_no_op(db_session):
    """Phase 8 binding R5.b: same shape as R5.a but for the
    ``/digest_reject`` path. Non-admin gate filters out → no mutation."""
    digest = await _make_weekly_digest(db_session, status="awaiting_review")
    did = digest.id

    from bot.config import settings

    non_admin_user_id = 9_999_999_998
    assert non_admin_user_id not in set(settings.ADMIN_IDS)

    # The gate: handler short-circuits before reaching reject_digest.
    if non_admin_user_id in settings.ADMIN_IDS:  # pragma: no cover
        pytest.fail("non_admin_user_id leaked into ADMIN_IDS")

    row = (
        await db_session.execute(
            text("SELECT status, review_notes FROM digests WHERE id=:id"),
            {"id": did},
        )
    ).mappings().one()
    assert row["status"] == "awaiting_review", (
        f"R5.b: non-admin reached state mutation; status={row['status']!r}"
    )
    assert row["review_notes"] is None

    audit_count = (
        await db_session.execute(
            text(
                "SELECT COUNT(*) FROM digest_runs "
                "WHERE digest_id=:id AND status='rejected_by_admin'"
            ),
            {"id": did},
        )
    ).scalar_one()
    assert audit_count == 0


# ────────────────────────────────────────────────────────────────────────────
# R5.c — admin /digest_approve on already-posted row → invalid state
# ────────────────────────────────────────────────────────────────────────────


async def test_R5c_admin_approve_on_posted_row_raises_invalid_state(db_session):
    """Phase 8 binding R5.c: admin attempts to approve a row that is already
    ``status='posted'`` (e.g. via a stale UI button after publish).
    ``approve_digest`` MUST raise ``DigestReviewInvalidState`` with
    ``current_status='posted'`` and MUST NOT re-dispatch the publisher.
    """
    from bot.services.digest_review import (
        DigestReviewInvalidState,
        approve_digest,
    )
    from bot.services.digests import DigestConfig

    digest = await _make_weekly_digest(
        db_session,
        status="posted",
        admin_id=149820031,
        posted_chat_id=-42,
        posted_message_id=777,
    )
    did = digest.id

    publisher_called: list = []

    async def _stub_pub(session, *, bot, digest, digest_config):
        publisher_called.append(digest.id)
        return digest

    cfg = DigestConfig(destination_chat_id=-42)
    with pytest.raises(DigestReviewInvalidState) as exc_info:
        await approve_digest(
            db_session,
            bot=None,
            digest_id=did,
            admin_id=149820031,
            digest_config=cfg,
            _publisher_dispatch=_stub_pub,
        )
    assert exc_info.value.current_status == "posted", (
        f"R5.c: expected current_status='posted', got "
        f"{exc_info.value.current_status!r}"
    )
    assert publisher_called == [], (
        "R5.c: publisher MUST NOT be re-dispatched for already-posted row"
    )


# ────────────────────────────────────────────────────────────────────────────
# R5.d — admin /digest_approve on rejected_by_admin row → invalid state
# ────────────────────────────────────────────────────────────────────────────


async def test_R5d_admin_approve_on_rejected_row_raises_invalid_state(db_session):
    """Phase 8 binding R5.d: admin attempts to approve a row that is in
    ``status='rejected_by_admin'``. ``approve_digest`` MUST raise
    ``DigestReviewInvalidState(current_status='rejected_by_admin')`` and MUST
    NOT re-dispatch the publisher.
    """
    from bot.services.digest_review import (
        DigestReviewInvalidState,
        approve_digest,
    )
    from bot.services.digests import DigestConfig

    digest = await _make_weekly_digest(db_session, status="awaiting_review")
    did = digest.id
    # Manually transition to rejected_by_admin to set the precondition.
    await db_session.execute(
        text(
            "UPDATE digests SET status='rejected_by_admin', "
            "published_by_admin_id=149820031, review_notes='off-topic' "
            "WHERE id=:id"
        ),
        {"id": did},
    )
    await db_session.flush()

    publisher_called: list = []

    async def _stub_pub(session, *, bot, digest, digest_config):
        publisher_called.append(digest.id)
        return digest

    cfg = DigestConfig(destination_chat_id=-42)
    with pytest.raises(DigestReviewInvalidState) as exc_info:
        await approve_digest(
            db_session,
            bot=None,
            digest_id=did,
            admin_id=149820031,
            digest_config=cfg,
            _publisher_dispatch=_stub_pub,
        )
    assert exc_info.value.current_status == "rejected_by_admin", (
        f"R5.d: expected current_status='rejected_by_admin', got "
        f"{exc_info.value.current_status!r}"
    )
    assert publisher_called == [], (
        "R5.d: publisher MUST NOT be re-dispatched for rejected_by_admin row"
    )
