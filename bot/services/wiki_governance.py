"""Wiki governance validator — T9-02 / Phase 9.

Public API
----------
validate_sources(session, *, page_id) -> SourceCheckResult
assert_publishable(session, *, page_id) -> None
WikiPageNotFoundError, WikiSourcesMissingError

Pure read-only service. No DB writes.

All invalid-source conditions are resolved in a **single batched SQL** that
covers both direct mv citations (wiki_page_message_sources) and transitive
mvids that flow through wiki_page_card_sources → card_sources. Tombstone
matching is done inside the SQL via `forget_events.tombstone_key` prefix
comparison — matching the project-wide read-side convention used in
search.py / extractor.py / llm_gateway.py / governance_revalidation.py.

Invalid conditions checked:
1. card_status != 'approved'        → reason "archived"
2. message_version.is_redacted      → reason "redacted"
3. chat_messages.memory_policy = 'offrecord' or 'forgotten'
                                    → reason "offrecord"
4. active forget_event tombstone_key 'message:<chat_id>:<message_id>'
                                    → reason "forgotten"
5. active forget_event tombstone_key 'message_hash:<content_hash>'
                                    → reason "tombstone:message_hash"
6. active forget_event tombstone_key 'user:<chat_messages.user_id>'
                                    → reason "tombstone:user"
7. transitive: card whose every card_sources mv triggers conditions 2-6
                                    → reason "transitive_forget"
   (surfaced on the card citation, not on the underlying mv)
8. card cited but card_sources_count = 0 (all sources have been deleted)
                                    → reason "transitive_forget"
9. message_version is not the chat_message current version
                                    → reason "non_current"
10. source belongs to a different explicit chat scope
                                    → reason "wrong_chat"

Join chain used: message_versions.chat_message_id → chat_messages.id
(NOT message_versions.message_id — that column does not exist on the table).

G1 lint: this file must NEVER import neo4j or bot.services.graph_* modules.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# ── Page existence check ──────────────────────────────────────────────────────
_PAGE_EXISTS_QUERY_SQL = "SELECT 1 FROM wiki_pages WHERE id = :page_id"

# ── Cards SQL — with card_sources count folded in to eliminate N+1 ─────────────
#
# Returns every card cited by the page along with its current status and the
# total count of card_sources rows that still exist. A card with cs_count=0
# means every source has been deleted by the forget cascade (or never had
# any) — the card must be flagged as transitive_forget.
_CARDS_QUERY_SQL = """
SELECT
    kc.id           AS card_id,
    kc.card_status  AS card_status,
    (SELECT count(*) FROM card_sources cs WHERE cs.card_id = kc.id) AS cs_count
