"""Tests for bot.services.visibility_derivation — pure-function visibility derivation.

TDD cycle: tests are written first, then the implementation.

Precedence rule (ratified here, tested in test_precedence_* tests):
  REDACTED > NOMEM > FORGOTTEN > VISIBLE

i.e.:
- offrecord/is_redacted → REDACTED (highest severity, beats everything)
- nomem policy → NOMEM (beats FORGOTTEN, VISIBLE)
- forget_events tombstone → FORGOTTEN (beats VISIBLE)
- otherwise → VISIBLE

Empty cited list → VISIBLE (no sources = no constraint).
Missing version IDs (not in DB) → treated as absent / invisible; no error raised.
"""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = pytest.mark.usefixtures("app_env")

_uid_counter = itertools.count(start=8_000_000_000)
_msg_counter = itertools.count(start=800_000)
_chat_counter = itertools.count(start=100)

GOLDEN_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "wiki_pages_golden"


def _next_uid() -> int:
    return next(_uid_counter)


def _next_msg_id() -> int:
    return next(_msg_counter)


def _next_chat_id() -> int:
    return -1_000_000_000_000 - next(_chat_counter)


# ─── helpers ──────────────────────────────────────────────────────────────────


async def _make_user(db_session) -> int:
    from bot.db.repos.user import UserRepo

    uid = _next_uid()
    await UserRepo.upsert(
        db_session,
        telegram_id=uid,
        username=f"u{uid}",
        first_name="Test",
        last_name=None,
    )
    return uid


async def _make_version(
    db_session,
    *,
    memory_policy: str = "normal",
    is_redacted: bool = False,
    content_hash: str | None = None,
) -> tuple[int, int]:
    """Create a ChatMessage + MessageVersion. Returns (chat_message_id, message_version_id)."""
    from bot.db.models import ChatMessage, MessageVersion

    uid = await _make_user(db_session)
    chat_id = _next_chat_id()
    msg_id = _next_msg_id()
    when = datetime.now(UTC)

    hash_val = content_hash or f"hash-{uuid4().hex[:12]}"

    msg = ChatMessage(
        message_id=msg_id,
        chat_id=chat_id,
        user_id=uid,
        text="some content" if not is_redacted else None,
        date=when,
        memory_policy=memory_policy,
        is_redacted=is_redacted,
        content_hash=hash_val,
    )
    db_session.add(msg)
    await db_session.flush()

    ver = MessageVersion(
        chat_message_id=msg.id,
        version_seq=1,
        text="some content" if not is_redacted else None,
        content_hash=hash_val,
        is_redacted=is_redacted,
    )
    db_session.add(ver)
    await db_session.flush()

    return msg.id, ver.id


async def _make_forget_event(db_session, *, tombstone_key: str) -> int:
    """Create a forget_events row. Returns its id."""
    from bot.db.repos.forget_event import ForgetEventRepo

    ev = await ForgetEventRepo.create(
        db_session,
        target_type="message_hash",
        target_id=None,
        actor_user_id=None,
        authorized_by="admin",
        tombstone_key=tombstone_key,
    )
    return ev.id


# ─── 1. happy paths ───────────────────────────────────────────────────────────


async def test_empty_cited_list_is_visible(db_session) -> None:
    """Empty cited_message_version_ids → VISIBLE (no constraints)."""
    from bot.services.visibility_derivation import CardVisibility, derive_card_visibility

    result = await derive_card_visibility(db_session, [])

    assert result.visibility == CardVisibility.VISIBLE
    assert result.blocking_source_ids == ()


async def test_all_normal_sources_visible(db_session) -> None:
    """All sources have memory_policy='normal', not redacted → VISIBLE."""
    from bot.services.visibility_derivation import CardVisibility, derive_card_visibility

    _, v1 = await _make_version(db_session, memory_policy="normal")
    _, v2 = await _make_version(db_session, memory_policy="normal")

    result = await derive_card_visibility(db_session, [v1, v2])

    assert result.visibility == CardVisibility.VISIBLE
    assert result.blocking_source_ids == ()


