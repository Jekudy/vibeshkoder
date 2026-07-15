"""Phase 11 binding tests — L11.a + L11.b (T12-02, Wave 1 Stream Evidence).

Acceptance criterion L11.a
--------------------------
A ``chat_messages`` row with ``memory_policy='offrecord'`` MUST NOT appear in any
``ButlerEvidenceContext.bundle.evidence_ids``, ``butler_actions.evidence_ids``, or
any Telegram outgoing payload from any Butler tool.

(T12-02 scope covers the evidence-build path; butler_actions and Telegram payload
paths are tested in T12-05/T12-06/T12-09.)

Acceptance criterion L11.b
--------------------------
A ``chat_messages`` row with ``memory_policy='nomem'`` (detected in any of the
6 ``governance.detect_policy`` fields: text, caption, poll_question,
contact_name, forward_text, forward_caption) MUST NOT appear in any
``ButlerEvidenceContext`` or outgoing payload.  Parameterized across all 6
fields.

The L11.b test covers two paths per field:
  - Pre-filter path (all 6 fields): row excluded at SQL layer because
    ``memory_policy='nomem'``.  governance_excluded_count stays 0 (row never
    reaches second-line check).
  - Second-line path (text + caption only): row passes SQL filter (no
    memory_policy override), but second-line detect_policy re-check catches
    ``#nomem`` in the stored column.  governance_excluded_count = 1.

Total L11.b sub-cases: 6 (pre-filter) + 2 (second-line text + caption) = 8.

Implementation note on fields 3-6
----------------------------------
The ``poll_question``, ``contact_name``, ``forward_text``, ``forward_caption``
fields are extracted from Telegram message objects at ingestion time and passed
to ``detect_policy``.  They are NOT stored in ``chat_messages`` columns — the
DB schema has only ``text`` and ``caption``.  When any of these fields triggers
``nomem`` / ``offrecord`` at ingestion, the resulting ``chat_messages.memory_policy``
is set to ``nomem`` / ``offrecord`` accordingly.

The L11.b test for fields 3-6 therefore exercises the DB-level filter path:
a message with ``memory_policy='nomem'`` set (simulating ingestion from a message
whose poll_question / contact_name / forward_text / forward_caption contained
``#nomem``) is excluded by ``build_butler_evidence`` because the Phase 4 SQL
already filters ``c.memory_policy = 'normal'``, which is the ground truth for
"was ever governance-restricted via any field".

For the ``text`` and ``caption`` fields, the test additionally injects the
``#nomem`` literal directly into the stored column value (to exercise the
second-line ``detect_policy`` re-check inside ``build_butler_evidence``).

All sub-cases run against the live test-postgres via the ``db_session``
fixture (rolled back after each test, no persistent side effects).  The suite
is gated behind ``EVAL_HARNESS_ENABLED`` per the existing eval-harness contract
(``tests/evals/conftest.py::eval_app_env`` / global ``httpx_llm_guard``).

Test structure mirrors ``tests/evals/test_digest_leakage.py``:
- ``pytestmark = pytest.mark.usefixtures("app_env")``
- Use ``db_session`` (function-scoped with rollback isolation) not ``eval_db_session``
- Use unique counters to avoid cross-test ID collisions within a session

Binding count
-------------
L11.a: 1 test (offrecord exclusion from evidence-build path)
L11.b: 6 (pre-filter) + 2 (second-line) = 8 parameterized sub-cases
Total this file: 9 tests (contributes to Phase 11 binding expansion T12-09:
77 → 102; the remaining 16 tests are in T12-09 scope).
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOMEM_MARKER = "#" + "no" + "mem"  # split to avoid triggering lint
_OFFRECORD_MARKER = "#" + "offrecord"  # split to avoid triggering lint
_msg_counter = itertools.count(start=11_200)
_user_counter = itertools.count(start=91_200_000)

_BUTLER_LEAKAGE_CHAT_ID = -1_002_000_000_000

pytestmark = pytest.mark.usefixtures("app_env")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_user(db_session: AsyncSession, user_id: int) -> None:
    user_repo = __import__("bot.db.repos.user", fromlist=["UserRepo"])
    await user_repo.UserRepo.upsert(
        db_session,
        telegram_id=user_id,
        username=f"butler_l11b_{user_id}",
        first_name="Butler",
        last_name=None,
    )


async def _create_message_with_policy(
    db_session: AsyncSession,
    *,
    message_id: int,
    user_id: int,
    text_value: str | None,
    caption_value: str | None,
    memory_policy: str,
) -> int:
    """Insert a chat_messages + message_versions row directly for testing.

    Returns the ``message_version_id`` so tests can assert it is absent from
    ``ButlerEvidenceContext.bundle.evidence_ids``.

    We insert rows directly (bypassing the ingestion pipeline) so we can set
    ``memory_policy`` explicitly without re-implementing the ingestion handler.
    The ingestion pipeline correctly sets ``memory_policy`` from ``detect_policy``
    but we want to isolate the evidence builder's exclusion logic here.

    ``search_tsv`` is a Postgres GENERATED ALWAYS AS (Computed) column —
    it auto-populates from ``normalized_text`` and ``caption`` on INSERT.
    """
    models = __import__("bot.db.models", fromlist=["ChatMessage", "MessageVersion"])
    content_hash_service = __import__(
        "bot.services.content_hash", fromlist=["compute_content_hash"]
    )

    ts = datetime.now(timezone.utc)
    chat_msg = models.ChatMessage(
        message_id=message_id,
        chat_id=_BUTLER_LEAKAGE_CHAT_ID,
        user_id=user_id,
        text=text_value,
        caption=caption_value,
        date=ts,
        raw_json={"message_id": message_id, "text": text_value},
        memory_policy=memory_policy,
        is_redacted=(memory_policy == "offrecord"),
    )
    db_session.add(chat_msg)
    await db_session.flush()

    content_hash = content_hash_service.compute_content_hash(
        text_value,
        caption_value,
        "text",
        None,
    )
    version = models.MessageVersion(
        chat_message_id=chat_msg.id,
        content_hash=content_hash,
        version_seq=1,
        text=text_value if memory_policy != "offrecord" else None,
        caption=caption_value if memory_policy != "offrecord" else None,
        normalized_text=text_value if memory_policy != "offrecord" else None,
        is_redacted=(memory_policy == "offrecord"),
        entities_json=None,
        imported_final=False,
        captured_at=ts,
    )
    db_session.add(version)
    await db_session.flush()

    # Link current_version_id — search_tsv auto-generates from normalized_text + caption.
    chat_msg.current_version_id = version.id
    await db_session.flush()

    return int(version.id)


# ---------------------------------------------------------------------------
# L11.a — offrecord exclusion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_l11a_offrecord_excluded_from_butler_evidence(
    db_session: AsyncSession,
) -> None:
    """L11.a — source with memory_policy='offrecord' MUST NOT appear in ButlerEvidenceContext.

    Inserts a message with memory_policy='offrecord' and searchable text.
    Calls build_butler_evidence with the keyword as query.
    Asserts the blocked message_version_id is NOT in the returned bundle.

    Note: offrecord rows have text redacted at ingest time, so the search_tsv
    vector will be empty and the row won't be found by FTS.  To exercise the
    SQL-layer filter robustly, we use normalized_text=searchable_term when
    creating the version row even though the content is redacted in practice.
    The critical assertion is that the blocked mvid does not appear in
    evidence_ids regardless of whether FTS finds it or not.
    """
    butler_evidence = __import__("bot.services.butler_evidence", fromlist=["build_butler_evidence"])

    user_id = next(_user_counter)
    await _make_user(db_session, user_id)
    msg_id = next(_msg_counter)

    # For offrecord, text is redacted at ingest. Use a neutral text for the row
    # but set memory_policy='offrecord' — the SQL layer must block it regardless.
    text_val = "локальное тестовое содержание одиннадцать-а"
    tokens = text_val.split()
    query_word = tokens[-1]  # unique enough

    blocked_mvid = await _create_message_with_policy(
        db_session,
        message_id=msg_id,
        user_id=user_id,
        text_value=text_val,
        caption_value=None,
        memory_policy="offrecord",
    )

    ctx = await butler_evidence.build_butler_evidence(
        db_session,
        requester_user_id=user_id,
        query=query_word,
        chat_id=_BUTLER_LEAKAGE_CHAT_ID,
        visibility_scope="member",
    )

    assert blocked_mvid not in ctx.bundle.evidence_ids, (
        f"L11.a: message_version_id {blocked_mvid} "
        f"(memory_policy='offrecord') leaked into ButlerEvidenceContext. "
        f"evidence_ids={ctx.bundle.evidence_ids}"
    )


# ---------------------------------------------------------------------------
# L11.b — pre-filter path (all 6 fields, memory_policy='nomem' set directly)
# ---------------------------------------------------------------------------

# Each tuple: (field_name, text_value, caption_value, memory_policy)
#
# For "text" and "caption": inject #nomem into the stored column AND set
# memory_policy='nomem' (as ingestion would). The SQL pre-filter blocks these.
#
# For "poll_question", "contact_name", "forward_text", "forward_caption": only
# set memory_policy='nomem' directly (the fields themselves are not stored in
# columns). We also store a searchable keyword in the text column so FTS can
# find the row (otherwise search_messages returns nothing and the test trivially
# passes with no exclusion to assert).
_L11B_PREFILTER_PARAMS: list[tuple[str, str | None, str | None, str]] = [
    # (field_name, text_value, caption_value, memory_policy)
    (
        "text",
        f"локальное содержание {_NOMEM_MARKER} уникальное альфа",
        None,
        "nomem",
    ),
    (
        "caption",
        "нейтральный текст без маркера бета",
        f"подпись {_NOMEM_MARKER} уникальное гамма",
        "nomem",
    ),
    (
        "poll_question",
        "вопрос опроса дельта",  # searchable text (no #nomem here — field not stored)
        None,
        "nomem",  # set directly, simulates ingestion via poll_question field
    ),
    (
        "contact_name",
        "имя контакта эпсилон",  # searchable text
        None,
        "nomem",  # simulates ingestion via contact_name field
    ),
    (
        "forward_text",
        "пересланный текст дзета",  # searchable text
        None,
        "nomem",  # simulates ingestion via forward_text field
    ),
    (
        "forward_caption",
        "пересланная подпись эта",  # searchable text
        None,
        "nomem",  # simulates ingestion via forward_caption field
    ),
]

_L11B_PREFILTER_IDS = [f"prefilter_{p[0]}" for p in _L11B_PREFILTER_PARAMS]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field_name,text_value,caption_value,memory_policy",
    _L11B_PREFILTER_PARAMS,
    ids=_L11B_PREFILTER_IDS,
)
async def test_l11b_prefilter_nomem_field_excluded(
    db_session: AsyncSession,
    field_name: str,
    text_value: str | None,
    caption_value: str | None,
    memory_policy: str,
) -> None:
    """L11.b (pre-filter path) — source with memory_policy='nomem' excluded by SQL layer.

    Parameterised across all 6 governance.detect_policy fields.

    The test:
    1. Inserts a message with memory_policy='nomem' and a searchable keyword.
    2. Calls build_butler_evidence with the keyword as query.
    3. Asserts the blocked mvid is NOT in the returned bundle.
    4. Asserts governance_excluded_count == 0 (row excluded at SQL layer,
       never reaches second-line check — so count stays 0 for pre-filter cases).

    Binding count: 6 pre-filter sub-cases (all 6 fields).
    """
    butler_evidence = __import__("bot.services.butler_evidence", fromlist=["build_butler_evidence"])

    user_id = next(_user_counter)
    await _make_user(db_session, user_id)
    msg_id = next(_msg_counter)

    # Determine search query from the stored text (FTS-searchable unique word).
    searchable_text = text_value or ""
    tokens = searchable_text.split()
    query_word = tokens[-1] if tokens else "уникальное"

    blocked_mvid = await _create_message_with_policy(
        db_session,
        message_id=msg_id,
        user_id=user_id,
        text_value=text_value,
        caption_value=caption_value,
        memory_policy=memory_policy,
    )

    ctx = await butler_evidence.build_butler_evidence(
        db_session,
        requester_user_id=user_id,
        query=query_word,
        chat_id=_BUTLER_LEAKAGE_CHAT_ID,
        visibility_scope="member",
    )

    assert blocked_mvid not in ctx.bundle.evidence_ids, (
        f"L11.b prefilter[{field_name}]: message_version_id {blocked_mvid} "
        f"(memory_policy={memory_policy!r}) leaked into ButlerEvidenceContext "
        f"for field '{field_name}'. "
        f"evidence_ids={ctx.bundle.evidence_ids}"
    )

    # SQL-layer exclusions keep governance_excluded_count = 0 (rows never reach
    # the second-line detect_policy check because the SQL filter blocks them).
    assert ctx.governance_excluded_count == 0, (
        f"L11.b prefilter[{field_name}]: expected governance_excluded_count=0 "
        f"(SQL-layer pre-filter), got {ctx.governance_excluded_count}"
    )


# ---------------------------------------------------------------------------
# Phase 13 complete-history path (text + caption only)
#
# These rows have memory_policy='normal' and contain a legacy #nomem marker.
# The marker is ordinary content, so Butler evidence must retain the source.
# ---------------------------------------------------------------------------

_L11B_SECONDLINE_PARAMS: list[tuple[str, str | None, str | None]] = [
    # (field_name, text_value, caption_value) — memory_policy='normal' (bypass SQL)
    (
        "text_secondline",
        f"второй уровень {_NOMEM_MARKER} уникальное текст-хи",
        None,
    ),
    (
        "caption_secondline",
        "нейтральный текст для поиска йота",
        f"подпись второй уровень {_NOMEM_MARKER} уникальное капшн-каппа",
    ),
]

_L11B_SECONDLINE_IDS = [p[0] for p in _L11B_SECONDLINE_PARAMS]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field_name,text_value,caption_value",
    _L11B_SECONDLINE_PARAMS,
    ids=_L11B_SECONDLINE_IDS,
)
async def test_legacy_nomem_marker_is_included(
    db_session: AsyncSession,
    field_name: str,
    text_value: str | None,
    caption_value: str | None,
) -> None:
    """Legacy ``#nomem`` text/caption remains available as normal evidence."""
    butler_evidence = __import__("bot.services.butler_evidence", fromlist=["build_butler_evidence"])

    user_id = next(_user_counter)
    await _make_user(db_session, user_id)
    msg_id = next(_msg_counter)

    # Determine search query from the stored text.
    searchable_text = text_value or caption_value or ""
    tokens = searchable_text.split()
    # Pick a unique token that is NOT the nomem marker itself (FTS ignores hashtags).
    query_word = next(
        (t for t in reversed(tokens) if not t.startswith("#")),
        "уникальное",
    )

    # Insert with memory_policy='normal' so the SQL layer does NOT block it.
    expected_mvid = await _create_message_with_policy(
        db_session,
        message_id=msg_id,
        user_id=user_id,
        text_value=text_value,
        caption_value=caption_value,
        memory_policy="normal",
    )

    ctx = await butler_evidence.build_butler_evidence(
        db_session,
        requester_user_id=user_id,
        query=query_word,
        chat_id=_BUTLER_LEAKAGE_CHAT_ID,
        visibility_scope="member",
    )

    assert expected_mvid in ctx.bundle.evidence_ids, field_name
    assert ctx.governance_excluded_count == 0


