"""Forget cascade worker (T3-04, issue #96).

Background worker that drives ``forget_events`` rows through the cascade defined in
HANDOFF.md §10. Phase 3 skeleton: only Phase 1 layers (``chat_messages`` and
``message_versions``) execute; layers whose tables do not yet exist (``message_entities``,
``message_links``, ``attachments``, ``fts_rows``) are recorded as ``skipped`` so the
cascade is forward-compatible — when those tables land in later phases, the cascade
order is already wired and only the per-layer functions need filling in.

Durability invariants (binding for every code path here):

* **Idempotent claim.** ``ForgetEventRepo.mark_status(status='processing')`` is an atomic
  ``UPDATE ... WHERE status='pending' RETURNING``; double-claim is impossible. A
  losing claimant gets ``ValueError`` and skips the row.
* **Restart-safe.** Per-layer progress is checkpointed via
  ``ForgetEventRepo.update_cascade_status``. After a crash mid-cascade, the next
  worker run reads ``cascade_status`` and skips already-completed layers.
* **Per-event isolation.** Each event's cascade is wrapped in its own try/except.
  A failure in one event marks that row ``failed`` but does NOT halt other events
  in the batch.
* **Irreversibility doctrine** (HANDOFF.md §10, ADR-0003). The cascade for
  ``chat_messages`` NULLs ``text``, ``caption``, ``raw_json`` and sets
  ``is_redacted=True``, ``memory_policy='forgotten'``. The cascade for
  ``message_versions`` NULLs ``text``, ``caption``, ``normalized_text``,
  ``entities_json`` and sets ``is_redacted=True``. ``content_hash`` is intentionally
  preserved so prior citations resolve; the redacted flag tells consumers to skip
  the body.

Production wiring is gated by feature flag ``memory.forget.cascade_worker.enabled``
(default OFF) — the scheduler reads the flag every tick and no-ops when off. This
mirrors the AUTHORIZED_SCOPE pattern for new ingestion-style paths.
"""

from __future__ import annotations

import hashlib
import logging
import struct
from typing import Any

from aiogram import Bot
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.engine import async_session
from bot.db.models import (
    ChatMessage,
    LlmUsageLedger,
    MessageVersion,
    QaTrace,
)
from bot.db.repos.feature_flag import FeatureFlagRepo
from bot.db.repos.forget_event import ForgetEventRepo
from bot.db.repos.llm_synthesis_cache import SynthesisCacheRepo

logger = logging.getLogger(__name__)


# ─── Phase 6 advisory-lock derivation (T6-01) ────────────────────────────────


def _p6_mvid_advisory_lock_id(mvid: int) -> int:
    """Derive the Phase 6 advisory lock id for a ``message_version_id``.

    Contract (PHASE6_PLAN.md §5.A.5 + §5.C step 2)::

        mvid_lock_id = signed_int64(first 8 bytes (big-endian)
                                    of sha256(f"p6:mvid:{mvid}"))

    Single source of truth for both call sites:

    * ``apply_forget_event`` orchestrator — acquires
      ``pg_advisory_xact_lock(mvid_lock_id)`` on every affected
      ``message_version_id`` as the FIRST operation in the transaction
      (before the ``forget_events`` INSERT and before any cascade call,
      including ``_cascade_qa_traces_llm`` and
      ``_cascade_card_sources_on_forget``).
    * ``/approve`` transaction protocol — acquires the same lock per
      candidate ``source_message_version_id`` BEFORE the ``forget_events``
      check, closing the H-Cdx-2 race window.

    The namespace prefix ``"p6:mvid:"`` keeps this disjoint from
    ``bot.services.import_chunking._derive_lock_id`` (which hashes raw
    8-byte ``ingestion_run_id``) so an in-progress ``import_apply`` lock
    and a P6 lock for the same numeric id are astronomically unlikely to
    collide (SHA-256 truncated to signed int64; 2^63 possible values).

    Returns a value in the signed-int64 range expected by
    ``pg_advisory_xact_lock(bigint)``.
    """
    payload = f"p6:mvid:{mvid}".encode("ascii")
    digest = hashlib.sha256(payload).digest()
    (lock_id,) = struct.unpack(">q", digest[:8])
    return lock_id


def _p6_event_advisory_lock_id(event_id) -> int:
    """Derive the Phase 6 advisory lock id for a ``forget_event.id`` (Integer).

    ``forget_events.id`` is a plain auto-increment Integer (not UUID) —
    see ``bot/db/models.py:ForgetEvent``. The lock payload is
    ``f"p6:event:{event_id}"`` encoded as ASCII, hashed via SHA-256 →
    signed int64. Accepts any type that str()-formats to a stable value.

    Coarse-grained gate per Codex round 2 CRITICAL #2 fix: the cascade
    orchestrator (``_process_one_event``) takes this lock as the FIRST DB
    action — BEFORE any read of ``chat_messages`` or ``message_versions``
    to resolve affected mvids — so the discipline "lock before any read
    that informs cascade work" is preserved.

    This namespace is DISTINCT from ``_p6_mvid_advisory_lock_id`` (which
    uses the ``"p6:mvid:"`` prefix) — different mvid and event ids are
    astronomically unlikely to collide (SHA-256 → int64; birthday bound
    at ~2^31 items). ``/approve`` does NOT take this lock; cross-transaction
    serialization with /approve remains at the mvid-lock layer, which is
    the contract pinned by the T6-09 collision test.

    Returns a signed-int64 derived from the event_id's str representation.
    """
    payload = f"p6:event:{event_id}".encode("ascii")
    digest = hashlib.sha256(payload).digest()
    (lock_id,) = struct.unpack(">q", digest[:8])
    return lock_id

# Feature flag key — read by the scheduler tick to decide whether to run the worker.
CASCADE_WORKER_FLAG = "memory.forget.cascade_worker.enabled"