async def test_single_offrecord_source_is_redacted(db_session) -> None:
    """One source with memory_policy='offrecord' → REDACTED."""
    from bot.services.visibility_derivation import CardVisibility, derive_card_visibility

    _, v_normal = await _make_version(db_session, memory_policy="normal")
    _, v_offrecord = await _make_version(db_session, memory_policy="offrecord")

    result = await derive_card_visibility(db_session, [v_normal, v_offrecord])

    assert result.visibility == CardVisibility.REDACTED
    assert v_offrecord in result.blocking_source_ids


async def test_single_redacted_flag_is_redacted(db_session) -> None:
    """One source with is_redacted=True → REDACTED."""
    from bot.services.visibility_derivation import CardVisibility, derive_card_visibility

    _, v_redacted = await _make_version(db_session, memory_policy="normal", is_redacted=True)

    result = await derive_card_visibility(db_session, [v_redacted])

    assert result.visibility == CardVisibility.REDACTED
    assert v_redacted in result.blocking_source_ids


async def test_single_nomem_source_is_nomem(db_session) -> None:
    """One source with memory_policy='nomem' → NOMEM."""
    from bot.services.visibility_derivation import CardVisibility, derive_card_visibility

    _, v_nomem = await _make_version(db_session, memory_policy="nomem")

    result = await derive_card_visibility(db_session, [v_nomem])

    assert result.visibility == CardVisibility.NOMEM
    assert v_nomem in result.blocking_source_ids


async def test_single_forgotten_source_is_forgotten(db_session) -> None:
    """One source whose content_hash matches a forget_events tombstone → FORGOTTEN."""
    from bot.services.visibility_derivation import CardVisibility, derive_card_visibility

    content_hash = f"hash-forgotten-{uuid4().hex[:8]}"
    _, v_id = await _make_version(db_session, content_hash=content_hash)
    await _make_forget_event(db_session, tombstone_key=f"message_hash:{content_hash}")

    result = await derive_card_visibility(db_session, [v_id])

    assert result.visibility == CardVisibility.FORGOTTEN
    assert v_id in result.blocking_source_ids


# ─── 2. multi-source combinatorics ────────────────────────────────────────────


async def test_visible_plus_offrecord_is_redacted(db_session) -> None:
    """1 visible + 1 offrecord → REDACTED."""
    from bot.services.visibility_derivation import CardVisibility, derive_card_visibility

    _, v_ok = await _make_version(db_session, memory_policy="normal")
    _, v_off = await _make_version(db_session, memory_policy="offrecord")

    result = await derive_card_visibility(db_session, [v_ok, v_off])

    assert result.visibility == CardVisibility.REDACTED
    assert v_off in result.blocking_source_ids
    assert v_ok not in result.blocking_source_ids


async def test_visible_plus_nomem_is_nomem(db_session) -> None:
    """1 visible + 1 nomem → NOMEM."""
    from bot.services.visibility_derivation import CardVisibility, derive_card_visibility

    _, v_ok = await _make_version(db_session, memory_policy="normal")
    _, v_nm = await _make_version(db_session, memory_policy="nomem")

    result = await derive_card_visibility(db_session, [v_ok, v_nm])

    assert result.visibility == CardVisibility.NOMEM
    assert v_nm in result.blocking_source_ids


async def test_nomem_plus_forgotten_is_nomem(db_session) -> None:
    """NOMEM beats FORGOTTEN — precedence invariant."""
    from bot.services.visibility_derivation import CardVisibility, derive_card_visibility

    content_hash = f"hash-fgt-{uuid4().hex[:8]}"
    _, v_nm = await _make_version(db_session, memory_policy="nomem")
    _, v_fgt = await _make_version(db_session, content_hash=content_hash)
    await _make_forget_event(db_session, tombstone_key=f"message_hash:{content_hash}")

    result = await derive_card_visibility(db_session, [v_nm, v_fgt])

    assert result.visibility == CardVisibility.NOMEM


