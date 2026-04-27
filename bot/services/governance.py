"""Memory governance — deterministic policy detector + redactor (T1-12 + T1-13).

Replaces the T1-04 stub with the real ``detect_policy`` and ``redact_raw_for_offrecord``.
Detection is deterministic (NO LLM) — token-match on ``#nomem`` and ``#offrecord`` in
the message text and caption, case-insensitive.

Policy semantics (per HANDOFF.md §10):

- ``"normal"`` — store + use as memory.
- ``"nomem"`` — store content but exclude from search / qa / extraction / catalog /
  digest / wiki / graph. The DB row keeps the content; downstream consumers filter by
  ``memory_policy``.
- ``"offrecord"`` — content fields (``text``, ``caption``, ``entities``) MUST be redacted
  before commit. The DB row keeps ids / timestamps / hash / policy marker. Downstream
  consumers see the row but no content.

If both tokens appear, ``"offrecord"`` takes precedence (stricter wins). The mark
payload returned alongside the policy carries audit metadata for the
``offrecord_marks`` row T1-13 inserts when policy != ``"normal"``.
"""

from __future__ import annotations

import re
from typing import Literal

PolicyOutcome = Literal["normal", "nomem", "offrecord"]

# Match #nomem / #offrecord as standalone hashtags. Case-insensitive. Negative
# lookahead `(?!\w)` rejects #nomembership / #offrecordings; the leading anchor is left
# implicit (start of string OR any non-word boundary), which Python's re handles via the
# negative lookbehind below. Reading order: optional leading non-word, literal hashtag,
# negative lookahead for trailing word char.
_NOMEM_PATTERN = re.compile(r"(?i)(?<![\w])#nomem(?!\w)")
_OFFRECORD_PATTERN = re.compile(r"(?i)(?<![\w])#offrecord(?!\w)")

_DETECTED_BY = "deterministic_token_match_v1"


def _contains(pattern: re.Pattern[str], value: str | None) -> bool:
    if not value:
        return False
    return bool(pattern.search(value))


def detect_policy(
    text: str | None, caption: str | None
) -> tuple[PolicyOutcome, dict | None]:
    """Run deterministic detection over text + caption.

    Returns ``(policy, mark_payload)``:
    - ``policy`` is one of ``"normal"`` / ``"nomem"`` / ``"offrecord"``.
    - ``mark_payload`` is ``None`` for ``"normal"``; otherwise a dict with audit
      metadata for the ``offrecord_marks`` row.

    Detection rules:
    - ``#offrecord`` in text or caption → ``"offrecord"`` (takes precedence).
    - Else ``#nomem`` in text or caption → ``"nomem"``.
    - Else → ``"normal"``.

    Token matching is case-insensitive. Hashtags must stand alone — ``#nomembership``
    and ``some#nomem`` do NOT match.
    """
    has_offrecord = _contains(_OFFRECORD_PATTERN, text) or _contains(
        _OFFRECORD_PATTERN, caption
    )
    if has_offrecord:
        return (
            "offrecord",
            {
                "detected_by": _DETECTED_BY,
                "in_text": _contains(_OFFRECORD_PATTERN, text),
                "in_caption": _contains(_OFFRECORD_PATTERN, caption),
            },
        )

    has_nomem = _contains(_NOMEM_PATTERN, text) or _contains(_NOMEM_PATTERN, caption)
    if has_nomem:
        return (
            "nomem",
            {
                "detected_by": _DETECTED_BY,
                "in_text": _contains(_NOMEM_PATTERN, text),
                "in_caption": _contains(_NOMEM_PATTERN, caption),
            },
        )

    return ("normal", None)


# Telegram update payload event fields that carry user content. The redactor walks each
# of these (only one is typically present per update) and strips content fields.
_EVENT_FIELDS: tuple[str, ...] = (
    "message",
    "edited_message",
    "channel_post",
    "edited_channel_post",
)
# Content fields to drop when redacting an event. ``reply_to_message`` is intentionally
# left UN-touched: it's a snapshot of the parent message and does not represent the
# offrecord author's content; redacting it would also drop reply context that q&a
# layer never reaches anyway (downstream filters by ``memory_policy``).
_CONTENT_FIELDS_TO_DROP: tuple[str, ...] = (
    "text",
    "caption",
    "entities",
    "caption_entities",
)


def redact_raw_for_offrecord(raw_json: dict | None) -> dict | None:
    """Return a sanitized copy of ``raw_json`` with content fields removed.

    Used by ``bot/services/ingestion.py`` when ``detect_policy`` returns ``"offrecord"``.
    Drops ``text``, ``caption``, ``entities``, ``caption_entities`` from each known
    event field. Keeps ids, timestamps, sender info, hash, policy marker.

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
        scrubbed = {**original_event}
        for content_field in _CONTENT_FIELDS_TO_DROP:
            scrubbed.pop(content_field, None)
        redacted[event_field] = scrubbed
    return redacted