# Cascade order per HANDOFF §10. The first two layers are Phase 1 tables; the rest
# are Phase 4+ derived layers whose tables do not yet exist. Skipped layers are
# recorded with ``{"status": "skipped", "reason": "table_not_exists"}`` so the
# cascade_status JSON is forward-compatible: a later phase that adds the table
# replaces the per-layer function and existing rows are reprocessed naturally.
CASCADE_LAYER_ORDER: tuple[str, ...] = (
    "chat_messages",
    "message_versions",
    "qa_traces",
    # Phase 5 / T5-04 layers — ORDER binding per contracts.md §8:
    # cache invalidated FIRST so no forgotten-content cache row outlives the
    # event; traces-llm SECOND so summaries are nulled before ledger NULLs the
    # hashes; ledger LAST to NULL prompt/response hashes (but preserve cost
    # aggregates for budget audit).
    "llm_synthesis_cache",
    "qa_traces_llm",
    "llm_usage_ledger",
    # Phase 7 / T7-05 layer — PHASE7_PLAN.md §5.H:
    # ``digests`` MUST run BEFORE ``card_sources`` so card_source ids are
    # still queryable when the JSONB scan resolves which digests cite each
    # source. Otherwise the card_sources DELETE that follows would orphan
    # the references and the redactor wouldn't know which bullets to mask.
    "digests",
    # Phase 6 / T6-01 layer — PHASE6_PLAN.md §5.A.5 invariant:
    # ``card_sources`` MUST run AFTER ``qa_traces_llm`` so qa_trace summaries
    # are NULL'd before the card_source rows that the citation_ids referenced
    # disappear. The card-sources cascade demotes affected cards to
    # ``card_status='archived'`` when all their sources are forgotten.
    "card_sources",
    "message_entities",
    "message_links",
    "attachments",
    "fts_rows",
)

# Layers that apply only to specific target_types. Layers absent from this dict
# apply to all target_types (preserves existing behavior).
_LAYER_APPLICABLE_TARGET_TYPES: dict[str, frozenset[str]] = {
    # Phase 1 layers operate on chat_messages.id / chat_messages.user_id; the
    # 'message_hash' target is handled by Phase 5 layers only (which join via
    # content_hash). Restricting them here makes the dispatcher route
    # message_hash through Phase 5 layers without raising in Phase 1.
    "chat_messages": frozenset({"message", "user"}),
    "message_versions": frozenset({"message", "user"}),
    "qa_traces": frozenset({"user"}),  # user-targeted forgets only
    # Phase 5 / T5-04 layers — applicability per contracts.md §8:
    "llm_synthesis_cache": frozenset({"message", "message_hash", "user"}),
    "qa_traces_llm": frozenset({"message", "message_hash", "user"}),
    "llm_usage_ledger": frozenset({"user"}),
    # Phase 6 / T6-01 (PHASE6_PLAN.md §5.A.5): card_sources point at
    # message_versions, so the same target_types as the Phase 5
    # message-level layers apply. user-level forget propagates because
    # card_sources can reference any message_version owned by the user.
    "card_sources": frozenset({"message", "message_hash", "user"}),
}


async def _cascade_chat_messages(session: AsyncSession, event) -> int:
    """NULL content fields on ``chat_messages`` rows targeted by this forget_event.

    Per HANDOFF.md §10 irreversibility doctrine, the cascade overwrites:
    - ``text``, ``caption``, ``raw_json`` → ``NULL``
    - ``is_redacted`` → ``True``
    - ``memory_policy`` → ``'forgotten'``

    Returns the number of rows affected.

    Supported target_types:
    - ``message``: single row by ``ChatMessage.id == int(target_id)``.
    - ``user``: all rows by ``ChatMessage.user_id == CAST(target_id AS BIGINT)``
      (User.id == telegram_id per codebase invariant).

    Other target types (``message_hash``, ``export``) are reserved for future
    streams (#97, #105); the caller (``_process_one_event``) must NOT invoke
    this function for them — it skips them before reaching ``_LAYER_FUNCS``.
    """
    if event.target_id is None:
        raise ValueError(
            f"forget_event target_type={event.target_type!r} requires a non-None target_id"
        )

    if event.target_type == "message":
        try:
            cm_id = int(event.target_id)
        except (TypeError, ValueError):
            raise ValueError(
                f"forget_event target_type='message' requires integer target_id; "
                f"got {event.target_id!r}"
            )
        stmt = (
            update(ChatMessage)
            .where(ChatMessage.id == cm_id)
            .values(
                text=None,
                caption=None,
                raw_json=None,
                is_redacted=True,
                memory_policy="forgotten",
            )
        )
        result = await session.execute(stmt)
        await session.flush()
        return result.rowcount or 0

    if event.target_type == "user":
        try:
            telegram_id = int(event.target_id)
        except (TypeError, ValueError):
            raise ValueError(
                f"forget_event target_type='user' requires integer target_id (telegram_id); "
                f"got {event.target_id!r}"
            )
        stmt = (
            update(ChatMessage)
            .where(ChatMessage.user_id == telegram_id)
            .values(
                text=None,
                caption=None,
                raw_json=None,
                is_redacted=True,
                memory_policy="forgotten",
            )
        )
        result = await session.execute(stmt)
        await session.flush()
        return result.rowcount or 0

    # Should not reach here: _process_one_event guards unsupported target_types
    # before calling _LAYER_FUNCS.  Raise so a regression surfaces immediately.
    raise ValueError(
        f"_cascade_chat_messages: unsupported target_type={event.target_type!r}"
    )


