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
from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.engine import async_session
from bot.db.models import (
    CardSource,
    ChatMessage,
    LlmUsageLedger,
    MessageVersion,
    QaTrace,
    SemanticQaAttempt,
    SemanticRetrievalTrace,
    SemanticRetrievalUnit,
    SemanticRetrievalUnitSource,
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
    # Issue #404 semantic memory is a derived privacy surface. Purge vectors,
    # provenance, and content-derived audit hashes while message_versions and
    # card_sources still exist, because both are required to resolve every
    # affected message/card revision. The durable quota attempt row survives.
    "semantic_retrieval",
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
    # Phase 9 / T9-07 layers — PHASE9_PLAN.md §T9-07 (I7c order binding):
    # ``wiki_pages`` MUST run AFTER ``digests`` so digest redaction of any
    # wiki-cited sources has already occurred. ``wiki_pages`` transitions
    # page_status and inserts an audit wiki_revisions row.
    # ``wiki_revisions`` MUST run AFTER ``wiki_pages`` and BEFORE
    # ``card_sources``: the page row is already stale/archived before the
    # revision body is masked, and card_source FKs still exist when the
    # wiki_revisions JSONB snapshot is inspected.
    "wiki_pages",
    "wiki_revisions",
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
    # Phase 10 / T10-06 layer:
    # MUST run AFTER card_sources so card_source ids are still
    # resolvable when we look up graph_provenance rows keyed by card.
    # MUST run AFTER message_versions so the affected mvid set is
    # fully resolved before the graph purge begins.
    # Advisory-lock-guarded Postgres writes only in this layer;
    # actual Neo4j bolt DELETE is delegated to graph_purge_worker (async).
    "graph_nodes",
    # Phase 12 / T12-01 layers — PHASE12_PLAN_REFRESH.md §4.4:
    # Butler layers go AFTER graph_nodes at the very tail. Order:
    # confirmations first (carry preview payload hashes — smallest privacy surface),
    # invocations second (carry response payloads / Telegram message ids),
    # actions last (parent audit row — status transitions reflect downstream state).
    # FK dependency: confirmations and invocations reference butler_actions(id)
    # with ON DELETE RESTRICT, so children MUST be processed before parent.
    "butler_action_confirmations",
    "butler_tool_invocations",
    # T12-07: butler_undo_invocations MUST run AFTER butler_tool_invocations
    # (FK constraint: undo rows reference tool invocation ids) and BEFORE
    # butler_actions (parent audit row must survive until child undo rows are redacted).
    "butler_undo_invocations",
    "butler_actions",
)

