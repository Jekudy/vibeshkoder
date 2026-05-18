"""Wiki governance validator — T9-02 / Phase 9.

Public API
----------
validate_sources(session, *, page_id) -> SourceCheckResult

Pure read-only service. No DB writes.

All invalid-source conditions are resolved in a **single batched SQL** that
covers both direct mv citations (wiki_page_message_sources) and transitive
mvids that flow through wiki_page_card_sources → card_sources.

Invalid conditions checked (7 total):
1. card_status != 'approved'        → reason "archived"
2. message_version.is_redacted      → reason "redacted"
3. chat_messages.memory_policy = 'offrecord' or 'forgotten'
                                    → reason "offrecord"
4. active forget_event by chat_message_id / message_version parent
                                    → reason "forgotten"
5. active forget_event message_hash tombstone matching mv.content_hash
                                    → reason "tombstone:message_hash"
6. active forget_event user tombstone matching mv author (chat_messages.user_id)
                                    → reason "tombstone:user"
7. transitive: card whose every card_sources mv triggers conditions 2-6
                                    → reason "transitive_forget"
   (surfaced on the card citation, not on the underlying mv)

Join chain used: message_versions.chat_message_id → chat_messages.id
(NOT message_versions.message_id — that column does not exist on the table).

G1 lint: this file must NEVER import neo4j or bot.services.graph_* modules.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# ── Active forget-event status set ────────────────────────────────────────────
# Mirrors the semantics from forget_cascade.py: any event that is not 'failed'
# is considered "active" and blocks publication.
_ACTIVE_FORGET_STATUSES = ("pending", "processing", "completed")

# ── Batched SQL ───────────────────────────────────────────────────────────────
#
# A single query that, given a wiki_page_id, returns one row per mv that is
# directly cited by the wiki page (via wiki_page_message_sources) OR
# transitively referenced via wiki_page_card_sources → card_sources.
#
# Columns returned:
#   mv_id            BIGINT  — message_versions.id
#   source_kind      TEXT    — 'direct' | 'transitive'
#   card_id          UUID    — non-null only for source_kind='transitive'
#   mv_is_redacted   BOOL
#   cm_memory_policy TEXT    — chat_messages.memory_policy
#   cm_user_id       BIGINT  — chat_messages.user_id (for user-tombstone check)
#   mv_content_hash  TEXT    — message_versions.content_hash (for hash-tombstone)
#   cm_id            INT     — chat_messages.id (for message-tombstone check)
#
# The join chain uses message_versions.chat_message_id → chat_messages.id
# (NOT message_versions.message_id, which is not a column on that table).
#
# This constant is exposed at module level so tests can assert the correct
# column name is referenced (AC 10/11).
BATCHED_QUERY_SQL = """
WITH direct_mvids AS (
    -- MVs directly cited by the wiki page
    SELECT
        wpm.message_version_id  AS mv_id,
        'direct'                AS source_kind,
        NULL::uuid              AS card_id
    FROM wiki_page_message_sources wpm
    WHERE wpm.wiki_page_id = :page_id
),
transitive_mvids AS (
    -- MVs reachable via wiki_page_card_sources → card_sources
    SELECT
        cs.message_version_id   AS mv_id,
        'transitive'            AS source_kind,
        wpcs.card_id            AS card_id
    FROM wiki_page_card_sources wpcs
    JOIN card_sources cs ON cs.card_id = wpcs.card_id
    WHERE wpcs.wiki_page_id = :page_id
),
all_mvids AS (
    SELECT mv_id, source_kind, card_id FROM direct_mvids
    UNION ALL
    SELECT mv_id, source_kind, card_id FROM transitive_mvids
)
SELECT
    a.mv_id,
    a.source_kind,
    a.card_id,
    mv.is_redacted          AS mv_is_redacted,
    cm.memory_policy        AS cm_memory_policy,
    cm.user_id              AS cm_user_id,
    mv.content_hash         AS mv_content_hash,
    cm.id                   AS cm_id
FROM all_mvids a
JOIN message_versions mv ON mv.id = a.mv_id
JOIN chat_messages cm ON cm.id = mv.chat_message_id
"""

# Query to fetch all card citations and their statuses for the page.
_CARDS_QUERY_SQL = """
SELECT
    kc.id           AS card_id,
    kc.card_status  AS card_status
FROM wiki_page_card_sources wpcs
JOIN knowledge_cards kc ON kc.id = wpcs.card_id
WHERE wpcs.wiki_page_id = :page_id
"""

# Query to fetch active forget_events that might match a set of mvids.
# Returns rows grouped by the tombstone type so callers can classify the reason.
_FORGET_EVENTS_QUERY_SQL = """
SELECT
    fe.tombstone_key,
    fe.target_type,
    fe.target_id,
    fe.status
