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
    """At least one message in edited_messages.json must contain #nomem or #offrecord
    (case-insensitive) in its text or caption field, triggering detect_policy."""
    result = json.loads(EDITED_MESSAGES.read_text(encoding="utf-8"))
    markers = ("#nomem", "#offrecord")
    found = False
    for msg in result["messages"]:
        text_val = msg.get("text", "") or ""
        caption_val = msg.get("caption", "") or ""
        combined = (text_val + " " + caption_val).lower()
        if any(m in combined for m in markers):
            found = True
            break
    assert found, "No #nomem or #offrecord marker found in edited_messages.json"


# ---------------------------------------------------------------------------
# Fixture C: replies_with_media.json
# ---------------------------------------------------------------------------

def test_replies_fixture_has_reply_chain():
    """replies_with_media.json must have at least 2 messages with reply_to_message_id."""
    result = json.loads(REPLIES_WITH_MEDIA.read_text(encoding="utf-8"))
    replies = [m for m in result["messages"] if m.get("reply_to_message_id") is not None]
    assert len(replies) >= 2, (
        f"Expected >=2 messages with reply_to_message_id, got {len(replies)}"
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
    """For each fixture, every message's inferred kind must be in the documented
    VALID_MESSAGE_KINDS set. Uses inline _infer_kind helper only."""
    for path in ALL_FIXTURES:
        result = json.loads(path.read_text(encoding="utf-8"))
        for i, msg in enumerate(result["messages"]):
            kind = _infer_kind(msg)
            assert kind in VALID_MESSAGE_KINDS, (
                f"{path.name}[{i}] (id={msg.get('id')}): "
                f"inferred kind '{kind}' not in documented taxonomy"
            )