async def test_redacted_beats_nomem(db_session) -> None:
    """REDACTED beats NOMEM — highest precedence invariant."""
    from bot.services.visibility_derivation import CardVisibility, derive_card_visibility

    _, v_nm = await _make_version(db_session, memory_policy="nomem")
    _, v_off = await _make_version(db_session, memory_policy="offrecord")

    result = await derive_card_visibility(db_session, [v_nm, v_off])

    assert result.visibility == CardVisibility.REDACTED


async def test_redacted_flag_beats_nomem(db_session) -> None:
    """is_redacted=True beats nomem policy."""
    from bot.services.visibility_derivation import CardVisibility, derive_card_visibility

    _, v_nm = await _make_version(db_session, memory_policy="nomem")
    _, v_red = await _make_version(db_session, memory_policy="normal", is_redacted=True)

    result = await derive_card_visibility(db_session, [v_nm, v_red])

    assert result.visibility == CardVisibility.REDACTED


async def test_redacted_beats_forgotten(db_session) -> None:
    """REDACTED beats FORGOTTEN — highest precedence."""
    from bot.services.visibility_derivation import CardVisibility, derive_card_visibility

    content_hash = f"hash-rfgt-{uuid4().hex[:8]}"
    _, v_fgt = await _make_version(db_session, content_hash=content_hash)
    _, v_off = await _make_version(db_session, memory_policy="offrecord")
    await _make_forget_event(db_session, tombstone_key=f"message_hash:{content_hash}")

    result = await derive_card_visibility(db_session, [v_fgt, v_off])

    assert result.visibility == CardVisibility.REDACTED


async def test_five_sources_one_nomem(db_session) -> None:
    """5 sources, 1 nomem + 4 normal → NOMEM (matches golden fixture 05)."""
    from bot.services.visibility_derivation import CardVisibility, derive_card_visibility

    ids = []
    for _ in range(4):
        _, vid = await _make_version(db_session, memory_policy="normal")
        ids.append(vid)
    _, v_nm = await _make_version(db_session, memory_policy="nomem")
    ids.append(v_nm)

    result = await derive_card_visibility(db_session, ids)

    assert result.visibility == CardVisibility.NOMEM
    assert v_nm in result.blocking_source_ids


async def test_all_categories_redacted_wins(db_session) -> None:
    """All four states present — REDACTED wins (strictest precedence)."""
    from bot.services.visibility_derivation import CardVisibility, derive_card_visibility

    content_hash = f"hash-all-{uuid4().hex[:8]}"
    _, v_ok = await _make_version(db_session, memory_policy="normal")
    _, v_nm = await _make_version(db_session, memory_policy="nomem")
    _, v_fgt = await _make_version(db_session, content_hash=content_hash)
    _, v_off = await _make_version(db_session, memory_policy="offrecord")
    await _make_forget_event(db_session, tombstone_key=f"message_hash:{content_hash}")

    result = await derive_card_visibility(db_session, [v_ok, v_nm, v_fgt, v_off])

    assert result.visibility == CardVisibility.REDACTED


# ─── 3. edge cases ────────────────────────────────────────────────────────────


async def test_missing_version_ids_treated_as_absent(db_session) -> None:
    """Cited IDs that don't exist in DB → treated as non-blocking (absent rows).

    Design decision: the function only reads rows that exist; phantom IDs are silently
    skipped. Callers are responsible for validating that all cited IDs are real before
    calling. An absent row is not a privacy violation — it's a data integrity issue for
    the caller to detect separately.
    """
    from bot.services.visibility_derivation import CardVisibility, derive_card_visibility

    result = await derive_card_visibility(db_session, [9_999_990, 9_999_991])

    assert result.visibility == CardVisibility.VISIBLE


