"""Memory governance and legacy raw-payload redactor tests.

Phase 13 makes ``normal`` the only live/import content policy.  The redactor remains
covered for maintenance of historical offrecord payloads.
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


# ─── opt-out-looking content remains normal memory ──────────────────────────


def test_detect_policy_nomem_in_text(app_env) -> None:
    from bot.services.governance import detect_policy

    policy, mark = detect_policy("important #nomem note", None)
    assert policy == "normal"
    assert mark is None


def test_detect_policy_nomem_in_caption(app_env) -> None:
    from bot.services.governance import detect_policy

    policy, mark = detect_policy(None, "see photo #nomem")
    assert policy == "normal"
    assert mark is None


def test_detect_policy_nomem_case_insensitive(app_env) -> None:
    from bot.services.governance import detect_policy

    for variant in ("#NoMem", "#NOMEM", "#nomem", "#NOmem"):
        policy, _ = detect_policy(f"hello {variant} world", None)
        assert policy == "normal", f"failed for variant: {variant!r}"


def test_detect_policy_offrecord_in_text(app_env) -> None:
    from bot.services.governance import detect_policy

    policy, mark = detect_policy("secret #offrecord note", None)
    assert policy == "normal"
    assert mark is None


def test_detect_policy_offrecord_in_caption(app_env) -> None:
    from bot.services.governance import detect_policy

    policy, mark = detect_policy(None, "media caption #offrecord")
    assert policy == "normal"
    assert mark is None


def test_detect_policy_offrecord_case_insensitive(app_env) -> None:
    from bot.services.governance import detect_policy

    for variant in ("#OffRecord", "#OFFRECORD", "#offrecord", "#offRECORD"):
        policy, _ = detect_policy(variant, None)
        assert policy == "normal", f"failed for variant: {variant!r}"


def test_offrecord_takes_precedence_over_nomem(app_env) -> None:
    """Both legacy tokens are stored as ordinary content."""
    from bot.services.governance import detect_policy

    policy, mark = detect_policy("#nomem and #offrecord both", None)
    assert policy == "normal"
    assert mark is None


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
    """Punctuation does not make legacy tokens active opt-outs."""
    from bot.services.governance import detect_policy

    policy, _ = detect_policy("note #nomem.", None)
    assert policy == "normal"

    policy, _ = detect_policy("urgent #offrecord!", None)
    assert policy == "normal"


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


# ─── redact: channel_post / edited_channel_post (Codex MEDIUM) ─────────────────────────────


def test_redact_handles_channel_post(app_env) -> None:
    from bot.services.governance import redact_raw_for_offrecord

    raw = {
        "update_id": 10,
        "channel_post": {
            "message_id": 50,
            "text": "channel secret",
            "caption": "channel cap",
            "entities": [{"type": "bold"}],
            "chat": {"id": -100, "type": "channel"},
        },
    }
    redacted = redact_raw_for_offrecord(raw)
    msg = redacted["channel_post"]
    assert "text" not in msg
    assert "caption" not in msg
    assert "entities" not in msg
    assert msg["message_id"] == 50
    assert msg["chat"] == {"id": -100, "type": "channel"}


def test_redact_handles_edited_channel_post(app_env) -> None:
    from bot.services.governance import redact_raw_for_offrecord

    raw = {
        "update_id": 11,
        "edited_channel_post": {
            "message_id": 51,
            "text": "channel edit secret",
            "caption_entities": [{"type": "italic"}],
        },
    }
    redacted = redact_raw_for_offrecord(raw)
    msg = redacted["edited_channel_post"]
    assert "text" not in msg
    assert "caption_entities" not in msg
    assert msg["message_id"] == 51


# ─── redact: nested message-shaped fields (Codex HIGH — reply_to_message leak) ─────────────


def test_redact_scrubs_reply_to_message_content(app_env) -> None:
    """Codex HIGH: shallow-copy redactor leaked parent content via reply_to_message.
    Telegram echoes parent text/caption inline; we must scrub the same content fields
    from reply_to_message too."""
    from bot.services.governance import redact_raw_for_offrecord

    raw = {
        "update_id": 20,
        "message": {
            "message_id": 100,
            "text": "child #offrecord",
            "reply_to_message": {
                "message_id": 99,
                "text": "parent secret content",
                "caption": "parent cap",
                "entities": [{"type": "bold"}],
                "from": {"id": 7},
            },
        },
    }
    redacted = redact_raw_for_offrecord(raw)
    parent = redacted["message"]["reply_to_message"]
    assert "text" not in parent, "parent text leaked through reply_to_message"
    assert "caption" not in parent
    assert "entities" not in parent
    # Non-content fields survive on the nested message:
    assert parent["message_id"] == 99
    assert parent["from"] == {"id": 7}


def test_redact_scrubs_nested_reply_chain(app_env) -> None:
    """Reply-of-reply: scrubbing must recurse so nested reply_to_message.reply_to_message
    is also content-stripped."""
    from bot.services.governance import redact_raw_for_offrecord

    raw = {
        "message": {
            "message_id": 3,
            "text": "leaf",
            "reply_to_message": {
                "message_id": 2,
                "text": "middle secret",
                "reply_to_message": {
                    "message_id": 1,
                    "text": "root secret",
                    "caption": "root cap",
                },
            },
        },
    }
    redacted = redact_raw_for_offrecord(raw)
    middle = redacted["message"]["reply_to_message"]
    root = middle["reply_to_message"]
    assert "text" not in middle
    assert "text" not in root
    assert "caption" not in root
    assert root["message_id"] == 1


def test_redact_scrubs_pinned_and_external_reply(app_env) -> None:
    """pinned_message / external_reply also echo content snapshots → must be scrubbed."""
    from bot.services.governance import redact_raw_for_offrecord

    raw = {
        "message": {
            "message_id": 4,
            "text": "child",
            "pinned_message": {
                "message_id": 1,
                "text": "pinned secret",
            },
            "external_reply": {
                "message_id": 99,
                "caption": "external cap secret",
            },
        },
    }
    redacted = redact_raw_for_offrecord(raw)
    assert "text" not in redacted["message"]["pinned_message"]
    assert "caption" not in redacted["message"]["external_reply"]
    assert redacted["message"]["pinned_message"]["message_id"] == 1


# ─── detect_policy: new keyword-only args (Sprint #89 Commit 2) ────────────────────────────


def test_detect_policy_offrecord_in_poll_question(app_env) -> None:
    """Legacy token in a poll remains normal memory."""
    from bot.services.governance import detect_policy

    policy, mark = detect_policy(None, None, poll_question="hello #offrecord")
    assert policy == "normal"
    assert mark is None


def test_detect_policy_nomem_in_contact_name(app_env) -> None:
    """Legacy token in a contact remains normal memory."""
    from bot.services.governance import detect_policy

    policy, mark = detect_policy(None, None, contact_name="Alice #nomem")
    assert policy == "normal"
    assert mark is None


def test_detect_policy_offrecord_in_forward_text(app_env) -> None:
    """Legacy token in forwarded content remains normal memory."""
    from bot.services.governance import detect_policy

    policy, mark = detect_policy(None, None, forward_text="see #offrecord")
    assert policy == "normal"
    assert mark is None


def test_detect_policy_offrecord_precedence_over_nomem_across_fields(app_env) -> None:
    """Legacy tokens across fields still do not activate an opt-out."""
    from bot.services.governance import detect_policy

    policy, mark = detect_policy("a #nomem", None, poll_question="b #offrecord")
    assert policy == "normal"
    assert mark is None
