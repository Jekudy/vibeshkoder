"""Tests for Telegram Desktop export JSON fixtures (T2-NEW-A / issue #91).

These tests verify the three fixture files in tests/fixtures/td_export/ are
well-formed, structurally correct, and cover the edge cases documented in
docs/memory-system/telegram-desktop-export-schema.md.

Only stdlib (json, pathlib) + pytest are used. All tests pass offline, no DB,
no network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURE_DIR = Path(__file__).parent / "td_export"
SMALL_CHAT = FIXTURE_DIR / "small_chat.json"
EDITED_MESSAGES = FIXTURE_DIR / "edited_messages.json"
REPLIES_WITH_MEDIA = FIXTURE_DIR / "replies_with_media.json"

ALL_FIXTURES = [SMALL_CHAT, EDITED_MESSAGES, REPLIES_WITH_MEDIA]

# Documented message_kind taxonomy (must match normalization.py _KIND_PROBES + "unknown")
VALID_MESSAGE_KINDS = {
    "text", "photo", "video", "voice", "audio", "document",
    "sticker", "animation", "video_note", "location", "contact",
    "poll", "dice", "forward", "service", "unknown",
}


def _infer_kind(msg: dict[str, Any]) -> str:
    """Inline helper: infer message_kind from a TD export message dict.

    Mirrors the priority ordering in bot/services/normalization.py::_KIND_PROBES
    translated to the TD export field shapes described in the schema doc.

    Must NOT import from bot/ — fixtures tests are standalone.
    """
    if msg.get("type") == "service":
        return "service"
    if msg.get("forwarded_from") is not None:
        return "forward"
    media_type = msg.get("media_type")
    if media_type == "photo" or "photo" in msg and msg.get("photo"):
        return "photo"
    if media_type == "video_file":
        return "video"
    if media_type == "voice_message":
        return "voice"
    if media_type == "audio_file":
        return "audio"
    if media_type == "sticker":
        return "sticker"
    if media_type == "animation":
        return "animation"
    if media_type == "video_message":
        return "video_note"
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
    # photo field without media_type discriminator
    if msg.get("photo"):
        return "photo"
    if msg.get("text") is not None or msg.get("text_entities") is not None:
        return "text"
    return "unknown"


# ---------------------------------------------------------------------------
# Structural validity
# ---------------------------------------------------------------------------

def test_each_fixture_is_valid_json():
    """All three fixtures must be parseable by json.load without error."""
    for path in ALL_FIXTURES:
        result = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(result, dict), f"{path.name}: top-level must be a dict"


def test_each_fixture_has_messages_array():
    """`result['messages']` must be a non-empty list in every fixture."""
    for path in ALL_FIXTURES:
        result = json.loads(path.read_text(encoding="utf-8"))
        assert "messages" in result, f"{path.name}: missing 'messages' key"
        assert isinstance(result["messages"], list), f"{path.name}: 'messages' must be a list"
        assert len(result["messages"]) > 0, f"{path.name}: 'messages' must not be empty"


def test_each_message_has_id_and_date():
    """Every entry in `messages` must have `id` and `date` fields."""
    for path in ALL_FIXTURES:
        result = json.loads(path.read_text(encoding="utf-8"))
        for i, msg in enumerate(result["messages"]):
            assert "id" in msg, f"{path.name}[{i}]: missing 'id'"
            assert "date" in msg, f"{path.name}[{i}]: missing 'date'"


# ---------------------------------------------------------------------------
# Fixture A: small_chat.json
# ---------------------------------------------------------------------------

def test_small_chat_under_10_messages():
    """small_chat.json must have fewer than 10 messages."""
    result = json.loads(SMALL_CHAT.read_text(encoding="utf-8"))
    count = len(result["messages"])
    assert count < 10, f"Expected <10 messages, got {count}"


# ---------------------------------------------------------------------------
# Fixture B: edited_messages.json
# ---------------------------------------------------------------------------

def test_edited_fixture_contains_edited_field():
    """At least one message in edited_messages.json must have an `edited` field."""
    result = json.loads(EDITED_MESSAGES.read_text(encoding="utf-8"))
    edited = [m for m in result["messages"] if "edited" in m]
    assert len(edited) >= 1, "No message with 'edited' field found in edited_messages.json"


def test_edited_fixture_contains_policy_markers():
    """edited_messages.json must contain BOTH #nomem AND #offrecord (case-insensitive)
    across its messages — the fixture is explicitly designed to exercise both policies."""
    result = json.loads(EDITED_MESSAGES.read_text(encoding="utf-8"))

    def _extract_text(msg: dict[str, Any]) -> str:
        """Extract all visible text from a message for policy scanning."""
        text_val = msg.get("text", "") or ""
        if isinstance(text_val, list):
            # mixed-array form: extract string segments and entity text
            parts = []
            for item in text_val:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    parts.append(item.get("text", ""))
            text_val = " ".join(parts)
        caption_val = msg.get("caption", "") or ""
        return (text_val + " " + caption_val).lower()

    all_text = " ".join(_extract_text(m) for m in result["messages"])

    assert "#nomem" in all_text, (
        "edited_messages.json must contain at least one message with #nomem marker"
    )
    assert "#offrecord" in all_text, (
        "edited_messages.json must contain at least one message with #offrecord marker"
    )


# ---------------------------------------------------------------------------
# Fixture C: replies_with_media.json
# ---------------------------------------------------------------------------

def test_replies_fixture_has_reply_chain():
    """replies_with_media.json must contain a documented A→B→C reply chain:
    an id_a, id_b, id_c where id_b.reply_to == id_a.id AND id_c.reply_to == id_b.id."""
    result = json.loads(REPLIES_WITH_MEDIA.read_text(encoding="utf-8"))
    msgs_by_id = {m["id"]: m for m in result["messages"]}

    chain_found = None
    for msg_c in result["messages"]:
        id_b = msg_c.get("reply_to_message_id")
        if id_b is None:
            continue
        msg_b = msgs_by_id.get(id_b)
        if msg_b is None:
            continue
        id_a = msg_b.get("reply_to_message_id")
        if id_a is None:
            continue
        msg_a = msgs_by_id.get(id_a)
        if msg_a is None:
            continue
        chain_found = (msg_a["id"], msg_b["id"], msg_c["id"])
        break

    assert chain_found is not None, (
        "No A→B→C reply chain found in replies_with_media.json. "
        "Expected: msg_b.reply_to == msg_a.id AND msg_c.reply_to == msg_b.id. "
        f"Messages present: {[m['id'] for m in result['messages']]}"
    )


def test_replies_fixture_has_anonymous_channel_post():
    """replies_with_media.json must have at least one message with from_id starting with 'channel'."""
    result = json.loads(REPLIES_WITH_MEDIA.read_text(encoding="utf-8"))
    channel_posts = [
        m for m in result["messages"]
        if isinstance(m.get("from_id"), str) and m["from_id"].startswith("channel")
    ]
    assert len(channel_posts) >= 1, (
        "No anonymous channel post (from_id starting with 'channel') found in replies_with_media.json"
    )


def test_replies_fixture_has_dangling_reply():
    """replies_with_media.json must have a reply_to_message_id that does not
    correspond to any message id in the fixture (dangling reference)."""
    result = json.loads(REPLIES_WITH_MEDIA.read_text(encoding="utf-8"))
    all_ids = {m["id"] for m in result["messages"]}
    dangling = [
        m for m in result["messages"]
        if m.get("reply_to_message_id") is not None
        and m["reply_to_message_id"] not in all_ids
    ]
    assert len(dangling) >= 1, (
        "No dangling reply_to_message_id found in replies_with_media.json "
        "(need at least one reply pointing to a message NOT in the fixture)"
    )


# ---------------------------------------------------------------------------
# Taxonomy coverage
# ---------------------------------------------------------------------------

def test_message_kinds_match_taxonomy():
    """Across all fixtures:
    1. No hand-crafted message may be classified as 'unknown' — all are known-shape.
    2. The fixture set collectively covers at least: text, photo, voice, forward, service,
       video (meaningful subset — not required to cover all 16 kinds).
    """
    REQUIRED_KINDS = {"text", "photo", "voice", "forward", "service", "video"}

    observed_kinds: set[str] = set()
    for path in ALL_FIXTURES:
        result = json.loads(path.read_text(encoding="utf-8"))
        for i, msg in enumerate(result["messages"]):
            kind = _infer_kind(msg)
            assert kind in VALID_MESSAGE_KINDS, (
                f"{path.name}[{i}] (id={msg.get('id')}): "
                f"inferred kind '{kind}' not in documented taxonomy"
            )
            assert kind != "unknown", (
                f"{path.name}[{i}] (id={msg.get('id')}): "
                f"hand-crafted fixture message classified as 'unknown' — "
                f"all fixture messages must have an identifiable kind"
            )
            observed_kinds.add(kind)

    missing = REQUIRED_KINDS - observed_kinds
    assert not missing, (
        f"Fixture set does not cover required message kinds: {missing}. "
        f"Observed kinds: {observed_kinds}"
    )


# ---------------------------------------------------------------------------
# NIT-3: date_unixtime presence
# ---------------------------------------------------------------------------

def test_each_message_has_date_unixtime():
    """Every NON-service message in every fixture must have a 'date_unixtime' field
    (string representation of a unix timestamp). Service messages may omit it."""
    for path in ALL_FIXTURES:
        result = json.loads(path.read_text(encoding="utf-8"))
        for i, msg in enumerate(result["messages"]):
            if msg.get("type") == "service":
                continue  # service messages may omit date_unixtime
            assert "date_unixtime" in msg, (
                f"{path.name}[{i}] (id={msg.get('id')}): "
                f"non-service message missing 'date_unixtime' field"
            )
            assert isinstance(msg["date_unixtime"], str), (
                f"{path.name}[{i}] (id={msg.get('id')}): "
                f"'date_unixtime' must be a string, got {type(msg['date_unixtime']).__name__}"
            )


# ---------------------------------------------------------------------------
# HIGH-3: small_chat fixture text mixed-array form coverage
# ---------------------------------------------------------------------------

def test_small_chat_has_mixed_array_text():
    """small_chat.json message id=1005 must use the mixed-array form for 'text'
    (list containing both plain strings and entity dicts) to demonstrate that shape."""
    result = json.loads(SMALL_CHAT.read_text(encoding="utf-8"))
    msg_1005 = next((m for m in result["messages"] if m["id"] == 1005), None)
    assert msg_1005 is not None, "Message id=1005 not found in small_chat.json"

    text_val = msg_1005.get("text")
    assert isinstance(text_val, list), (
        f"Message id=1005 'text' must be a list (mixed-array form), got {type(text_val).__name__}"
    )
    has_plain_str = any(isinstance(item, str) for item in text_val)
    has_entity_dict = any(isinstance(item, dict) for item in text_val)
    assert has_plain_str and has_entity_dict, (
        "Message id=1005 'text' array must contain both plain strings and entity dicts "
        f"(interleaved mixed form). Got: {text_val}"
    )
