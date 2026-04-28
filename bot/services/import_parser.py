"""Telegram Desktop export dry-run parser (T2-01 / issue #94).

Reads a single-chat TD export JSON and returns an ``ImportDryRunReport`` with
counts and metadata. NEVER writes to the DB, never calls an LLM, never mutates
the input file.

Usage:
    from bot.services.import_parser import parse_export
    report = parse_export("/path/to/result.json")

CLI:
    python -m bot.cli import_dry_run /path/to/result.json
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Literal

from bot.services.governance import detect_policy

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

PolicyMarker = Literal["normal", "nomem", "offrecord"]
MessageKind = Literal[
    "text", "photo", "video", "voice", "audio", "document", "sticker",
    "animation", "video_note", "location", "contact", "poll", "dice",
    "forward", "service", "unknown",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MEDIA_KINDS: frozenset[str] = frozenset({
    "photo", "video", "voice", "audio", "document", "sticker",
    "animation", "video_note",
})

SUPPORTED_CHAT_TYPES: frozenset[str] = frozenset({
    "personal_chat", "private_group", "private_supergroup",
    "public_supergroup", "private_channel", "public_channel",
    "saved_messages",
})


# ---------------------------------------------------------------------------
# Report dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ImportDryRunReport:
    """Summary report from a dry-run parse. NO content is included — only counts and metadata.

    # NOTE: frozen=True does not deep-freeze list/dict fields; downstream consumers must treat these as read-only.
    """

    source_file: str
    """Absolute path to the export JSON."""

    chat_id: int | None
    """Top-level chat id from the envelope (negative for groups/channels)."""

    chat_name: str | None
    """Display name of the chat at export time."""

    chat_type: str | None
    """Export type string: 'personal_chat', 'private_supergroup', etc."""

    total_messages: int
    """Total entries in messages[] including service messages."""

    user_messages: int
    """Messages where type == 'message'."""

    service_messages: int
    """Messages where type == 'service'."""

    media_count: int
    """User messages with kind in MEDIA_KINDS."""

    distinct_users: int
    """Count of distinct from_id values across user messages (excludes channel posts)."""

    distinct_export_user_ids: list[str]
    """Sorted list of all distinct from_id strings seen in user messages."""

    date_range_start: datetime | None
    """Datetime of the earliest message (by date_unixtime)."""

    date_range_end: datetime | None
    """Datetime of the latest message (by date_unixtime)."""

    reply_count: int
    """User messages with reply_to_message_id present."""

    dangling_reply_count: int
    """Reply references pointing to an id NOT present in this export."""

    duplicate_export_msg_ids: list[int]
    """Message ids that appear more than once in messages[]."""

    edited_message_count: int
    """User messages that have an 'edited' field."""

    forward_count: int
    """User messages classified as 'forward' (forwarded_from non-empty)."""

    anonymous_channel_message_count: int
    """User messages with from_id starting with 'channel'."""

    message_kind_counts: dict[str, int]
    """Full breakdown of message_kind values across ALL messages (including service)."""

    policy_marker_counts: dict[str, int]
    """Counts of each PolicyMarker across user messages: {'normal': N, 'nomem': M, 'offrecord': K}."""

    parse_warnings: list[str]
    """Soft-fail warnings from tolerant-reader. Empty for clean exports."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_export(path: str | Path) -> ImportDryRunReport:
    """Parse a Telegram Desktop single-chat JSON export and return a dry-run report.

    NEVER WRITES TO DB. NEVER CALLS LLM. NEVER MUTATES INPUT.

    Reads ``path``, validates the envelope shape, walks ``messages[]`` once,
    accumulates statistics, calls ``bot.services.governance.detect_policy(text, caption)``
    on each user message to count policy markers.

    Raises:
        FileNotFoundError: if path doesn't exist.
        ValueError: if the JSON is unparseable OR the envelope doesn't match a
            single-chat TD export (e.g. has top-level ``chats:[]`` for full-account export).

    Soft-fail behaviour: unknown message shapes, missing optional fields, or
    type-mismatched optional fields are logged into ``parse_warnings`` and the
    parse continues. Hard-fail: missing ``messages`` array, full-account export
    envelope detected, or unparseable JSON.

    Performance contract: O(N) over messages, single pass. No full-message
    buffering beyond what's needed for distinct-id sets and duplicate detection.
    """
    path = Path(path)
    warnings: list[str] = []
    envelope = _load_envelope(path, warnings)

    chat_id: int | None = envelope.get("id")
    chat_name: str | None = envelope.get("name")
    chat_type: str | None = envelope.get("type")

    if chat_type and chat_type not in SUPPORTED_CHAT_TYPES:
        warnings.append(f"Unrecognised chat type: {chat_type!r}; proceeding anyway.")

    total_messages = 0
    user_messages = 0
    service_messages = 0
    media_count = 0
    forward_count = 0
    anonymous_channel_count = 0
    edited_count = 0
    reply_count = 0

    all_msg_ids: list[int] = []
    all_from_ids: list[str] = []
    all_datetimes: list[datetime] = []
    all_reply_targets: list[int] = []  # reply_to_message_id values from user messages

    kind_counter: Counter[str] = Counter()
    policy_counter: Counter[str] = Counter(normal=0, nomem=0, offrecord=0)

    for msg in _iter_messages(envelope, warnings):
        total_messages += 1
        msg_id = msg.get("id")
        if isinstance(msg_id, int):
            all_msg_ids.append(msg_id)

        msg_type = msg.get("type", "message")
        dt = _to_datetime(msg.get("date_unixtime"), msg.get("date"), warnings)
        if dt is not None:
            all_datetimes.append(dt)

        kind = _classify_td_kind(msg, warnings)
        kind_counter[kind] += 1

        if msg_type == "service":
            service_messages += 1
            continue

        # --- user message ---
        user_messages += 1

        from_id = msg.get("from_id")
        if isinstance(from_id, str):
            all_from_ids.append(from_id)
            if from_id.startswith("channel"):
                anonymous_channel_count += 1
        elif from_id is not None:
            warnings.append(
                f"messages[id={msg_id}]: expected from_id to be str, got "
                f"{type(from_id).__name__}; skipping for distinct-user count"
            )

        if "edited" in msg:
            edited_count += 1

        if kind in MEDIA_KINDS:
            media_count += 1

        if kind == "forward":
            forward_count += 1

        reply_to = msg.get("reply_to_message_id")
        if reply_to is not None:
            if isinstance(reply_to, int):
                reply_count += 1
                all_reply_targets.append(reply_to)
            else:
                warnings.append(
                    f"messages[id={msg_id}]: expected reply_to_message_id to be int, got "
                    f"{type(reply_to).__name__}; skipping reply tracking"
                )

        # Governance classification (must be called for every user message)
        text_str, caption_str = _extract_text_content(msg, kind)
        policy_outcome, _ = detect_policy(text_str or None, caption_str or None)
        policy_counter[policy_outcome] += 1

    # Post-pass aggregation
    known_ids: set[int] = set(all_msg_ids)
    dangling_reply_count = sum(
        1 for rid in all_reply_targets if rid not in known_ids
    )
    duplicate_ids = _check_duplicates(all_msg_ids)

    distinct_from_ids = sorted(set(all_from_ids))
    # distinct_users counts only user* prefixed ids (not channel*)
    distinct_user_from_ids = [fid for fid in distinct_from_ids if not fid.startswith("channel")]
    distinct_users = len(distinct_user_from_ids)

    date_range_start = min(all_datetimes) if all_datetimes else None
    date_range_end = max(all_datetimes) if all_datetimes else None

    return ImportDryRunReport(
        source_file=str(path.resolve()),
        chat_id=chat_id,
        chat_name=chat_name,
        chat_type=chat_type,
        total_messages=total_messages,
        user_messages=user_messages,
        service_messages=service_messages,
        media_count=media_count,
        distinct_users=distinct_users,
        distinct_export_user_ids=distinct_from_ids,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        reply_count=reply_count,
        dangling_reply_count=dangling_reply_count,
        duplicate_export_msg_ids=duplicate_ids,
        edited_message_count=edited_count,
        forward_count=forward_count,
        anonymous_channel_message_count=anonymous_channel_count,
        message_kind_counts=dict(kind_counter),
        policy_marker_counts=dict(policy_counter),
        parse_warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_envelope(path: Path, warnings: list[str]) -> dict:
    """Open, parse, and validate top-level shape.

    Raises FileNotFoundError if the file is absent.
    Raises ValueError if the JSON is invalid or has an unsupported envelope shape.
    """
    if not path.exists():
        raise FileNotFoundError(f"Export file not found: {path}")

    try:
        with path.open(encoding="utf-8") as fh:
            # NOTE: full file is read into memory; size guard deferred to follow-up ticket — see PR description.
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Failed to parse JSON from {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object at top level, got {type(data).__name__}: {path}")

    # Reject full-account archives (they have a top-level 'chats' list)
    if "chats" in data and isinstance(data.get("chats"), list):
        raise ValueError(
            f"Unsupported export type: full-account archive detected "
            f"(top-level 'chats' list found). Please export a single chat. File: {path}"
        )

    if "messages" not in data:
        raise ValueError(
            f"Missing required 'messages' array in export envelope. File: {path}"
        )

    if not isinstance(data["messages"], list):
        raise ValueError(
            f"Expected 'messages' to be a list, got {type(data['messages']).__name__}. File: {path}"
        )

    return data


def _classify_td_kind(msg: dict, warnings: list[str] | None = None) -> MessageKind:
    """Classify a TD export message dict into a MessageKind value.

    Priority order (highest to lowest):
    1. type == 'service'  → 'service'
    2. forwarded_from set → 'forward'
    3. media_type or photo field → media kind
    4. location_information / contact_information / poll / dice → specific kind
    5. mime_type starts with 'application/' → 'document'
    6. text or text_entities present → 'text'
    7. fallback → 'unknown'

    This mirrors the priority table in docs/memory-system/telegram-desktop-export-schema.md §3.
    Service and forward outrank media kinds to match the live-ingestion _KIND_PROBES priority.

    Args:
        msg: The message dict from the TD export.
        warnings: If provided, unknown media_type values are appended as parse warnings.
    """
    if msg.get("type") == "service":
        return "service"

    if msg.get("forwarded_from") is not None:
        return "forward"

    media_type = msg.get("media_type")
    if media_type is not None:
        _MEDIA_TYPE_MAP: dict[str, MessageKind] = {
            "photo": "photo",
            "video_file": "video",
            "voice_message": "voice",
            "audio_file": "audio",
            "sticker": "sticker",
            "animation": "animation",
            "video_message": "video_note",
        }
        if media_type in _MEDIA_TYPE_MAP:
            return _MEDIA_TYPE_MAP[media_type]
        # Unknown media_type: return 'unknown' and emit a parse warning.
        # This prevents silently misclassifying new TD media kinds as 'text'.
        if warnings is not None:
            warnings.append(
                f"Unknown media_type {media_type!r}; classifying as 'unknown'."
            )
        return "unknown"

    # photo field without media_type discriminator
    if msg.get("photo"):
        return "photo"

    if msg.get("location_information"):
        return "location"
    if msg.get("contact_information"):
        return "contact"
    if msg.get("poll"):
        return "poll"
    if msg.get("dice"):
        return "dice"

    mime = msg.get("mime_type", "")
    if isinstance(mime, str) and mime.startswith("application/"):
        return "document"

    if msg.get("text") is not None or msg.get("text_entities") is not None:
        return "text"

    return "unknown"


def _extract_text_string(text_field) -> str:
    """Flatten TD export's ``text`` field to a plain string.

    TD's ``text`` field can be:
    - A plain string: returned as-is.
    - A mixed array of plain strings and entity dicts (each with a ``text`` key).
    - None or missing: returns empty string.

    Tolerant: non-str/non-dict items in the array are skipped or coerced:
    - int/float → coerced to str.
    - dict with non-string ``text`` value (e.g. None, nested dict) → skipped.
    - nested list → recursed into.
    - anything else unexpected → skipped without raising.

    Always returns a str. Never raises.

    Example mixed array:
        ["Hello ", {"type": "bold", "text": "world"}]
        → "Hello world"
    """
    if text_field is None:
        return ""
    if isinstance(text_field, str):
        return text_field
    if isinstance(text_field, (int, float)):
        return str(text_field)
    if isinstance(text_field, list):
        parts: list[str] = []
        for item in text_field:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, (int, float)):
                parts.append(str(item))
            elif isinstance(item, list):
                # Recurse into nested lists (defensive; TD doesn't normally produce these)
                parts.append(_extract_text_string(item))
            elif isinstance(item, dict):
                text_val = item.get("text")
                if isinstance(text_val, str):
                    parts.append(text_val)
                # Non-string text values (None, nested dict, int, etc.) are skipped
            # Any other type (bool, bytes, etc.) is skipped without raising
        return "".join(parts)
    return ""


def _extract_text_content(msg: dict, kind: MessageKind) -> tuple[str, str]:
    """Return (text_str, caption_str) for governance classification.

    Design decision: for media messages, TD uses the ``text`` field as a caption
    (per schema §3.1 — 'For photo, video, voice, and other media messages the
    text field contains the caption'). We pass it as ``caption`` to
    ``detect_policy(text=..., caption=...)`` to match the semantics of live
    ingestion where captions are a separate field.

    - Media messages (kind in MEDIA_KINDS): pass (text="", caption=text_value).
    - Non-media messages (text, forward, unknown, service): pass (text=text_value, caption="").

    Service messages: both empty (governance not called for service messages, but
    this helper may still be invoked for completeness; caller skips service msgs).

    Fallback: if ``text`` is empty/missing but ``text_entities`` is present, flatten
    ``text_entities`` to extract the full text content. This handles malformed exports
    where governance hashtags (#nomem, #offrecord) are present only in text_entities.
    """
    raw_text = msg.get("text", "")
    text_str = _extract_text_string(raw_text)

    # Fallback to text_entities when text is empty: governance hashtags may live there
    if not text_str:
        entities = msg.get("text_entities")
        if entities is not None:
            text_str = _extract_text_string(entities)

    if kind in MEDIA_KINDS:
        # TD text field is the caption for media messages
        return ("", text_str)
    else:
        return (text_str, "")


def _iter_messages(envelope: dict, warnings: list[str]) -> Iterable[dict]:
    """Yield message dicts from the envelope's messages array.

    Tolerant: malformed entries (non-dict, missing id) emit a warning and are skipped.
    """
    messages = envelope.get("messages", [])
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            warnings.append(f"messages[{i}]: expected dict, got {type(msg).__name__}; skipping.")
            continue
        if "id" not in msg:
            warnings.append(f"messages[{i}]: missing 'id' field; skipping.")
            continue
        yield msg


def _to_datetime(
    unix_str: str | None,
    iso_str: str | None,
    warnings: list[str],
) -> datetime | None:
    """Parse ``date_unixtime`` (preferred) or fallback ``date`` ISO string.

    Returns None and logs a warning if neither is parseable.
    """
    if unix_str is not None:
        try:
            ts = float(unix_str)
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, TypeError, OSError, OverflowError) as exc:
            warnings.append(f"Could not parse date_unixtime {unix_str!r}: {exc}")

    if iso_str is not None:
        try:
            # ISO 8601 — may or may not have timezone; treat as UTC
            dt = datetime.fromisoformat(iso_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError) as exc:
            warnings.append(f"Could not parse ISO date {iso_str!r}: {exc}")

    warnings.append("Message has no parseable date; date_range may be incomplete.")
    return None


def _check_duplicates(ids: list[int]) -> list[int]:
    """Return ids that appear more than once in the list."""
    counts = Counter(ids)
    return sorted(mid for mid, cnt in counts.items() if cnt > 1)
