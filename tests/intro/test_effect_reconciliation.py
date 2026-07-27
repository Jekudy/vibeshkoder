from __future__ import annotations

import logging

import pytest
from sqlalchemy import select

from tests.intro.test_effect_worker import CHAT_ID, _confirmed_application, _effect, _user_id


async def _unknown_effect(
    session, application_id: int, *, effect_kind: str = "refresh_intro", attempt_count: int = 1
):
    effect = await _effect(session, application_id, effect_kind, status="processing")
    effect.status = "unknown"
    effect.attempt_count = attempt_count
    await session.flush()
    return effect


async def _admin_operator(session) -> int:
    from bot.db.models import User

    operator_id = 149820031
    if await session.get(User, operator_id) is None:
        session.add(
            User(
                id=operator_id,
                username="operator",
                first_name="Operator",
                last_name=None,
                is_admin=True,
            )
        )
        await session.flush()
    return operator_id


@pytest.mark.asyncio
async def test_record_sent_reuses_refresh_finalization_requires_identity_and_writes_audit_log(
    app_env, caplog, db_session
) -> None:
    from bot.db.models import Intro, IntroEffectOutbox, IntroEffectReconciliation
    from bot.services.intro_effect_worker import IntroEffectReconcileError, reconcile_intro_effect

    user_id = _user_id()
    base = await _confirmed_application(db_session, user_id=user_id, flow_kind="admission")
    base.status = "added"
    current = Intro(
        user_id=user_id,
        application_id=base.id,
        intro_text="old",
        vouched_by_name="@voucher",
    )
    db_session.add(current)
    refresh = await _confirmed_application(
        db_session, user_id=user_id, flow_kind="refresh", base_id=base.id
    )
    effect = await _unknown_effect(db_session, refresh.id, attempt_count=4)
    operator_id = await _admin_operator(db_session)

    with pytest.raises(IntroEffectReconcileError):
        await reconcile_intro_effect(
            db_session,
            effect_id=effect.id,
            action="record-sent",
            chat_id=None,
            message_id=615,
            operator_user_id=operator_id,
            reason="found it",
        )
    with pytest.raises(IntroEffectReconcileError):
        await reconcile_intro_effect(
            db_session,
            effect_id=effect.id,
            action="record-sent",
            chat_id=CHAT_ID,
            message_id=None,
            operator_user_id=operator_id,
            reason="found it",
        )
    with pytest.raises(IntroEffectReconcileError):
        await reconcile_intro_effect(
            db_session,
            effect_id=effect.id,
            action="record-sent",
            chat_id=CHAT_ID,
            message_id=616,
            operator_user_id=operator_id,
            reason="",
        )

    with caplog.at_level(logging.INFO):
        result = await reconcile_intro_effect(
            db_session,
            effect_id=effect.id,
            action="record-sent",
            chat_id=CHAT_ID,
            message_id=616,
            operator_user_id=operator_id,
            reason="found it in chat history",
        )
    await db_session.refresh(current)
    rows = list(
        (
            await db_session.execute(
                select(IntroEffectOutbox).where(IntroEffectOutbox.application_id == refresh.id)
            )
        ).scalars()
    )

    assert (result.status, result.chat_id, result.message_id) == ("sent", CHAT_ID, 616)
    assert result.attempt_count == 4
    assert current.application_id == refresh.id
    assert {(row.effect_kind, row.status) for row in rows} == {
        ("refresh_intro", "sent"),
        ("sheet_projection", "pending"),
    }
    audit = next(record for record in caplog.records if record.message == "intro_effect_reconciled")
    assert (audit.operator_user_id, audit.reason, audit.action) == (
        149820031,
        "found it in chat history",
        "record-sent",
    )
    assert audit.effect_id == effect.id
    reconciliation = (
        await db_session.execute(
            select(IntroEffectReconciliation).where(
                IntroEffectReconciliation.effect_id == effect.id
            )
        )
    ).scalar_one()
    assert (
        reconciliation.effect_id,
        reconciliation.action,
        reconciliation.operator_user_id,
        reconciliation.reason,
        reconciliation.evidence_sha256,
        reconciliation.attempt_count,
    ) == (effect.id, "record-sent", operator_id, "found it in chat history", None, 4)
    with pytest.raises(IntroEffectReconcileError):
        await reconcile_intro_effect(
            db_session,
            effect_id=effect.id,
            action="record-sent",
            chat_id=CHAT_ID,
            message_id=616,
            operator_user_id=operator_id,
            reason="concurrent second decision",
        )