async def _cascade_message_versions(session: AsyncSession, event) -> int:
    """NULL content fields on ``message_versions`` rows whose ``chat_message_id``
    matches this forget_event's target.

    Per HANDOFF.md §10 irreversibility doctrine, the cascade overwrites:
    - ``text``, ``caption``, ``normalized_text``, ``entities_json`` → ``NULL``
    - ``is_redacted`` → ``True``

    ``content_hash`` is intentionally PRESERVED so prior citations remain
    resolvable; the redacted flag tells consumers to skip the body. Matches the
    T1-14 hotfix invariant (closes Codex Phase 1 final-review CRITICAL
    PRIVACY_LEAK_CLASS_4).

    Returns the number of rows affected.

    Supported target_types:
    - ``message``: versions for the single chat_messages row.
    - ``user``: versions for ALL chat_messages rows owned by the user.
      Because ``_cascade_chat_messages`` NULLs text/caption/raw_json but does
      NOT touch ``user_id``, the subquery ``WHERE user_id = telegram_id``
      still resolves correctly.
    """
    if event.target_id is None:
        raise ValueError(
            f"forget_event target_type={event.target_type!r} requires a non-None target_id"
        )

    if event.target_type == "message":
        try:
            cm_id = int(event.target_id)
        except (TypeError, ValueError):
            raise ValueError(
                f"forget_event target_type='message' requires integer target_id; "
                f"got {event.target_id!r}"
            )
        stmt = (
            update(MessageVersion)
            .where(MessageVersion.chat_message_id == cm_id)
            .values(
                text=None,
                caption=None,
                normalized_text=None,
                entities_json=None,
                is_redacted=True,
            )
        )
        result = await session.execute(stmt)
        await session.flush()
        return result.rowcount or 0

    if event.target_type == "user":
        try:
            telegram_id = int(event.target_id)
        except (TypeError, ValueError):
            raise ValueError(
                f"forget_event target_type='user' requires integer target_id (telegram_id); "
                f"got {event.target_id!r}"
            )
        # Select version ids via subquery on chat_messages.user_id. The previous
        # layer NULLed text/caption/raw_json but user_id column is untouched, so
        # this subquery resolves correctly regardless of layer order.
        from sqlalchemy import select as sa_select

        stmt = (
            update(MessageVersion)
            .where(
                MessageVersion.chat_message_id.in_(
                    sa_select(ChatMessage.id).where(ChatMessage.user_id == telegram_id)
                )
            )
            .values(
                text=None,
                caption=None,
                normalized_text=None,
                entities_json=None,
                is_redacted=True,
            )
        )
        result = await session.execute(stmt)
        await session.flush()
        return result.rowcount or 0

    # Should not reach here: _process_one_event guards unsupported target_types.
    raise ValueError(
        f"_cascade_message_versions: unsupported target_type={event.target_type!r}"
    )


async def _cascade_qa_traces(session: AsyncSession, event) -> int:
    """Redact qa_traces.query_text for the forget_event's user.

    Per ADR-0003, preserves the row (audit) but nulls query content and flips
    query_redacted=True. Idempotent: re-runs find no un-redacted rows for the user.

    Pre-condition: dispatcher has verified event.target_type == 'user' via
    _LAYER_APPLICABLE_TARGET_TYPES. This function does not re-validate.
    """
    if event.target_id is None:
        raise ValueError("forget_event target_type='user' requires non-None target_id")

    try:
        telegram_id = int(event.target_id)
    except (TypeError, ValueError):
        raise ValueError(
            f"target_id must be integer telegram_id; got {event.target_id!r}"
        )

    stmt = (
        update(QaTrace)
        .where(
            QaTrace.user_tg_id == telegram_id,
            QaTrace.query_redacted == False,  # idempotency guard  # noqa: E712
        )
        .values(
            query_text=None,
            query_redacted=True,
        )
    )
    result = await session.execute(stmt)
    await session.flush()
    return result.rowcount or 0


# ─── Phase 5 / T5-04 cascade layers (contracts.md §8) ───────────────────────


async def _cascade_llm_synthesis_cache(session: AsyncSession, event) -> int:
    """Invalidate ``llm_synthesis_cache`` rows that cite forgotten message_versions.

    Runs FIRST among Phase 5 layers so no forgotten-content cache row survives
    the cascade (invariant #9 — tombstones durable).

    target_types:

    * ``message`` — resolve the chat_message's current_version_id → call
      ``SynthesisCacheRepo.invalidate_by_citation`` once.
    * ``message_hash`` — resolve all message_version_ids sharing the
      ``content_hash`` → invalidate each.
    * ``user`` — bulk DELETE every cache row citing ANY of the user's
      message_version_ids via JSONB containment.

    Returns total rowcount across all invalidations.
    """
    if event.target_id is None:
        raise ValueError(
            f"forget_event target_type={event.target_type!r} requires non-None target_id"
        )

    if event.target_type == "message":
        try:
            cm_id = int(event.target_id)
        except (TypeError, ValueError):
            raise ValueError(
                f"forget_event target_type='message' requires integer target_id; "
                f"got {event.target_id!r}"
            )
        # Resolve current_version_id of the chat_message. The chat_messages
        # cascade layer (which runs earlier) NULLed text/caption/raw_json but
        # left current_version_id intact, so this read still resolves.
        row = (
            await session.execute(
                select(ChatMessage.current_version_id).where(ChatMessage.id == cm_id)
            )
        ).first()
        if row is None or row[0] is None:
            return 0
        return await SynthesisCacheRepo.invalidate_by_citation(
            session, message_version_id=int(row[0])
        )

    if event.target_type == "message_hash":
        # Resolve every message_version_id whose chat_message has the given
        # content_hash. (The hash is stored on message_versions.content_hash;
        # chat_messages does NOT carry content_hash. So we query
        # message_versions directly.)
        target_hash = str(event.target_id)
        version_ids = (
            await session.execute(
                select(MessageVersion.id).where(MessageVersion.content_hash == target_hash)
            )
        ).scalars().all()
        total = 0
        for vid in version_ids:
            total += await SynthesisCacheRepo.invalidate_by_citation(
                session, message_version_id=int(vid)
            )
        return total

    if event.target_type == "user":
        try:
            telegram_id = int(event.target_id)
        except (TypeError, ValueError):
            raise ValueError(
                f"forget_event target_type='user' requires integer target_id (telegram_id); "
                f"got {event.target_id!r}"
            )
        # Resolve user's complete set of message_version_ids and invalidate
        # every cache row whose ``citation_ids`` JSONB array intersects.
        # The chat_messages layer NULLed body fields but the user_id column
        # is preserved, so this query resolves correctly regardless of layer
        # ordering. Iterates per-id (instead of a single DELETE) so the
        # repo's portable PG / SQLite fallback path is reused.
        version_ids = (
            await session.execute(
                select(MessageVersion.id)
                .join(ChatMessage, ChatMessage.id == MessageVersion.chat_message_id)
                .where(ChatMessage.user_id == telegram_id)
            )
        ).scalars().all()
        total = 0
        for vid in version_ids:
            total += await SynthesisCacheRepo.invalidate_by_citation(
                session, message_version_id=int(vid)
            )
        return total

    raise ValueError(
        f"_cascade_llm_synthesis_cache: unsupported target_type={event.target_type!r}"
    )