async def test_forgotten_policy_in_chat_messages_is_redacted(db_session) -> None:
    """message.memory_policy='forgotten' on chat_messages → treated as REDACTED
    (cascade has already run; content is gone, effectively offrecord)."""
    from bot.services.visibility_derivation import CardVisibility, derive_card_visibility

    _, v_id = await _make_version(db_session, memory_policy="forgotten", is_redacted=True)

    result = await derive_card_visibility(db_session, [v_id])

    assert result.visibility == CardVisibility.REDACTED


async def test_tombstone_match_by_content_hash_not_id(db_session) -> None:
    """Tombstone matching is by content_hash, NOT by message_version.id.

    A forget_events row with tombstone_key='message_hash:<hash>' blocks the
    version that carries that hash, regardless of row id.
    """
    from bot.services.visibility_derivation import CardVisibility, derive_card_visibility

    specific_hash = f"hash-tombstone-{uuid4().hex[:8]}"
    _, v_with_hash = await _make_version(db_session, content_hash=specific_hash)
    _, v_different = await _make_version(db_session)

    await _make_forget_event(db_session, tombstone_key=f"message_hash:{specific_hash}")

    # Only v_with_hash is blocked
    result_blocked = await derive_card_visibility(db_session, [v_with_hash])
    assert result_blocked.visibility == CardVisibility.FORGOTTEN

    # v_different is not blocked
    result_clear = await derive_card_visibility(db_session, [v_different])
    assert result_clear.visibility == CardVisibility.VISIBLE


async def test_tombstone_wrong_key_format_no_match(db_session) -> None:
    """A forget_events row with target_type='message' (not 'message_hash') does NOT
    block via content_hash. The tombstone_key format must be 'message_hash:<hash>'."""
    from bot.services.visibility_derivation import CardVisibility, derive_card_visibility

    content_hash = f"hash-wrongtype-{uuid4().hex[:8]}"
    _, v_id = await _make_version(db_session, content_hash=content_hash)

    # A 'message' tombstone key — different format
    from bot.db.repos.forget_event import ForgetEventRepo

    await ForgetEventRepo.create(
        db_session,
        target_type="message",
        target_id="12345",
        actor_user_id=None,
        authorized_by="admin",
        tombstone_key=f"message:99999:{content_hash}",  # not 'message_hash:...'
    )

    result = await derive_card_visibility(db_session, [v_id])

    # Should be VISIBLE — the message:... key doesn't match content_hash lookup
    assert result.visibility == CardVisibility.VISIBLE


async def test_multiple_blocking_sources_all_listed(db_session) -> None:
    """When multiple sources block, all are listed in blocking_source_ids."""
    from bot.services.visibility_derivation import CardVisibility, derive_card_visibility

    _, v1 = await _make_version(db_session, memory_policy="offrecord")
    _, v2 = await _make_version(db_session, memory_policy="offrecord")

    result = await derive_card_visibility(db_session, [v1, v2])

    assert result.visibility == CardVisibility.REDACTED
    assert v1 in result.blocking_source_ids
    assert v2 in result.blocking_source_ids


async def test_reason_is_non_empty_string(db_session) -> None:
    """VisibilityDerivation.reason must always be a non-empty string."""
    from bot.services.visibility_derivation import derive_card_visibility

    _, v_id = await _make_version(db_session, memory_policy="normal")
    result = await derive_card_visibility(db_session, [v_id])

    assert isinstance(result.reason, str)
    assert len(result.reason) > 0


async def test_reason_describes_blocking_state(db_session) -> None:
    """Reason for non-visible states mentions the blocking policy."""
    from bot.services.visibility_derivation import derive_card_visibility

    _, v_id = await _make_version(db_session, memory_policy="offrecord")
    result = await derive_card_visibility(db_session, [v_id])

    assert "offrecord" in result.reason.lower() or "redact" in result.reason.lower()