@pytest.mark.asyncio
async def test_record_sent_admission_intro_preserves_attempt_and_creates_intro_with_voucher_metadata(
    app_env, db_session
) -> None:
    from bot.db.models import Intro, IntroEffectReconciliation
    from bot.services.intro_effect_worker import reconcile_intro_effect

    application = await _confirmed_application(
        db_session, user_id=_user_id(), flow_kind="admission"
    )
    application.status = "added"
    operator_id = await _admin_operator(db_session)
    application.vouched_by = operator_id
    effect = await _unknown_effect(
        db_session, application.id, effect_kind="admission_intro", attempt_count=3
    )
    original_attempt_started_at = effect.attempt_started_at

    result = await reconcile_intro_effect(
        db_session,
        effect_id=effect.id,
        action="record-sent",
        chat_id=CHAT_ID,
        message_id=719,
        operator_user_id=operator_id,
        reason="found admission intro",
    )
    intro = (
        await db_session.execute(select(Intro).where(Intro.user_id == application.user_id))
    ).scalar_one()
    audit = (
        await db_session.execute(
            select(IntroEffectReconciliation).where(
                IntroEffectReconciliation.effect_id == effect.id
            )
        )
    ).scalar_one()

    assert (result.status, result.attempt_count, result.message_id, result.attempt_started_at) == (
        "sent",
        3,
        719,
        original_attempt_started_at,
    )
    assert (intro.application_id, intro.intro_text, intro.vouched_by_name) == (
        application.id,
        application.confirmed_intro_html,
        "@operator",
    )
    assert (audit.action, audit.attempt_count, audit.operator_user_id) == (
        "record-sent",
        3,
        operator_id,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence", [None, "A" * 64, "a" * 63, "g" * 64])
async def test_retry_absent_requires_exact_lowercase_sha256_evidence(
    app_env, db_session, evidence
) -> None:
    from bot.services.intro_effect_worker import IntroEffectReconcileError, reconcile_intro_effect

    application = await _confirmed_application(db_session, user_id=_user_id())
    effect = await _unknown_effect(db_session, application.id, attempt_count=4)

    with pytest.raises(IntroEffectReconcileError):
        await reconcile_intro_effect(
            db_session,
            effect_id=effect.id,
            action="retry-absent",
            evidence_sha256=evidence,
            operator_user_id=149820031,
            reason="checked chat history",
        )


@pytest.mark.asyncio
async def test_retry_absent_logs_audit_and_allows_exactly_one_unknown_to_pending(
    app_env, caplog, db_session
) -> None:
    from bot.db.repos.intro_effect_outbox import IntroEffectOutboxRepo
    from bot.db.models import IntroEffectReconciliation
    from bot.services.intro_effect_worker import IntroEffectReconcileError, reconcile_intro_effect

    application = await _confirmed_application(db_session, user_id=_user_id())
    effect = await _unknown_effect(db_session, application.id, attempt_count=4)
    operator_id = await _admin_operator(db_session)
    evidence = "a" * 64

    with caplog.at_level(logging.INFO):
        result = await reconcile_intro_effect(
            db_session,
            effect_id=effect.id,
            action="retry-absent",
            evidence_sha256=evidence,
            operator_user_id=operator_id,
            reason="checked chat history",
        )
    assert result.status == "pending"
    audit = next(record for record in caplog.records if record.message == "intro_effect_reconciled")
    assert (audit.operator_user_id, audit.reason, audit.evidence_sha256) == (
        149820031,
        "checked chat history",
        evidence,
    )
    assert (audit.effect_id, audit.action) == (effect.id, "retry-absent")
    reconciliation = (
        await db_session.execute(
            select(IntroEffectReconciliation).where(
                IntroEffectReconciliation.effect_id == effect.id
            )
        )
    ).scalar_one()
    assert (
        reconciliation.action,
        reconciliation.operator_user_id,
        reconciliation.reason,
        reconciliation.evidence_sha256,
        reconciliation.attempt_count,
    ) == ("retry-absent", operator_id, "checked chat history", evidence, 4)
    claimed = await IntroEffectOutboxRepo.claim_pending(db_session, limit=1)
    assert [(row.id, row.attempt_count) for row in claimed] == [(effect.id, 5)]
    with pytest.raises(IntroEffectReconcileError):
        await reconcile_intro_effect(
            db_session,
            effect_id=effect.id,
            action="retry-absent",
            evidence_sha256=evidence,
            operator_user_id=operator_id,
            reason="cannot retry twice",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "processing", "sent", "failed", "stale"])
