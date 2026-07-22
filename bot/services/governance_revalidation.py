"""T6-04 deterministic governance re-validation (PHASE6_PLAN.md §5.C step 3+4).

Used by ``/approve`` to re-run the governance filter on every candidate
source ``message_version_id`` BEFORE writing the knowledge_cards row. NO
LLM re-prompt is involved (R3); the entire check is deterministic SQL only.

Two failure modes:

* **forget_tombstone_match** — any of the three tombstone keys
  (``message:<chat>:<msg>``, ``message_hash:<mv.content_hash>``,
  ``user:<telegram_id>``) matches a forget_event in status
  ``{'pending','processing','completed'}``. The age of the tombstone is
  irrelevant — any hit blocks regardless of when it was inserted.
* **source_redacted** — ``chat_messages.is_redacted=TRUE`` OR
  ``message_versions.is_redacted=TRUE``.
* **source_memory_policy_not_normal** — ``chat_messages.memory_policy``
  is anything other than ``'normal'`` (non-normal policies all block).
* **source_not_current** — the cited version was superseded by a Telegram edit.

Canonical tombstone-key form: ``mv.content_hash``, NOT
``chat_messages.content_hash``. The live-persistence path leaves
``chat_messages.content_hash`` NULL — using it silently no-ops every
``message_hash:`` tombstone and leaks tombstoned content. See
``bot/services/extractor.py`` lines 240-289 + Codex round 3 CRITICAL on
T6-02. Keep this module in sync with the extractor's predicate.
"""

from __future__ import annotations

from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.control_messages import control_message_excludes_sql_fragment

# Single statement that returns the FIRST blocking row for the given mvid, or
# nothing if the source is clean. Short-circuiting at SQL level means one
# round-trip per mvid even when multiple violations exist on the same row.
#
# Ordering of CASE WHEN branches is the priority of the failure_reason that
# the caller surfaces back to the admin — tombstone first (it implies the
# source was already governance-cleared and then revoked), then redacted,
# then memory_policy.
#
# Codex round 2 HIGH: ``FOR SHARE`` on message_versions blocks concurrent
# writes (forget cascade UPDATEs to is_redacted, etc.) until the /approve
# transaction commits. Without FOR SHARE the source row's state could be
# stale between this read and the subsequent ``INSERT card_sources`` step,
# narrowing but not closing the H-Cdx-2 race. FOR SHARE complements the
# advisory lock by adding a row-level read lock on the actual data rows.
# ``FOR SHARE NOWAIT`` is NOT used — we want to wait for the cascade to
# finish if it grabbed the row first, then re-read its final state.
_CONTROL_MESSAGE_EXCLUDES = control_message_excludes_sql_fragment("mv")

_REVALIDATE_SQL = text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- static in-code SQL/fragments; all runtime values are bound parameters.
    f"""
    WITH src AS (
        SELECT
            mv.id AS message_version_id,
            mv.content_hash AS mv_content_hash,
            mv.is_redacted AS mv_is_redacted,
            c.chat_id AS chat_id,
            c.message_id AS message_id,
            c.user_id AS user_id,
            c.current_version_id = mv.id AS is_current,
            c.memory_policy AS memory_policy,
            c.is_redacted AS c_is_redacted,
            ({_CONTROL_MESSAGE_EXCLUDES}) AS is_content_eligible
        FROM message_versions AS mv
        JOIN chat_messages AS c ON c.id = mv.chat_message_id
        WHERE mv.id = :mvid
        FOR SHARE OF mv, c
    ),
    tombstone_hit AS (
        SELECT fe.id AS forget_event_id
        FROM src, forget_events AS fe
        WHERE fe.status IN ('pending', 'processing', 'completed')
          AND (
              fe.tombstone_key = 'message:' || src.chat_id::text || ':' || src.message_id::text
              OR (
                  src.mv_content_hash IS NOT NULL
                  AND fe.tombstone_key = 'message_hash:' || src.mv_content_hash
              )
              OR (
                  src.user_id IS NOT NULL
                  AND fe.tombstone_key = 'user:' || src.user_id::text
              )
          )
        LIMIT 1
    )
    SELECT
        src.message_version_id AS mvid,
        (SELECT forget_event_id FROM tombstone_hit LIMIT 1) AS forget_event_id,
        src.is_current AS is_current,
        src.mv_is_redacted AS mv_is_redacted,
        src.c_is_redacted AS c_is_redacted,
        src.memory_policy AS memory_policy,
        src.is_content_eligible AS is_content_eligible
    FROM src
    """
)


async def revalidate_sources(
    session: AsyncSession,
    mvids: list[int],
) -> tuple[Literal["ok"], None] | tuple[Literal["blocked"], dict[str, Any]]:
    """Re-run the governance filter on every source message_version_id.

    Returns:
        ``("ok", None)`` if every mvid is governance-clean.
        ``("blocked", {...})`` with payload fields:
            * ``failure_reason``: ``forget_tombstone_match`` |
              ``source_redacted`` | ``source_memory_policy_not_normal`` |
              ``source_not_current`` | ``source_control_message`` |
              ``source_missing``.
            * ``mvid``: the offending message_version_id.
            * ``forget_event_id`` (only on tombstone_match): the
              forget_events row id that fired the block.

    Short-circuits on the first blocking source — callers should treat the
    payload as "the first reason we found", not necessarily the only one.
    """
    for mvid in mvids:
        result = await session.execute(_REVALIDATE_SQL, {"mvid": mvid})
        row = result.first()
        if row is None:
            # Defence-in-depth: an unknown mvid is a caller bug, but we refuse
            # to fail open. /approve should never reach this branch in
            # production because the candidate row's source_message_version_ids
            # are FK-validated at extraction time.
            return (
                "blocked",
                {"failure_reason": "source_missing", "mvid": mvid},
            )
        if row.forget_event_id is not None:
            return (
                "blocked",
                {
                    "failure_reason": "forget_tombstone_match",
                    "mvid": int(row.mvid),
                    "forget_event_id": int(row.forget_event_id),
                },
            )
        if not row.is_current:
            return (
                "blocked",
                {
                    "failure_reason": "source_not_current",
                    "mvid": int(row.mvid),
                },
            )
        if not row.is_content_eligible:
            return (
                "blocked",
                {
                    "failure_reason": "source_control_message",
                    "mvid": int(row.mvid),
                },
            )
        if row.c_is_redacted or row.mv_is_redacted:
            return (
                "blocked",
                {
                    "failure_reason": "source_redacted",
                    "mvid": int(row.mvid),
                },
            )
        if row.memory_policy != "normal":
            return (
                "blocked",
                {
                    "failure_reason": "source_memory_policy_not_normal",
                    "mvid": int(row.mvid),
                },
            )
    return ("ok", None)