async def _cascade_qa_traces_llm(session: AsyncSession, event) -> int:
    """NULL ``qa_traces.llm_response_summary`` for traces citing forgotten content.

    Runs SECOND among Phase 5 layers. The Phase 4 ``qa_traces`` cascade
    layer NULLs ``query_text`` for ``target_type='user'`` only; this layer
    extends coverage to per-message and per-message_hash forgets AND to
    the LLM-synthesis response summary.

    target_types:

    * ``message`` — NULL ``llm_response_summary`` on every trace whose
      ``evidence_ids JSONB`` contains the chat_message's current_version_id.
    * ``message_hash`` — NULL on every trace citing ANY version_id matching
      the content_hash.
    * ``user`` — NULL ``llm_response_summary`` for every trace owned by the
      user (query_text already handled by Phase 4 ``_cascade_qa_traces``).

    Returns rowcount.
    """
    if event.target_id is None:
        raise ValueError(
            f"forget_event target_type={event.target_type!r} requires non-None target_id"
        )

    if event.target_type == "message":
        try:
            cm_id = int(event.target_id)
        except (TypeError, ValueError):
            raise ValueError(
                f"target_type='message' requires integer target_id; got {event.target_id!r}"
            )
        # Resolve the chat_message's current_version_id (preserved across the
        # Phase 1 cascade layers).
        row = (
            await session.execute(
                select(ChatMessage.current_version_id).where(ChatMessage.id == cm_id)
            )
        ).first()
        if row is None or row[0] is None:
            return 0
        vid = int(row[0])
        # JSONB containment: NULL summary on traces citing this version.
        stmt = text(
            "UPDATE qa_traces SET llm_response_summary = NULL "
            "WHERE evidence_ids @> CAST(:vid AS jsonb)"
        )
        result = await session.execute(stmt, {"vid": f"[{vid}]"})
        await session.flush()
        return result.rowcount or 0

    if event.target_type == "message_hash":
        target_hash = str(event.target_id)
        version_ids = (
            await session.execute(
                select(MessageVersion.id).where(MessageVersion.content_hash == target_hash)
            )
        ).scalars().all()
        total = 0
        for vid in version_ids:
            stmt = text(
                "UPDATE qa_traces SET llm_response_summary = NULL "
                "WHERE evidence_ids @> CAST(:vid AS jsonb)"
            )
            result = await session.execute(stmt, {"vid": f"[{int(vid)}]"})
            total += result.rowcount or 0
        if total:
            await session.flush()
        return total

    if event.target_type == "user":
        try:
            telegram_id = int(event.target_id)
        except (TypeError, ValueError):
            raise ValueError(
                f"target_type='user' requires integer target_id (telegram_id); "
                f"got {event.target_id!r}"
            )
        stmt = (
            update(QaTrace)
            .where(QaTrace.user_tg_id == telegram_id)
            .values(llm_response_summary=None)
        )
        result = await session.execute(stmt)
        await session.flush()
        return result.rowcount or 0

    raise ValueError(
        f"_cascade_qa_traces_llm: unsupported target_type={event.target_type!r}"
    )


async def _cascade_llm_usage_ledger(session: AsyncSession, event) -> int:
    """NULL ``prompt_hash`` + ``response_hash`` for the user's ledger rows.

    Runs LAST among Phase 5 layers. Touches ONLY the PII hash fields;
    cost / token / latency aggregates are PRESERVED for budget audit.

    target_type:

    * ``user`` — NULL both hashes for ledger rows where ``qa_trace_id IN
      (subquery: user's traces)``.
    * ``message`` / ``message_hash`` — no-op (filtered upstream via
      ``_LAYER_APPLICABLE_TARGET_TYPES``).

    Migration 025 relaxed ``prompt_hash`` to NULLable specifically for
    this layer.
    """
    if event.target_type != "user":
        raise ValueError(
            f"_cascade_llm_usage_ledger: only target_type='user' is supported; "
            f"got {event.target_type!r}"
        )
    if event.target_id is None:
        raise ValueError("forget_event target_type='user' requires non-None target_id")

    try:
        telegram_id = int(event.target_id)
    except (TypeError, ValueError):
        raise ValueError(
            f"target_id must be integer telegram_id; got {event.target_id!r}"
        )

    subq = select(QaTrace.id).where(QaTrace.user_tg_id == telegram_id)
    stmt = (
        update(LlmUsageLedger)
        .where(LlmUsageLedger.qa_trace_id.in_(subq))
        .values(prompt_hash=None, response_hash=None)
    )
    result = await session.execute(stmt)
    await session.flush()
    return result.rowcount or 0


# ─── Phase 6 / T6-01 cascade layer (PHASE6_PLAN.md §5.A.5) ───────────────────

# Orchestrator-level ``pg_advisory_xact_lock(_p6_mvid_advisory_lock_id(mvid))``
# is acquired by ``_process_one_event`` BEFORE this cascade runs (T6-04 /
# PHASE6_PLAN §5.A.5 step 1). The lock is the canonical serialization point
# with ``/approve``; the ORM-level ``SELECT ... FOR UPDATE`` here remains as
# defence-in-depth for stragglers inserted under a previous advisory lock
# that has since released.