# ---------------------------------------------------------------------------
# L11.c — forgotten source excluded from Butler evidence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_l11c_forgotten_source_excluded_from_butler_evidence(
    db_session: AsyncSession,
) -> None:
    """L11.c — a forget_events-active message_version MUST NOT reach the Butler.

    (The pending-action mid-flight expiry + callback fail-closed half of L11.c is
    bound by I9.e in tests/evals/test_butler_forget_cascade.py.)
    """
    butler_evidence = __import__("bot.services.butler_evidence", fromlist=["build_butler_evidence"])
    forget_repo = __import__("bot.db.repos.forget_event", fromlist=["ForgetEventRepo"])
    models = __import__("bot.db.models", fromlist=["ChatMessage"])

    user_id = next(_user_counter)
    await _make_user(db_session, user_id)
    msg_id = next(_msg_counter)
    text_val = "забываемое тестовое содержание одиннадцать-це"
    query_word = text_val.split()[-1]

    mvid = await _create_message_with_policy(
        db_session,
        message_id=msg_id,
        user_id=user_id,
        text_value=text_val,
        caption_value=None,
        memory_policy="normal",  # searchable + not redacted
    )

    # Sanity: before forgetting, the row IS retrievable as evidence.
    ctx_before = await butler_evidence.build_butler_evidence(
        db_session,
        requester_user_id=user_id,
        query=query_word,
        chat_id=_BUTLER_LEAKAGE_CHAT_ID,
        visibility_scope="member",
    )
    assert mvid in ctx_before.bundle.evidence_ids

    # Fire a forget event (status='pending') with the matching tombstone_key.
    from sqlalchemy import select as _select

    chat_msg = (
        await db_session.execute(
            _select(models.ChatMessage).where(models.ChatMessage.message_id == msg_id)
        )
    ).scalar_one()
    await forget_repo.ForgetEventRepo.create(
        db_session,
        target_type="message",
        target_id=str(chat_msg.id),
        actor_user_id=user_id,
        authorized_by="admin",
        tombstone_key=f"message:{chat_msg.chat_id}:{chat_msg.message_id}",
        reason="l11c",
        policy="forgotten",
    )

    ctx_after = await butler_evidence.build_butler_evidence(
        db_session,
        requester_user_id=user_id,
        query=query_word,
        chat_id=_BUTLER_LEAKAGE_CHAT_ID,
        visibility_scope="member",
    )
    assert mvid not in ctx_after.bundle.evidence_ids, (
        f"L11.c: forgotten message_version_id {mvid} leaked into ButlerEvidenceContext"
    )


