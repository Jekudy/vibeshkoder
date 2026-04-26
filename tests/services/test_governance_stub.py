"""T1-04 governance stub tests.

The stub MUST always return ``('normal', None)``. T1-12 will replace it; this test
exercises the contract so the swap is mechanically verifiable (T1-12's PR will
intentionally break some of these assertions).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("app_env")


def test_detect_policy_normal_for_plain_text(app_env) -> None:
    from bot.services.governance import detect_policy

    policy, mark = detect_policy("hello world", None)
    assert policy == "normal"
    assert mark is None


def test_detect_policy_normal_for_text_with_offrecord_token_in_t104_stub(app_env) -> None:
    """T1-04 stub does NOT detect tokens. T1-12 will replace this test with one that
    asserts the opposite — the token IS detected as 'offrecord'."""
    from bot.services.governance import detect_policy

    policy, mark = detect_policy("important #offrecord secret note", None)
    assert policy == "normal", "T1-04 stub returns 'normal' even with offrecord token"
    assert mark is None


def test_detect_policy_normal_for_caption_with_nomem_token_in_t104_stub(app_env) -> None:
    from bot.services.governance import detect_policy

    policy, mark = detect_policy(None, "caption text #nomem")
    assert policy == "normal"
    assert mark is None


def test_detect_policy_normal_for_none_inputs(app_env) -> None:
    from bot.services.governance import detect_policy

    policy, mark = detect_policy(None, None)
    assert policy == "normal"
    assert mark is None


def test_redact_raw_for_offrecord_passthrough_in_t104_stub(app_env) -> None:
    """The stub redactor returns input unchanged. T1-12 will implement real redaction
    (drop text/caption/entities, keep ids/timestamps/hash/marker)."""
    from bot.services.governance import redact_raw_for_offrecord

    payload = {"message": {"text": "secret", "message_id": 1}}
    assert redact_raw_for_offrecord(payload) == payload
    assert redact_raw_for_offrecord(None) is None