FROM forget_events fe
WHERE fe.status IN ('pending', 'processing', 'completed')
  AND fe.target_type IN ('message', 'message_hash', 'user')
"""


# ── Result dataclass ──────────────────────────────────────────────────────────


@dataclass
class SourceCheckResult:
    """Result of validate_sources().

    Attributes:
        valid:           True iff no invalid sources detected.
        invalid_card_ids: List of card UUIDs that failed validation.
        invalid_mvids:   List of message_version ids that failed validation.
        reasons:         Mapping from "card:<uuid>" or "mvid:<id>" to a short
                         reason string: "archived", "redacted", "offrecord",
                         "forgotten", "tombstone:message_hash", "tombstone:user",
                         "transitive_forget".
    """

    valid: bool
    invalid_card_ids: list[uuid.UUID] = field(default_factory=list)
    invalid_mvids: list[int] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Return a JSON-serializable representation.

        Used to persist the result in wiki_publication_log.source_check_result.
        """
        return {
            "valid": self.valid,
            "invalid_card_ids": [str(c) for c in self.invalid_card_ids],
            "invalid_mvids": list(self.invalid_mvids),
            "reasons": dict(self.reasons),
        }


# ── Public API ────────────────────────────────────────────────────────────────


async def validate_sources(
    session: AsyncSession,
    *,
    page_id: uuid.UUID | int,
) -> SourceCheckResult:
    """Validate all sources cited by a wiki page.

    Parameters
    ----------
    session:
        Active AsyncSession. Read-only — no DB writes are performed.
    page_id:
        UUID of the wiki page to validate. Accepts a ``uuid.UUID`` object or
        an integer (for future callers that pass UUID.int).

    Returns
    -------
    SourceCheckResult
        ``valid=True`` iff all source conditions pass.
    """
    if isinstance(page_id, int):
        # Accept UUID.int for test convenience — convert back to UUID
        page_id = uuid.UUID(int=page_id)

    page_id_str = str(page_id)

    invalid_card_ids: list[uuid.UUID] = []
    invalid_mvids: list[int] = []
    reasons: dict[str, str] = {}

    # ── Step 1: fetch card statuses ───────────────────────────────────────────
    card_rows = (
        await session.execute(text(_CARDS_QUERY_SQL), {"page_id": page_id_str})
    ).fetchall()

    # card_id → status
    card_statuses: dict[uuid.UUID, str] = {}
    for row in card_rows:
        cid = uuid.UUID(str(row.card_id))
        card_statuses[cid] = row.card_status

    # Cards that are not 'approved' are immediately invalid
    for cid, status in card_statuses.items():
        if status != "approved":
            invalid_card_ids.append(cid)
            reasons[f"card:{cid}"] = "archived"

    # ── Step 2: fetch all mvids (direct + transitive) in a single batched query
    mv_rows = (
        await session.execute(text(BATCHED_QUERY_SQL), {"page_id": page_id_str})
    ).fetchall()

    if not mv_rows and not card_rows:
        # Empty page — no sources at all. Return valid=True (no constraints violated).
        return SourceCheckResult(valid=True)

    # ── Step 3: load active forget_events once ────────────────────────────────
    fe_rows = (
        await session.execute(text(_FORGET_EVENTS_QUERY_SQL))
    ).fetchall()

    # Build lookup structures for O(1) matching per mv row.
    # message-type events: target_id is chat_message_id (as stored in target_id field)
    forgotten_cm_ids: set[str] = set()
    # message_hash-type events: target_id is the content_hash
    forgotten_hashes: set[str] = set()
    # user-type events: target_id is the telegram user_id
    forgotten_user_ids: set[str] = set()

    for fe in fe_rows:
        if fe.target_type == "message" and fe.target_id is not None:
            forgotten_cm_ids.add(str(fe.target_id))
        elif fe.target_type == "message_hash" and fe.target_id is not None:
            forgotten_hashes.add(str(fe.target_id))
        elif fe.target_type == "user" and fe.target_id is not None:
            forgotten_user_ids.add(str(fe.target_id))

    # ── Step 4: evaluate each mv row ─────────────────────────────────────────
    # Track which transitive card ids have ANY valid source remaining.
    # We start by assuming all transitive cards have no valid source;
    # then we clear the flag when we find at least one clean mv for a card.
    transitive_card_ids: set[uuid.UUID] = set()
    # card_id → bool: True means at least one source is clean
    card_has_clean_source: dict[uuid.UUID, bool] = {}

    for row in mv_rows:
        mv_id = int(row.mv_id)
        source_kind = row.source_kind  # 'direct' or 'transitive'
        card_id_raw = row.card_id
        card_id = uuid.UUID(str(card_id_raw)) if card_id_raw is not None else None

        if source_kind == "transitive" and card_id is not None:
            transitive_card_ids.add(card_id)
            if card_id not in card_has_clean_source:
                card_has_clean_source[card_id] = False  # pessimistic default

        # Determine invalid reason for this mv
        mv_reason = _classify_mv(
            mv_id=mv_id,
            mv_is_redacted=bool(row.mv_is_redacted),
            cm_memory_policy=str(row.cm_memory_policy),
            cm_user_id=str(row.cm_user_id),
            mv_content_hash=str(row.mv_content_hash),
            cm_id=str(row.cm_id),
            forgotten_cm_ids=forgotten_cm_ids,
            forgotten_hashes=forgotten_hashes,
            forgotten_user_ids=forgotten_user_ids,
        )

        if mv_reason is None:
            # This mv is clean
            if source_kind == "transitive" and card_id is not None:
                card_has_clean_source[card_id] = True
        else:
            if source_kind == "direct":
                if mv_id not in invalid_mvids:
                    invalid_mvids.append(mv_id)
                    reasons[f"mvid:{mv_id}"] = mv_reason
            # For transitive: do NOT flag the mv directly; flag the card instead
            # (handled below after we know all sources)

    # ── Step 5: evaluate transitive card validity ─────────────────────────────
    for cid in transitive_card_ids:
        # Skip cards already marked invalid via card_status check
        if cid in {c for c in invalid_card_ids}:
            continue
        if not card_has_clean_source.get(cid, False):
            # All sources for this card are tainted
            if cid not in invalid_card_ids:
                invalid_card_ids.append(cid)
                reasons[f"card:{cid}"] = "transitive_forget"

    # ── Step 6: check "all card_sources forgotten" for directly-cited cards ───
    # A card may be approved and not flagged by transitive check above if it has
    # NO card_sources rows at all, or if all its sources are forgotten but it
    # wasn't picked up in mv_rows (e.g., card_sources rows have been deleted).
    # Check via a targeted subquery for each cited card.
    for cid in list(card_statuses.keys()):
        if cid in {c for c in invalid_card_ids}:
            continue  # already flagged
        if cid in transitive_card_ids:
            continue  # handled above
        # Card is cited but has no (transitive) mvids in our result — either no
        # card_sources exist or all were deleted. Count remaining card_sources
        # for this card to decide.
        cs_count_row = (
            await session.execute(
                text("SELECT count(*) FROM card_sources WHERE card_id = :cid"),
                {"cid": str(cid)},
            )
        ).scalar()
        if cs_count_row == 0:
            # No sources remain — check if there are any active forget events
            # that would have caused deletion (or just flag as all-sources-forgotten)
            invalid_card_ids.append(cid)
            reasons[f"card:{cid}"] = "all_sources_forgotten"

    valid = not invalid_card_ids and not invalid_mvids

    return SourceCheckResult(
        valid=valid,
        invalid_card_ids=invalid_card_ids,
        invalid_mvids=invalid_mvids,
        reasons=reasons,
    )


