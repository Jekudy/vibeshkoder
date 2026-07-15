"""Memory governance for the Phase 13 complete-history product contract.

Every human community-chat message is persisted as ``normal`` memory.  Strings such as
``#nomem`` and ``#offrecord`` are ordinary message content and do not create an opt-out.

``redact_raw_for_offrecord`` remains available for historical rows and maintenance code
that may need to sanitize an already-classified legacy payload.  The live/import policy
detector no longer produces that classification.
"""

from __future__ import annotations

from typing import Literal

PolicyOutcome = Literal["normal", "nomem", "offrecord"]

# Version string for the governance filter.  Frozen into ButlerEvidenceContext
# (T12-02) so the hash is stable across replays.  Bump if detect_policy logic
# changes in a way that would produce different policy outcomes for the same
# inputs (e.g. new patterns, new fields).
GOVERNANCE_FILTER_VERSION = "phase13-v1"


def detect_policy(
    text: str | None,
    caption: str | None,
    *,
    poll_question: str | None = None,
    contact_name: str | None = None,
    forward_text: str | None = None,
    forward_caption: str | None = None,
) -> tuple[PolicyOutcome, dict | None]:
    """Return the single supported policy for all human message content.

    The full signature is retained so live ingestion and historical import adapters keep
    one stable API.  Content is intentionally not inspected: opt-out tokens are ordinary
    searchable text under the complete-history contract.
    """
    return ("normal", None)


# Telegram update payload event fields that carry user content. The redactor walks each
# of these (only one is typically present per update) and strips content fields.
_EVENT_FIELDS: tuple[str, ...] = (
    "message",
    "edited_message",
    "channel_post",
    "edited_channel_post",
)
_CONTENT_FIELDS_TO_DROP: tuple[str, ...] = (
    "text",
    "caption",
    "entities",
    "caption_entities",
)
# Nested message-shaped fields that Telegram echoes inside an event. Each contains
# its own ``text``/``caption`` snapshot of a related message and must be scrubbed too —
# without this, a user replying with ``#offrecord`` to a sensitive parent message would
# still leak the parent content via ``message.reply_to_message.text``. Recursion handles
# nested ``reply_to_message`` chains.
_NESTED_MESSAGE_FIELDS: tuple[str, ...] = (
    "reply_to_message",
    "pinned_message",
    "external_reply",
    "quote",
)


def _scrub_message(node: dict) -> dict:
    """Return a shallow copy of a message-shaped dict with content fields dropped and
    nested message-shaped children recursively scrubbed."""
    scrubbed = {**node}
    for field in _CONTENT_FIELDS_TO_DROP:
        scrubbed.pop(field, None)
    for field in _NESTED_MESSAGE_FIELDS:
        nested = scrubbed.get(field)
        if isinstance(nested, dict):
            scrubbed[field] = _scrub_message(nested)
    return scrubbed


def redact_raw_for_offrecord(raw_json: dict | None) -> dict | None:
    """Return a sanitized copy of ``raw_json`` with content fields removed.

    Used by ``bot/services/ingestion.py`` when ``detect_policy`` returns ``"offrecord"``.
    Drops ``text``, ``caption``, ``entities``, ``caption_entities`` from each known
    event field AND from any nested message-shaped fields (``reply_to_message``,
    ``pinned_message``, ``external_reply``, ``quote``). Keeps ids, timestamps, sender
    info, hash, policy marker.

    The function takes and returns a dict (not a SQLAlchemy row) so it can be unit-tested
    without a DB and re-used by the importer (T2-* tickets).
    """
    if raw_json is None:
        return None
    redacted: dict = {**raw_json}
    for event_field in _EVENT_FIELDS:
        original_event = redacted.get(event_field)
        if not isinstance(original_event, dict):
            continue
        redacted[event_field] = _scrub_message(original_event)
    return redacted