# ─── 4. precedence ordering invariants ───────────────────────────────────────


async def test_precedence_redacted_over_all(db_session) -> None:
    """REDACTED > NOMEM > FORGOTTEN > VISIBLE: REDACTED wins over all others."""
    from bot.services.visibility_derivation import CardVisibility, derive_card_visibility

    content_hash = f"hash-prec-{uuid4().hex[:8]}"
    _, v_vis = await _make_version(db_session, memory_policy="normal")
    _, v_nm = await _make_version(db_session, memory_policy="nomem")
    _, v_fgt = await _make_version(db_session, content_hash=content_hash)
    _, v_red = await _make_version(db_session, is_redacted=True)
    await _make_forget_event(db_session, tombstone_key=f"message_hash:{content_hash}")

    result = await derive_card_visibility(db_session, [v_vis, v_nm, v_fgt, v_red])

    assert result.visibility == CardVisibility.REDACTED


async def test_precedence_nomem_over_forgotten(db_session) -> None:
    """NOMEM beats FORGOTTEN."""
    from bot.services.visibility_derivation import CardVisibility, derive_card_visibility

    content_hash = f"hash-nm-fgt-{uuid4().hex[:8]}"
    _, v_nm = await _make_version(db_session, memory_policy="nomem")
    _, v_fgt = await _make_version(db_session, content_hash=content_hash)
    await _make_forget_event(db_session, tombstone_key=f"message_hash:{content_hash}")

    result = await derive_card_visibility(db_session, [v_nm, v_fgt])

    assert result.visibility == CardVisibility.NOMEM


async def test_precedence_forgotten_over_visible(db_session) -> None:
    """FORGOTTEN beats VISIBLE."""
    from bot.services.visibility_derivation import CardVisibility, derive_card_visibility

    content_hash = f"hash-fgt-vis-{uuid4().hex[:8]}"
    _, v_vis = await _make_version(db_session, memory_policy="normal")
    _, v_fgt = await _make_version(db_session, content_hash=content_hash)
    await _make_forget_event(db_session, tombstone_key=f"message_hash:{content_hash}")

    result = await derive_card_visibility(db_session, [v_vis, v_fgt])

    assert result.visibility == CardVisibility.FORGOTTEN


async def test_precedence_nomem_over_visible(db_session) -> None:
    """NOMEM beats VISIBLE."""
    from bot.services.visibility_derivation import CardVisibility, derive_card_visibility

    _, v_vis = await _make_version(db_session, memory_policy="normal")
    _, v_nm = await _make_version(db_session, memory_policy="nomem")

    result = await derive_card_visibility(db_session, [v_vis, v_nm])

    assert result.visibility == CardVisibility.NOMEM


# ─── 5. VisibilityDerivation dataclass properties ────────────────────────────


async def test_derivation_is_frozen(db_session) -> None:
    """VisibilityDerivation must be frozen (immutable)."""
    from bot.services.visibility_derivation import derive_card_visibility

    result = await derive_card_visibility(db_session, [])

    with pytest.raises((AttributeError, TypeError)):
        result.visibility = "anything"  # type: ignore[misc]


async def test_blocking_source_ids_is_tuple(db_session) -> None:
    """blocking_source_ids must be a tuple (frozen)."""
    from bot.services.visibility_derivation import derive_card_visibility

    result = await derive_card_visibility(db_session, [])

    assert isinstance(result.blocking_source_ids, tuple)


async def test_card_visibility_enum_values() -> None:
    """CardVisibility enum has the four required string values."""
    from bot.services.visibility_derivation import CardVisibility

    assert CardVisibility.VISIBLE == "visible"
    assert CardVisibility.REDACTED == "redacted"
    assert CardVisibility.FORGOTTEN == "forgotten"
    assert CardVisibility.NOMEM == "nomem"