async def _cascade_card_sources_on_forget(session: AsyncSession, event) -> int:
    """Demote knowledge_cards whose sources are forgotten (PHASE6_PLAN §5.A.5).

    For every ``message_version_id`` covered by this forget_event:

    1. ``SELECT ... FOR UPDATE`` on every affected ``knowledge_cards`` row
       (defence-in-depth row-level lock; primary serialization with
       ``/approve`` runs at the ``apply_forget_event`` orchestrator level
       via ``pg_advisory_xact_lock(_p6_mvid_advisory_lock_id(mvid))`` — the
       advisory lock is taken there, NOT here).
    2. DELETE every ``card_sources`` row whose ``message_version_id``
       matches the forgotten version.
    3. For each affected ``card_id``, recount remaining ``card_sources``
       rows.
    4. If remaining count == 0, demote the card:
       ``card_status='archived'``,
       ``archived_reason='all sources forgotten via cascade <forget_event_id>'``,
       ``updated_at=now()``.
    5. If remaining count > 0, leave the card alone (source unlinked,
       partial attribution; flag for later admin review — out of scope).

    Returns the number of ``card_sources`` rows deleted (not the number of
    cards demoted).

    **Privacy invariant:** ``archived_reason`` MUST NOT contain quoted
    body content from the forgotten message — only the
    ``forget_event_id`` reference. Body content is already redacted at
    the ``message_versions`` cascade layer (which runs much earlier in
    ``CASCADE_LAYER_ORDER``), but this function never reads the body in
    the first place.

    Run-order invariant: this function MUST run AFTER ``_cascade_qa_traces_llm``
    (see ``CASCADE_LAYER_ORDER``) — qa_traces summaries that referenced the
    cited card_source via ``citation_ids`` are NULL'd first, so removing
    the card_source row cannot leave a dangling citation in a populated
    summary.

    Target type semantics:

    * ``message`` — resolve the ``chat_message.current_version_id`` and
      treat it as a single mvid. Earlier layers preserve
      ``current_version_id`` so this lookup still succeeds.
    * ``message_hash`` — resolve every ``message_version_id`` whose
      ``content_hash`` matches; demote each affected card per the rules
      above.
    * ``user`` — resolve every ``message_version_id`` belonging to any of
      the user's chat_messages; demote each affected card per the rules
      above.
    """
    # Local import keeps the module import surface small for callers that
    # only need Phase 1 layers (e.g. early-startup paths).
    from bot.db.models import CardSource, KnowledgeCard

    if event.target_id is None:
        raise ValueError(
            f"forget_event target_type={event.target_type!r} requires non-None target_id"
        )

    # ── Step A: resolve the set of message_version_ids covered by this event ──
    if event.target_type == "message":
        try:
            cm_id = int(event.target_id)
        except (TypeError, ValueError):
            raise ValueError(
                f"target_type='message' requires integer target_id; "
                f"got {event.target_id!r}"
            )
        row = (
            await session.execute(
                select(ChatMessage.current_version_id).where(
                    ChatMessage.id == cm_id
                )
            )
        ).first()
        if row is None or row[0] is None:
            return 0
        mvids = [int(row[0])]

    elif event.target_type == "message_hash":
        target_hash = str(event.target_id)
        mvids = [
            int(v)
            for v in (
                await session.execute(
                    select(MessageVersion.id).where(
                        MessageVersion.content_hash == target_hash
                    )
                )
            ).scalars().all()
        ]
        if not mvids:
            return 0

    elif event.target_type == "user":
        try:
            telegram_id = int(event.target_id)
        except (TypeError, ValueError):
            raise ValueError(
                f"target_type='user' requires integer target_id (telegram_id); "
                f"got {event.target_id!r}"
            )
        mvids = [
            int(v)
            for v in (
                await session.execute(
                    select(MessageVersion.id)
                    .join(ChatMessage, ChatMessage.id == MessageVersion.chat_message_id)
                    .where(ChatMessage.user_id == telegram_id)
                )
            ).scalars().all()
        ]
        if not mvids:
            return 0

    else:
        raise ValueError(
            f"_cascade_card_sources_on_forget: unsupported target_type="
            f"{event.target_type!r}"
        )

    # ── Step B: gather affected card_ids and lock them FOR UPDATE ────────────
    affected_card_ids = [
        cid
        for cid in (
            await session.execute(
                select(CardSource.card_id)
                .where(CardSource.message_version_id.in_(mvids))
                .distinct()
            )
        ).scalars().all()
    ]
    if not affected_card_ids:
        return 0

    # Defence-in-depth: row-lock affected cards before mutating card_sources.
    # PHASE6_PLAN.md §5.A.5 step 1 — primary serialization is at the
    # apply_forget_event orchestrator level, but this guards stragglers
    # inserted under a PREVIOUS advisory lock that has since released.
    await session.execute(
        select(KnowledgeCard)
        .where(KnowledgeCard.id.in_(affected_card_ids))
        .with_for_update()
    )

    # ── Step C: DELETE matching card_sources rows ────────────────────────────
    delete_result = await session.execute(
        delete(CardSource).where(CardSource.message_version_id.in_(mvids))
    )
    deleted_count = delete_result.rowcount or 0
    await session.flush()

    # ── Step D: per-card recount + demote when remaining == 0 ────────────────
    # PHASE6_PLAN.md §5.A.5 privacy invariant: archived_reason carries only
    # the forget_event_id reference, never quoted body content.
    archived_reason = (
        f"all sources forgotten via cascade {event.id}"
    )
    for card_id in affected_card_ids:
        remaining = (
            await session.execute(
                select(CardSource).where(CardSource.card_id == card_id).limit(1)
            )
        ).scalar()
        if remaining is None:
            # All sources gone → demote.
            await session.execute(
                update(KnowledgeCard)
                .where(KnowledgeCard.id == card_id)
                .values(
                    card_status="archived",
                    archived_reason=archived_reason,
                    updated_at=func.now(),
                )
            )
    await session.flush()
    return deleted_count


