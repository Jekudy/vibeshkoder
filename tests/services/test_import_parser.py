"""Tests for bot/services/import_parser.py (T2-01 / issue #94).

All tests are offline: no DB, no network, no LLM.
Fixtures live in tests/fixtures/td_export/.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "td_export"
SMALL_CHAT = FIXTURE_DIR / "small_chat.json"
EDITED_MESSAGES = FIXTURE_DIR / "edited_messages.json"
REPLIES_WITH_MEDIA = FIXTURE_DIR / "replies_with_media.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse(path: Path):
    from bot.services.import_parser import parse_export
    return parse_export(path)


def _report_as_dict(path: Path) -> dict:
    from dataclasses import asdict
    return asdict(_parse(path))


# ---------------------------------------------------------------------------
# 1. small_chat basic counts
# ---------------------------------------------------------------------------

def test_parse_small_chat_returns_report():
    report = _parse(SMALL_CHAT)
    assert report.chat_name == "Vibe Community Chat"
    assert report.chat_id == -1001999999999
    assert report.chat_type == "private_supergroup"
    assert report.total_messages == 6
    assert report.user_messages == 5
    assert report.service_messages == 1


# ---------------------------------------------------------------------------
# 2. distinct users
# ---------------------------------------------------------------------------

def test_parse_small_chat_distinct_users():
    report = _parse(SMALL_CHAT)
    # user1000001, user1000002, user1000003 — 3 distinct user from_id values
    assert report.distinct_users == 3
    assert sorted(report.distinct_export_user_ids) == [
        "user1000001", "user1000002", "user1000003"
    ]


# ---------------------------------------------------------------------------
# 3. message_kind_counts
# ---------------------------------------------------------------------------

def test_parse_small_chat_kind_counts():
    report = _parse(SMALL_CHAT)
    counts = report.message_kind_counts
    assert counts.get("text", 0) >= 1    # id 1001, 1004
    assert counts.get("photo", 0) >= 1   # id 1002
    assert counts.get("voice", 0) >= 1   # id 1003
    assert counts.get("forward", 0) >= 1 # id 1005
    assert counts.get("service", 0) == 1 # id 1006


# ---------------------------------------------------------------------------
# 4. date range
# ---------------------------------------------------------------------------

def test_parse_small_chat_date_range():
    report = _parse(SMALL_CHAT)
    assert report.date_range_start is not None
    assert report.date_range_end is not None
    # earliest: 1705312800 → 2024-01-15T10:00:00
    # latest: 1705314600 → 2024-01-15T10:30:00
    assert report.date_range_start.timestamp() == pytest.approx(1705312800, abs=1)
    assert report.date_range_end.timestamp() == pytest.approx(1705314600, abs=1)
    assert report.date_range_start <= report.date_range_end


# ---------------------------------------------------------------------------
# 5. reply count
# ---------------------------------------------------------------------------

def test_parse_small_chat_reply_count():
    report = _parse(SMALL_CHAT)
    # id 1004 replies to 1001 (non-dangling)
    assert report.reply_count == 1
    assert report.dangling_reply_count == 0


# ---------------------------------------------------------------------------
# 6. no duplicates
# ---------------------------------------------------------------------------

def test_parse_small_chat_no_duplicates():
    report = _parse(SMALL_CHAT)
    assert report.duplicate_export_msg_ids == []


# ---------------------------------------------------------------------------
# 7. no policy markers in small_chat
# ---------------------------------------------------------------------------

def test_parse_small_chat_no_policy_markers():
    report = _parse(SMALL_CHAT)
    # small_chat has no #nomem or #offrecord — all 5 user messages are normal
    assert report.policy_marker_counts == {"normal": 5, "nomem": 0, "offrecord": 0}


# ---------------------------------------------------------------------------
# 8. edited_messages edit count
# ---------------------------------------------------------------------------

def test_parse_edited_messages_counts_edits():
    report = _parse(EDITED_MESSAGES)
    # id 2001 and id 2002 both have `edited` field
    assert report.edited_message_count >= 2


# ---------------------------------------------------------------------------
# 9. edited_messages policy counts
# ---------------------------------------------------------------------------

def test_parse_edited_messages_counts_policy():
    report = _parse(EDITED_MESSAGES)
    assert report.policy_marker_counts["nomem"] >= 1    # id 2003 has #nomem
    assert report.policy_marker_counts["offrecord"] >= 1  # id 2004 has #offrecord


# ---------------------------------------------------------------------------
# 10. replies_with_media dangling reply
# ---------------------------------------------------------------------------

def test_parse_replies_with_media_dangling_reply():
    report = _parse(REPLIES_WITH_MEDIA)
    # id 3007 references reply_to_message_id=2999 which is not in the export
    assert report.dangling_reply_count >= 1


# ---------------------------------------------------------------------------
# 11. anonymous channel messages
# ---------------------------------------------------------------------------

def test_parse_replies_anonymous_channel():
    report = _parse(REPLIES_WITH_MEDIA)
    # id 3006 has from_id="channel1000050"
    assert report.anonymous_channel_message_count >= 1


# ---------------------------------------------------------------------------
# 12. no content in report
# ---------------------------------------------------------------------------

def test_parse_returns_no_content_in_report():
    from dataclasses import asdict
    report = _parse(SMALL_CHAT)
    payload = asdict(report)
    # Convert datetimes to strings for serialisation
    if payload.get("date_range_start"):
        payload["date_range_start"] = payload["date_range_start"].isoformat()
    if payload.get("date_range_end"):
        payload["date_range_end"] = payload["date_range_end"].isoformat()
    serialised = json.dumps(payload)
    # sentinel substrings unique to fixture message content
    assert "Glad to be here" not in serialised
    assert "warm welcome" not in serialised
    assert "meetup" not in serialised


# ---------------------------------------------------------------------------
# 13. invalid JSON raises
# ---------------------------------------------------------------------------

def test_parse_invalid_json_raises(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json", encoding="utf-8")
    with pytest.raises((ValueError, json.JSONDecodeError)):
        _parse(bad)


# ---------------------------------------------------------------------------
# 14. missing messages array raises
# ---------------------------------------------------------------------------

def test_parse_missing_messages_array_raises(tmp_path: Path):
    f = tmp_path / "no_messages.json"
    f.write_text(json.dumps({"name": "X"}), encoding="utf-8")
    with pytest.raises(ValueError, match="messages"):
        _parse(f)


# ---------------------------------------------------------------------------
# 15. full account export raises
# ---------------------------------------------------------------------------

def test_parse_full_account_export_raises(tmp_path: Path):
    f = tmp_path / "full_export.json"
    f.write_text(json.dumps({"chats": [{"name": "Chat", "messages": []}]}), encoding="utf-8")
    with pytest.raises(ValueError):
        _parse(f)


# ---------------------------------------------------------------------------
# 16. missing file raises FileNotFoundError
# ---------------------------------------------------------------------------

def test_parse_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        _parse(Path("/nonexistent/path/export.json"))


# ---------------------------------------------------------------------------
# 17. mixed-array text message parses without crash
# ---------------------------------------------------------------------------

def test_parse_mixed_array_text_handled():
    report = _parse(SMALL_CHAT)
    # id 1005 has mixed-array text AND forwarded_from — must parse cleanly
    assert report.parse_warnings == [] or isinstance(report.parse_warnings, list)
    # The forward kind must be counted (id 1005 is classified as forward)
    assert report.message_kind_counts.get("forward", 0) >= 1


# ---------------------------------------------------------------------------
# 18. helper unit tests: _extract_text_string
# ---------------------------------------------------------------------------

def test_extract_text_string_handles_string():
    from bot.services.import_parser import _extract_text_string
    assert _extract_text_string("hello") == "hello"
    assert _extract_text_string("") == ""
    assert _extract_text_string(None) == ""


def test_extract_text_string_handles_mixed_array():
    from bot.services.import_parser import _extract_text_string
    mixed = [
        "Interesting article: ",
        {"type": "text_link", "text": "read more", "href": "https://example.com"},
    ]
    result = _extract_text_string(mixed)
    assert "Interesting article:" in result
    assert "read more" in result


# ---------------------------------------------------------------------------
# 19. _classify_td_kind priority
# ---------------------------------------------------------------------------

def test_classify_td_kind_priority():
    from bot.services.import_parser import _classify_td_kind

    service_msg = {"type": "service", "action": "join_group_by_link"}
    assert _classify_td_kind(service_msg) == "service"

    forward_msg = {"type": "message", "forwarded_from": "Someone", "photo": "photos/x.jpg"}
    assert _classify_td_kind(forward_msg) == "forward"  # forward beats photo

    photo_msg = {"type": "message", "photo": "photos/x.jpg"}
    assert _classify_td_kind(photo_msg) == "photo"

    text_msg = {"type": "message", "text": "hello", "text_entities": []}
    assert _classify_td_kind(text_msg) == "text"


# ---------------------------------------------------------------------------
# 20. governance detect_policy called for each user message
# ---------------------------------------------------------------------------

def test_governance_called_per_message():
    from bot.services.import_parser import parse_export

    with patch("bot.services.import_parser.detect_policy", wraps=__import__(
        "bot.services.governance", fromlist=["detect_policy"]
    ).detect_policy) as mock_dp:
        report = parse_export(SMALL_CHAT)
        # Must be called for each user message (5), NOT for service messages
        assert mock_dp.call_count == report.user_messages


# ---------------------------------------------------------------------------
# 21. Fix 1 — text_entities fallback for governance when text is empty
# ---------------------------------------------------------------------------

def test_text_entities_fallback_detects_nomem_policy(tmp_path: Path):
    """When text='' but text_entities contains #nomem, governance must fire."""
    export = {
        "id": -100111,
        "name": "Test",
        "type": "private_supergroup",
        "messages": [
            {
                "id": 9001,
                "type": "message",
                "date": "2024-01-15T10:00:00",
                "date_unixtime": "1705312800",
                "from": "Alice",
                "from_id": "user9001",
                "text": "",
                "text_entities": [
                    {"type": "hashtag", "text": "#nomem"},
                    {"type": "plain", "text": " some text"},
                ],
            }
        ],
    }
    f = tmp_path / "entities_only.json"
    f.write_text(json.dumps(export), encoding="utf-8")
    from bot.services.import_parser import parse_export
    report = parse_export(f)
    assert report.policy_marker_counts["nomem"] == 1


