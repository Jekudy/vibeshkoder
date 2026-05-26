"""Sealed evidence envelope for the Butler / Action Layer (T12-02).

Phase 12 (Wave 1 Stream Evidence) introduces ``ButlerEvidenceContext`` — a
frozen wrapper around the Phase 4 ``EvidenceBundle`` that adds Butler-specific
metadata and a governance pre-filter pass.

Design rationale
----------------
* ``ButlerEvidenceContext`` wraps (does NOT subclass) ``EvidenceBundle`` so the
  Phase 4 contract is preserved byte-for-byte while Butler adds its own fields.
* ``build_butler_evidence`` is the ONLY entry-point for Butler memory reads.
  It calls the existing Phase 4/6 search path and applies ``governance.detect_policy``
  over the 2 stored content fields (text, caption) before including any source in
  the sealed context.  The other 4 governance fields (poll_question, contact_name,
  forward_text, forward_caption) are NOT stored in ``chat_messages`` columns — they
  are checked at ingestion time (Phase 0 ``detect_policy``) and their result is
  durable in ``chat_messages.memory_policy``.  The SQL first-line filter
  ``c.memory_policy = 'normal'`` covers those 4 fields transitively.
* NO LLM calls are made here (Hard Constraint #1 from charter §"Hard Constraints").
* NO raw DB access outside existing service/repo layer (Hard Constraint #2).

Privacy invariants
------------------
* Sources with ``memory_policy != 'normal'`` are excluded by the Phase 4 SQL layer
  (first-line filter).  This transitively covers poll_question / contact_name /
  forward_text / forward_caption since those fields are not stored in columns —
  their governance classification is durably recorded in ``memory_policy`` at ingest.
* ``_fetch_governance_fields`` re-reads text + caption from the DB for ALL source
  mvids in a single batched SELECT (``= ANY(:mvids)``), so even if the search snippet
  is truncated, the full fields are inspected for the second-line ``detect_policy`` re-check.
* The batched query in ``_fetch_governance_fields`` folds the forget/tombstone
  predicate into the same SELECT, closing the race window between search and re-read.
  One query regardless of the number of hits (no N+1).
* Vanished rows (row absent from the single-query JOIN) are treated as fail-closed:
  excluded and counted in ``governance_excluded_count``.

``context_hash``
----------------
Deterministic SHA-256 hash computed by the canonical ``butler_context_hash`` helper
(spec §3.6 step 1).  Inputs: bundle items (source_type + message_version_id +
card_id + sorted card_source_message_version_ids), visibility_scope,
governance_filter_version.  Used by ``confirm_action`` pre-execute revalidation
(T12-08) to detect if the evidence set or visibility context has changed since
the action was planned.  G3.b binding test recomputes via this same helper and
asserts byte equality.

Orchestrator-side metadata
--------------------------
``requester_user_id``, ``chat_id``, ``query``, ``snapshot_at``,
``governance_excluded_count`` are NOT part of the canonical hash inputs.  They
are runtime metadata for audit, logging, and TTL revalidation.  The hash inputs
are: bundle items + visibility_scope + governance_filter_version.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.evidence import EvidenceBundle, EvidenceItem, _build_evidence_item
from bot.services.governance import GOVERNANCE_FILTER_VERSION, detect_policy
from bot.services.search import search_messages

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Frozen sealed envelope — canonical fields per spec §4.2
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)  # type: ignore[call-overload]
class ButlerEvidenceContext:
    """Sealed Butler-facing wrapper around an EvidenceBundle.

    Immutable after construction — the Butler never re-ranks or augments
    after ``recall_evidence`` returns.

    Canonical fields (spec §4.2)
    ----------------------------
    bundle
        The sealed Phase 4 EvidenceBundle.  Contains the filtered items that
        passed ``governance.detect_policy`` at build time.
    visibility_scope
        Visibility scope under which this evidence was assembled.  Frozen at
        build time — cannot be changed post-construction.
    context_hash
        ``butler_context_hash(bundle, visibility_scope, governance_filter_version)``
        per spec §3.6 step 1.  Stable across replays; G3.b asserts byte equality.
    governance_filter_version
        Version string of the governance filter used at build time (= GOVERNANCE_FILTER_VERSION
        from ``bot/services/governance.py``).  Frozen at creation; never re-evaluated.

    Orchestrator-side metadata (NOT part of canonical hash inputs)
    --------------------------------------------------------------
    requester_user_id
        Telegram user_id of the member who invoked ``/butler``.
    chat_id
        Community chat_id scope for the recall (``None`` if no scope filter).
    query
        The original recall query string.
    snapshot_at
        UTC timestamp when this context was built.  Used by TTL revalidation
        (evidence snapshot TTL = 30 min, §14.3 of PHASE12_PLAN_REFRESH.md).
    governance_excluded_count
        How many candidate sources were rejected by ``detect_policy`` at build
        time.  Non-zero means some content was actively filtered (audit trail).
        Logged at WARNING level for operator visibility.
    """

    # Canonical fields per spec §4.2
    bundle: EvidenceBundle
    visibility_scope: Literal["member", "admin", "self"]
    context_hash: str  # = butler_context_hash(bundle, visibility_scope, governance_filter_version)
    governance_filter_version: str

    # Orchestrator-side metadata — not part of canonical hash inputs
    requester_user_id: int
    chat_id: int | None
    query: str
    snapshot_at: datetime
    governance_excluded_count: int

    _VALID_VISIBILITY_SCOPES: frozenset[str] = frozenset({"member", "admin", "self"})

    def __post_init__(self) -> None:
        # Runtime validation: Literal annotation is type-checker only; a bad scope would
        # silently change the hash and produce non-replayable contexts in T12-04.
        if self.visibility_scope not in self._VALID_VISIBILITY_SCOPES:
            raise ValueError(
                f"visibility_scope must be one of {sorted(self._VALID_VISIBILITY_SCOPES)}, "
                f"got {self.visibility_scope!r}"
            )

    @property
    def evidence_ids(self) -> list[int]:
        """Proxy to bundle.evidence_ids — list of message_version_id values."""
        return self.bundle.evidence_ids

    @property
    def items(self) -> tuple[EvidenceItem, ...]:
        """Proxy to bundle.items."""
        return self.bundle.items


# ---------------------------------------------------------------------------
# Canonical hash helper — spec §3.6 step 1 (public: T12-04 + T12-08 + G3.b use it)
# ---------------------------------------------------------------------------


def butler_context_hash(
    bundle: EvidenceBundle,
    visibility_scope: str,
    governance_filter_version: str,
) -> str:
    """Canonical context hash. Stable across replays.

    This is the ONE canonical hash function used by BOTH build_butler_evidence
    and the future confirm_action revalidation in T12-04 + the G3.b binding in
    T12-09 — byte-equal hash on the same inputs.

    Card identity (card_id + card_source_message_version_ids) is part
    of the input — EvidenceBundle.evidence_ids only flattens to
    message_version_ids, which would LOSE card identity (a card-hit
    with the same anchor mvid as a message-hit would hash equal).

    Per spec §3.6 step 1 verbatim.
    """
    items_canonical = sorted(
        [
            {
                "source_type": item.source_type,
                "message_version_id": item.message_version_id,
                "card_id": str(item.card_id) if item.card_id is not None else None,
                "card_source_message_version_ids": sorted(
                    item.card_source_message_version_ids or ()
                ),
            }
            for item in bundle.items
        ],
        key=lambda d: (
            d["source_type"],
            d["message_version_id"] if d["message_version_id"] is not None else -1,
            d["card_id"] or "",
        ),
    )
    payload = {
        "items": items_canonical,
        "visibility_scope": visibility_scope,
        "governance_filter_version": governance_filter_version,
    }
    canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Internal items-only hash (used when visibility_scope + gov_version are already
# baked into the canonical hash externally — kept for internal use by build)
# ---------------------------------------------------------------------------


def _compute_context_hash(
    items: tuple[EvidenceItem, ...],
) -> str:
    """Deterministic SHA-256 hash of the canonical source-identity set (items only).

    Order-independent: items are sorted by (source_type, mvid, str(card_id))
    before hashing.  Card identity includes the sorted card_source_mvids tuple
    so a card-hit with the same anchor mvid as a message-hit hashes differently.

    Note: this helper hashes ONLY the items, without visibility_scope or
    governance_filter_version.  Use ``butler_context_hash`` (the public helper
    per spec §3.6 step 1) for the full canonical hash that includes all three
    inputs.
    """
    canonical = sorted(
        [
            {
                "source_type": item.source_type,
                "message_version_id": item.message_version_id,
                "card_id": str(item.card_id) if item.card_id is not None else None,
                "card_source_message_version_ids": sorted(
                    item.card_source_message_version_ids or ()
                ),
            }
            for item in items
        ],
        key=lambda d: (
            d["source_type"],
            d["message_version_id"] if d["message_version_id"] is not None else -1,
            d["card_id"] or "",
        ),
    )
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Governance field fetcher (internal)
#
# Batched single-query approach: accepts a collection of mvids, returns a
# mapping mvid → fields dict (or None sentinel).  Folds the forget/tombstone
# predicate INTO the same SELECT so there is no race window between the
# original search result and this re-check.
#
# Why batched?
#   The old single-mvid helper was called inside a per-hit loop, producing
#   an N+1 query pattern for multi-hit responses.  By accepting a Collection
#   and using = ANY(:mvids), we issue exactly ONE query regardless of how many
#   hits (or card-source mvids) need re-checking.
#
# Sentinel semantics:
#   An mvid present in the input set but ABSENT from the result set means the
#   row is forgotten, tombstoned, redacted, or vanished — the dict entry for
#   that mvid is None (fail-closed).
# ---------------------------------------------------------------------------


async def _fetch_governance_fields(
    session: AsyncSession,
    *,
    mvids: Collection[int],
) -> dict[int, dict[str, str | None] | None]:
    """Fetch text + caption for a batch of message_version_ids.

    Issues a SINGLE query for all requested mvids using ``= ANY(:mvids)``.
    Folds the forget/tombstone predicate into the same SELECT to close the
    race window between the Phase 4 search result and this second-line
    re-check.

    Returns a dict mapping each input mvid to either:
    - ``{"text": ..., "caption": ...}`` — row present and passes all filters, OR
    - ``None`` (sentinel) — row absent, forgotten, tombstoned, or
      governance-excluded (fail-closed).

    The caller treats ``None`` entries as governance exclusions.

    Note: poll_question, contact_name, forward_text, forward_caption are NOT
    stored in chat_messages columns — their classification is durably captured
    in memory_policy at ingestion time.  Only text + caption need second-line
    re-check here.
    """
    if not mvids:
        return {}

    mvid_list = list(mvids)

    result = await session.execute(
        text(
            """
            SELECT
                mv.id AS mvid,
                c.text AS text,
                c.caption AS caption
            FROM message_versions mv
            JOIN chat_messages c ON c.id = mv.chat_message_id
            WHERE mv.id = ANY(:mvids)
              AND c.memory_policy = 'normal'
              AND c.is_redacted = FALSE
              AND mv.is_redacted = FALSE
              AND NOT EXISTS (
                  SELECT 1 FROM forget_events fe
                  WHERE fe.status IN ('pending', 'processing', 'completed')
                    AND (
                        fe.tombstone_key = 'message:' || c.chat_id::text || ':' || c.message_id::text
                        OR fe.tombstone_key = 'message_hash:' || mv.content_hash
                        OR fe.tombstone_key = 'user:' || c.user_id::text
                    )
              )
            """
        ),
        {"mvids": mvid_list},
    )

    # Build lookup from query results
    found: dict[int, dict[str, str | None]] = {}
    for row in result.mappings():
        found[int(row["mvid"])] = {"text": row["text"], "caption": row["caption"]}

    # Return full mapping: requested mvids present in found get their fields,
    # absent mvids (vanished/forgotten/excluded) get the None sentinel.
    out: dict[int, dict[str, str | None] | None] = {}
    for mvid in mvid_list:
        if mvid in found:
            out[mvid] = found[mvid]
        else:
            # Row is absent — vanished, forgotten, or governance-excluded.  Fail-closed.
            logger.warning(
                "butler_evidence: source row absent or forgotten in governance re-check — "
                "excluding as fail-closed",
                extra={"message_version_id": mvid},
            )
            out[mvid] = None
    return out


# ---------------------------------------------------------------------------
# Public builder
# ---------------------------------------------------------------------------


async def build_butler_evidence(
    session: AsyncSession,
    *,
    requester_user_id: int,
    query: str,
    chat_id: int | None = None,
    visibility_scope: Literal["member", "admin", "self"] = "member",
    governance_filter_version: str = GOVERNANCE_FILTER_VERSION,
) -> ButlerEvidenceContext:
    """Build a sealed ButlerEvidenceContext for a Butler planning request.

    Steps
    -----
    1. Call Phase 4/6 ``search_messages`` — returns governance-filtered hits
       (tombstoned / redacted / offrecord / nomem rows already excluded by SQL
       via memory_policy='normal' + forget_events predicate).
    2. Pre-collect all mvids across all hits and fetch text + caption in a
       SINGLE batched query (``= ANY(:mvids)``) with the same forget predicate
       re-applied (no race window, fail-closed on vanished/forgotten rows).
       Call ``governance.detect_policy(text, caption)`` for the second-line
       re-check using dict lookups — no additional DB round-trips.
    3. For card hits: all ``card_source_message_version_ids`` are individually
       re-checked.  If ANY source mvid is absent/forgotten or fails detect_policy,
       the entire card hit is rejected.
    4. Excluded sources (policy != 'normal' OR row vanished) are counted in
       ``governance_excluded_count`` and logged at WARNING — never silently dropped.
    5. The surviving items are assembled into a sealed ``EvidenceBundle`` and
       wrapped in an immutable ``ButlerEvidenceContext`` with the canonical
       ``context_hash`` computed via ``butler_context_hash``.

    Empty query → empty bundle (abstained=True), no crash.
    No LLM calls (Hard Constraint #1).
    No raw DB outside search_messages + _fetch_governance_fields (Hard Constraint #2).

    Parameters
    ----------
    session
        SQLAlchemy async session bound to the Postgres DB.
    requester_user_id
        Telegram user_id of the Butler requester (for audit).
    query
        Recall query string.  Empty / whitespace → empty bundle.
    chat_id
        Community chat scope.  Passed directly to ``search_messages``.
    visibility_scope
        Visibility scope for this recall context.  Frozen into the context_hash.
    governance_filter_version
        Version of the governance filter to record.  Defaults to the module-level
        constant from ``bot/services/governance.py``.
    """
    normalized_query = query.strip()

    # Empty query → empty bundle, no DB round-trip needed.
    if not normalized_query:
        empty_bundle = EvidenceBundle(
            query=query,
            chat_id=chat_id if chat_id is not None else 0,
            items=(),
            abstained=True,
            created_at=datetime.now(timezone.utc),
        )
        ctx_hash = butler_context_hash(empty_bundle, visibility_scope, governance_filter_version)
        return ButlerEvidenceContext(
            bundle=empty_bundle,
            visibility_scope=visibility_scope,
            context_hash=ctx_hash,
            governance_filter_version=governance_filter_version,
            requester_user_id=requester_user_id,
            chat_id=chat_id,
            query=query,
            snapshot_at=datetime.now(timezone.utc),
            governance_excluded_count=0,
        )

    # Phase 4/6 search — first-line governance filter (SQL-level).
    # search_messages requires chat_id; default to 0 if not provided (caller
    # should always provide it for a meaningful search scope, but 0 is safe
    # as it will return no results on a real DB).
    effective_chat_id = chat_id if chat_id is not None else 0
    hits = await search_messages(session, normalized_query, chat_id=effective_chat_id)

    # Second-line governance filter: re-run detect_policy on text + caption
    # fetched directly from the DB (with forget predicate applied).
    #
    # Batch approach: collect the COMPLETE set of mvids across ALL hits first,
    # then issue a SINGLE batched query.  Outer hit-iteration loop uses dict
    # lookups — no additional DB calls.
    all_mvids_to_check: set[int] = set()
    for hit in hits:
        all_mvids_to_check.add(hit.message_version_id)
        card_source_mvids = getattr(hit, "card_source_message_version_ids", ()) or ()
        all_mvids_to_check.update(card_source_mvids)

    # Single batched SELECT — dict[mvid → fields | None]
    governance_map = await _fetch_governance_fields(session, mvids=all_mvids_to_check)

    accepted: list[EvidenceItem] = []
    excluded_count = 0

    for hit in hits:
        # For card hits: anchor + all card source mvids must all pass.
        card_source_mvids = tuple(getattr(hit, "card_source_message_version_ids", ()) or ())
        mvids_for_hit: list[int] = [hit.message_version_id]
        if card_source_mvids:
            # H-3: check ALL source mvids for card hits, not just the anchor.
            mvids_for_hit.extend(card_source_mvids)

        # Check all relevant mvids via dict lookup — if ANY fails, reject the hit.
        hit_excluded = False
        for mvid in mvids_for_hit:
            fields = governance_map.get(mvid)

            if fields is None:
                # Vanished or forgotten row — fail-closed.
                logger.warning(
                    "butler_evidence: source mvid vanished or forgotten — "
                    "excluding hit as fail-closed",
                    extra={
                        "message_version_id": hit.message_version_id,
                        "failing_mvid": mvid,
                        "requester_user_id": requester_user_id,
                    },
                )
                hit_excluded = True
                break

            policy, _mark = detect_policy(
                fields.get("text"),
                fields.get("caption"),
            )

            if policy != "normal":
                logger.warning(
                    "butler_evidence: source excluded by second-line governance pre-filter",
                    extra={
                        "message_version_id": hit.message_version_id,
                        "failing_mvid": mvid,
                        "policy": policy,
                        "requester_user_id": requester_user_id,
                    },
                )
                hit_excluded = True
                break

        if hit_excluded:
            excluded_count += 1
            continue

        item = _build_evidence_item(hit)
        accepted.append(item)

    snapshot_at = datetime.now(timezone.utc)
    items_tuple = tuple(accepted)

    bundle = EvidenceBundle(
        query=normalized_query,
        chat_id=effective_chat_id,
        items=items_tuple,
        abstained=len(items_tuple) == 0,
        created_at=snapshot_at,
    )

    if excluded_count > 0:
        logger.warning(
            "butler_evidence: governance pre-filter excluded sources",
            extra={
                "excluded_count": excluded_count,
                "accepted_count": len(accepted),
                "requester_user_id": requester_user_id,
                "query": normalized_query,
            },
        )

    ctx_hash = butler_context_hash(bundle, visibility_scope, governance_filter_version)

    return ButlerEvidenceContext(
        bundle=bundle,
        visibility_scope=visibility_scope,
        context_hash=ctx_hash,
        governance_filter_version=governance_filter_version,
        requester_user_id=requester_user_id,
        chat_id=chat_id,
        query=query,
        snapshot_at=snapshot_at,
        governance_excluded_count=excluded_count,
    )