# Layers that apply only to specific target_types. Layers absent from this dict
# apply to all target_types (preserves existing behavior).
_LAYER_APPLICABLE_TARGET_TYPES: dict[str, frozenset[str]] = {
    # Phase 1 layers operate on chat_messages.id / chat_messages.user_id; the
    # 'message_hash' target is handled by Phase 5 layers only (which join via
    # content_hash). Restricting them here makes the dispatcher route
    # message_hash through Phase 5 layers without raising in Phase 1.
    "chat_messages": frozenset({"message", "user"}),
    "semantic_retrieval": frozenset({"message", "message_hash", "user"}),
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
    # Phase 9 / T9-07 (PHASE9_PLAN.md §T9-07): wiki_pages and wiki_revisions
    # cite message_versions via wiki_page_message_sources and
    # wiki_page_card_sources → card_sources. All three target_types propagate
    # (L9d: message_hash; L9e: user).
    "wiki_pages": frozenset({"message", "message_hash", "user"}),
    "wiki_revisions": frozenset({"message", "message_hash", "user"}),
    # Phase 10 / T10-06 (PHASE10_PLAN.md §5.F): graph_nodes layer applies to
    # all target_types that touch message_versions or knowledge_cards.
    # Invariant #9: the layer MUST run even when memory.graph.projection.enabled
    # is OFF — the flag gates new projections, not purge of already-projected content.
    "graph_nodes": frozenset({"message", "message_hash", "user"}),
    # Phase 12 / T12-01 (PHASE12_PLAN_REFRESH.md §4.4): butler layers apply to
    # all target_types that touch message_versions (evidence_ids) or users
    # (requester_tg_id). message_hash is included because evidence may be
    # keyed by content_hash.
    "butler_action_confirmations": frozenset({"message", "message_hash", "user"}),
    "butler_tool_invocations": frozenset({"message", "message_hash", "user"}),
    "butler_actions": frozenset({"message", "message_hash", "user"}),
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
    raise ValueError(f"_cascade_chat_messages: unsupported target_type={event.target_type!r}")


async def _semantic_affected_mvids(session: AsyncSession, event) -> list[int]:
    """Resolve every message revision covered by a semantic-memory purge.

    Unlike the Phase 6 card resolver, a ``message`` target intentionally returns
    *all* versions of the chat message. Old semantic units are retained after an
    edit for audit/idempotency, so purging only ``current_version_id`` would leave
    stale vectors derived from the forgotten body behind.
    """
    if event.target_id is None:
        raise ValueError(
            f"forget_event target_type={event.target_type!r} requires a non-None target_id"
        )

    if event.target_type == "message":
        try:
            chat_message_id = int(event.target_id)
        except (TypeError, ValueError):
            raise ValueError(
                "forget_event target_type='message' requires integer target_id; "
                f"got {event.target_id!r}"
            )
        statement = select(MessageVersion.id).where(
            MessageVersion.chat_message_id == chat_message_id
        )
    elif event.target_type == "message_hash":
        statement = select(MessageVersion.id).where(
            MessageVersion.content_hash == str(event.target_id)
        )
    elif event.target_type == "user":
        try:
            telegram_id = int(event.target_id)
        except (TypeError, ValueError):
            raise ValueError(
                "forget_event target_type='user' requires integer target_id "
                f"(telegram_id); got {event.target_id!r}"
            )
        statement = (
            select(MessageVersion.id)
            .join(ChatMessage, ChatMessage.id == MessageVersion.chat_message_id)
            .where(ChatMessage.user_id == telegram_id)
        )
    else:
        raise ValueError(f"_semantic_affected_mvids: unsupported target_type={event.target_type!r}")

    result = await session.execute(statement)
    return sorted({int(message_version_id) for message_version_id in result.scalars()})


async def _cascade_semantic_retrieval(session: AsyncSession, event) -> int:
    """Purge issue #404 semantic derived data while preserving budget audit.

    The layer runs before ``message_versions`` and ``card_sources`` are mutated.
    It therefore can resolve message and card source keys, remove every vector
    unit plus its normalized provenance, and redact the hashes of the embedding
    ledger calls that produced those units. For a user forget, query retrieval
    traces are deleted as derived content while the quota-attempt row survives;
    the attempt's provider-ledger hashes are also NULLed. Cost, token, latency,
    outcome, and slot aggregates remain intact, matching the existing Phase 5
    ``NULL hashes, preserve audit aggregates`` policy.
    """
    from sqlalchemy import String, bindparam
    from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY

    affected_mvids = await _semantic_affected_mvids(session, event)
    unit_rows: list[Any] = []
    affected_source_keys = {
        f"message:{message_version_id}" for message_version_id in affected_mvids
    }

    if affected_mvids:
        units_result = await session.execute(
            select(
                SemanticRetrievalUnit.id,
                SemanticRetrievalUnit.llm_usage_ledger_id,
            )
            .join(
                SemanticRetrievalUnitSource,
                SemanticRetrievalUnitSource.unit_id == SemanticRetrievalUnit.id,
            )
            .where(SemanticRetrievalUnitSource.message_version_id.in_(affected_mvids))
            .distinct()
        )
        unit_rows = list(units_result.all())

        # A card may have surfaced through FTS even when its semantic unit was
        # never built. Resolve card keys from canonical provenance as well so
        # no retrieval trace retains a forgotten-source reference.
        card_result = await session.execute(
            select(CardSource.card_id)
            .where(CardSource.message_version_id.in_(affected_mvids))
            .distinct()
        )
        affected_source_keys.update(f"card:{card_id}" for card_id in card_result.scalars())

    unit_ids = {int(row.id) for row in unit_rows}
    ledger_ids = {int(row.llm_usage_ledger_id) for row in unit_rows}
    attempt_ids: set[int] = set()
    qa_trace_ids: set[int] = set()
    parent_chat_message_ids: set[int] = set()

    attempt_filters = []
    if event.target_type == "message":
        # A persisted question can have a QaTrace even when no semantic attempt
        # was admitted (quota denial) or no MessageVersion survived. Resolve it
        # directly from the forget target instead of relying on attempt FKs.
        parent_chat_message_ids.add(int(event.target_id))
    if event.target_type == "message_hash" and affected_mvids:
        parent_result = await session.execute(
            select(MessageVersion.chat_message_id)
            .where(MessageVersion.id.in_(affected_mvids))
            .distinct()
        )
        parent_chat_message_ids.update(
            int(chat_message_id) for chat_message_id in parent_result.scalars()
        )
    if parent_chat_message_ids:
        # The forgotten message can itself be a semantic question. It is
        # deliberately excluded from retrieval results, so source-key matching
        # cannot find its raw query or provider ledgers.
        attempt_filters.append(
            SemanticQaAttempt.source_chat_message_id.in_(parent_chat_message_ids)
        )
    if event.target_type == "user":
        attempt_filters.append(SemanticQaAttempt.user_tg_id == int(event.target_id))

    qa_trace_filters = []
    if parent_chat_message_ids:
        qa_trace_filters.append(QaTrace.source_chat_message_id.in_(parent_chat_message_ids))
    if event.target_type == "user":
        qa_trace_filters.append(QaTrace.user_tg_id == int(event.target_id))
    if qa_trace_filters:
        direct_trace_result = await session.execute(
            select(QaTrace.id).where(or_(*qa_trace_filters))
        )
        qa_trace_ids.update(int(trace_id) for trace_id in direct_trace_result.scalars())

    if attempt_filters:
        attempts_result = await session.execute(
            select(
                SemanticQaAttempt.id,
                SemanticQaAttempt.qa_trace_id,
                SemanticQaAttempt.embedding_llm_call_id,
                SemanticQaAttempt.synthesis_llm_call_id,
            ).where(or_(*attempt_filters))
        )
        for row in attempts_result.all():
            attempt_ids.add(int(row.id))
            if row.qa_trace_id is not None:
                qa_trace_ids.add(int(row.qa_trace_id))
            if row.embedding_llm_call_id is not None:
                ledger_ids.add(int(row.embedding_llm_call_id))
            if row.synthesis_llm_call_id is not None:
                ledger_ids.add(int(row.synthesis_llm_call_id))

    qa_traces_redacted = 0
    if qa_trace_ids:
        qa_result = await session.execute(
            update(QaTrace)
            .where(QaTrace.id.in_(qa_trace_ids))
            .values(
                query_text=None,
                query_redacted=True,
                llm_response_summary=None,
                llm_response_redacted=True,
            )
            .returning(QaTrace.id)
        )
        qa_traces_redacted = len(qa_result.scalars().all())

    traces_deleted = 0
    if attempt_ids:
        trace_result = await session.execute(
            delete(SemanticRetrievalTrace)
            .where(SemanticRetrievalTrace.attempt_id.in_(attempt_ids))
            .returning(SemanticRetrievalTrace.id)
        )
        traces_deleted += len(trace_result.scalars().all())

    if affected_source_keys:
        # candidate_ranks is a JSON object keyed by ``message:<mvid>`` or
        # ``card:<uuid>``; result_source_ids is a JSON array of the same keys.
        # Delete the derived trace instead of replacing non-null JSON/hash
        # columns with an invented sentinel.
        trace_statement = text(
            "DELETE FROM semantic_retrieval_traces AS trace "
            "WHERE EXISTS ("
            "  SELECT 1 "
            "  FROM jsonb_array_elements_text(trace.result_source_ids::jsonb) "
            "       AS result_key(value) "
            "  WHERE result_key.value = ANY(:source_keys)"
            ") OR EXISTS ("
            "  SELECT 1 "
            "  FROM jsonb_object_keys(trace.candidate_ranks::jsonb) "
            "       AS candidate_key(value) "
            "  WHERE candidate_key.value = ANY(:source_keys)"
            ") "
            "RETURNING trace.attempt_id"
        ).bindparams(bindparam("source_keys", type_=PG_ARRAY(String())))
        trace_result = await session.execute(
            trace_statement,
            {"source_keys": sorted(affected_source_keys)},
        )
        source_trace_attempt_ids = {int(attempt_id) for attempt_id in trace_result.scalars().all()}
        traces_deleted += len(source_trace_attempt_ids)
        if source_trace_attempt_ids:
            synthesis_result = await session.execute(
                select(
                    SemanticQaAttempt.qa_trace_id,
                    SemanticQaAttempt.synthesis_llm_call_id,
                ).where(
                    SemanticQaAttempt.id.in_(source_trace_attempt_ids),
                )
            )
            source_qa_trace_ids: set[int] = set()
            for row in synthesis_result.all():
                if row.qa_trace_id is not None:
                    source_qa_trace_ids.add(int(row.qa_trace_id))
                if row.synthesis_llm_call_id is not None:
                    ledger_ids.add(int(row.synthesis_llm_call_id))
            if source_qa_trace_ids:
                qa_trace_ids.update(source_qa_trace_ids)
                source_qa_result = await session.execute(
                    update(QaTrace)
                    .where(QaTrace.id.in_(source_qa_trace_ids))
                    .values(
                        llm_response_summary=None,
                        llm_response_redacted=True,
                    )
                    .returning(QaTrace.id)
                )
                qa_traces_redacted += len(source_qa_result.scalars().all())

    # Durable provider placeholders are linked to qa_trace_id before HTTP
    # dispatch, while attempt-level embedding/synthesis FKs are attached only
    # after a result exists. Discover both in-flight calls through the trace so
    # a concurrent forget can irreversibly redact their hashes as well.
    if qa_trace_ids:
        trace_ledger_result = await session.execute(
            select(LlmUsageLedger.id).where(LlmUsageLedger.qa_trace_id.in_(qa_trace_ids))
        )
        ledger_ids.update(int(value) for value in trace_ledger_result.scalars())

    ledger_hashes_redacted = 0
    if ledger_ids:
        ledger_result = await session.execute(
            update(LlmUsageLedger)
            .where(
                LlmUsageLedger.id.in_(ledger_ids),
                (LlmUsageLedger.prompt_hash.is_not(None))
                | (LlmUsageLedger.response_hash.is_not(None)),
            )
            .values(prompt_hash=None, response_hash=None)
            .returning(LlmUsageLedger.id)
        )
        ledger_hashes_redacted = len(ledger_result.scalars().all())

    provenance_deleted = 0
    units_deleted = 0
    if unit_ids:
        provenance_result = await session.execute(
            delete(SemanticRetrievalUnitSource)
            .where(SemanticRetrievalUnitSource.unit_id.in_(unit_ids))
            .returning(SemanticRetrievalUnitSource.unit_id)
        )
        provenance_deleted = len(provenance_result.scalars().all())
        units_result = await session.execute(
            delete(SemanticRetrievalUnit)
            .where(SemanticRetrievalUnit.id.in_(unit_ids))
            .returning(SemanticRetrievalUnit.id)
        )
        units_deleted = len(units_result.scalars().all())

    await session.flush()
    logger.info(
        "semantic_forget_cascade_completed",
        extra={
            "forget_event_id": event.id,
            "target_type": event.target_type,
            "affected_message_version_count": len(affected_mvids),
            "semantic_unit_count": units_deleted,
            "semantic_provenance_count": provenance_deleted,
            "semantic_trace_count": traces_deleted,
            "qa_trace_count": qa_traces_redacted,
            "semantic_ledger_hash_count": ledger_hashes_redacted,
        },
    )
    return (
        units_deleted
        + provenance_deleted
        + traces_deleted
        + qa_traces_redacted
        + ledger_hashes_redacted
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
    raise ValueError(f"_cascade_message_versions: unsupported target_type={event.target_type!r}")


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
        raise ValueError(f"target_id must be integer telegram_id; got {event.target_id!r}")

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

    * ``message`` — invalidate every revision of the chat_message.
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

    if event.target_type in {"message", "message_hash", "user"}:
        version_ids = await _semantic_affected_mvids(session, event)
        total = 0
        for vid in version_ids:
            total += await SynthesisCacheRepo.invalidate_by_citation(
                session, message_version_id=int(vid)
            )
        return total

    raise ValueError(f"_cascade_llm_synthesis_cache: unsupported target_type={event.target_type!r}")


async def _cascade_qa_traces_llm(session: AsyncSession, event) -> int:
    """NULL ``qa_traces.llm_response_summary`` for traces citing forgotten content.

    Runs SECOND among Phase 5 layers. The Phase 4 ``qa_traces`` cascade
    layer NULLs ``query_text`` for ``target_type='user'`` only; this layer
    extends coverage to per-message and per-message_hash forgets AND to
    the LLM-synthesis response summary.

    target_types:

    * ``message`` — redact summaries citing any revision of the chat_message.
    * ``message_hash`` — NULL on every trace citing ANY version_id matching
      the content_hash.
    * ``user`` — redact traces owned by the user and traces citing any of the
      user's message revisions.

    Returns rowcount.
    """
    if event.target_id is None:
        raise ValueError(
            f"forget_event target_type={event.target_type!r} requires non-None target_id"
        )

    if event.target_type in {"message", "message_hash", "user"}:
        version_ids = await _semantic_affected_mvids(session, event)
        total = 0
        for vid in version_ids:
            stmt = text(
                "UPDATE qa_traces SET llm_response_summary = NULL, "
                "llm_response_redacted = TRUE "
                "WHERE evidence_ids @> CAST(:vid AS jsonb)"
            )
            result = await session.execute(stmt, {"vid": f"[{int(vid)}]"})
            total += result.rowcount or 0
        if event.target_type == "user":
            owner_result = await session.execute(
                update(QaTrace)
                .where(QaTrace.user_tg_id == int(event.target_id))
                .values(
                    llm_response_summary=None,
                    llm_response_redacted=True,
                )
            )
            total += owner_result.rowcount or 0
        await session.flush()
        return total

    raise ValueError(f"_cascade_qa_traces_llm: unsupported target_type={event.target_type!r}")


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
        raise ValueError(f"target_id must be integer telegram_id; got {event.target_id!r}")

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
                f"target_type='message' requires integer target_id; got {event.target_id!r}"
            )
        row = (
            await session.execute(
                select(ChatMessage.current_version_id).where(ChatMessage.id == cm_id)
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
                    select(MessageVersion.id).where(MessageVersion.content_hash == target_hash)
                )
            )
            .scalars()
            .all()
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
            )
            .scalars()
            .all()
        ]
        if not mvids:
            return 0

    else:
        raise ValueError(
            f"_cascade_card_sources_on_forget: unsupported target_type={event.target_type!r}"
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
        )
        .scalars()
        .all()
    ]
    if not affected_card_ids:
        return 0

    # Defence-in-depth: row-lock affected cards before mutating card_sources.
    # PHASE6_PLAN.md §5.A.5 step 1 — primary serialization is at the
    # apply_forget_event orchestrator level, but this guards stragglers
    # inserted under a PREVIOUS advisory lock that has since released.
    await session.execute(
        select(KnowledgeCard).where(KnowledgeCard.id.in_(affected_card_ids)).with_for_update()
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
    archived_reason = f"all sources forgotten via cascade {event.id}"
    for card_id in affected_card_ids:
        remaining = (
            await session.execute(select(CardSource).where(CardSource.card_id == card_id).limit(1))
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
        text("SELECT id::text FROM card_sources WHERE message_version_id = ANY(:mvids)").bindparams(
            bindparam("mvids", type_=PG_ARRAY(BigInteger))
        ),
        {"mvids": list(mvids)},
    )
    affected_cs_ids = {r[0] for r in cs_rows}

    # JSONB scan for digests citing either kind.
    # T8-04 / Phase 8 §5.D — status filter widened to include the four new
    # review-gate statuses (awaiting_review, approved_for_publish,
    # rejected_by_admin) so the cascade reaches drafts under admin review.
    # MUST mirror digest_redactor._REDACTOR_ELIGIBLE_STATUSES — both must
    # be widened together (§5.K C1 fix: cascade-only widening leaves a
    # silent privacy regression).
    digest_rows = await session.execute(
        text(
            "SELECT d.id FROM digests d "
            "WHERE d.status IN "
            "('draft','awaiting_review','approved_for_publish','posting',"
            " 'posted','redacted','redacted_edit_failed','rejected_by_admin') "
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
            logger.exception("_cascade_digests: redact failed for digest_id=%s", digest_id)
    return count


# ─── Phase 9 / T9-07 cascade layers (PHASE9_PLAN.md §T9-07) ──────────────────


async def _cascade_wiki_pages(session: AsyncSession, event) -> int:
    """Mark wiki pages stale/archived when a forget event covers their cited sources.

    For every wiki page that directly or transitively references a message_version_id
    covered by this forget event:

    1. Set ``page_status='stale'`` (or ``'archived'`` if no valid sources remain).
    2. Set ``public_enabled=false``.
    3. INSERT a ``wiki_revisions`` row with ``edit_reason='forget_cascade'``.

    Returns count of wiki_pages rows modified.

    Run-order invariant: MUST run AFTER ``digests`` and BEFORE ``wiki_revisions``
    (see CASCADE_LAYER_ORDER). The ``wiki_revisions`` layer that follows masks the
    body_markdown of any revision snapshot that overlaps the forgotten mvids.

    Target type semantics:

    * ``message`` — resolve chat_message → current_version_id → affected pages.
    * ``message_hash`` — resolve every mv with matching content_hash → affected pages.
    * ``user`` — resolve every mv owned by user → affected pages.

    Privacy invariant: ``edit_reason`` carries only the string literal
    ``'forget_cascade'`` — never quoted body content from the forgotten message.
    """
    if event.target_id is None:
        raise ValueError(
            f"forget_event target_type={event.target_type!r} requires non-None target_id"
        )

    from sqlalchemy import bindparam
    from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
    from sqlalchemy.types import BigInteger

    _ARRAY_BIGINT = PG_ARRAY(BigInteger)

    mvids = await _resolve_affected_mvids(session, event)
    if not mvids:
        return 0

    # Find all wiki pages that directly cite any of the affected mvids.
    direct_rows = await session.execute(
        text(
            "SELECT DISTINCT wiki_page_id::text FROM wiki_page_message_sources "
            "WHERE message_version_id = ANY(:mvids)"
        ).bindparams(bindparam("mvids", type_=_ARRAY_BIGINT)),
        {"mvids": list(mvids)},
    )
    direct_page_ids: set[str] = {r[0] for r in direct_rows}

    # Find wiki pages that cite any card whose card_sources include an affected mvid.
    transitive_rows = await session.execute(
        text(
            "SELECT DISTINCT wpcs.wiki_page_id::text "
            "FROM wiki_page_card_sources wpcs "
            "JOIN card_sources cs ON cs.card_id = wpcs.card_id "
            "WHERE cs.message_version_id = ANY(:mvids)"
        ).bindparams(bindparam("mvids", type_=_ARRAY_BIGINT)),
        {"mvids": list(mvids)},
    )
    transitive_page_ids: set[str] = {r[0] for r in transitive_rows}

    all_page_ids = direct_page_ids | transitive_page_ids
    if not all_page_ids:
        return 0

    # Import locally to avoid module-level circular import (governance imports models
    # which are also pulled by this module).
    import uuid as _uuid
    from bot.services.wiki_governance import validate_sources

    modified_count = 0
    for page_id_str in all_page_ids:
        # Idempotency guard (Codex CRITICAL #1 second-half fix): if an
        # audit revision row for THIS forget event already exists on this
        # page, skip — re-running the cascade for the same event must NOT
        # duplicate revision rows or counter increments.
        existing_audit = await session.execute(
            text(
                "SELECT 1 FROM wiki_revisions "
                "WHERE wiki_page_id = CAST(:pid AS uuid) "
                "  AND redacted_by_forget_event_id = :event_id "
                "  AND edit_reason = 'forget_cascade' "
                "LIMIT 1"
            ),
            {"pid": page_id_str, "event_id": int(event.id)},
        )
        if existing_audit.scalar() is not None:
            continue

        # Governance-aware status decision (Codex MED #6 fix): consult
        # validate_sources to determine TRULY-remaining-valid sources.
        # This accounts for prior forgets, redactions, offrecord policies,
        # and transitive card invalidity — not just "mvid not in current
        # event's target set". An mvid already redacted from a prior event
        # is still "invalid" for surviving-source purposes; the old
        # mvid-counting query missed that and could leave page_status='stale'
        # when it should have been 'archived'.
        page_uuid = _uuid.UUID(page_id_str)
        gov = await validate_sources(session, page_id=page_uuid)

        # Totals: count direct and transitive (card-linked) sources.
        direct_total_row = await session.execute(
            text(
                "SELECT count(*) FROM wiki_page_message_sources "
                "WHERE wiki_page_id = CAST(:pid AS uuid)"
            ),
            {"pid": page_id_str},
        )
        direct_total = int(direct_total_row.scalar_one() or 0)
        card_total_row = await session.execute(
            text(
                "SELECT count(*) FROM wiki_page_card_sources "
                "WHERE wiki_page_id = CAST(:pid AS uuid)"
            ),
            {"pid": page_id_str},
        )
        card_total = int(card_total_row.scalar_one() or 0)

        # Direct mvids that survive: those NOT in gov.invalid_mvids.
        surviving_direct = direct_total - len(set(gov.invalid_mvids))
        surviving_cards = card_total - len(set(gov.invalid_card_ids))
        # archived when every cited source is invalid (or there are none).
        new_status = "archived" if (surviving_direct <= 0 and surviving_cards <= 0) else "stale"

        # UPDATE wiki_pages: set page_status, public_enabled=false, AND robots
        # policy back to 'noindex' (Codex MED #6 fix part b — cascade flip
        # without robots reset left crawlers seeing the prior indexable hint).
        await session.execute(
            text(
                "UPDATE wiki_pages "
                "SET page_status = :status, public_enabled = false, "
                "    robots_policy = 'noindex', updated_at = now() "
                "WHERE id = CAST(:pid AS uuid)"
            ),
            {"status": new_status, "pid": page_id_str},
        )

        # Derive next revision_seq for this page.
        seq_row = await session.execute(
            text(
                "SELECT COALESCE(MAX(revision_seq), 0) + 1 "
                "FROM wiki_revisions WHERE wiki_page_id = CAST(:pid AS uuid)"
            ),
            {"pid": page_id_str},
        )
        next_seq = seq_row.scalar_one()

        # INSERT audit revision row — pre-masked to prevent forgotten content
        # from leaking via audit log queries (Codex CRITICAL #1 fix).
        # The cascade-created row has empty source snapshots, so _cascade_wiki_revisions
        # overlap filter would never touch it; we must mask at INSERT time.
        redact_text = f"[CONTENT_REDACTED: forget_event_id={event.id}]"
        await session.execute(
            text(
                "INSERT INTO wiki_revisions "
                "(id, wiki_page_id, revision_seq, body_markdown, revision_status, "
                " source_message_version_ids_snapshot, source_card_ids_snapshot, "
                " edit_reason, edited_at, created_at, "
                " redacted_at, redacted_by_forget_event_id) "
                "VALUES "
                "(gen_random_uuid(), CAST(:pid AS uuid), :seq, :body, 'forgotten_redacted', "
                " '[]'::jsonb, '[]'::jsonb, "
                " 'forget_cascade', now(), now(), now(), :event_id)"
            ),
            {
                "pid": page_id_str,
                "seq": next_seq,
                "body": redact_text,
                "event_id": int(event.id),
            },
        )

        modified_count += 1

    await session.flush()
    return modified_count


async def _cascade_wiki_revisions(session: AsyncSession, event) -> int:
    """Mask ``wiki_revisions.body_markdown`` for revisions citing forgotten mvids.

    For every ``wiki_revisions`` row whose
    ``source_message_version_ids_snapshot`` JSONB overlaps the affected mvids
    AND whose ``revision_status != 'forgotten_redacted'``:

    1. Set ``body_markdown = '[CONTENT_REDACTED: forget_event_id={n}]'``.
    2. Set ``revision_status = 'forgotten_redacted'``.
    3. Set ``redacted_at = now()``.
    4. Set ``redacted_by_forget_event_id = event.id``.
    5. Set ``revision_sources_resolved_at = now()``.

    Returns count of wiki_revisions rows modified.

    Idempotency (9.5-C): rows already in ``revision_status='forgotten_redacted'``
    are skipped on re-run — their first-redaction provenance
    (``redacted_by_forget_event_id``, ``redacted_at``, ``body_markdown``) is
    preserved unchanged.  A second cascade pass for the same forget_event logs
    ``already_redacted_skip`` and returns 0.

    Run-order invariant: MUST run AFTER ``wiki_pages`` and BEFORE ``card_sources``
    (see CASCADE_LAYER_ORDER). Running after ``wiki_pages`` ensures the page row
    has already been transitioned to stale/archived before revisions are masked.

    Target type semantics: same as ``_cascade_wiki_pages`` — resolved via
    ``_resolve_affected_mvids``.

    Privacy invariant: the redact string uses only ``forget_event_id`` (an
    integer) — never any user-readable body content.
    """
    import logging

    _log = logging.getLogger(__name__)

    if event.target_id is None:
        raise ValueError(
            f"forget_event target_type={event.target_type!r} requires non-None target_id"
        )

    from sqlalchemy import bindparam
    from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
    from sqlalchemy.types import BigInteger

    _ARRAY_BIGINT = PG_ARRAY(BigInteger)

    mvids = await _resolve_affected_mvids(session, event)
    if not mvids:
        return 0

    redact_text = f"[CONTENT_REDACTED: forget_event_id={event.id}]"

    result = await session.execute(
        text(
            "UPDATE wiki_revisions "
            "SET body_markdown = :redact_text, "
            "    revision_status = 'forgotten_redacted', "
            "    redacted_at = now(), "
            "    redacted_by_forget_event_id = :event_id, "
            "    revision_sources_resolved_at = now() "
            "WHERE revision_status != 'forgotten_redacted' "
            "  AND source_message_version_ids_snapshot @> ANY("
            "    SELECT jsonb_build_array(v::bigint) "
            "    FROM unnest(CAST(:mvids AS bigint[])) AS v"
            ")"
        ).bindparams(bindparam("mvids", type_=_ARRAY_BIGINT)),
        {
            "redact_text": redact_text,
            "event_id": event.id,
            "mvids": list(mvids),
        },
    )
    count = result.rowcount or 0
    if count:
        await session.flush()
    else:
        _log.debug(
            "already_redacted_skip: _cascade_wiki_revisions found 0 rows to redact "
            "(all matching revisions already forgotten_redacted) for forget_event_id=%s",
            event.id,
        )
    return count


# ─── Phase 10 / T10-06 cascade layer ─────────────────────────────────────────


async def _cascade_graph_provenance(session: AsyncSession, event) -> int:
    """Logical cascade for graph projections (T10-06 / PHASE10_PLAN.md §5.F).

    1. Find all graph_provenance rows where (source_table, source_pk) matches
       the forget_event target (via find_by_source). The lookup is by
       source_message_version_id membership in the resolved affected_mvids set.
    2. For each row: mark_inactive (soft-delete purged_at) + atomically enqueue a
       graph_purge_pending row carrying graph_node_key + graph_edge_key.
    3. graph_purge_pending rows drive the async Neo4j DETACH DELETE in a separate
       worker (graph_purge_worker_tick). This layer does NO Neo4j bolt calls.
    4. Fails closed: any DB write failure here means the entire forget cascade
       FAILS for this event — does NOT swallow exceptions.

    Invariant #9 binding: this layer is mandatory and runs even when
    memory.graph.projection.enabled is OFF. The flag gates new projections,
    not purge of already-projected content.

    Returns the count of graph_purge_pending rows enqueued.
    """
    from bot.db.repos.graph_provenance import find_by_source, mark_inactive
    from bot.db.repos.graph_purge_pending import enqueue as enqueue_purge

    # Resolve affected message_version_ids (same helper as other layers).
    mvids = await _resolve_affected_mvids(session, event)

    if not mvids and event.target_type not in ("message", "message_hash", "user"):
        return 0

    # Build the set of (source_table, source_pk) pairs to query.
    # Path 1: source_table='message_versions' — one pair per affected mvid.
    source_pairs: list[tuple[str, str]] = []
    for mvid in mvids:
        source_pairs.append(("message_versions", str(mvid)))

    # Path 2 (CRITICAL-2): source_table='knowledge_cards' — find cards whose
    # graph_provenance rows must also be purged.
    #
    # Design note: this layer runs AFTER card_sources (per CASCADE_LAYER_ORDER).
    # The card_sources layer has already deleted the card_sources rows and
    # archived knowledge_cards whose all sources were forgotten. So we cannot
    # query card_sources here (they are gone). Instead we look at:
    #   graph_provenance WHERE source_table='knowledge_cards'
    #     AND source_card_id IN (archived knowledge_cards)
    #
    # "Archived" cards are those set to card_status='archived' by the
    # _cascade_card_sources_on_forget layer that ran before us. Purging all
    # graph_provenance for archived cards is correct: an archived card's graph
    # representation is stale and must not be served in queries.
    if mvids:
        from bot.db.models import KnowledgeCard, GraphProvenance as _GP
        from sqlalchemy import select as _select

        archived_card_subq = _select(KnowledgeCard.id).where(
            KnowledgeCard.card_status == "archived"
        )
        archived_prov_rows = await session.execute(
            _select(_GP.source_pk)
            .where(_GP.source_table == "knowledge_cards")
            .where(_GP.source_card_id.in_(archived_card_subq))
            .where(_GP.purged_at.is_(None))
        )
        affected_card_ids = [str(r[0]) for r in archived_prov_rows.all()]
        seen_card_ids: set[str] = set()
        for card_id_str in affected_card_ids:
            if card_id_str not in seen_card_ids:
                seen_card_ids.add(card_id_str)
                source_pairs.append(("knowledge_cards", card_id_str))

    if not source_pairs:
        return 0

    enqueued_count = 0
    for source_table, source_pk in source_pairs:
        # find_by_source returns both active and already-purged rows.
        provenance_rows = await find_by_source(
            session, source_table=source_table, source_pk=source_pk
        )
        for prov in provenance_rows:
            # Soft-delete the provenance row (idempotent if already purged).
            # MEDIUM-8 fix: LookupError is re-raised as a cascade failure so the
            # forget_event is NOT silently marked complete with missing purges.
            await mark_inactive(session, prov.id)

            # Enqueue purge_pending (idempotent per provenance row via
            # ON CONFLICT DO NOTHING on the 4-column unique key).
            await enqueue_purge(
                session,
                forget_event_id=int(event.id),
                source_table=source_table,
                source_pk=source_pk,
                graph_node_key=prov.graph_node_key,
                graph_edge_key=prov.graph_edge_key,
                graph_provenance_id=prov.id,
            )
            enqueued_count += 1

    # HIGH-4: soft-delete graph_edges rows for all provenance ids in this batch.
    # Must run in the same transaction before the cascade layer completes.
    if enqueued_count > 0:
        from bot.db.models import GraphEdge
        from sqlalchemy import update
        from datetime import datetime, timezone

        # Collect the provenance ids we just processed.
        processed_prov_ids: list[int] = []
        for source_table, source_pk in source_pairs:
            prov_rows = await find_by_source(
                session, source_table=source_table, source_pk=source_pk
            )
            for prov in prov_rows:
                processed_prov_ids.append(prov.id)

        if processed_prov_ids:
            await session.execute(
                update(GraphEdge)
                .where(GraphEdge.graph_provenance_id.in_(processed_prov_ids))
                .where(GraphEdge.purged_at.is_(None))
                .values(purged_at=datetime.now(tz=timezone.utc))
            )
            await session.flush()

    return enqueued_count


# ─── Phase 12 / T12-01 cascade layers ────────────────────────────────────────


async def _cascade_butler_action_confirmations(session: AsyncSession, event) -> int:
    """Expire or redact butler_action_confirmations rows tied to a forget event (T12-01).

    For non-terminal confirmation rows (status='pending'):
        UPDATE status='expired', rejection_reason='source_forgotten'.

    For terminal rows (confirmed/rejected/expired/cancelled):
        Redact preview_payload_hash with the standard Phase 9 format
        '[CONTENT_REDACTED: forget_event_id={n}]' to preserve audit
        continuity while removing the privacy-sensitive hash.

    Affected rows are those belonging to butler_actions whose evidence_ids
    JSONB array contains any of the affected mvids, OR whose requester_tg_id
    matches a user-targeted forget event.

    F4 (parent-lock ordering): acquires SELECT FOR UPDATE NOWAIT on all affected
    butler_actions rows BEFORE updating any child confirmation rows.  This ensures
    that a concurrent confirm_action / cancel_action (which also uses NOWAIT) will
    fail-fast with CascadeInFlightError rather than racing past the child updates
    into a state where the parent row is temporarily inconsistent.

    Convention note (write-side): affected rows resolved via event.target_id +
        _resolve_affected_mvids — write-side cascade convention; read-side filters
        use fe.tombstone_key prefix (see memory/feedback-tombstone-key-read-side-convention.md).

    Returns count of rows modified.
    """
    from sqlalchemy import bindparam
    from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
    from sqlalchemy.types import BigInteger

    mvids = await _resolve_affected_mvids(session, event)

    # Build the set of butler_action_ids whose evidence overlaps.
    action_ids: list[int] = []

    if mvids:
        # Find butler_actions where any mvid appears in evidence_ids JSONB array.
        rows = await session.execute(
            text(
                "SELECT id FROM butler_actions "
                "WHERE evidence_ids @> ANY("
                "  SELECT jsonb_build_array(v::bigint) "
                "  FROM unnest(CAST(:mvids AS bigint[])) AS v"
                ")"
            ).bindparams(bindparam("mvids", type_=PG_ARRAY(BigInteger))),
            {"mvids": list(mvids)},
        )
        action_ids = [r[0] for r in rows]

    if event.target_type == "user" and event.target_id is not None:
        try:
            tg_id = int(event.target_id)
        except (TypeError, ValueError):
            pass
        else:
            rows = await session.execute(
                text("SELECT id FROM butler_actions WHERE requester_tg_id = :tg_id"),
                {"tg_id": tg_id},
            )
            for r in rows:
                if r[0] not in action_ids:
                    action_ids.append(r[0])

    if not action_ids:
        return 0

    # F4: Acquire SELECT FOR UPDATE NOWAIT on parent butler_actions rows BEFORE
    # updating any child rows.  A concurrent confirm_action or cancel_action that
    # calls get_for_update(nowait=True) on the same rows will immediately receive
    # OperationalError (LockNotAvailable) → CascadeInFlightError, preventing any
    # state transition while cascade is in-flight.
    await session.execute(
        text(
            "SELECT id FROM butler_actions "
            "WHERE id = ANY(CAST(:action_ids AS bigint[])) "
            "FOR UPDATE NOWAIT"
        ).bindparams(bindparam("action_ids", type_=PG_ARRAY(BigInteger))),
        {"action_ids": action_ids},
    )

    redact_text = f"[CONTENT_REDACTED: forget_event_id={event.id}]"
    count = 0

    # Expire non-terminal pending rows.
    result = await session.execute(
        text(
            "UPDATE butler_action_confirmations "
            "SET status = 'expired', "
            "    preview_payload_hash = :redact_text "
            "WHERE action_id = ANY(CAST(:action_ids AS bigint[])) "
            "  AND status = 'pending' "
            "RETURNING id"
        ).bindparams(bindparam("action_ids", type_=PG_ARRAY(BigInteger))),
        {"action_ids": action_ids, "redact_text": redact_text},
    )
    count += len(result.fetchall())

    # Redact preview_payload_hash on terminal rows (audit continuity).
    result2 = await session.execute(
        text(
            "UPDATE butler_action_confirmations "
            "SET preview_payload_hash = :redact_text "
            "WHERE action_id = ANY(CAST(:action_ids AS bigint[])) "
            "  AND status != 'pending' "
            "  AND preview_payload_hash != :redact_text "
            "RETURNING id"
        ).bindparams(bindparam("action_ids", type_=PG_ARRAY(BigInteger))),
        {"action_ids": action_ids, "redact_text": redact_text},
    )
    count += len(result2.fetchall())

    if count:
        await session.flush()
    return count


async def _cascade_butler_tool_invocations(session: AsyncSession, event) -> int:
    """Redact response_payload on butler_tool_invocations rows tied to a forget event (T12-01).

    For all invocations belonging to affected butler_actions:
        SET response_payload = '{"redacted": true, "forget_event_id": <n>}'
            WHERE response_payload IS NOT NULL
            AND (response_payload->>'forget_event_id') IS NULL  -- idempotency guard.

    Convention note (write-side): affected rows resolved via event.target_id +
        _resolve_affected_mvids — this is the write-side cascade convention; read-side
        filters use fe.tombstone_key prefix (see memory/feedback-tombstone-key-read-side-convention.md).

    Returns count of rows modified.
    """
    from sqlalchemy import bindparam
    from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
    from sqlalchemy.types import BigInteger

    mvids = await _resolve_affected_mvids(session, event)
    action_ids: list[int] = []

    if mvids:
        rows = await session.execute(
            text(
                "SELECT id FROM butler_actions "
                "WHERE evidence_ids @> ANY("
                "  SELECT jsonb_build_array(v::bigint) "
                "  FROM unnest(CAST(:mvids AS bigint[])) AS v"
                ")"
            ).bindparams(bindparam("mvids", type_=PG_ARRAY(BigInteger))),
            {"mvids": list(mvids)},
        )
        action_ids = [r[0] for r in rows]

    if event.target_type == "user" and event.target_id is not None:
        try:
            tg_id = int(event.target_id)
        except (TypeError, ValueError):
            pass
        else:
            rows = await session.execute(
                text("SELECT id FROM butler_actions WHERE requester_tg_id = :tg_id"),
                {"tg_id": tg_id},
            )
            for r in rows:
                if r[0] not in action_ids:
                    action_ids.append(r[0])

    if not action_ids:
        return 0

    redact_payload = f'{{"redacted": true, "forget_event_id": {event.id}}}'
    result = await session.execute(
        text(
            "UPDATE butler_tool_invocations "
            "SET response_payload = CAST(:redact_payload AS jsonb) "
            "WHERE action_id = ANY(CAST(:action_ids AS bigint[])) "
            "  AND response_payload IS NOT NULL "
            "  AND (response_payload->>'forget_event_id') IS NULL "
            "RETURNING id"
        ).bindparams(bindparam("action_ids", type_=PG_ARRAY(BigInteger))),
        {"action_ids": action_ids, "redact_payload": redact_payload},
    )
    count = len(result.fetchall())
    if count:
        await session.flush()
    return count


async def _cascade_butler_actions(session: AsyncSession, event) -> int:
    """Expire non-terminal butler_actions and redact evidence_ids + result_payload on terminal rows (T12-01).

    For non-terminal status rows ('pending_confirmation', 'confirmed', 'executing'):
        UPDATE status='expired', rejection_reason='source_forgotten'.

    For terminal rows:
        Redact evidence_ids and result_payload with {"redacted": true, "forget_event_id": <n>}
        (preserves structural metadata for audit replay; removes user-visible privacy surface).
        result_payload carries rendered Telegram message text / proposed-meeting JSON / intro
        text — must be masked alongside evidence_ids on terminal rows (F1, Codex CRITICAL #2).

    Convention note (write-side vs read-side tombstone resolution):
        This is a WRITE-SIDE cascade layer; affected rows are resolved via event.target_id +
        _resolve_affected_mvids (matching all other write-side layers: digests, wiki_pages,
        graph_nodes). Read-side filters (validators / search / extractor / gateway) MUST use
        fe.tombstone_key prefix instead — see memory/feedback-tombstone-key-read-side-convention.md.

    Returns count of rows modified.
    """
    from sqlalchemy import bindparam
    from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
    from sqlalchemy.types import BigInteger

    mvids = await _resolve_affected_mvids(session, event)
    action_ids: list[int] = []

    if mvids:
        rows = await session.execute(
            text(
                "SELECT id FROM butler_actions "
                "WHERE evidence_ids @> ANY("
                "  SELECT jsonb_build_array(v::bigint) "
                "  FROM unnest(CAST(:mvids AS bigint[])) AS v"
                ")"
            ).bindparams(bindparam("mvids", type_=PG_ARRAY(BigInteger))),
            {"mvids": list(mvids)},
        )
        action_ids = [r[0] for r in rows]

    if event.target_type == "user" and event.target_id is not None:
        try:
            tg_id = int(event.target_id)
        except (TypeError, ValueError):
            pass
        else:
            rows = await session.execute(
                text("SELECT id FROM butler_actions WHERE requester_tg_id = :tg_id"),
                {"tg_id": tg_id},
            )
            for r in rows:
                if r[0] not in action_ids:
                    action_ids.append(r[0])

    if not action_ids:
        return 0

    redact_json = f'{{"redacted": true, "forget_event_id": {event.id}}}'
    count = 0

    # Expire non-terminal rows.
    result = await session.execute(
        text(
            "UPDATE butler_actions "
            "SET status = 'expired', "
            "    rejection_reason = 'source_forgotten', "
            "    evidence_ids = CAST(:redact_json AS jsonb), "
            "    updated_at = NOW() "
            "WHERE id = ANY(CAST(:action_ids AS bigint[])) "
            "  AND status = ANY(ARRAY['pending_confirmation','confirmed','executing']) "
            "RETURNING id"
        ).bindparams(bindparam("action_ids", type_=PG_ARRAY(BigInteger))),
        {"action_ids": action_ids, "redact_json": redact_json},
    )
    count += len(result.fetchall())

    # Redact evidence_ids + result_payload on terminal rows (preserve audit row but
    # remove privacy surface).  result_payload carries rendered Telegram message text /
    # proposed-meeting JSON / intro text — user-visible content that must be masked.
    # Write-side cascade uses event.target_id (resolved via _resolve_affected_mvids) per
    # the tombstone-key convention: only read-side filters use fe.tombstone_key prefix;
    # see memory/feedback-tombstone-key-read-side-convention.md.
    result2 = await session.execute(
        text(
            "UPDATE butler_actions "
            "SET evidence_ids = CAST(:redact_json AS jsonb), "
            "    result_payload = CASE "
            "        WHEN result_payload IS NOT NULL "
            "             AND (result_payload->>'forget_event_id') IS NULL "
            "        THEN CAST(:redact_json AS jsonb) "
            "        ELSE result_payload "
            "    END, "
            "    updated_at = NOW() "
            "WHERE id = ANY(CAST(:action_ids AS bigint[])) "
            "  AND status NOT IN ('pending_confirmation','confirmed','executing') "
            "  AND (evidence_ids->>'forget_event_id') IS NULL "
            "RETURNING id"
        ).bindparams(bindparam("action_ids", type_=PG_ARRAY(BigInteger))),
        {"action_ids": action_ids, "redact_json": redact_json},
    )
    count += len(result2.fetchall())

    if count:
        await session.flush()
    return count


async def _cascade_butler_undo_invocations(session: AsyncSession, event) -> int:
    """Redact butler_undo_invocations rows tied to a forget event (T12-07).

    For all undo invocations belonging to affected butler_actions:
        SET error_message = '[CONTENT_REDACTED: forget_event_id=<n>]'
        WHERE error_message IS NOT NULL
        AND (error_message LIKE '[CONTENT_REDACTED%') IS FALSE  -- idempotency guard.

    Preserves the undo audit row itself (status, rollback_kind) so the audit chain
    remains intact; only user-visible error text is masked.

    Convention note (write-side): affected rows resolved via event.target_id +
        _resolve_affected_mvids — write-side cascade convention; read-side filters
        use fe.tombstone_key prefix.

    Returns count of rows modified.
    """
    from sqlalchemy import bindparam
    from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
    from sqlalchemy.types import BigInteger

    mvids = await _resolve_affected_mvids(session, event)
    action_ids: list[int] = []

    if mvids:
        rows = await session.execute(
            text(
                "SELECT id FROM butler_actions "
                "WHERE evidence_ids @> ANY("
                "  SELECT jsonb_build_array(v::bigint) "
                "  FROM unnest(CAST(:mvids AS bigint[])) AS v"
                ")"
            ).bindparams(bindparam("mvids", type_=PG_ARRAY(BigInteger))),
            {"mvids": list(mvids)},
        )
        action_ids = [r[0] for r in rows]

    if event.target_type == "user" and event.target_id is not None:
        try:
            tg_id = int(event.target_id)
        except (TypeError, ValueError):
            pass
        else:
            rows = await session.execute(
                text("SELECT id FROM butler_actions WHERE requester_tg_id = :tg_id"),
                {"tg_id": tg_id},
            )
            for r in rows:
                if r[0] not in action_ids:
                    action_ids.append(r[0])

    if not action_ids:
        return 0

    redact_text = f"[CONTENT_REDACTED: forget_event_id={event.id}]"
    result = await session.execute(
        text(
            "UPDATE butler_undo_invocations "
            "SET error_message = :redact_text "
            "WHERE butler_action_id = ANY(CAST(:action_ids AS bigint[])) "
            "  AND error_message IS NOT NULL "
            "  AND error_message NOT LIKE '[CONTENT_REDACTED%' "
            "RETURNING id"
        ).bindparams(bindparam("action_ids", type_=PG_ARRAY(BigInteger))),
        {"action_ids": action_ids, "redact_text": redact_text},
    )
    count = len(result.fetchall())
    if count:
        await session.flush()
    return count


# Map layer name → cascade function. Layers absent from this map are recorded as
# skipped. When a future phase adds a layer's table, add its function here.
_LAYER_FUNCS: dict[str, Any] = {
    "chat_messages": _cascade_chat_messages,
    # Issue #404: derived vector/provenance purge must precede message redaction.
    "semantic_retrieval": _cascade_semantic_retrieval,
    "message_versions": _cascade_message_versions,
    "qa_traces": _cascade_qa_traces,
    # T5-04 Phase 5 layers — ORDER binding per contracts.md §8.
    "llm_synthesis_cache": _cascade_llm_synthesis_cache,
    "qa_traces_llm": _cascade_qa_traces_llm,
    "llm_usage_ledger": _cascade_llm_usage_ledger,
    # T7-05 Phase 7 layer — PHASE7_PLAN.md §5.H. Runs BEFORE card_sources.
    "digests": _cascade_digests,
    # T9-07 Phase 9 layers — PHASE9_PLAN.md §T9-07. Runs AFTER digests, BEFORE card_sources.
    "wiki_pages": _cascade_wiki_pages,
    "wiki_revisions": _cascade_wiki_revisions,
    # T6-01 Phase 6 layer — PHASE6_PLAN.md §5.A.5.
    "card_sources": _cascade_card_sources_on_forget,
    # T10-06 Phase 10 layer — PHASE10_PLAN.md §5.F. Runs AFTER card_sources.
    "graph_nodes": _cascade_graph_provenance,
    # T12-01 Phase 12 layers — PHASE12_PLAN_REFRESH.md §4.4. Runs AFTER graph_nodes.
    "butler_action_confirmations": _cascade_butler_action_confirmations,
    "butler_tool_invocations": _cascade_butler_tool_invocations,
    # T12-07: undo audit runs AFTER tool invocations, BEFORE parent action row.
    "butler_undo_invocations": _cascade_butler_undo_invocations,
    "butler_actions": _cascade_butler_actions,
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
                select(ChatMessage.current_version_id).where(ChatMessage.id == cm_id)
            )
        ).first()
        if row is None or row[0] is None:
            return []
        return [int(row[0])]

    if event.target_type == "message_hash":
        target_hash = str(event.target_id)
        rows = (
            (
                await session.execute(
                    select(MessageVersion.id).where(MessageVersion.content_hash == target_hash)
                )
            )
            .scalars()
            .all()
        )
        return [int(v) for v in rows]

    if event.target_type == "user":
        try:
            telegram_id = int(event.target_id)
        except (TypeError, ValueError):
            return []
        rows = (
            (
                await session.execute(
                    select(MessageVersion.id)
                    .join(ChatMessage, ChatMessage.id == MessageVersion.chat_message_id)
                    .where(ChatMessage.user_id == telegram_id)
                )
            )
            .scalars()
            .all()
        )
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
            for lock_id in sorted(_p6_mvid_advisory_lock_id(m) for m in affected_mvids):
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
            claimed = await ForgetEventRepo.mark_status(session, event.id, status="processing")
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
            stats = await run_cascade_worker_once(own_session, bot=bot, batch_size=batch_size)
            await own_session.commit()
            return stats
        except Exception:
            await own_session.rollback()
            raise