# ─── 6. forgotten policy on chat_messages (cascade already ran) ───────────────


async def test_parent_memory_policy_forgotten_is_redacted(db_session) -> None:
    """If chat_messages.memory_policy='forgotten' (cascade already ran), result is REDACTED.

    After forget cascade completes, is_redacted=True on both chat_messages and
    message_versions. The 'forgotten' memory_policy on parent is treated as REDACTED
    because content is gone and the row is effectively protected.
    """
    from bot.services.visibility_derivation import CardVisibility, derive_card_visibility

    # After cascade: is_redacted=True, memory_policy='forgotten'
    _, v_id = await _make_version(db_session, memory_policy="forgotten", is_redacted=True)

    result = await derive_card_visibility(db_session, [v_id])

    assert result.visibility == CardVisibility.REDACTED


# ─── 7. async behavior — read-only guarantee ─────────────────────────────────


async def test_no_db_writes_on_visible(db_session) -> None:
    """derive_card_visibility must not write to the DB (read-only invariant).

    We verify by running on a clean visible source and asserting no flush side-effects
    (the session dirty set is empty after the call).
    """
    from bot.services.visibility_derivation import CardVisibility, derive_card_visibility

    _, v_id = await _make_version(db_session, memory_policy="normal")

    # Flush pending (the version itself)
    await db_session.flush()

    # Nothing dirty before the call
    assert not db_session.dirty

    result = await derive_card_visibility(db_session, [v_id])

    assert result.visibility == CardVisibility.VISIBLE
    # Still nothing dirty after the call
    assert not db_session.dirty


async def test_no_db_writes_on_redacted(db_session) -> None:
    """derive_card_visibility must not write to the DB even for blocking sources."""
    from bot.services.visibility_derivation import CardVisibility, derive_card_visibility

    _, v_id = await _make_version(db_session, memory_policy="offrecord")
    await db_session.flush()

    assert not db_session.dirty

    result = await derive_card_visibility(db_session, [v_id])

    assert result.visibility == CardVisibility.REDACTED
    assert not db_session.dirty


# ─── 8. golden fixture parameterized tests ───────────────────────────────────


def _golden_fixture_ids():
    """Return golden fixture paths as test IDs."""
    if not GOLDEN_FIXTURES_DIR.exists():
        return []
    return sorted(GOLDEN_FIXTURES_DIR.glob("*.json"))


@pytest.mark.parametrize("fixture_path", _golden_fixture_ids(), ids=lambda p: p.stem)
async def test_golden_fixture(db_session, fixture_path: Path) -> None:
    """Parameterized: each golden JSON file → run derive_card_visibility and assert expected."""
    from bot.services.visibility_derivation import CardVisibility, derive_card_visibility

    data = json.loads(fixture_path.read_text())

    # Build message_version_id map: fixture_local_id → real DB id
    id_map: dict[int, int] = {}

    for src in data["sources"]:
        local_id = src["local_id"]
        memory_policy = src.get("memory_policy", "normal")
        is_redacted = src.get("is_redacted", False)
        content_hash = src.get("content_hash", f"hash-golden-{uuid4().hex[:8]}")

        _, ver_id = await _make_version(
            db_session,
            memory_policy=memory_policy,
            is_redacted=is_redacted,
            content_hash=content_hash,
        )
        id_map[local_id] = ver_id

        # If this source has a tombstone, create the forget_event
        if src.get("tombstone_key"):
            await _make_forget_event(db_session, tombstone_key=src["tombstone_key"])

    cited_version_ids = [id_map[src["local_id"]] for src in data["sources"]]

    result = await derive_card_visibility(db_session, cited_version_ids)

    expected_visibility = data["expected"]["visibility"]
    assert result.visibility == CardVisibility(expected_visibility), (
        f"Expected {expected_visibility!r}, got {result.visibility!r} "
        f"for fixture {fixture_path.name}"
    )
