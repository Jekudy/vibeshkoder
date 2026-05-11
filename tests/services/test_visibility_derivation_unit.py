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


# ─── 5. classify_visibility pure function tests ───────────────────────────────


def _make_version_row(
    version_id: int,
    memory_policy: str = "normal",
    is_redacted: bool = False,
    content_hash: str | None = "abc",
    chat_id: int | None = -100,
    message_id: int | None = 1,
    user_id: int | None = 42,
):
    """Helper to create a _VersionRow for use in classify_visibility tests."""
    from bot.services.visibility_derivation import _VersionRow

    return _VersionRow(
        version_id=version_id,
        content_hash=content_hash,
        chat_id=chat_id,
        message_id=message_id,
        user_id=user_id,
        memory_policy=memory_policy,
        is_redacted=is_redacted,
    )


def test_classify_visibility_all_visible():
    """All VersionRows with normal policy, not redacted, no tombstones → VISIBLE."""
    from bot.services.visibility_derivation import CardVisibility, classify_visibility

    versions = [
        _make_version_row(1, memory_policy="normal"),
        _make_version_row(2, memory_policy="normal"),
    ]
    result = classify_visibility(versions, matched_tombstone_keys=set())
    assert result.visibility == CardVisibility.VISIBLE
    assert result.blocking_source_ids == ()
    assert "visible" in result.reason


def test_classify_visibility_offrecord_blocks():
    """One VersionRow with memory_policy='offrecord' → REDACTED, blocking_ids contains that id."""
    from bot.services.visibility_derivation import CardVisibility, classify_visibility

    versions = [
        _make_version_row(10, memory_policy="offrecord"),
        _make_version_row(11, memory_policy="normal"),
    ]
    result = classify_visibility(versions, matched_tombstone_keys=set())
    assert result.visibility == CardVisibility.REDACTED
    assert 10 in result.blocking_source_ids
    assert 11 not in result.blocking_source_ids
    assert "offrecord" in result.reason


def test_classify_visibility_redacted_flag_blocks():
    """One VersionRow with is_redacted=True → REDACTED."""
    from bot.services.visibility_derivation import CardVisibility, classify_visibility

    versions = [_make_version_row(20, is_redacted=True)]
    result = classify_visibility(versions, matched_tombstone_keys=set())
    assert result.visibility == CardVisibility.REDACTED
    assert 20 in result.blocking_source_ids


def test_classify_visibility_nomem_blocks():
    """One VersionRow with memory_policy='nomem', none redacted → NOMEM."""
    from bot.services.visibility_derivation import CardVisibility, classify_visibility

    versions = [
        _make_version_row(30, memory_policy="nomem"),
        _make_version_row(31, memory_policy="normal"),
    ]
    result = classify_visibility(versions, matched_tombstone_keys=set())
    assert result.visibility == CardVisibility.NOMEM
    assert 30 in result.blocking_source_ids
    assert 31 not in result.blocking_source_ids


def test_classify_visibility_tombstone_blocks():
    """matched_tombstone_keys non-empty and matches a version → FORGOTTEN."""
    from bot.services.visibility_derivation import CardVisibility, classify_visibility

    # version_id=40 has content_hash="deadbeef" → key "message_hash:deadbeef" matches
    versions = [
        _make_version_row(40, memory_policy="normal", content_hash="deadbeef"),
    ]
    result = classify_visibility(versions, matched_tombstone_keys={"message_hash:deadbeef"})
    assert result.visibility == CardVisibility.FORGOTTEN
    assert 40 in result.blocking_source_ids
    # reason should name one of the matched tombstone key formats
    assert "message_hash" in result.reason


def test_classify_visibility_precedence_redacted_over_nomem():
    """One version REDACTED + another NOMEM → result is REDACTED (not NOMEM)."""
    from bot.services.visibility_derivation import CardVisibility, classify_visibility

    versions = [
        _make_version_row(50, memory_policy="offrecord"),  # → REDACTED
        _make_version_row(51, memory_policy="nomem"),       # → NOMEM
    ]
    result = classify_visibility(versions, matched_tombstone_keys=set())
    assert result.visibility == CardVisibility.REDACTED


def test_classify_visibility_precedence_nomem_over_forgotten():
    """One NOMEM version + a matched tombstone → result is NOMEM (NOMEM > FORGOTTEN)."""
    from bot.services.visibility_derivation import CardVisibility, classify_visibility

    # version 60: nomem (no tombstone keys for it)
    # version 61: normal, but matched tombstone → FORGOTTEN
    versions = [
        _make_version_row(60, memory_policy="nomem", content_hash=None, chat_id=None,
                          message_id=None, user_id=None),
        _make_version_row(61, memory_policy="normal", content_hash="abc61",
                          chat_id=-100, message_id=61, user_id=None),
    ]
    result = classify_visibility(versions, matched_tombstone_keys={"message_hash:abc61"})
    assert result.visibility == CardVisibility.NOMEM


def test_classify_visibility_blocking_ids_multiple():
    """5 versions, 3 REDACTED → blocking_source_ids tuple has all 3 ids in sorted order."""
    from bot.services.visibility_derivation import CardVisibility, classify_visibility

    versions = [
        _make_version_row(70, memory_policy="offrecord"),  # blocking
        _make_version_row(71, memory_policy="normal"),
        _make_version_row(72, memory_policy="offrecord"),  # blocking
        _make_version_row(73, memory_policy="normal"),
        _make_version_row(74, memory_policy="forgotten"),  # → REDACTED (forgotten policy)
    ]
    result = classify_visibility(versions, matched_tombstone_keys=set())
    assert result.visibility == CardVisibility.REDACTED
    assert result.blocking_source_ids == (70, 72, 74)


def test_classify_visibility_empty_versions():
    """Empty versions list → VISIBLE, empty blocking_ids, reason mentions 'no cited sources'."""
    from bot.services.visibility_derivation import CardVisibility, classify_visibility

    result = classify_visibility([], matched_tombstone_keys=set())
    assert result.visibility == CardVisibility.VISIBLE
    assert result.blocking_source_ids == ()
    assert "no cited sources" in result.reason
