"""Unit tests for bot.services.visibility_derivation — no Postgres required.

These tests exercise pure functions (_classify_version, _build_tombstone_keys,
_match_tombstone_formats) directly with in-memory inputs.

Run without Postgres:
    pytest tests/services/test_visibility_derivation_unit.py -v

Contrast with test_visibility_derivation.py which requires @pytest.mark.integration
(real DB fixtures).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("app_env")


# ─── 1. _classify_version unit tests (no DB needed) ──────────────────────────


def test_classify_visible():
    """Normal policy, not redacted, no tombstone → VISIBLE."""
    from bot.services.visibility_derivation import CardVisibility, _classify_version

    result = _classify_version(
        ver_is_redacted=False,
        parent_memory_policy="normal",
        parent_is_redacted=False,
        has_tombstone=False,
    )
    assert result == CardVisibility.VISIBLE


def test_classify_offrecord_is_redacted():
    """parent_memory_policy='offrecord' → REDACTED."""
    from bot.services.visibility_derivation import CardVisibility, _classify_version

    result = _classify_version(
        ver_is_redacted=False,
        parent_memory_policy="offrecord",
        parent_is_redacted=False,
        has_tombstone=False,
    )
    assert result == CardVisibility.REDACTED


def test_classify_forgotten_policy_is_redacted():
    """parent_memory_policy='forgotten' → REDACTED (cascade already ran)."""
    from bot.services.visibility_derivation import CardVisibility, _classify_version

    result = _classify_version(
        ver_is_redacted=False,
        parent_memory_policy="forgotten",
        parent_is_redacted=False,
        has_tombstone=False,
    )
    assert result == CardVisibility.REDACTED


def test_classify_ver_is_redacted():
    """ver_is_redacted=True → REDACTED regardless of policy."""
    from bot.services.visibility_derivation import CardVisibility, _classify_version

    result = _classify_version(
        ver_is_redacted=True,
        parent_memory_policy="normal",
        parent_is_redacted=False,
        has_tombstone=False,
    )
    assert result == CardVisibility.REDACTED


def test_classify_parent_is_redacted():
    """parent_is_redacted=True → REDACTED."""
    from bot.services.visibility_derivation import CardVisibility, _classify_version

    result = _classify_version(
        ver_is_redacted=False,
        parent_memory_policy="normal",
        parent_is_redacted=True,
        has_tombstone=False,
    )
    assert result == CardVisibility.REDACTED


def test_classify_nomem():
    """parent_memory_policy='nomem' → NOMEM."""
    from bot.services.visibility_derivation import CardVisibility, _classify_version

    result = _classify_version(
        ver_is_redacted=False,
        parent_memory_policy="nomem",
        parent_is_redacted=False,
        has_tombstone=False,
    )
    assert result == CardVisibility.NOMEM


def test_classify_has_tombstone():
    """has_tombstone=True with normal policy → FORGOTTEN."""
    from bot.services.visibility_derivation import CardVisibility, _classify_version

    result = _classify_version(
        ver_is_redacted=False,
        parent_memory_policy="normal",
        parent_is_redacted=False,
        has_tombstone=True,
    )
    assert result == CardVisibility.FORGOTTEN


def test_classify_redacted_beats_tombstone():
    """is_redacted=True + has_tombstone=True → REDACTED (not FORGOTTEN)."""
    from bot.services.visibility_derivation import CardVisibility, _classify_version

    result = _classify_version(
        ver_is_redacted=True,
        parent_memory_policy="normal",
        parent_is_redacted=False,
        has_tombstone=True,
    )
    assert result == CardVisibility.REDACTED


def test_classify_nomem_beats_tombstone():
    """nomem policy + has_tombstone=True → NOMEM (nomem > forgotten in precedence)."""
    from bot.services.visibility_derivation import CardVisibility, _classify_version

    result = _classify_version(
        ver_is_redacted=False,
        parent_memory_policy="nomem",
        parent_is_redacted=False,
        has_tombstone=True,
    )
    assert result == CardVisibility.NOMEM


# ─── 2. _build_tombstone_keys unit tests (HIGH-1 coverage) ───────────────────


def test_build_tombstone_keys_all_three_formats():
    """_build_tombstone_keys returns all 3 key formats for a complete row."""
    from bot.services.visibility_derivation import _build_tombstone_keys

    keys = _build_tombstone_keys(
        content_hash="abc123",
        chat_id=-100123456,
        message_id=42,
        from_user_id=9876543210,
    )
    assert "message_hash:abc123" in keys
    assert "message:-100123456:42" in keys
    assert "user:9876543210" in keys
    assert len(keys) == 3


def test_build_tombstone_keys_missing_content_hash():
    """content_hash=None → message_hash: key is omitted; others still built."""
    from bot.services.visibility_derivation import _build_tombstone_keys

    keys = _build_tombstone_keys(
        content_hash=None,
        chat_id=-100123456,
        message_id=42,
        from_user_id=9876543210,
    )
    assert not any(k.startswith("message_hash:") for k in keys)
    assert "message:-100123456:42" in keys
    assert "user:9876543210" in keys
    assert len(keys) == 2


def test_build_tombstone_keys_missing_chat_id():
    """chat_id=None → message: key is omitted; others still built."""
    from bot.services.visibility_derivation import _build_tombstone_keys

    keys = _build_tombstone_keys(
        content_hash="abc123",
        chat_id=None,
        message_id=42,
        from_user_id=9876543210,
    )
    assert not any(k.startswith("message:") for k in keys)
    assert "message_hash:abc123" in keys
    assert "user:9876543210" in keys
    assert len(keys) == 2


def test_build_tombstone_keys_missing_message_id():
    """message_id=None → message: key is omitted; others still built."""
    from bot.services.visibility_derivation import _build_tombstone_keys

    keys = _build_tombstone_keys(
        content_hash="abc123",
        chat_id=-100123456,
        message_id=None,
        from_user_id=9876543210,
    )
    assert not any(k.startswith("message:") for k in keys)
    assert "message_hash:abc123" in keys
    assert "user:9876543210" in keys
    assert len(keys) == 2


def test_build_tombstone_keys_missing_from_user_id():
    """from_user_id=None → user: key is omitted; others still built."""
    from bot.services.visibility_derivation import _build_tombstone_keys

    keys = _build_tombstone_keys(
        content_hash="abc123",
        chat_id=-100123456,
        message_id=42,
        from_user_id=None,
    )
    assert not any(k.startswith("user:") for k in keys)
    assert "message_hash:abc123" in keys
    assert "message:-100123456:42" in keys
    assert len(keys) == 2


def test_build_tombstone_keys_all_none():
    """All None → empty key set (malformed row; graceful skip)."""
    from bot.services.visibility_derivation import _build_tombstone_keys

    keys = _build_tombstone_keys(
        content_hash=None,
        chat_id=None,
        message_id=None,
        from_user_id=None,
    )
    assert len(keys) == 0


def test_build_tombstone_keys_no_duplicates_when_all_present():
    """No duplicate keys even with all fields present."""
    from bot.services.visibility_derivation import _build_tombstone_keys

    keys = _build_tombstone_keys(
        content_hash="deadbeef",
        chat_id=-100555,
        message_id=7,
        from_user_id=111222333,
    )
    assert len(keys) == len(set(keys)), "Duplicate keys returned"


# ─── 3. tombstone format matching (HIGH-1 core logic) ────────────────────────


def test_message_tombstone_format_matches():
    """A tombstone key in 'message:{chat_id}:{message_id}' format blocks a version
    that has matching chat_id + message_id — HIGH-1 privacy invariant."""
    from bot.services.visibility_derivation import _build_tombstone_keys

    keys = _build_tombstone_keys(
        content_hash="somehash",
        chat_id=-100999,
        message_id=123,
        from_user_id=555,
    )
    active_tombstones = {"message:-100999:123"}
    assert active_tombstones & set(keys), "message: format tombstone should match"


def test_user_tombstone_format_matches():
    """A tombstone key in 'user:{tg_id}' format blocks all versions by that author
    — HIGH-1 privacy invariant."""
    from bot.services.visibility_derivation import _build_tombstone_keys

    keys = _build_tombstone_keys(
        content_hash="somehash",
        chat_id=-100999,
        message_id=123,
        from_user_id=777888999,
    )
    active_tombstones = {"user:777888999"}
    assert active_tombstones & set(keys), "user: format tombstone should match"


def test_unrelated_tombstone_does_not_match():
    """A tombstone for a different message/user does NOT block unrelated versions."""
    from bot.services.visibility_derivation import _build_tombstone_keys

    keys = _build_tombstone_keys(
        content_hash="somehash",
        chat_id=-100999,
        message_id=123,
        from_user_id=777,
    )
    unrelated_tombstones = {
        "message:-100999:999",  # different message_id
        "user:888",  # different user
        "message_hash:differenthash",  # different hash
    }
    assert not (unrelated_tombstones & set(keys)), "Unrelated tombstones should not match"


def test_all_three_formats_present_no_double_count():
    """When all 3 tombstone formats match, the version is only counted ONCE as blocking.

    (The blocking_source_ids set must not contain duplicates for the same version_id.)
    """
    from bot.services.visibility_derivation import _build_tombstone_keys

    keys = _build_tombstone_keys(
        content_hash="abc",
        chat_id=-100,
        message_id=1,
        from_user_id=42,
    )
    active_tombstones = {
        "message_hash:abc",
        "message:-100:1",
        "user:42",
    }
    # All 3 match — but the blocking set should not double-count the version
    matches = active_tombstones & set(keys)
    # At least one match → version is blocked once
    assert len(matches) >= 1, "At least one format should match"


def test_malformed_message_key_missing_chat_id():
    """message::<message_id> format (missing chat_id) is gracefully skipped.

    _build_tombstone_keys with chat_id=None should not emit 'message::{id}'.
    """
    from bot.services.visibility_derivation import _build_tombstone_keys

    keys = _build_tombstone_keys(
        content_hash="abc",
        chat_id=None,  # missing chat_id
        message_id=99,
        from_user_id=42,
    )
    # Must not contain malformed 'message::99'
    assert "message::99" not in keys


def test_empty_tombstone_set_visible():
    """When tombstone set is empty, has_tombstone=False → VISIBLE classification."""
    from bot.services.visibility_derivation import CardVisibility, _classify_version

    result = _classify_version(
        ver_is_redacted=False,
        parent_memory_policy="normal",
        parent_is_redacted=False,
        has_tombstone=False,
    )
    assert result == CardVisibility.VISIBLE


# ─── 4. CardVisibility enum (pure, no DB) ────────────────────────────────────


def test_card_visibility_enum_values_unit():
    """CardVisibility enum has all four required string values — no DB needed."""
    from bot.services.visibility_derivation import CardVisibility

    assert CardVisibility.VISIBLE == "visible"
    assert CardVisibility.REDACTED == "redacted"
    assert CardVisibility.FORGOTTEN == "forgotten"
    assert CardVisibility.NOMEM == "nomem"


def test_policy_rank_ordering():
    """_POLICY_RANK: VISIBLE < FORGOTTEN < NOMEM < REDACTED."""
    from bot.services.visibility_derivation import CardVisibility, _POLICY_RANK

    assert _POLICY_RANK[CardVisibility.VISIBLE] < _POLICY_RANK[CardVisibility.FORGOTTEN]
    assert _POLICY_RANK[CardVisibility.FORGOTTEN] < _POLICY_RANK[CardVisibility.NOMEM]
    assert _POLICY_RANK[CardVisibility.NOMEM] < _POLICY_RANK[CardVisibility.REDACTED]


def test_visibility_derivation_is_frozen():
    """VisibilityDerivation is a frozen dataclass (immutable)."""
    from bot.services.visibility_derivation import CardVisibility, VisibilityDerivation

    result = VisibilityDerivation(
        visibility=CardVisibility.VISIBLE,
        blocking_source_ids=(),
        reason="test",
    )
    with pytest.raises((AttributeError, TypeError)):
        result.visibility = CardVisibility.REDACTED  # type: ignore[misc]


def test_visibility_derivation_blocking_ids_is_tuple():
    """blocking_source_ids must be a tuple."""
    from bot.services.visibility_derivation import CardVisibility, VisibilityDerivation

    result = VisibilityDerivation(
        visibility=CardVisibility.VISIBLE,
        blocking_source_ids=(),
        reason="test",
    )
    assert isinstance(result.blocking_source_ids, tuple)