@pytest.mark.parametrize("action", ["record-sent", "retry-absent"])
async def test_reconciliation_fails_closed_for_every_non_unknown_status_and_action(
    app_env, db_session, status, action
) -> None:
    from bot.services.intro_effect_worker import IntroEffectReconcileError, reconcile_intro_effect

    application = await _confirmed_application(db_session, user_id=_user_id())
    effect = await _effect(db_session, application.id, "refresh_intro", status=status)
    if status == "processing":
        effect.attempt_count = 1
        effect.attempt_started_at = application.created_at
        await db_session.flush()

    with pytest.raises(IntroEffectReconcileError):
        await reconcile_intro_effect(
            db_session,
            effect_id=effect.id,
            action=action,
            chat_id=CHAT_ID,
            message_id=717,
            evidence_sha256="b" * 64,
            operator_user_id=149820031,
            reason="must fail closed",
        )


@pytest.mark.asyncio
async def test_reconciliation_rejects_unknown_action_without_state_change(
    app_env, db_session
) -> None:
    from bot.services.intro_effect_worker import IntroEffectReconcileError, reconcile_intro_effect

    application = await _confirmed_application(db_session, user_id=_user_id())
    effect = await _unknown_effect(db_session, application.id)

    with pytest.raises(IntroEffectReconcileError):
        await reconcile_intro_effect(
            db_session,
            effect_id=effect.id,
            action="send-now",
            operator_user_id=149820031,
            reason="forbidden direct send",
        )
    await db_session.refresh(effect)
    assert effect.status == "unknown"


@pytest.mark.asyncio
async def test_retry_absent_rejects_empty_reason(app_env, db_session) -> None:
    from bot.services.intro_effect_worker import IntroEffectReconcileError, reconcile_intro_effect

    application = await _confirmed_application(db_session, user_id=_user_id())
    effect = await _unknown_effect(db_session, application.id)

    with pytest.raises(IntroEffectReconcileError):
        await reconcile_intro_effect(
            db_session,
            effect_id=effect.id,
            action="retry-absent",
            evidence_sha256="c" * 64,
            operator_user_id=149820031,
            reason="",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "invalid"),
    [
        ("record-sent", "wrong-chat"),
        ("record-sent", "nonpositive-message"),
        ("record-sent", "blank-reason"),
        ("retry-absent", "blank-reason"),
        ("record-sent", "non-admin"),
        ("retry-absent", "non-admin"),
    ],
)
async def test_unknown_reconciliation_fails_closed_without_audit_for_invalid_inputs(
    app_env, db_session, action, invalid
) -> None:
    from bot.db.models import IntroEffectReconciliation
    from bot.services.intro_effect_worker import IntroEffectReconcileError, reconcile_intro_effect

    application = await _confirmed_application(db_session, user_id=_user_id())
    effect = await _unknown_effect(db_session, application.id)
    kwargs = {"operator_user_id": 149820031, "reason": "audited reconciliation"}
    if action == "record-sent":
        kwargs.update(chat_id=CHAT_ID, message_id=818)
    else:
        kwargs.update(evidence_sha256="d" * 64)
    if invalid == "wrong-chat":
        kwargs["chat_id"] = CHAT_ID + 1
    elif invalid == "nonpositive-message":
        kwargs["message_id"] = 0
    elif invalid == "blank-reason":
        kwargs["reason"] = "  "
    elif invalid == "non-admin":
        kwargs["operator_user_id"] = application.user_id

    with pytest.raises(IntroEffectReconcileError):
        await reconcile_intro_effect(db_session, effect_id=effect.id, action=action, **kwargs)
    await db_session.refresh(effect)
    assert effect.status == "unknown"
    assert (
        await db_session.execute(
            select(IntroEffectReconciliation).where(
                IntroEffectReconciliation.effect_id == effect.id
            )
        )
    ).scalars().all() == []
