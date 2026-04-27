"""T1-12 governance detector tests (replaces T1-04 stub tests).

The T1-04 stub always returned ('normal', None). T1-12 implements REAL
deterministic detection over text + caption tokens. These tests pin that
contract — case-insensitive, hashtag-bounded, offrecord-precedence — and the
``redact_raw_for_offrecord`` content stripping.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("app_env")


# ─── detect_policy: normal ─────────────────────────────────────────────────────────────────

def test_detect_policy_normal_for_plain_text(app_env) -> None:
    from bot.services.governance import detect_policy

    policy, mark = detect_policy("hello world", None)
    assert policy == "normal"
    assert mark is None


def test_detect_policy_normal_for_none_inputs(app_env) -> None:
    from bot.services.governance import detect_policy

    policy, mark = detect_policy(None, None)
    assert policy == "normal"
    assert mark is None


# ─── detect_policy: nomem ──────────────────────────────────────────────────────────────────

def test_detect_policy_nomem_in_text(app_env) -> None:
    from bot.services.governance import detect_policy

    policy, mark = detect_policy("important #nomem note", None)
    assert policy == "nomem"
    assert mark is not None
    assert mark["in_text"] is True
    assert mark["in_caption"] is False
    assert "detected_by" in mark


def test_detect_policy_nomem_in_caption(app_env) -> None:
    from bot.services.governance import detect_policy

    policy, mark = detect_policy(None, "see photo #nomem")
    assert policy == "nomem"
    assert mark is not None
    assert mark["in_text"] is False
    assert mark["in_caption"] is True


def test_detect_policy_nomem_case_insensitive(app_env) -> None:
    from bot.services.governance import detect_policy

    for variant in ("#NoMem", "#NOMEM", "#nomem", "#NOmem"):
        policy, _ = detect_policy(f"hello {variant} world", None)
        assert policy == "nomem", f"failed for variant: {variant!r}"


# ─── detect_policy: offrecord ──────────────────────────────────────────────────────────────

def test_detect_policy_offrecord_in_text(app_env) -> None:
    from bot.services.governance import detect_policy

    policy, mark = detect_policy("secret #offrecord note", None)
    assert policy == "offrecord"
    assert mark is not None
    assert mark["in_text"] is True
    assert mark["in_caption"] is False


def test_detect_policy_offrecord_in_caption(app_env) -> None:
    from bot.services.governance import detect_policy

    policy, _ = detect_policy(None, "media caption #offrecord")
    assert policy == "offrecord"


def test_detect_policy_offrecord_case_insensitive(app_env) -> None:
    from bot.services.governance import detect_policy

    for variant in ("#OffRecord", "#OFFRECORD", "#offrecord", "#offRECORD"):
        policy, _ = detect_policy(variant, None)
        assert policy == "offrecord", f"failed for variant: {variant!r}"


# ─── detect_policy: precedence ─────────────────────────────────────────────────────────────

def test_offrecord_takes_precedence_over_nomem(app_env) -> None:
    """Both tokens present → offrecord (stricter wins)."""
    from bot.services.governance import detect_policy

    policy, _ = detect_policy("#nomem and #offrecord both", None)
    assert policy == "offrecord"


# ─── detect_policy: token boundaries ───────────────────────────────────────────────────────

def test_detect_policy_does_not_match_substring_in_word(app_env) -> None:
    """``#nomembership`` and ``#offrecording`` are not the standalone token."""
    from bot.services.governance import detect_policy

    policy, _ = detect_policy("about #nomembership status", None)
    assert policy == "normal"

    policy, _ = detect_policy("the #offrecording session", None)
    assert policy == "normal"


def test_detect_policy_does_not_match_when_attached_to_word(app_env) -> None:
    """``some#nomem`` (attached to a word with no separator) doesn't count — Telegram
    hashtags require a leading non-word boundary."""
    from bot.services.governance import detect_policy

    policy, _ = detect_policy("some#nomem", None)
    assert policy == "normal"


def test_detect_policy_matches_with_trailing_punctuation(app_env) -> None:
    """``#nomem.`` and ``#offrecord!`` should still match — the negative lookahead is
    on word chars, not punctuation."""
    from bot.services.governance import detect_policy

    policy, _ = detect_policy("note #nomem.", None)
    assert policy == "nomem"

    policy, _ = detect_policy("urgent #offrecord!", None)
    assert policy == "offrecord"


# ─── redact_raw_for_offrecord ──────────────────────────────────────────────────────────────

def test_redact_drops_text_caption_entities_from_message(app_env) -> None:
    from bot.services.governance import redact_raw_for_offrecord

    raw = {
        "update_id": 1,
        "message": {
            "message_id": 42,
            "text": "secret",
            "caption": "secret cap",
            "entities": [{"type": "bold", "offset": 0, "length": 6}],
            "caption_entities": [{"type": "italic"}],
            "from": {"id": 100},
            "chat": {"id": -1, "type": "supergroup"},
        },
    }
    redacted = redact_raw_for_offrecord(raw)
    assert redacted is not None
    msg = redacted["message"]
    assert "text" not in msg
    assert "caption" not in msg
    assert "entities" not in msg
    assert "caption_entities" not in msg
    # Non-content fields survive:
    assert msg["message_id"] == 42
    assert msg["from"] == {"id": 100}
    assert msg["chat"] == {"id": -1, "type": "supergroup"}
    # Top-level update_id survives:
    assert redacted["update_id"] == 1


def test_redact_handles_edited_message(app_env) -> None:
    from bot.services.governance import redact_raw_for_offrecord

    raw = {
        "update_id": 2,
        "edited_message": {
            "message_id": 7,
            "text": "edited secret",
            "from": {"id": 200},
        },
    }
    redacted = redact_raw_for_offrecord(raw)
    assert "text" not in redacted["edited_message"]
    assert redacted["edited_message"]["message_id"] == 7


def test_redact_passes_through_when_no_event_field(app_env) -> None:
    """Update with no message/edited_message etc — pass through unchanged."""
    from bot.services.governance import redact_raw_for_offrecord

    raw = {"update_id": 3, "callback_query": {"id": "abc", "data": "btn"}}
    redacted = redact_raw_for_offrecord(raw)
    # callback_query is not in _EVENT_FIELDS, so not touched. Acceptable for T1-12 —
    # callback_query.data is structured input, not user content.
    assert redacted == raw


def test_redact_returns_none_for_none(app_env) -> None:
    from bot.services.governance import redact_raw_for_offrecord

    assert redact_raw_for_offrecord(None) is None


def test_redact_does_not_mutate_input(app_env) -> None:
    """The redactor returns a new dict; the caller's original raw_json is not changed."""
    from bot.services.governance import redact_raw_for_offrecord

    raw = {"message": {"message_id": 1, "text": "secret"}}
    original_text = raw["message"]["text"]
    _ = redact_raw_for_offrecord(raw)
    assert raw["message"]["text"] == original_text
