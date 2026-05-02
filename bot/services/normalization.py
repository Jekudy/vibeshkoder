"""Message normalization (T1-09/10/11).

Extracts the normalized fields T1-05 added to ``chat_messages``:

- ``reply_to_message_id`` (T1-09) — Telegram reply target id, or None
- ``message_thread_id`` (T1-10) — forum-topic thread id, or None
- ``caption`` (T1-11) — media caption (separate from text), or None
- ``message_kind`` (T1-11) — classified message type ("text", "photo", "video", ...)

Pure functions — no DB writes here. Handlers call ``extract_normalized_fields()`` and
pass the dict into ``MessageRepo.save`` which already accepts the new keyword args from
T1-05.

T1-08 ``compute_content_hash`` consumes ``message_kind`` + ``caption`` directly from
this module's output, so the canonical recipe stays in sync.

Future tickets (T1-12 detector, T1-14 edited handler) will compose with this — they
take the normalized dict, pass through their own logic, and write versions/marks.
"""

from __future__ import annotations

from typing import Any

# Mapping aiogram Message attribute -> message_kind value. The classifier walks this in
# order; first non-None attribute wins. The order matters — e.g. forwarded messages
# carry both `forward_origin` and `text`, and we want to flag them as "forward" not
# "text".
_KIND_PROBES: tuple[tuple[str, str], ...] = (
    ("forward_origin", "forward"),
    ("photo", "photo"),
    ("video", "video"),
    ("voice", "voice"),
    ("audio", "audio"),
    ("document", "document"),
    ("sticker", "sticker"),
    ("animation", "animation"),
    ("video_note", "video_note"),
    ("location", "location"),
    ("contact", "contact"),
    ("poll", "poll"),
    ("dice", "dice"),
    ("new_chat_members", "service"),
    ("left_chat_member", "service"),
    ("pinned_message", "service"),
    ("text", "text"),
)


def classify_message_kind(message: Any) -> str:
    """Return a deterministic ``message_kind`` for an aiogram Message.

    The classifier inspects message attributes in priority order. First non-None match
    wins. Returns ``"unknown"`` if nothing matches.
    """
    for attr, kind in _KIND_PROBES:
        if getattr(message, attr, None) is not None:
            return kind
    return "unknown"


def extract_reply_to_message_id(message: Any) -> int | None:
    """Return the id of the message this one replies to, or None."""
    reply = getattr(message, "reply_to_message", None)
    if reply is None:
        return None
    return getattr(reply, "message_id", None)


def extract_message_thread_id(message: Any) -> int | None:
    """Return the forum-topic thread id, or None.

    For non-forum chats and messages outside topics, Telegram leaves
    ``message_thread_id`` unset (None). We pass it through unchanged.
    """
    return getattr(message, "message_thread_id", None)


def extract_caption(message: Any) -> str | None:
    """Return the media caption verbatim, or None.

    Caption is intentionally kept SEPARATE from ``text`` even though the gatekeeper bot
    historically saved caption-bearing messages with ``text=None, raw_json=full_payload``.
    Phase 4 q&a wants to cite captions as first-class content.
    """
    return getattr(message, "caption", None)


def extract_normalized_fields(message: Any) -> dict[str, Any]:
    """Return all four T1-09/10/11 fields in one call.

    Returned dict keys map 1:1 to the ``chat_messages`` columns added in T1-05, ready
    to splat into ``MessageRepo.save(**fields)`` once that method accepts them.
    """
    return {
        "reply_to_message_id": extract_reply_to_message_id(message),
        "message_thread_id": extract_message_thread_id(message),
        "caption": extract_caption(message),
        "message_kind": classify_message_kind(message),
    }


def extract_entities_unified(message: Any) -> list[dict] | None:
    """Merge ``message.entities`` and ``message.caption_entities`` into one list.

    Returns a deduplicated list of entity dicts (deduplicated by ``(offset, length,
    type)``), or ``None`` when neither attribute yields any entities.

    The legacy ``_build_entities_json`` in ``bot/handlers/edited_message.py`` only
    looked at ``entities`` and silently ignored ``caption_entities``. This caused
    caption-only photos with caption entities to produce empty entity lists, yielding
    a different content_hash than the unified helper. This function fixes that
    asymmetry — both the v1 creation path and the edit handler now use it.

    Deduplication preserves the first occurrence (entities before caption_entities)
    when both lists contain the same ``(offset, length, type)`` triple.
    """
    seen: set[tuple[int, int, str]] = set()
    result: list[dict] = []

    def _append_entities(attr_name: str) -> None:
        entities = getattr(message, attr_name, None)
        if entities is None:
            return
        for e in entities:
            try:
                d = e.model_dump(mode="json", exclude_none=True)
            except Exception:
                continue
            key = (d.get("offset", 0), d.get("length", 0), d.get("type", ""))
            if key not in seen:
                seen.add(key)
                result.append(d)

    _append_entities("entities")
    _append_entities("caption_entities")

    return result if result else None