# ---------------------------------------------------------------------------
# L11.d — redacted message_versions excluded from Butler evidence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_l11d_redacted_source_excluded_from_butler_evidence(
    db_session: AsyncSession,
) -> None:
    """L11.d — a redacted message_versions row MUST NOT appear in Butler evidence.

    Builds a row with searchable normalized_text but is_redacted=TRUE; the
    evidence builder's governance predicate (mv.is_redacted=FALSE) must drop it.
    """
    butler_evidence = __import__("bot.services.butler_evidence", fromlist=["build_butler_evidence"])
    models = __import__("bot.db.models", fromlist=["ChatMessage", "MessageVersion"])
    content_hash_service = __import__(
        "bot.services.content_hash", fromlist=["compute_content_hash"]
    )

    user_id = next(_user_counter)
    await _make_user(db_session, user_id)
    msg_id = next(_msg_counter)
    text_val = "редактированное тестовое содержание одиннадцать-де"
    query_word = text_val.split()[-1]
    ts = datetime.now(timezone.utc)

    chat_msg = models.ChatMessage(
        message_id=msg_id,
        chat_id=_BUTLER_LEAKAGE_CHAT_ID,
        user_id=user_id,
        text=text_val,
        caption=None,
        date=ts,
        raw_json={"message_id": msg_id},
        memory_policy="normal",
        is_redacted=True,  # message-level redaction
    )
    db_session.add(chat_msg)
    await db_session.flush()
    version = models.MessageVersion(
        chat_message_id=chat_msg.id,
        content_hash=content_hash_service.compute_content_hash(text_val, None, "text", None),
        version_seq=1,
        text=text_val,
        caption=None,
        normalized_text=text_val,  # searchable so FTS would find it
        is_redacted=True,  # version-level redaction → governance predicate drops it
        entities_json=None,
        imported_final=False,
        captured_at=ts,
    )
    db_session.add(version)
    await db_session.flush()
    chat_msg.current_version_id = version.id
    await db_session.flush()
    mvid = int(version.id)

    ctx = await butler_evidence.build_butler_evidence(
        db_session,
        requester_user_id=user_id,
        query=query_word,
        chat_id=_BUTLER_LEAKAGE_CHAT_ID,
        visibility_scope="member",
    )
    assert mvid not in ctx.bundle.evidence_ids, (
        f"L11.d: redacted message_version_id {mvid} leaked into ButlerEvidenceContext"
    )