FROM wiki_page_card_sources wpcs
JOIN knowledge_cards kc ON kc.id = wpcs.card_id
WHERE wpcs.wiki_page_id = :page_id
"""

# ── Batched MVID query ────────────────────────────────────────────────────────
#
# A single query that, given a wiki_page_id, returns one row per mv that is
# directly cited by the wiki page (via wiki_page_message_sources) OR
# transitively referenced via wiki_page_card_sources → card_sources.
#
# Tombstone matching is performed IN-SQL via three EXISTS subqueries against
# forget_events.tombstone_key — matching the canonical read-side convention.
# We deliberately do NOT consult forget_events.target_id; nothing in the
# schema guarantees target_id is consistent with tombstone_key on read.
#
# Columns returned:
#   mv_id            BIGINT  — message_versions.id
#   source_kind      TEXT    — 'direct' | 'transitive'
#   card_id          UUID    — non-null only for source_kind='transitive'
#   mv_is_redacted   BOOL
#   cm_memory_policy TEXT    — chat_messages.memory_policy
#   fe_msg_active    BOOL    — tombstone_key 'message:<chat>:<msg>' is active
#   fe_hash_active   BOOL    — tombstone_key 'message_hash:<hash>' is active
#   fe_user_active   BOOL    — tombstone_key 'user:<user_id>' is active
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
    cm.is_redacted          AS cm_is_redacted,
    cm.memory_policy        AS cm_memory_policy,
    cm.current_version_id = mv.id AS mv_is_current,
    cm.chat_id              AS source_chat_id,
    EXISTS (
        SELECT 1 FROM forget_events fe
        WHERE fe.status IN ('pending','processing','completed')
          AND fe.tombstone_key = 'message:' || cm.chat_id::text || ':' || cm.message_id::text
    )                       AS fe_msg_active,
    EXISTS (
        SELECT 1 FROM forget_events fe
        WHERE fe.status IN ('pending','processing','completed')
          AND fe.tombstone_key = 'message_hash:' || mv.content_hash
    )                       AS fe_hash_active,
    EXISTS (
        SELECT 1 FROM forget_events fe
        WHERE fe.status IN ('pending','processing','completed')
          AND fe.tombstone_key = 'user:' || cm.user_id::text
    )                       AS fe_user_active
FROM all_mvids a
JOIN message_versions mv ON mv.id = a.mv_id
JOIN chat_messages cm ON cm.id = mv.chat_message_id
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
                         "non_current", "wrong_chat", "transitive_forget"
                         (covers both "card with no clean
                         transitive source remaining" AND "card with zero
                         card_sources rows left after cascade").
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
    source_chat_id: int | None = None,
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

    Raises
    ------
    WikiPageNotFoundError
        If no ``wiki_pages`` row exists with the supplied ``page_id``.
    """
    if isinstance(page_id, int):
        # Accept UUID.int for test convenience — convert back to UUID
        page_id = uuid.UUID(int=page_id)
    if source_chat_id is not None and (
        isinstance(source_chat_id, bool)
        or not isinstance(source_chat_id, int)
        or source_chat_id == 0
    ):
        raise ValueError("source_chat_id must be a non-zero integer")

    page_id_str = str(page_id)

    invalid_card_ids: list[uuid.UUID] = []
    invalid_mvids: list[int] = []
    reasons: dict[str, str] = {}

    # ── Step 0: page must exist ───────────────────────────────────────────────
    page_exists = (
        await session.execute(text(_PAGE_EXISTS_QUERY_SQL), {"page_id": page_id_str})
    ).scalar()
    if not page_exists:
        raise WikiPageNotFoundError(f"wiki_page {page_id} does not exist")

    # ── Step 1: fetch card statuses + card_sources counts ─────────────────────
    card_rows = (await session.execute(text(_CARDS_QUERY_SQL), {"page_id": page_id_str})).fetchall()

    # card_id → (status, cs_count)
    card_info: dict[uuid.UUID, tuple[str, int]] = {}
    for row in card_rows:
        cid = uuid.UUID(str(row.card_id))
        card_info[cid] = (row.card_status, int(row.cs_count))

    # Cards that are not 'approved' are immediately invalid
    for cid, (status, _cs) in card_info.items():
        if status != "approved":
            invalid_card_ids.append(cid)
            reasons[f"card:{cid}"] = "archived"

    # ── Step 2: fetch all mvids (direct + transitive) — single batched SQL ────
    # Each row carries per-mv tombstone-active booleans evaluated in-SQL via
    # forget_events.tombstone_key prefix matching (not target_id).
    mv_rows = (
        await session.execute(
            text(BATCHED_QUERY_SQL),
            {"page_id": page_id_str},
        )
    ).fetchall()

    # ── Step 3: evaluate each mv row ─────────────────────────────────────────
    # Track which transitive card ids have ANY valid source remaining.
    transitive_card_ids: set[uuid.UUID] = set()
    card_has_clean_source: dict[uuid.UUID, bool] = {}
    card_invalid_reason: dict[uuid.UUID, str] = {}

    for row in mv_rows:
        mv_id = int(row.mv_id)
        source_kind = row.source_kind  # 'direct' or 'transitive'
        card_id_raw = row.card_id
        card_id = uuid.UUID(str(card_id_raw)) if card_id_raw is not None else None

        if source_kind == "transitive" and card_id is not None:
            transitive_card_ids.add(card_id)
            if card_id not in card_has_clean_source:
                card_has_clean_source[card_id] = False  # pessimistic default

        mv_reason = _classify_mv(
            mv_is_redacted=bool(row.mv_is_redacted),
            cm_is_redacted=bool(row.cm_is_redacted),
            cm_memory_policy=str(row.cm_memory_policy),
            mv_is_current=bool(row.mv_is_current),
            wrong_chat=(source_chat_id is not None and int(row.source_chat_id) != source_chat_id),
            fe_msg_active=bool(row.fe_msg_active),
            fe_hash_active=bool(row.fe_hash_active),
            fe_user_active=bool(row.fe_user_active),
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
            elif card_id is not None:
                card_invalid_reason.setdefault(card_id, mv_reason)
            # For transitive: do NOT flag the mv directly; flag the card instead
            # (handled below after we know all sources)

    # ── Step 4: flag cards whose transitive sources are all tainted ──────────
    for cid in transitive_card_ids:
        if cid in {c for c in invalid_card_ids}:
            continue
        strict_reason = card_invalid_reason.get(cid) if source_chat_id is not None else None
        if strict_reason is not None or not card_has_clean_source.get(cid, False):
            if cid not in invalid_card_ids:
                invalid_card_ids.append(cid)
                reasons[f"card:{cid}"] = strict_reason or "transitive_forget"

    # ── Step 5: flag cards whose card_sources_count = 0 (no rows reachable) ──
    # Read the count from the cards query result — no extra round-trip.
    for cid, (_status, cs_count) in card_info.items():
        if cid in {c for c in invalid_card_ids}:
            continue
        if cs_count == 0:
            invalid_card_ids.append(cid)
            reasons[f"card:{cid}"] = "transitive_forget"

    if source_chat_id is not None and not card_rows and not mv_rows:
        reasons["page"] = "sources_missing"

    valid = not invalid_card_ids and not invalid_mvids and "page" not in reasons

    return SourceCheckResult(
        valid=valid,
        invalid_card_ids=invalid_card_ids,
        invalid_mvids=invalid_mvids,
        reasons=reasons,
    )


# ── Internal helpers ──────────────────────────────────────────────────────────


def _classify_mv(
    *,
    mv_is_redacted: bool,
    cm_is_redacted: bool,
    cm_memory_policy: str,
    mv_is_current: bool,
    wrong_chat: bool,
    fe_msg_active: bool,
    fe_hash_active: bool,
    fe_user_active: bool,
) -> str | None:
    """Return the first invalid reason for this mv, or None if clean.

    Priority order: wrong_chat → non_current → redacted → offrecord →
    forgotten (message) → tombstone:message_hash → tombstone:user.

    All tombstone booleans come from the BATCHED_QUERY_SQL EXISTS clauses,
    which match against forget_events.tombstone_key prefix (not target_id).
    """
    if wrong_chat:
        return "wrong_chat"

    if not mv_is_current:
        return "non_current"

    if mv_is_redacted or cm_is_redacted:
        return "redacted"

    if cm_memory_policy in ("offrecord", "forgotten"):
        return "offrecord"

    if fe_msg_active:
        return "forgotten"

    if fe_hash_active:
        return "tombstone:message_hash"

    if fe_user_active:
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


class WikiPageNotFoundError(LookupError):
    """Raised by validate_sources when the supplied page_id does not exist."""