# ---------------------------------------------------------------------------
# 22. Fix 2 — _extract_text_string tolerant about malformed array elements
# ---------------------------------------------------------------------------

def test_extract_text_string_tolerant_mixed_array():
    """Must not crash on non-str/non-dict items; coerce ints, skip None text, recurse lists."""
    from bot.services.import_parser import _extract_text_string
    # 'a' → kept, 5 → coerced to '5', {type:x, text:None} → skip (None text),
    # {type:y} → no text key → skip, ['nested', 'list'] → recurse to 'nestedlist'
    result = _extract_text_string(["a", 5, {"type": "x", "text": None}, {"type": "y"}, ["nested", "list"]])
    assert result == "a5nestedlist"


# ---------------------------------------------------------------------------
# 23. Fix 3 — _classify_td_kind returns 'unknown' + warning for unknown media_type
# ---------------------------------------------------------------------------

def test_classify_td_kind_unknown_media_type_emits_warning():
    """Unknown media_type → return 'unknown', append a warning string."""
    from bot.services.import_parser import _classify_td_kind
    warnings: list[str] = []
    result = _classify_td_kind({"type": "message", "media_type": "unknown_xyz", "text": "hello"}, warnings=warnings)
    assert result == "unknown"
    assert any("unknown_xyz" in w for w in warnings)


# ---------------------------------------------------------------------------
# 24. Fix 5 — parse warning emitted for non-str from_id
# ---------------------------------------------------------------------------

def test_parse_warning_for_int_from_id(tmp_path: Path):
    """from_id=12345 (int) must emit a parse warning."""
    export = {
        "id": -100222,
        "name": "Test",
        "type": "private_supergroup",
        "messages": [
            {
                "id": 9002,
                "type": "message",
                "date": "2024-01-15T10:00:00",
                "date_unixtime": "1705312800",
                "from": "Bob",
                "from_id": 12345,  # int instead of str
                "text": "hello",
            }
        ],
    }
    f = tmp_path / "int_from_id.json"
    f.write_text(json.dumps(export), encoding="utf-8")
    from bot.services.import_parser import parse_export
    report = parse_export(f)
    assert any("from_id" in w for w in report.parse_warnings)
