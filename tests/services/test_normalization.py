"""T1-09/10/11 normalization service tests.

Pure-Python tests over aiogram-shaped objects via SimpleNamespace — no DB needed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.usefixtures("app_env")


def _msg(**attrs):
    """Build a Message-shaped namespace. All attrs default to None unless overridden."""
    defaults = dict(
        text=None,
        caption=None,
        message_thread_id=None,
        reply_to_message=None,
        forward_origin=None,
        photo=None,
        video=None,
        voice=None,
        audio=None,
        document=None,
        sticker=None,
        animation=None,
        video_note=None,
        location=None,
        contact=None,
        poll=None,
        dice=None,
        new_chat_members=None,
        left_chat_member=None,
        pinned_message=None,
    )
    defaults.update(attrs)
    return SimpleNamespace(**defaults)


# ─── T1-09 reply_to_message_id ─────────────────────────────────────────────────────────────


def test_extract_reply_to_message_id_present(app_env) -> None:
    from bot.services.normalization import extract_reply_to_message_id

    msg = _msg(reply_to_message=SimpleNamespace(message_id=42))
    assert extract_reply_to_message_id(msg) == 42


def test_extract_reply_to_message_id_absent(app_env) -> None:
    from bot.services.normalization import extract_reply_to_message_id

    msg = _msg(reply_to_message=None)
    assert extract_reply_to_message_id(msg) is None


def test_extract_reply_to_message_id_unresolved_target(app_env) -> None:
    """Telegram sometimes ships a reply_to_message stub without message_id (rare).
    Function returns None gracefully rather than raising."""
    from bot.services.normalization import extract_reply_to_message_id

    msg = _msg(reply_to_message=SimpleNamespace(message_id=None))
    assert extract_reply_to_message_id(msg) is None


# ─── T1-10 message_thread_id ───────────────────────────────────────────────────────────────


def test_extract_message_thread_id_present(app_env) -> None:
    from bot.services.normalization import extract_message_thread_id

    msg = _msg(message_thread_id=999)
    assert extract_message_thread_id(msg) == 999


def test_extract_message_thread_id_absent(app_env) -> None:
    from bot.services.normalization import extract_message_thread_id

    msg = _msg(message_thread_id=None)
    assert extract_message_thread_id(msg) is None


# ─── T1-11 caption ─────────────────────────────────────────────────────────────────────────


def test_extract_caption_present(app_env) -> None:
    from bot.services.normalization import extract_caption

    msg = _msg(caption="check the photo")
    assert extract_caption(msg) == "check the photo"


def test_extract_caption_absent(app_env) -> None:
    from bot.services.normalization import extract_caption

    msg = _msg(caption=None)
    assert extract_caption(msg) is None


# ─── T1-11 message_kind ────────────────────────────────────────────────────────────────────


def test_classify_text_message(app_env) -> None:
    from bot.services.normalization import classify_message_kind

    msg = _msg(text="hello")
    assert classify_message_kind(msg) == "text"


def test_classify_photo_message(app_env) -> None:
    from bot.services.normalization import classify_message_kind

    msg = _msg(photo=[SimpleNamespace(file_id="x")], caption="cap")
    assert classify_message_kind(msg) == "photo"


def test_classify_video_message(app_env) -> None:
    from bot.services.normalization import classify_message_kind

    msg = _msg(video=SimpleNamespace(file_id="x"))
    assert classify_message_kind(msg) == "video"


def test_classify_voice_message(app_env) -> None:
    from bot.services.normalization import classify_message_kind

    msg = _msg(voice=SimpleNamespace(file_id="x"))
    assert classify_message_kind(msg) == "voice"


def test_classify_document_message(app_env) -> None:
    from bot.services.normalization import classify_message_kind

    msg = _msg(document=SimpleNamespace(file_id="x"))
    assert classify_message_kind(msg) == "document"


def test_classify_forward_takes_priority_over_text(app_env) -> None:
    """Forwarded messages carry both forward_origin AND text. The classifier flags
    them as 'forward' so downstream knows the content was authored elsewhere."""
    from bot.services.normalization import classify_message_kind

    msg = _msg(forward_origin=SimpleNamespace(type="user"), text="forwarded text")
    assert classify_message_kind(msg) == "forward"


def test_classify_forward_takes_priority_over_caption(app_env) -> None:
    """A forwarded photo with caption is still 'forward' — author attribution wins
    over media-kind classification for downstream q&a citation correctness."""
    from bot.services.normalization import classify_message_kind

    msg = _msg(
        forward_origin=SimpleNamespace(type="channel"),
        photo=[SimpleNamespace(file_id="x")],
        caption="forwarded caption",
    )
    assert classify_message_kind(msg) == "forward"


def test_classify_service_message(app_env) -> None:
    from bot.services.normalization import classify_message_kind

    msg = _msg(new_chat_members=[SimpleNamespace(id=1)])
    assert classify_message_kind(msg) == "service"


def test_classify_unknown_falls_back(app_env) -> None:
    """An empty Message with no recognizable attributes returns 'unknown'."""
    from bot.services.normalization import classify_message_kind

    msg = _msg()
    assert classify_message_kind(msg) == "unknown"


# ─── extract_normalized_fields composition ────────────────────────────────────────────────


def test_extract_all_fields_text_message(app_env) -> None:
    from bot.services.normalization import extract_normalized_fields

    msg = _msg(
        text="hi",
        reply_to_message=SimpleNamespace(message_id=10),
        message_thread_id=5,
    )
    out = extract_normalized_fields(msg)
    assert out == {
        "reply_to_message_id": 10,
        "message_thread_id": 5,
        "caption": None,
        "message_kind": "text",
    }


def test_extract_all_fields_media_with_caption(app_env) -> None:
    """Media-only message with caption — text is None, caption preserved separately,
    kind is media-specific (not 'text')."""
    from bot.services.normalization import extract_normalized_fields

    msg = _msg(
        photo=[SimpleNamespace(file_id="abc")],
        caption="see this",
    )
    out = extract_normalized_fields(msg)
    assert out == {
        "reply_to_message_id": None,
        "message_thread_id": None,
        "caption": "see this",
        "message_kind": "photo",
    }


def test_extract_all_fields_minimal(app_env) -> None:
    from bot.services.normalization import extract_normalized_fields

    msg = _msg()
    out = extract_normalized_fields(msg)
    assert out == {
        "reply_to_message_id": None,
        "message_thread_id": None,
        "caption": None,
        "message_kind": "unknown",
    }


# ─── _extract_entities_unified (commit 1 — hotfix #164) ──────────────────────


def _make_entity(offset: int, length: int, entity_type: str) -> SimpleNamespace:
    """Build a SimpleNamespace that mimics an aiogram MessageEntity."""
    return SimpleNamespace(
        offset=offset,
        length=length,
        type=entity_type,
        model_dump=lambda mode="json", exclude_none=True: {
            "offset": offset,
            "length": length,
            "type": entity_type,
        },
    )


def test_extract_entities_unified_no_entities(app_env) -> None:
    """Message with no entities and no caption_entities returns None."""
    from bot.services.normalization import extract_entities_unified

    msg = _msg(entities=None, caption_entities=None)
    assert extract_entities_unified(msg) is None


def test_extract_entities_unified_only_entities(app_env) -> None:
    """Message with entities but no caption_entities returns entities list."""
    from bot.services.normalization import extract_entities_unified

    e = _make_entity(0, 5, "bold")
    msg = _msg(entities=[e], caption_entities=None)
    result = extract_entities_unified(msg)
    assert result is not None
    assert len(result) == 1
    assert result[0]["type"] == "bold"


def test_extract_entities_unified_only_caption_entities(app_env) -> None:
    """Caption-only photo with caption_entities — unified returns the caption entities."""
    from bot.services.normalization import extract_entities_unified

    ce = _make_entity(0, 5, "bold")
    msg = _msg(
        photo=[SimpleNamespace(file_id="abc")],
        entities=None,
        caption_entities=[ce],
    )
    result = extract_entities_unified(msg)
    assert result is not None
    assert len(result) == 1
    assert result[0]["type"] == "bold"


def test_extract_entities_unified_merges_caption_entities(app_env) -> None:
    """Both entities and caption_entities are merged and deduplicated by (offset, length, type)."""
    from bot.services.normalization import extract_entities_unified

    e1 = _make_entity(0, 5, "bold")
    e2 = _make_entity(6, 3, "italic")
    # caption_entity at same position as e1 (duplicate)
    ce_dup = _make_entity(0, 5, "bold")
    # caption_entity at new position
    ce_new = _make_entity(10, 4, "code")

    msg = _msg(
        entities=[e1, e2],
        caption_entities=[ce_dup, ce_new],
    )
    result = extract_entities_unified(msg)
    assert result is not None
    # After dedup: e1(bold 0-5), e2(italic 6-3), ce_new(code 10-4) — ce_dup is dropped
    assert len(result) == 3
    types = {r["type"] for r in result}
    assert types == {"bold", "italic", "code"}


def test_extract_entities_unified_old_build_entities_json_would_miss_caption(app_env) -> None:
    """Counter-fixture: old _build_entities_json (entities only) returns [] for caption-only photo.

    The unified helper returns the caption entities — proving the asymmetry is fixed.
    """
    from bot.services.normalization import extract_entities_unified

    ce = _make_entity(0, 5, "bold")
    # caption-only photo — no .entities attribute
    msg = _msg(
        photo=[SimpleNamespace(file_id="abc")],
        entities=None,
        caption_entities=[ce],
    )
    # Simulate old _build_entities_json behaviour (entities only, ignoring caption_entities)
    old_result = getattr(msg, "entities", None)  # returns None
    assert old_result is None  # old path would produce [] / None — missed caption entities

    # Unified helper captures them
    new_result = extract_entities_unified(msg)
    assert new_result is not None
    assert len(new_result) == 1