async def _cascade_digests(session: AsyncSession, event) -> int:
    """T7-05 / Phase 7 — single merged digests layer.

    Detect digests citing the tombstoned source and redact them in one
    transaction. Handles both ``kind='message_version'`` and
    ``kind='card_source'`` citations. Runs BEFORE ``_cascade_card_sources``
    so the card_source rows still exist when the JSONB scan runs.

    Per-event isolation: on any failure inside redact_digest_for_forget,
    log + continue (forget event proceeds to next layer). The
    statement_timeout guard inside the redactor handles the publisher-race
    case (5s blocking wait then log-and-skip).
    """
    from sqlalchemy import bindparam
    from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
    from sqlalchemy.types import BigInteger, String

    mvids = await _resolve_affected_mvids(session, event)
    if not mvids:
        return 0

    # Resolve card_source ids whose message_version is in the affected set.
    # This MUST happen BEFORE the card_sources cascade layer DELETEs the rows.
    cs_rows = await session.execute(
        text(
            "SELECT id::text FROM card_sources WHERE message_version_id = ANY(:mvids)"
        ).bindparams(bindparam("mvids", type_=PG_ARRAY(BigInteger))),
        {"mvids": list(mvids)},
    )
    affected_cs_ids = {r[0] for r in cs_rows}

    # JSONB scan for digests citing either kind.
    digest_rows = await session.execute(
        text(
            "SELECT d.id FROM digests d "
            "WHERE d.status IN "
            "('draft','posting','posted','redacted','redacted_edit_failed') "
            "  AND EXISTS ("
            "      SELECT 1 FROM jsonb_array_elements(d.citations) AS elem "
            "      WHERE ("
            "          (elem->>'kind') = 'message_version' "
            "          AND (elem->>'id')::bigint = ANY(:mvids) "
            "      ) OR ("
            "          (elem->>'kind') = 'card_source' "
            "          AND (elem->>'id') = ANY(:cs_ids) "
            "      ) "
            "  ) "
            "ORDER BY d.id"
        ).bindparams(
            bindparam("mvids", type_=PG_ARRAY(BigInteger)),
            bindparam("cs_ids", type_=PG_ARRAY(String)),
        ),
        {"mvids": list(mvids), "cs_ids": list(affected_cs_ids)},
    )
    affected_digest_ids = [r[0] for r in digest_rows]
    if not affected_digest_ids:
        return 0

    from bot.services.digest_redactor import redact_digest_for_forget

    runtime_bot = getattr(event, "_runtime_bot", None)
    count = 0
    for digest_id in affected_digest_ids:
        try:
            await redact_digest_for_forget(
                session,
                digest_id=digest_id,
                affected_mvids=set(mvids),
                affected_card_source_ids=affected_cs_ids,
                bot=runtime_bot,
            )
            count += 1
        except Exception:
            logger.exception(
                "_cascade_digests: redact failed for digest_id=%s", digest_id
            )
    return count


# Map layer name → cascade function. Layers absent from this map are recorded as
# skipped. When a future phase adds a layer's table, add its function here.
_LAYER_FUNCS: dict[str, Any] = {
    "chat_messages": _cascade_chat_messages,
    "message_versions": _cascade_message_versions,
    "qa_traces": _cascade_qa_traces,
    # T5-04 Phase 5 layers — ORDER binding per contracts.md §8.
    "llm_synthesis_cache": _cascade_llm_synthesis_cache,
    "qa_traces_llm": _cascade_qa_traces_llm,
    "llm_usage_ledger": _cascade_llm_usage_ledger,
    # T7-05 Phase 7 layer — PHASE7_PLAN.md §5.H. Runs BEFORE card_sources.
    "digests": _cascade_digests,
    # T6-01 Phase 6 layer — PHASE6_PLAN.md §5.A.5.
    "card_sources": _cascade_card_sources_on_forget,
}


async def _resolve_affected_mvids(session: AsyncSession, event) -> list[int]:
    """Resolve the set of ``message_version_id`` rows touched by a forget_event.

    Pure resolver — does NOT mutate anything. Used by ``_process_one_event`` to
    compute the advisory-lock key set BEFORE the first cascade layer runs
    (PHASE6_PLAN §5.A.5 step 1 / T6-04 acceptance bullet 5).

    Mirrors the resolution logic inline in ``_cascade_card_sources_on_forget``
    (lines 645-700) but lives as a top-level helper so both call sites (P6
    cascade demote + orchestrator lock acquisition) share one source of truth.

    Returns:
        list of mvids touched by the event. Empty for ``target_type='export'``
        (cascade skipped) or when the target chat_message has no current
        version. Empty list ⇒ no lock taken.
    """
    if event.target_type == "export":
        # No mvids to lock; cascade is skipped entirely.
        return []
    if event.target_id is None:
        return []

    if event.target_type == "message":
        try:
            cm_id = int(event.target_id)
        except (TypeError, ValueError):
            return []
        row = (
            await session.execute(
                select(ChatMessage.current_version_id).where(
                    ChatMessage.id == cm_id
                )
            )
        ).first()
        if row is None or row[0] is None:
            return []
        return [int(row[0])]

    if event.target_type == "message_hash":
        target_hash = str(event.target_id)
        rows = (
            await session.execute(
                select(MessageVersion.id).where(
                    MessageVersion.content_hash == target_hash
                )
            )
        ).scalars().all()
        return [int(v) for v in rows]

    if event.target_type == "user":
        try:
            telegram_id = int(event.target_id)
        except (TypeError, ValueError):
            return []
        rows = (
            await session.execute(
                select(MessageVersion.id)
                .join(ChatMessage, ChatMessage.id == MessageVersion.chat_message_id)
                .where(ChatMessage.user_id == telegram_id)
            )
        ).scalars().all()
        return [int(v) for v in rows]

    return []