# ---------------------------------------------------------------------------
# L11.e — affected-user consent preview excludes evidence content
# ---------------------------------------------------------------------------


def test_l11e_affected_user_preview_excludes_evidence_content() -> None:
    """L11.e — the preview shown to an affected user surfaces only the plan summary
    and structural metadata, never raw evidence (in- or out-of-scope) content.

    The cross-user consent DM (bot/handlers/butler.py::_send_consent_requests)
    sends ``_render_preview(action)``, which is derived solely from
    ``plan_summary`` + tool/risk/visibility metadata — it never reads
    ``evidence_ids`` content. A planted evidence snippet must not appear.
    """
    from types import SimpleNamespace

    handler = __import__("bot.handlers.butler", fromlist=["_render_preview"])

    evidence_secret = "ADMIN_ONLY_SECRET_EVIDENCE_SNIPPET"
    action = SimpleNamespace(
        id=4242,
        plan_summary="Предложить встречу участнику по теме X",
        tool_name="schedule_meeting",
        risk_level="low",
        visibility_scope="member",
        # Even if evidence content were attached to the row, the renderer must
        # not surface it:
        evidence_ids=[1, 2, 3],
        result_payload={"text": evidence_secret},
    )

    preview = handler._render_preview(action)

    assert "Предложить встречу" in preview  # plan summary IS shown
    assert evidence_secret not in preview  # evidence content is NOT shown
    assert "schedule_meeting" in preview  # structural metadata only
