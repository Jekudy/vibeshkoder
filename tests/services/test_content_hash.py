"""T1-08 content_hash canonical recipe tests.

Verifies:
- determinism (same inputs → same hash)
- entity-list order independence
- entity-dict key order independence (sort_keys handles this)
- empty / None entity equivalence
- different entities → different hashes
- format version tag is in the hashed payload (changing the constant changes hashes)
- function signature rejects unknown kwargs (no raw_json sneaking in)
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("app_env")


# ─── determinism ──────────────────────────────────────────────────────────────────────────

def test_same_content_same_hash(app_env) -> None:
    from bot.services.content_hash import compute_content_hash

    a = compute_content_hash(text="hello", caption="cap", message_kind="text", entities=None)
    b = compute_content_hash(text="hello", caption="cap", message_kind="text", entities=None)
    assert a == b


def test_different_text_different_hash(app_env) -> None:
    from bot.services.content_hash import compute_content_hash

    a = compute_content_hash(text="hello", caption=None, message_kind=None)
    b = compute_content_hash(text="HELLO", caption=None, message_kind=None)
    assert a != b


def test_different_caption_different_hash(app_env) -> None:
    from bot.services.content_hash import compute_content_hash

    a = compute_content_hash(text="x", caption="one", message_kind=None)
    b = compute_content_hash(text="x", caption="two", message_kind=None)
    assert a != b


def test_different_message_kind_different_hash(app_env) -> None:
    from bot.services.content_hash import compute_content_hash

    a = compute_content_hash(text="x", caption=None, message_kind="text")
    b = compute_content_hash(text="x", caption=None, message_kind="photo")
    assert a != b


def test_message_kind_none_defaults_to_text(app_env) -> None:
    from bot.services.content_hash import compute_content_hash

    a = compute_content_hash(text="x", caption=None, message_kind=None)
    b = compute_content_hash(text="x", caption=None, message_kind="text")
    assert a == b


# ─── entity normalization ─────────────────────────────────────────────────────────────────

def test_entities_sorted_by_offset_then_length_then_type(app_env) -> None:
    """Entities given in different list-orders but identical content sets must hash the same."""
    from bot.services.content_hash import compute_content_hash

    e1 = [
        {"type": "bold", "offset": 0, "length": 5},
        {"type": "italic", "offset": 6, "length": 4},
    ]
    e2 = [
        {"type": "italic", "offset": 6, "length": 4},
        {"type": "bold", "offset": 0, "length": 5},
    ]

    a = compute_content_hash(text="hello world", caption=None, message_kind=None, entities=e1)
    b = compute_content_hash(text="hello world", caption=None, message_kind=None, entities=e2)
    assert a == b


def test_entities_with_different_dict_key_order_same_hash(app_env) -> None:
    from bot.services.content_hash import compute_content_hash

    e1 = [{"type": "bold", "offset": 0, "length": 5}]
    e2 = [{"length": 5, "offset": 0, "type": "bold"}]  # same dict, different key order

    a = compute_content_hash(text="hello", caption=None, message_kind=None, entities=e1)
    b = compute_content_hash(text="hello", caption=None, message_kind=None, entities=e2)
    assert a == b


def test_empty_entities_list_equals_none(app_env) -> None:
    from bot.services.content_hash import compute_content_hash

    a = compute_content_hash(text="x", caption=None, message_kind=None, entities=None)
    b = compute_content_hash(text="x", caption=None, message_kind=None, entities=[])
    assert a == b


def test_different_entities_different_hash(app_env) -> None:
    from bot.services.content_hash import compute_content_hash

    e1 = [{"type": "bold", "offset": 0, "length": 5}]
    e2 = [{"type": "italic", "offset": 0, "length": 5}]  # different type

    a = compute_content_hash(text="hello", caption=None, message_kind=None, entities=e1)
    b = compute_content_hash(text="hello", caption=None, message_kind=None, entities=e2)
    assert a != b


def test_entity_at_different_offset_different_hash(app_env) -> None:
    from bot.services.content_hash import compute_content_hash

    e1 = [{"type": "bold", "offset": 0, "length": 5}]
    e2 = [{"type": "bold", "offset": 1, "length": 5}]

    a = compute_content_hash(text="hello world", caption=None, message_kind=None, entities=e1)
    b = compute_content_hash(text="hello world", caption=None, message_kind=None, entities=e2)
    assert a != b


# ─── format version tag ───────────────────────────────────────────────────────────────────

def test_format_version_tag_is_in_payload(app_env, monkeypatch) -> None:
    """If the format version constant changes, the same content produces a different
    hash. This pins the version-tag-in-payload contract: future format changes are
    detectable as hash divergence."""
    from bot.services import content_hash as ch

    a = ch.compute_content_hash(text="x", caption=None, message_kind=None)
    monkeypatch.setattr(ch, "HASH_FORMAT_VERSION", "chv999")
    b = ch.compute_content_hash(text="x", caption=None, message_kind=None)
    assert a != b


def test_current_format_version_is_chv1(app_env) -> None:
    """Smoke that we know what version we ship. T1-08 = chv1. T1-09+ may bump it."""
    from bot.services.content_hash import HASH_FORMAT_VERSION

    assert HASH_FORMAT_VERSION == "chv1"


# ─── output shape ─────────────────────────────────────────────────────────────────────────

def test_output_is_64_char_hex(app_env) -> None:
    from bot.services.content_hash import compute_content_hash

    h = compute_content_hash(text="hello", caption=None, message_kind=None)
    assert len(h) == 64
    int(h, 16)  # raises if not hex


def test_unicode_text_handled(app_env) -> None:
    """ensure_ascii=False in the JSON payload — verify hash works on non-ASCII."""
    from bot.services.content_hash import compute_content_hash

    a = compute_content_hash(text="привет мир", caption=None, message_kind=None)
    b = compute_content_hash(text="привет мир", caption=None, message_kind=None)
    assert a == b
    assert len(a) == 64


# ─── golden-value: pin the chv1 recipe so accidental drift is caught ─────────────────────

def test_chv1_golden_value_simple(app_env) -> None:
    """Canonical chv1 hash for a known input, pinned. If this assertion fails, the
    hash recipe changed in a way that is NOT a pure refactor — the format version
    tag MUST be bumped before merging that change."""
    from bot.services.content_hash import compute_content_hash

    # Computed once for chv1 from compute_content_hash("hello", None, None, None).
    # Pinned here so any silent recipe drift trips this test.
    expected = (
        "ae70abb0e93e71d2c1c1367f83c8391609fa529c7d0f48f2717b6f73fe4992bb"
    )
    actual = compute_content_hash(
        text="hello", caption=None, message_kind=None, entities=None
    )
    # The pinned value must match. If T1-08's recipe drifts, regenerate this fixture
    # AND bump HASH_FORMAT_VERSION in bot/services/content_hash.py.
    assert actual == expected, (
        f"chv1 recipe drift detected. Got {actual}, expected {expected}. "
        "Either revert the change or bump HASH_FORMAT_VERSION."
    )


# ─── signature: only canonical inputs accepted ────────────────────────────────────────────

def test_no_kwargs_for_volatile_fields(app_env) -> None:
    """The function deliberately does NOT accept date / message_id / from_user etc.
    Passing them must raise TypeError (Python's argument validation enforces this)."""
    from bot.services.content_hash import compute_content_hash

    with pytest.raises(TypeError):
        compute_content_hash(  # type: ignore[call-arg]
            text="x",
            caption=None,
            message_kind=None,
            date="2026-04-27",
        )

    with pytest.raises(TypeError):
        compute_content_hash(  # type: ignore[call-arg]
            text="x",
            caption=None,
            message_kind=None,
            raw_json={"key": "value"},
        )
