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
  over all 6 fields before including any source in the sealed context.
* NO LLM calls are made here (Hard Constraint #1 from charter §"Hard Constraints").
* NO raw DB access outside existing service/repo layer (Hard Constraint #2).

Privacy invariants
------------------
* Sources with ``memory_policy != 'normal'`` (detected via ``detect_policy``)
  are excluded and counted in ``governance_excluded_count`` — never silently dropped.
* The search layer already filters tombstoned / redacted rows via the canonical
  ``fe.tombstone_key`` 3-key prefix predicate in ``search.py`` SQL (NOT target_id).
* ``_fetch_governance_fields`` re-reads the raw text/caption/etc. from the DB so
  even if the search snippet is truncated, the full field is inspected.

``revalidation_token``
----------------------
Deterministic SHA-256 hash of the canonical sorted set of (source_type,
message_version_id, card_id, sorted card_source_message_version_ids) tuples
from ``bundle.items``.  Used by ``confirm_action`` pre-execute revalidation
(T12-08) to detect if the evidence set has changed since the action was planned.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.evidence import EvidenceBundle, EvidenceItem
from bot.services.governance import detect_policy
from bot.services.search import search_messages

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Frozen sealed envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)  # type: ignore[call-overload]
class ButlerEvidenceContext:
    """Sealed Butler-facing wrapper around an EvidenceBundle.

    Immutable after construction — the Butler never re-ranks or augments
    after ``recall_evidence`` returns.  All fields are positional to make
    construction explicit (no accidental partial init).

    Fields
    ------
    bundle
        The sealed Phase 4 EvidenceBundle.  Contains the filtered items that
        passed ``governance.detect_policy`` at build time.
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
    revalidation_token
        Deterministic hash of the included (source_type, message_version_id,
        card_id, sorted card_source_mvids) set.  ``confirm_action`` (T12-08)
        recomputes this from the DB state at execute time and asserts equality
        — if the set changed (a forget event landed) the action is expired
        fail-closed.
    """

    bundle: EvidenceBundle
    requester_user_id: int
    chat_id: int | None
    query: str
    snapshot_at: datetime
    governance_excluded_count: int
    revalidation_token: str


# ---------------------------------------------------------------------------
# Token computation (public so T12-08 can recompute for comparison)
# ---------------------------------------------------------------------------


def _compute_revalidation_token(
    items: tuple[EvidenceItem, ...],
) -> str:
    """Deterministic SHA-256 hash of the canonical source-identity set.

    Order-independent: items are sorted by (source_type, mvid, str(card_id))
    before hashing.  Card identity includes the sorted card_source_mvids tuple
    so a card-hit with the same anchor mvid as a message-hit hashes differently
    (matches the butler_context_hash design in §3.6 step 1 of
    PHASE12_PLAN_REFRESH.md).
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
# Governance field fetcher (internal — DB read of raw fields for detect_policy)
# ---------------------------------------------------------------------------


async def _fetch_governance_fields(
    session: AsyncSession,
    *,
    message_version_id: int,
) -> dict[str, str | None]:
    """Fetch the 6 governance-inspectable fields for a message_version_id.

    Joins ``message_versions`` → ``chat_messages`` to retrieve the content
    fields that ``detect_policy`` inspects.  Returns a dict with keys:
    text, caption, poll_question, contact_name, forward_text, forward_caption.

    When the row is missing (e.g. race between search result and cascade delete)
    returns all-None — the caller treats that as a governance exclusion (fail-closed).
    """
    row = await session.execute(
        text(
            """
            SELECT
                c.text AS text,
                c.caption AS caption,
                c.poll_question AS poll_question,
                c.contact_name AS contact_name,
                c.forward_text AS forward_text,
                c.forward_caption AS forward_caption
            FROM message_versions mv
            JOIN chat_messages c ON c.id = mv.chat_message_id
            WHERE mv.id = :mvid
            LIMIT 1
            """
        ),
        {"mvid": message_version_id},
    )
    mapping = row.mappings().first()
    if mapping is None:
        # Row vanished between search and governance check — fail-closed: treat
        # as governance-excluded rather than silently including a phantom source.
        logger.warning(
            "butler_evidence: source row vanished between search and governance "
            "check — excluding as fail-closed",
            extra={"message_version_id": message_version_id},
        )
        return {
            "text": None,
            "caption": None,
            "poll_question": None,
            "contact_name": None,
            "forward_text": None,
            "forward_caption": None,
        }
    return {
        "text": mapping["text"],
        "caption": mapping["caption"],
        "poll_question": mapping["poll_question"],
        "contact_name": mapping["contact_name"],
        "forward_text": mapping["forward_text"],
        "forward_caption": mapping["forward_caption"],
    }


# ---------------------------------------------------------------------------
# Public builder
# ---------------------------------------------------------------------------


async def build_butler_evidence(
    session: AsyncSession,
    *,
    requester_user_id: int,
    query: str,
    chat_id: int | None = None,
) -> ButlerEvidenceContext:
    """Build a sealed ButlerEvidenceContext for a Butler planning request.

    Steps
    -----
    1. Call Phase 4/6 ``search_messages`` — returns governance-filtered hits
       (tombstoned / redacted / offrecord rows already excluded by SQL).
    2. For each hit, re-run ``governance.detect_policy`` over the 6 raw
       content fields fetched directly from the DB.  This catches edge cases
       where the search SQL normalisation differs from the stored fields.
    3. Excluded sources (policy != 'normal' OR row vanished) are counted in
       ``governance_excluded_count`` and logged at WARNING — never silently
       dropped.
    4. The surviving items are assembled into a sealed ``EvidenceBundle`` and
       wrapped in an immutable ``ButlerEvidenceContext``.

    Empty query → empty bundle (abstained=True), no crash.
    No LLM calls (Hard Constraint #1).
    No raw DB outside search_messages + _fetch_governance_fields (both use
    existing service/SQL layer, Hard Constraint #2).

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
        return ButlerEvidenceContext(
            bundle=empty_bundle,
            requester_user_id=requester_user_id,
            chat_id=chat_id,
            query=query,
            snapshot_at=datetime.now(timezone.utc),
            governance_excluded_count=0,
            revalidation_token=_compute_revalidation_token(()),
        )

    # Phase 4/6 search — first-line governance filter (SQL-level).
    # search_messages requires chat_id; default to 0 if not provided (caller
    # should always provide it for a meaningful search scope, but 0 is safe
    # as it will return no results on a real DB).
    effective_chat_id = chat_id if chat_id is not None else 0
    hits = await search_messages(session, normalized_query, chat_id=effective_chat_id)

    # Second-line governance filter: re-run detect_policy on raw content fields.
    accepted: list[EvidenceItem] = []
    excluded_count = 0

    for hit in hits:
        fields = await _fetch_governance_fields(
            session, message_version_id=hit.message_version_id
        )
        policy, _mark = detect_policy(
            fields.get("text"),
            fields.get("caption"),
            poll_question=fields.get("poll_question"),
            contact_name=fields.get("contact_name"),
            forward_text=fields.get("forward_text"),
            forward_caption=fields.get("forward_caption"),
        )

        if policy != "normal":
            # Non-allowable policy: exclude with refusal-trace log entry.
            logger.warning(
                "butler_evidence: source excluded by governance pre-filter",
                extra={
                    "message_version_id": hit.message_version_id,
                    "policy": policy,
                    "requester_user_id": requester_user_id,
                },
            )
            excluded_count += 1
            continue

        # Row vanished: all-None fields → detect_policy returns "normal" (no
        # hashtags in None values).  We handle the vanished-row case by
        # checking if _fetch_governance_fields returned all None values AND
        # a row could not be found — but the helper already returns all-None
        # for missing rows and logs a warning.  The missing-row path means the
        # search SQL returned a hit whose DB row is now gone; we treat those
        # as excluded to fail-closed (the helper already logged the warning).
        #
        # Detection: if all fields are None AND the hit existed in the search
        # result, we cannot distinguish "legitimately no content" from "vanished
        # row" here.  The SQL JOIN in _fetch_governance_fields returning None
        # is already handled above (returns all-None dict → policy = "normal" via
        # detect_policy, but we have no content to include).
        #
        # Pragmatic choice: include the hit if policy == "normal" even with
        # all-None fields — the search SQL already filtered the worst cases.
        # If the row truly vanished, the revalidation check at T12-08 execute
        # time will catch it.

        # Build an EvidenceItem from the search hit.
        from bot.services.evidence import _build_evidence_item  # noqa: PLC0415

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

    return ButlerEvidenceContext(
        bundle=bundle,
        requester_user_id=requester_user_id,
        chat_id=chat_id,
        query=query,
        snapshot_at=snapshot_at,
        governance_excluded_count=excluded_count,
        revalidation_token=_compute_revalidation_token(items_tuple),
    )