# ── Internal helpers ──────────────────────────────────────────────────────────


def _classify_mv(
    *,
    mv_id: int,
    mv_is_redacted: bool,
    cm_memory_policy: str,
    cm_user_id: str,
    mv_content_hash: str,
    cm_id: str,
    forgotten_cm_ids: set[str],
    forgotten_hashes: set[str],
    forgotten_user_ids: set[str],
) -> str | None:
    """Return the first invalid reason for this mv, or None if clean.

    Priority order: redacted → offrecord → forgotten (message) →
    tombstone:message_hash → tombstone:user.
    """
    if mv_is_redacted:
        return "redacted"

    if cm_memory_policy in ("offrecord", "forgotten"):
        return "offrecord"

    # Active forget_event for the parent chat_message
    if cm_id in forgotten_cm_ids:
        return "forgotten"

    # message_hash tombstone
    if mv_content_hash in forgotten_hashes:
        return "tombstone:message_hash"

    # user tombstone
    if cm_user_id in forgotten_user_ids:
        return "tombstone:user"

    return None


async def assert_publishable(session: AsyncSession, *, page_id: uuid.UUID) -> None:
    """Raise WikiSourcesMissingError when the page has zero sources.

    T9-01 AC: service-layer source-presence guard replacing the removed
    JSONB ``ck_wiki_pages_reviewed_requires_sources`` constraint.
    """
    page_id_str = str(page_id)
    row = (
        await session.execute(
            text(
                "SELECT "
                "  (SELECT count(*) FROM wiki_page_card_sources WHERE wiki_page_id = :pid) + "
                "  (SELECT count(*) FROM wiki_page_message_sources WHERE wiki_page_id = :pid) "
                "AS total"
            ),
            {"pid": page_id_str},
        )
    ).scalar()
    if not row:
        raise WikiSourcesMissingError(f"wiki_page {page_id} has no cited sources")


class WikiSourcesMissingError(ValueError):
    """Raised when a wiki page has no cited sources at publication time."""