async def _process_one_event(session: AsyncSession, event, bot: Bot | None = None) -> None:
    """Run the full cascade for a single (already-claimed) forget_event row.

    **Naming note (issue #260 / PHASE6_PLAN.md §5.A.5):** This function IS the
    ``apply_forget_event`` orchestrator described in PHASE6_PLAN.md §5.A.5.
    The name ``_process_one_event`` reflects its role as the inner loop body of
    ``run_cascade_worker_once`` rather than a public entry point. The advisory lock
    is acquired HERE (at the cascade worker tick), NOT at the ``forget_events``
    INSERT, because the H-Cdx-2 closure relies on ``revalidate_sources`` (inside
    ``_cascade_card_sources_on_forget``) seeing committed ``forget_event`` rows
    regardless of the cascade-worker lock state — i.e., the lock discipline is
    "lock before any read that informs cascade work", not "lock at event creation".

    Resumes from ``cascade_status``: layers already marked ``completed`` are
    skipped. Each layer's outcome is checkpointed via ``update_cascade_status``
    BEFORE moving on, so a crash between layers leaves the worker in a state
    the next run can resume from.

    On success, transitions the row to ``status='completed'`` with the final
    cascade_status. On exception, transitions to ``status='failed'`` with the
    exception captured under ``cascade_status['error']``.

    H4 fix (p2-hotfix): the entire cascade is wrapped in ``begin_nested()``
    (SAVEPOINT). This ensures that a real PostgreSQL-level DB error in any layer
    function aborts only this event's sub-transaction, leaving the outer
    transaction valid so ``run_cascade_worker_once`` can continue with the next
    event. Without the savepoint, a DB error would abort the outer transaction and
    subsequent events would fail with InFailedSQLTransactionError.

    T6-04 (PHASE6_PLAN §5.A.5 step 1): the orchestrator acquires
    ``pg_advisory_xact_lock(_p6_mvid_advisory_lock_id(mvid))`` for every
    affected ``message_version_id`` BEFORE any cascade layer fires. Sorted
    iteration ensures the same acquisition order as the ``/approve`` handler
    so no deadlock is possible. Lock auto-releases on COMMIT/ROLLBACK of the
    outer transaction. Closes H-Cdx-2 race window: ``/approve`` and cascade
    cannot interleave on the same mvid set.
    """
    # Thread bot through to _cascade_digests → redact_digest_for_forget so the
    # Telegram edit_message_text side-effect fires in production (F2 fix).
    event._runtime_bot = bot

    # Snapshot current per-layer progress so we can resume.
    cascade_state: dict[str, Any] = dict(event.cascade_status or {})

    # target_types whose cascade is not yet implemented. The event still finalises
    # as 'completed' (all layers explicitly accounted for), but each layer records
    # status='skipped' so the audit trail shows no work was done.
    # Stream Delta #97 (message_hash) and Bravo importer (#105) will fill these in.
    # T5-04: ``message_hash`` is now handled by Phase 5 layers (cache + traces_llm)
    # via JSONB joins on ``content_hash``. Phase 1 layers fall through to
    # ``not_applicable`` (per ``_LAYER_APPLICABLE_TARGET_TYPES``).
    _SKIP_TARGET_TYPES = frozenset({"export"})

    try:
        # T6-04: orchestrator-level advisory lock acquisition. MUST run before
        # the cascade layer loop so /approve cannot land between this lock and
        # the card_sources cascade. Dialect-guarded — SQLite test paths skip
        # the lock (Postgres-only SQL); production always takes it.
        #
        # Codex round 2 CRITICAL #2: the event-level coarse lock is taken
        # FIRST, BEFORE any DB read that informs the cascade work. The
        # previous implementation called ``_resolve_affected_mvids`` (which
        # reads chat_messages / message_versions) BEFORE acquiring any
        # advisory lock, opening a race window where the resolved mvid set
        # could become stale by the time the per-mvid locks were taken.
        #
        # Choice of fix (documented per Codex round 2 request): the
        # event-level coarse lock is the SIMPLEST closure of the discipline
        # gap. It guarantees "lock before any read that informs cascade
        # work" without requiring a heavier user_id or content_hash lock
        # namespace. The actual cross-tx serialization with /approve
        # remains at the per-mvid layer (taken next), where it has always
        # lived — this fix layers a coarse gate on top, not a replacement.
        if (
            session.bind is not None
            and session.bind.dialect.name == "postgresql"
            and event.target_type not in _SKIP_TARGET_TYPES
        ):
            # Step 1: coarse event-level lock — FIRST DB action. Disjoint
            # namespace from per-mvid locks; cannot collide with /approve
            # or with another event's cascade. Skipped for
            # ``target_type='export'`` where the cascade has no work and
            # no resources to serialize against (matches the original
            # "no locks for skipped events" invariant pinned by
            # ``test_process_one_event_no_lock_for_export``).
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": _p6_event_advisory_lock_id(event.id)},
            )

            # Step 2: canonical mvid resolution INSIDE the event-locked
            # region. Now the resolution is the "lock-held read" the cascade
            # uses to derive per-mvid lock keys.
            affected_mvids = await _resolve_affected_mvids(session, event)

            # Step 3: per-mvid advisory locks (sorted) — matches the order
            # /approve acquires them in, preserving deadlock-avoidance.
            for lock_id in sorted(
                _p6_mvid_advisory_lock_id(m) for m in affected_mvids
            ):
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(:lock_id)"),
                    {"lock_id": lock_id},
                )

        for layer in CASCADE_LAYER_ORDER:
            existing = cascade_state.get(layer)
            if isinstance(existing, dict) and existing.get("status") == "completed":
                # Already done in a previous run — skip.
                continue

            if event.target_type in _SKIP_TARGET_TYPES:
                # Uniform reason: target_type_not_supported_yet (regardless of whether
                # the layer's table exists yet — the dominant reason is the outer
                # unsupported target_type). Phase-1 layers include rows=0 for consistency
                # with the supported-target_type completion shape.
                if layer in _LAYER_FUNCS:
                    cascade_state[layer] = {
                        "status": "skipped",
                        "reason": "target_type_not_supported_yet",
                        "rows": 0,
                    }
                else:
                    cascade_state[layer] = {
                        "status": "skipped",
                        "reason": "target_type_not_supported_yet",
                    }
            elif layer in _LAYER_FUNCS:
                # Check per-layer target_type applicability before invoking.
                applicable = _LAYER_APPLICABLE_TARGET_TYPES.get(layer)
                if applicable is not None and event.target_type not in applicable:
                    # Layer is present but does not apply to this target_type.
                    cascade_state[layer] = {
                        "status": "completed",
                        "rows": 0,
                        "reason": "not_applicable",
                    }
                else:
                    # H4 fix (p2-hotfix): wrap each active layer call in a SAVEPOINT so
                    # that a real PostgreSQL-level DB error in one layer aborts only that
                    # layer's sub-transaction, leaving the outer transaction valid. Without
                    # per-layer savepoints, a DB error would abort the outer transaction and
                    # subsequent events in the batch would fail with InFailedSQLTransactionError,
                    # breaking the per-event isolation guarantee.
                    async with session.begin_nested():
                        rows = await _LAYER_FUNCS[layer](session, event)
                    cascade_state[layer] = {"status": "completed", "rows": rows}
            else:
                # Phase 4+ layers — table not yet present in this codebase.
                cascade_state[layer] = {
                    "status": "skipped",
                    "reason": "table_not_exists",
                }

            # Checkpoint after every layer so a crash mid-cascade is recoverable.
            await ForgetEventRepo.update_cascade_status(
                session, event.id, cascade_status=cascade_state
            )

        await ForgetEventRepo.mark_status(
            session, event.id, status="completed", cascade_status=cascade_state
        )
    except Exception as exc:
        cascade_state["error"] = repr(exc)
        # Try to record the failure; if THAT itself fails (e.g. terminal state
        # already set), let the outer exception propagate to the batch loop.
        # The per-layer begin_nested() savepoint was rolled back on exception exit,
        # so the outer transaction is still valid here.
        await ForgetEventRepo.mark_status(
            session, event.id, status="failed", cascade_status=cascade_state
        )
        raise


