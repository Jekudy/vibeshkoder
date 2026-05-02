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

    # Step 1: Fetch versions + parent chat_messages data in one query.
    # We join chat_messages to get memory_policy and is_redacted at the parent level.
    stmt = (
        select(
            MessageVersion.id.label("ver_id"),
            MessageVersion.content_hash.label("ver_content_hash"),
            MessageVersion.is_redacted.label("ver_is_redacted"),
            ChatMessage.memory_policy.label("parent_policy"),
            ChatMessage.is_redacted.label("parent_is_redacted"),
        )
        .join(ChatMessage, MessageVersion.chat_message_id == ChatMessage.id)
        .where(MessageVersion.id.in_(cited_message_version_ids))
    )
    rows = (await session.execute(stmt)).all()

    # Step 2: Fetch content hashes for these versions to check tombstones.
    # Collect hashes from the fetched rows; then check forget_events.tombstone_key.
    content_hashes = [row.ver_content_hash for row in rows if row.ver_content_hash]

    tombstoned_hashes: set[str] = set()
    if content_hashes:
        # tombstone_key format for hash-based tombstones: "message_hash:<sha256>"
        tombstone_keys = [f"message_hash:{h}" for h in content_hashes]
        tomb_stmt = select(ForgetEvent.tombstone_key).where(
            ForgetEvent.tombstone_key.in_(tombstone_keys)
        )
        tomb_rows = (await session.execute(tomb_stmt)).scalars().all()
        # Extract just the hash portion after "message_hash:"
        tombstoned_hashes = {key.split(":", 1)[1] for key in tomb_rows}

    # Step 3: Classify each version and track blocking sources.
    worst: CardVisibility = CardVisibility.VISIBLE
    blocking: list[int] = []

    for row in rows:
        has_tombstone = row.ver_content_hash in tombstoned_hashes
        classification = _classify_version(
            ver_is_redacted=row.ver_is_redacted,
            parent_memory_policy=row.parent_policy,
            parent_is_redacted=row.parent_is_redacted,
            has_tombstone=has_tombstone,
        )

        if _POLICY_RANK[classification] > _POLICY_RANK[CardVisibility.VISIBLE]:
            blocking.append(row.ver_id)

        if _POLICY_RANK[classification] > _POLICY_RANK[worst]:
            worst = classification

    # Step 4: Build reason string for audit log.
    reason = _build_reason(worst, blocking, len(rows), len(cited_message_version_ids))

    return VisibilityDerivation(
        visibility=worst,
        blocking_source_ids=tuple(sorted(blocking)),
        reason=reason,
    )


def _build_reason(
    visibility: CardVisibility,
    blocking: list[int],
    fetched: int,
    requested: int,
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
        return (
            f"{n_blocking} source(s) match a forget_events tombstone by content_hash "
            f"(blocking ids: {blocking}){suffix}"
        )
    # Unreachable, but keep exhaustive
    return f"unknown visibility state: {visibility}"
