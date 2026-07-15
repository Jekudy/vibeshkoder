"""Phase 13 memory policy: every human message remains usable memory."""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.usefixtures("app_env")


@pytest.mark.parametrize(
    ("text", "caption", "extra"),
    [
        ("обычное сообщение", None, {}),
        ("раньше это скрывалось #nomem", None, {}),
        ("раньше это редактировалось #offrecord", None, {}),
        (None, "подпись #nomem", {}),
        (None, None, {"poll_question": "опрос #offrecord"}),
        (None, None, {"contact_name": "User #nomem"}),
        (None, None, {"forward_text": "forward #offrecord"}),
        (None, None, {"forward_caption": "caption #nomem"}),
    ],
)
def test_every_content_variant_has_normal_memory_policy(
    text: str | None,
    caption: str | None,
    extra: dict[str, str],
) -> None:
    from bot.services.governance import detect_policy

    policy, mark = detect_policy(text, caption, **extra)

    assert policy == "normal"
    assert mark is None


def test_governance_filter_version_marks_phase13_contract() -> None:
    from bot.services.governance import GOVERNANCE_FILTER_VERSION

    assert GOVERNANCE_FILTER_VERSION == "phase13-v1"