async def run_cascade_worker_once(
    session: AsyncSession,
    *,
    bot: Bot | None = None,
    batch_size: int = 10,
) -> dict[str, int]:
    """Process up to ``batch_size`` pending forget_events.

    Returns a stats dict ``{claimed, processed, failed}``:
    - ``claimed`` — events successfully transitioned ``pending → processing``
    - ``processed`` — events that completed the cascade (status='completed')
    - ``failed`` — events whose cascade raised (status='failed')

    The function does NOT commit. Caller controls the transaction lifecycle.
    Per-event isolation: a failure in one event's cascade marks ONLY that event
    as ``failed`` and continues with the rest of the batch. The function returns
    normally even if some events failed.

    ``bot`` is threaded through to ``_process_one_event`` so the Telegram
    redaction side-effect (``bot.edit_message_text``) fires for posted digests.
    """
    pending = await ForgetEventRepo.list_pending(session, limit=batch_size)
    stats = {"claimed": 0, "processed": 0, "failed": 0}

    for event in pending:
        # Atomic claim: pending → processing. Race-safe via the repo's
        # WHERE-status filter; if another worker already claimed this row, the
        # repo raises ValueError and we skip silently.
        try:
            claimed = await ForgetEventRepo.mark_status(
                session, event.id, status="processing"
            )
        except ValueError:
            logger.debug(
                "cascade_worker: skipping already-claimed forget_event id=%s",
                event.id,
            )
            continue
        stats["claimed"] += 1

        try:
            await _process_one_event(session, claimed, bot=bot)
            stats["processed"] += 1
        except Exception:
            logger.exception(
                "cascade_worker: cascade failed for forget_event id=%s "
                "(other events in batch unaffected)",
                claimed.id,
            )
            stats["failed"] += 1

    return stats


async def cascade_worker_tick(
    bot: Bot | None = None,
    session: AsyncSession | None = None,
    *,
    batch_size: int = 10,
) -> dict[str, int]:
    """Scheduler entry point for the cascade worker.

    Reads the ``memory.forget.cascade_worker.enabled`` feature flag (default
    OFF) and either runs one batch of ``run_cascade_worker_once`` or returns
    immediately. Mirrors the AUTHORIZED_SCOPE pattern for new ingestion-style
    paths: code lands first, the flag stays OFF in production until the
    implementation is verified end-to-end.

    Two callers:
    - Production: APScheduler tick. ``session`` is None — the function opens
      its own session via ``async_session()`` and commits at the end (same
      pattern as ``process_invite_outbox`` and ``check_intro_refresh``).
      ``bot`` is the first positional arg to match APScheduler ``args=[bot]``
      registration so the Telegram redaction side-effect fires in production.
    - Tests: an explicit ``session`` is passed; the function uses it directly
      WITHOUT committing, so outer-tx isolation is preserved.

    Returns the same stats dict as ``run_cascade_worker_once`` (with all-zero
    counts when the flag is off).
    """
    if session is not None:
        if not await FeatureFlagRepo.get(session, CASCADE_WORKER_FLAG):
            return {"claimed": 0, "processed": 0, "failed": 0}
        return await run_cascade_worker_once(session, bot=bot, batch_size=batch_size)

    # Production path: own session + commit on success.
    async with async_session() as own_session:
        if not await FeatureFlagRepo.get(own_session, CASCADE_WORKER_FLAG):
            return {"claimed": 0, "processed": 0, "failed": 0}
        try:
            stats = await run_cascade_worker_once(
                own_session, bot=bot, batch_size=batch_size
            )
            await own_session.commit()
            return stats
        except Exception:
            await own_session.rollback()
            raise
