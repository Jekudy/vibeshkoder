"""Visibility derivation for derived artifacts (knowledge_cards, wiki_pages, graph_nodes).

Given a list of cited ``message_version_ids``, determines the combined visibility state
of the artifact by inspecting the underlying ``message_versions`` and ``chat_messages``
rows, plus any active ``forget_events`` tombstones.

Precedence (strictest wins, per invariant #3 HANDOFF.md §1):
  REDACTED > NOMEM > FORGOTTEN > VISIBLE

Rationale for precedence:
- REDACTED: content is gone or under offrecord policy — highest privacy concern; beats all.
- NOMEM: owner opted out of memory use — content still present but excluded from all
  downstream use; beats FORGOTTEN (a tombstone is a specific erase request, but a nomem
  opt-out is a broader categorical exclusion that should surface prominently).
- FORGOTTEN: a specific forget_events tombstone has been applied — content may be wiped.
- VISIBLE: no constraints; artifact is safe to surface.

Read-only contract: this module NEVER writes to the database. It is safe to call inside
any existing transaction without side effects. No LLM calls (invariant #2).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import ChatMessage, ForgetEvent, MessageVersion


def _build_tombstone_keys(
    content_hash: str | None,
    chat_id: int | None,
    message_id: int | None,
    from_user_id: int | None,
) -> list[str]:
    """Build all tombstone key formats for a single message_version row.

    Production creates three tombstone formats (HANDOFF §10):
      - ``message_hash:{sha256}``  — content-level forget (forget_cascade.py)
      - ``message:{chat_id}:{message_id}`` — /forget_reply handler
      - ``user:{tg_id}``           — /forget_me handler

    Missing fields → that key format is omitted (graceful skip, never emits
    malformed keys like ``message::99``).

    Returns a list of 0-3 keys (no duplicates).
    """
    keys: list[str] = []
    if content_hash is not None:
        keys.append(f"message_hash:{content_hash}")
    if chat_id is not None and message_id is not None:
        keys.append(f"message:{chat_id}:{message_id}")
    if from_user_id is not None:
        keys.append(f"user:{from_user_id}")
    return keys


logger = logging.getLogger(__name__)


class CardVisibility(StrEnum):
    VISIBLE = "visible"
    REDACTED = "redacted"
    FORGOTTEN = "forgotten"
    NOMEM = "nomem"


@dataclass(frozen=True)
class VisibilityDerivation:
    """Result of deriving an artifact's visibility from its cited sources.

    Attributes:
        visibility: The derived visibility state (strictest wins).
        blocking_source_ids: Tuple of message_version_ids that caused a non-visible state.
                             Empty tuple when visibility is VISIBLE.
        reason: Human-readable explanation for audit log.
    """

    visibility: CardVisibility
    blocking_source_ids: tuple[int, ...]
    reason: str


# _POLICY_RANK maps each visibility level to an integer; higher = stricter.
# Used when combining multiple sources: the final visibility is the maximum rank found.
_POLICY_RANK: dict[CardVisibility, int] = {
    CardVisibility.VISIBLE: 0,
    CardVisibility.FORGOTTEN: 1,
    CardVisibility.NOMEM: 2,
    CardVisibility.REDACTED: 3,
}


def _classify_version(
    ver_is_redacted: bool,
    parent_memory_policy: str,
    parent_is_redacted: bool,
    has_tombstone: bool,
) -> CardVisibility:
    """Classify a single message_version row into a CardVisibility level.

    Called once per cited version. The final artifact visibility is the maximum
    classification across all cited versions.
    """
    # REDACTED: is_redacted flag on either version or parent, or offrecord/forgotten policy
    if ver_is_redacted or parent_is_redacted:
        return CardVisibility.REDACTED
    if parent_memory_policy in ("offrecord", "forgotten"):
        return CardVisibility.REDACTED
    # NOMEM: parent has nomem policy
    if parent_memory_policy == "nomem":
        return CardVisibility.NOMEM
    # FORGOTTEN: a forget_events tombstone matched this version's content_hash
    if has_tombstone:
        return CardVisibility.FORGOTTEN
    return CardVisibility.VISIBLE


@dataclass(frozen=True)
class _VersionRow:
    """Pure-data row representing a fetched message_version + joined chat_messages fields.

    Internal carrier: converts SQLAlchemy Row objects into a plain frozen dataclass
    so that classify_visibility() can be called without a session.

    Both ver_is_redacted and parent_is_redacted are preserved separately to allow
    classify_visibility to pass them independently to _classify_version (matching the
    original derive_card_visibility inline logic exactly).
    """

    version_id: int
    content_hash: str | None
    chat_id: int | None
    message_id: int | None
    user_id: int | None
    memory_policy: str  # 'normal' | 'nomem' | 'offrecord' | 'forgotten' | etc.
    # is_redacted is the version-level flag (message_versions.is_redacted)
    is_redacted: bool
    # parent_is_redacted is the chat_messages-level flag (chat_messages.is_redacted)
    parent_is_redacted: bool = False


async def _fetch_versions(
    session: AsyncSession,
    cited_message_version_ids: list[int],
) -> list[_VersionRow]:
    """Fetch message_versions JOIN chat_messages for the given version IDs.

    Returns a list of _VersionRow with all fields needed for classify_visibility().
    Single SQL query; no N+1.
    """
    stmt = (
        select(
            MessageVersion.id.label("ver_id"),
            MessageVersion.content_hash.label("ver_content_hash"),
            MessageVersion.is_redacted.label("ver_is_redacted"),
            ChatMessage.memory_policy.label("parent_policy"),
            ChatMessage.is_redacted.label("parent_is_redacted"),
            ChatMessage.chat_id.label("parent_chat_id"),
            ChatMessage.message_id.label("parent_message_id"),
            ChatMessage.user_id.label("parent_user_id"),
        )
        .join(ChatMessage, MessageVersion.chat_message_id == ChatMessage.id)
        .where(MessageVersion.id.in_(cited_message_version_ids))
    )
    rows = (await session.execute(stmt)).all()
    return [
        _VersionRow(
            version_id=row.ver_id,
            content_hash=row.ver_content_hash,
            chat_id=row.parent_chat_id,
            message_id=row.parent_message_id,
            user_id=row.parent_user_id,
            memory_policy=row.parent_policy,
            is_redacted=row.ver_is_redacted,
            parent_is_redacted=row.parent_is_redacted,
        )
        for row in rows
    ]


async def _fetch_matched_tombstones(
    session: AsyncSession,
    all_keys: list[str],
) -> set[str]:
    """Return the subset of all_keys that exist in forget_events.tombstone_key.

    Single IN query regardless of key count. Returns empty set when all_keys is empty.
    """
    if not all_keys:
        return set()
    tomb_stmt = select(ForgetEvent.tombstone_key).where(
        ForgetEvent.tombstone_key.in_(all_keys)
    )
    tomb_rows = (await session.execute(tomb_stmt)).scalars().all()
    return set(tomb_rows)


def classify_visibility(
    versions: list[_VersionRow],
    matched_tombstone_keys: set[str],
) -> VisibilityDerivation:
    """Pure function: given fetched version rows + matched tombstone keys, compute
    precedence-resolved visibility + blocking_ids + reason.

    NO session access. Fully unit-testable with synthetic inputs.

    Args:
        versions: List of _VersionRow from _fetch_versions() (or synthetic in tests).
        matched_tombstone_keys: Set of tombstone keys active in forget_events
                                (from _fetch_matched_tombstones() or synthetic in tests).

    Returns:
        VisibilityDerivation with the strictest visibility found across all sources.
    """
    if not versions:
        return VisibilityDerivation(
            visibility=CardVisibility.VISIBLE,
            blocking_source_ids=(),
            reason="no cited sources",
        )

    worst: CardVisibility = CardVisibility.VISIBLE
    blocking: list[int] = []
    # Track which tombstone keys actually matched for audit reason string.
    matched_keys_found: set[str] = set()

    for v in versions:
        row_keys = _build_tombstone_keys(
            content_hash=v.content_hash,
            chat_id=v.chat_id,
            message_id=v.message_id,
            from_user_id=v.user_id,
        )
        row_matches = matched_tombstone_keys & set(row_keys)
        has_tombstone = bool(row_matches)
        if has_tombstone:
            matched_keys_found.update(row_matches)

        classification = _classify_version(
            ver_is_redacted=v.is_redacted,
            parent_memory_policy=v.memory_policy,
            parent_is_redacted=v.parent_is_redacted,
            has_tombstone=has_tombstone,
        )

        if _POLICY_RANK[classification] > _POLICY_RANK[CardVisibility.VISIBLE]:
            blocking.append(v.version_id)

        if _POLICY_RANK[classification] > _POLICY_RANK[worst]:
            worst = classification

    matched_keys_list = sorted(matched_keys_found)
    reason = _build_reason(worst, blocking, len(versions), len(versions), matched_keys_list)
    return VisibilityDerivation(
        visibility=worst,
        blocking_source_ids=tuple(sorted(blocking)),
        reason=reason,
    )


async def derive_card_visibility(
    session: AsyncSession,
    cited_message_version_ids: list[int],
) -> VisibilityDerivation:
    """Derive the visibility state of a derived artifact from its cited message versions.

    Reads message_versions + chat_messages + forget_events in a single JOIN query.
    NO writes. Safe inside any transaction.

    Args:
        session: An active AsyncSession. Caller owns the transaction lifecycle.
        cited_message_version_ids: List of message_versions.id values cited by the artifact.

    Returns:
        VisibilityDerivation with the strictest visibility found across all sources.
    """
    if not cited_message_version_ids:
        return VisibilityDerivation(
            visibility=CardVisibility.VISIBLE,
            blocking_source_ids=(),
            reason="no cited sources; artifact is unconstrained",
        )

    # Step 1: Fetch versions + parent chat_messages data.
    versions = await _fetch_versions(session, cited_message_version_ids)

    # Step 2: Build all tombstone keys for all versions (3 formats per message_version),
    # then do a SINGLE forget_events lookup covering all key formats in one IN query.
    # Production formats (HANDOFF §10, forget_cascade.py, forget_reply.py, forget_me.py):
    #   message_hash:{sha256}         — content-level redact
    #   message:{chat_id}:{message_id} — /forget_reply handler
    #   user:{tg_id}                  — /forget_me handler
    all_tombstone_keys: list[str] = []
    for v in versions:
        all_tombstone_keys.extend(
            _build_tombstone_keys(
                content_hash=v.content_hash,
                chat_id=v.chat_id,
                message_id=v.message_id,
                from_user_id=v.user_id,
            )
        )

    matched = await _fetch_matched_tombstones(session, all_tombstone_keys)

    # Step 3: Delegate all classification + precedence + reason logic to classify_visibility().
    return classify_visibility(versions, matched)


def _build_reason(
    visibility: CardVisibility,
    blocking: list[int],
    fetched: int,
    requested: int,
    matched_tombstone_keys: list[str] | None = None,
) -> str:
    """Build a human-readable reason string for the audit log."""
    if visibility == CardVisibility.VISIBLE:
        return f"all {fetched} cited sources are visible (requested {requested})"

    n_blocking = len(blocking)
    suffix = f"; {fetched} sources checked (requested {requested})"

    if visibility == CardVisibility.REDACTED:
        return (
            f"{n_blocking} source(s) have offrecord policy or are_redacted=True "
            f"(blocking ids: {blocking}){suffix}"
        )
    if visibility == CardVisibility.NOMEM:
        return (
            f"{n_blocking} source(s) have nomem policy "
            f"(blocking ids: {blocking}){suffix}"
        )
    if visibility == CardVisibility.FORGOTTEN:
        # Include matched tombstone key formats for audit trail specificity.
        # Keys may be message_hash:, message:, or user: format.
        if matched_tombstone_keys:
            formats = sorted({k.split(":")[0] for k in matched_tombstone_keys})
            keys_repr = matched_tombstone_keys[:5]  # cap at 5 to avoid huge logs
            return (
                f"{n_blocking} source(s) match a forget_events tombstone "
                f"(formats: {formats}, keys: {keys_repr}, "
                f"blocking ids: {blocking}){suffix}"
            )
        return (
            f"{n_blocking} source(s) match a forget_events tombstone "
            f"(blocking ids: {blocking}){suffix}"
        )
    # Unreachable, but keep exhaustive
    return f"unknown visibility state: {visibility}"
