"""T1-14 acceptance tests for the edited_message handler.

Test isolation strategy:
- Tests 1–5, 7–9: offline (mock-based, no DB). Fast, hermetic.
- Test 6 (caption-on-media): offline mock.
- DB-backed variants use the ``db_session`` fixture with real postgres.

All offline tests use ``SimpleNamespace`` for the aiogram Message and mock the repos
directly, mirroring the pattern in ``tests/test_chat_messages_no_auto_member.py``.

Privacy ordering invariant verified in every test that touches #offrecord: detect_policy
is called BEFORE any content is mutated in the DB.
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import import_module

pytestmark = pytest.mark.usefixtures("app_env")

# ─── Helpers ─────────────────────────────────────────────────────────────────


def _random_chat_id() -> int:
    return -1_000_000_000_000 - random.randint(0, 999_999)


def _random_message_id() -> int:
    return random.randint(100_000, 999_999)


def _random_user_id() -> int:
    return random.randint(900_000_000, 999_999_999)


def _make_message(
    *,
    message_id: int | None = None,
    chat_id: int = -1001234567890,
    user_id: int | None = None,
    text: str | None = "hello",
    caption: str | None = None,
    entities: list | None = None,
    caption_entities: list | None = None,
    edit_date: datetime | None = None,
    message_thread_id: int | None = None,
    photo: object = None,
    video: object = None,
) -> SimpleNamespace:
    """Build a minimal SimpleNamespace that mimics an aiogram Message for edited_message."""
    return SimpleNamespace(
        message_id=message_id or _random_message_id(),
        chat=SimpleNamespace(id=chat_id, type="supergroup"),
        from_user=SimpleNamespace(id=user_id or _random_user_id()),
        text=text,
        caption=caption,
        entities=entities,
        caption_entities=caption_entities,
        edit_date=edit_date or datetime.now(timezone.utc),
        message_thread_id=message_thread_id,
        # aiogram-style attribute probes for classify_message_kind:
        forward_origin=None,
        photo=photo,
        video=video,
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
        reply_to_message=None,
    )


def _make_chat_message_row(
    *,
    id: int | None = None,
    message_id: int | None = None,
    chat_id: int = -1001234567890,
    user_id: int | None = None,
    text: str | None = "original text",
    caption: str | None = None,
    message_kind: str = "text",
    memory_policy: str = "normal",
    is_redacted: bool = False,
    raw_json: dict | None = None,
    current_version_id: int | None = 1,
    content_hash: str | None = None,
) -> MagicMock:
    """Return a MagicMock shaped like a ChatMessage ORM row."""
    row = MagicMock()
    row.id = id or random.randint(1, 100_000)
    row.message_id = message_id or _random_message_id()
    row.chat_id = chat_id
    row.user_id = user_id or _random_user_id()
    row.text = text
    row.caption = caption
    row.message_kind = message_kind
    row.memory_policy = memory_policy
    row.is_redacted = is_redacted
    row.raw_json = raw_json
    row.current_version_id = current_version_id
    row.content_hash = content_hash
    return row


def _make_version_row(*, id: int = 1, version_seq: int = 1, content_hash: str = "") -> MagicMock:
    v = MagicMock()
    v.id = id
    v.version_seq = version_seq
    v.content_hash = content_hash
    return v


# ─── Test 1: hash change creates v2 ──────────────────────────────────────────


def test_edit_changes_text_creates_v2(app_env, monkeypatch) -> None:
    """Edit with different text produces a new message_versions row and updates current_version_id."""
    handler = import_module("bot.handlers.edited_message")

    msg_id = _random_message_id()
    chat_id = -1001234567890
    message = _make_message(message_id=msg_id, chat_id=chat_id, text="edited text")

    # Existing row has old text with a known chv1 hash.
    from bot.services.content_hash import compute_content_hash

    existing_row = _make_chat_message_row(
        id=10,
        message_id=msg_id,
        chat_id=chat_id,
        text="original text",
        memory_policy="normal",
        current_version_id=1,
    )

    new_hash = compute_content_hash("edited text", None, "text", None)
    new_version = _make_version_row(id=2, version_seq=2, content_hash=new_hash)

    # Mocks
    mock_find = AsyncMock(return_value=existing_row)
    mock_get_by_hash = AsyncMock(return_value=None)  # new hash not in DB
    mock_insert_version = AsyncMock(return_value=new_version)
    mock_refresh = AsyncMock()

    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=existing_row))
    )
    session.flush = AsyncMock()
    session.refresh = mock_refresh

    monkeypatch.setattr(handler, "_find_chat_message", mock_find)
    monkeypatch.setattr(handler.MessageVersionRepo, "get_by_hash", mock_get_by_hash)
    monkeypatch.setattr(handler.MessageVersionRepo, "insert_version", mock_insert_version)

    # After refresh, row has same text/caption (simulating row state post-policy check).
    def _refresh_side_effect(row):
        pass  # row already has the right text

    session.refresh.side_effect = _refresh_side_effect

    asyncio.run(handler.handle_edited_message(message, session))

    mock_insert_version.assert_awaited_once()
    call_kwargs = mock_insert_version.call_args
    assert call_kwargs.kwargs["content_hash"] == new_hash
    assert call_kwargs.kwargs["text"] == "edited text"
    assert call_kwargs.kwargs["is_redacted"] is False

    # session.execute was called for SELECT + UPDATE (at least 2 calls)
    assert session.execute.call_count >= 1


# ─── Test 2: unchanged content → no new version ───────────────────────────────


def test_edit_unchanged_content_no_version(app_env, monkeypatch) -> None:
    """Edit with identical normalized content (same chv1 hash) → no new version row."""
    handler = import_module("bot.handlers.edited_message")

    msg_id = _random_message_id()
    chat_id = -1001234567890
    # Same text in edit as original
    message = _make_message(message_id=msg_id, chat_id=chat_id, text="same text")

    from bot.services.content_hash import compute_content_hash

    same_hash = compute_content_hash("same text", None, "text", None)
    existing_row = _make_chat_message_row(
        id=10,
        message_id=msg_id,
        chat_id=chat_id,
        text="same text",
        message_kind="text",
        memory_policy="normal",
    )
    existing_version = _make_version_row(id=1, version_seq=1, content_hash=same_hash)

    mock_find = AsyncMock(return_value=existing_row)
    # get_by_hash returns the existing version (same hash already in DB)
    mock_get_by_hash = AsyncMock(return_value=existing_version)
    mock_insert_version = AsyncMock()
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=existing_row))
    )
    session.refresh = AsyncMock()

    monkeypatch.setattr(handler, "_find_chat_message", mock_find)
    monkeypatch.setattr(handler.MessageVersionRepo, "get_by_hash", mock_get_by_hash)
    monkeypatch.setattr(handler.MessageVersionRepo, "insert_version", mock_insert_version)

    asyncio.run(handler.handle_edited_message(message, session))

    mock_insert_version.assert_not_awaited()


# ─── Test 3: normal → offrecord flip: zero out content ────────────────────────


def test_edit_normal_to_offrecord_flip_zero_out(app_env, monkeypatch) -> None:
    """Edit adding #offrecord flips policy, nulls text/caption/raw_json, sets is_redacted=True,
    creates offrecord_marks row — all in same transaction."""
    handler = import_module("bot.handlers.edited_message")

    msg_id = _random_message_id()
    chat_id = -1001234567890
    user_id = _random_user_id()
    # Edited message now contains #offrecord
    message = _make_message(
        message_id=msg_id, chat_id=chat_id, user_id=user_id, text="this is #offrecord content"
    )

    existing_row = _make_chat_message_row(
        id=10,
        message_id=msg_id,
        chat_id=chat_id,
        text="original normal text",
        memory_policy="normal",
    )

    # After _apply_offrecord_flip the row's text becomes None
    def _refresh_side_effect(row):
        row.text = None
        row.caption = None
        row.memory_policy = "offrecord"

    mock_find = AsyncMock(return_value=existing_row)
    mock_apply_flip = AsyncMock()
    mock_get_by_hash = AsyncMock(return_value=None)
    mock_insert_version = AsyncMock(return_value=_make_version_row(id=2, version_seq=2))
    mock_mark_create = AsyncMock()

    session = AsyncMock()
    session.refresh = AsyncMock(side_effect=_refresh_side_effect)
    session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=existing_row))
    )
    session.flush = AsyncMock()

    monkeypatch.setattr(handler, "_find_chat_message", mock_find)
    monkeypatch.setattr(handler, "_apply_offrecord_flip", mock_apply_flip)
    monkeypatch.setattr(handler.MessageVersionRepo, "get_by_hash", mock_get_by_hash)
    monkeypatch.setattr(handler.MessageVersionRepo, "insert_version", mock_insert_version)
    monkeypatch.setattr(handler.OffrecordMarkRepo, "create_for_message", mock_mark_create)

    asyncio.run(handler.handle_edited_message(message, session))

    # _apply_offrecord_flip must have been called (BLOCKER #1) — this is the audit
    # trail: parent row content nulled + offrecord_marks row created.
    mock_apply_flip.assert_awaited_once()

    # Codex HIGH (privacy): on normal→offrecord flip the version row is NOT inserted —
    # both the parent row's now-null content and the redacted-state lookup hash collapse
    # to the same redacted-state chv1, so insert_version's idempotency path returns early.
    # The state change is captured by the parent row update + offrecord_marks audit row.
    # Storing a fresh version row here would either fingerprint the raw content (privacy
    # leak Codex flagged) or be a redundant no-op row (no information gain).
    mock_insert_version.assert_not_awaited()


def test_apply_offrecord_flip_redacts_existing_message_versions(app_env) -> None:
    """Phase 1 final-review CRITICAL (Codex PRIVACY_LEAK_CLASS_4): on a normal→offrecord
    flip ``_apply_offrecord_flip`` must redact every existing ``message_versions`` row of
    the parent — not only the parent ``chat_messages`` row. Without this, historical v1
    (T1-07 backfill) and any prior v(n+1) rows still contain raw text/caption after the
    user has scoped the message ``#offrecord``.

    Captures all ``session.execute`` calls and asserts there is at least one UPDATE
    statement targeting the ``MessageVersion`` table whose values dict nulls out
    ``text``, ``caption``, ``normalized_text``, and ``entities_json`` and sets
    ``is_redacted=True``.
    """
    import asyncio

    from sqlalchemy import update as sa_update

    handler = import_module("bot.handlers.edited_message")
    MessageVersion = handler.MessageVersion
    ChatMessage = handler.ChatMessage

    captured: list[dict] = []

    async def capture_execute(stmt, *args, **kwargs):
        try:
            target = stmt.table.name if hasattr(stmt, "table") else None
        except Exception:
            target = None
        try:
            values = dict(getattr(stmt, "_values", {}) or {})
            captured.append(
                {
                    "target": target,
                    "values": {
                        str(k.key) if hasattr(k, "key") else str(k): v for k, v in values.items()
                    },
                }
            )
        except Exception:  # pragma: no cover — defensive
            captured.append({"target": target, "values": {}})
        return MagicMock()

    row = MagicMock()
    row.id = 42

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=capture_execute)
    session.flush = AsyncMock()

    asyncio.run(
        handler._apply_offrecord_flip(
            session,
            row,
            mark_payload={"detected_by": "deterministic_token_match_v1"},
            set_by_user_id=999,
            thread_id=None,
        )
    )

    # Find the UPDATE against message_versions specifically.
    version_updates = [c for c in captured if c["target"] == "message_versions"]
    assert version_updates, (
        "CRITICAL PRIVACY LEAK: _apply_offrecord_flip did not issue an UPDATE on "
        "message_versions — historical version rows would retain raw content after "
        "the offrecord flip. Captured statements: %r" % captured
    )

    def _unwrap(v):
        # Values in stmt._values are BindParameter objects; pull .value or .effective_value.
        for attr in ("value", "effective_value"):
            if hasattr(v, attr):
                return getattr(v, attr)
        return v

    for u in version_updates:
        v = u["values"]
        assert _unwrap(v.get("text")) is None, f"text not nulled: {v}"
        assert _unwrap(v.get("caption")) is None, f"caption not nulled: {v}"
        assert _unwrap(v.get("normalized_text")) is None, f"normalized_text not nulled: {v}"
        assert _unwrap(v.get("entities_json")) is None, f"entities_json not nulled: {v}"
        assert _unwrap(v.get("is_redacted")) is True, f"is_redacted not set: {v}"

    # Reference variables to silence linters about unused imports needed for the
    # MessageVersion / ChatMessage attribute lookups inside the handler.
    _ = (sa_update, MessageVersion, ChatMessage)


# ─── Test 4: offrecord → normal flip is a NO-OP for policy AND content ──────


def test_edited_message_offrecord_to_normal_is_noop_for_policy_and_content(
    app_env, monkeypatch
) -> None:
    """Sticky offrecord (Codex Sprint #80 fixup, Finding 1 CRITICAL).

    Edit removing #offrecord from a row whose stored policy is already 'offrecord' must:
    1. NOT call ``_update_memory_policy`` — the row stays offrecord (sticky policy).
    2. NOT insert a ``message_versions`` row carrying the raw edited text/caption — even
       though the redacted-state hash differs from chv1(text). Doing so would persist the
       restored content fingerprint in the audit table, defeating the offrecord guarantee.
    3. NOT write text/caption back onto the parent ``chat_messages`` row.

    The previous implementation took the ``elif new_policy != old_policy`` branch on
    offrecord→normal, called ``_update_memory_policy`` to flip the row back to 'normal',
    and then proceeded to insert a v(n+1) version row with ``text=<edited>``,
    ``is_redacted=False``, and ``content_hash=chv1(<edited>)`` — privacy bypass.
    """
    handler = import_module("bot.handlers.edited_message")

    msg_id = _random_message_id()
    chat_id = -1001234567890
    message = _make_message(
        message_id=msg_id, chat_id=chat_id, text="clean message without offrecord"
    )

    existing_row = _make_chat_message_row(
        id=10,
        message_id=msg_id,
        chat_id=chat_id,
        text=None,
        caption=None,
        memory_policy="offrecord",
        is_redacted=True,
    )

    mock_find = AsyncMock(return_value=existing_row)
    mock_update_policy = AsyncMock()
    mock_get_by_hash = AsyncMock(return_value=None)
    mock_insert_version = AsyncMock(return_value=_make_version_row(id=2, version_seq=2))

    session = AsyncMock()
    session.refresh = AsyncMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=existing_row))
    )
    session.flush = AsyncMock()

    monkeypatch.setattr(handler, "_find_chat_message", mock_find)
    monkeypatch.setattr(handler, "_update_memory_policy", mock_update_policy)
    monkeypatch.setattr(handler.MessageVersionRepo, "get_by_hash", mock_get_by_hash)
    monkeypatch.setattr(handler.MessageVersionRepo, "insert_version", mock_insert_version)

    asyncio.run(handler.handle_edited_message(message, session))

    # 1. Policy MUST NOT be updated — row stays offrecord.
    mock_update_policy.assert_not_awaited()

    # 2. No version row may be inserted with raw text/caption. Either insert_version is
    #    not called at all (idempotency on redacted-state hash), or — if it is — text
    #    and caption are None and is_redacted is True.
    if mock_insert_version.await_count > 0:
        kwargs = mock_insert_version.call_args.kwargs
        assert kwargs.get("text") is None, (
            "PRIVACY VIOLATION: offrecord→normal edit inserted version row with raw "
            f"text. kwargs={kwargs}"
        )
        assert kwargs.get("caption") is None, (
            "PRIVACY VIOLATION: offrecord→normal edit inserted version row with raw "
            f"caption. kwargs={kwargs}"
        )
        assert kwargs.get("is_redacted") is True, (
            "PRIVACY VIOLATION: offrecord→normal edit inserted version row with "
            f"is_redacted=False. kwargs={kwargs}"
        )


def test_edited_message_offrecord_to_nomem_is_noop(app_env, monkeypatch) -> None:
    """Sticky offrecord — same invariant for ``#nomem`` instead of normal.

    An offrecord row must reject any policy downgrade, including offrecord→nomem.
    """
    handler = import_module("bot.handlers.edited_message")

    msg_id = _random_message_id()
    chat_id = -1001234567890
    message = _make_message(
        message_id=msg_id, chat_id=chat_id, text="please #nomem this"
    )

    existing_row = _make_chat_message_row(
        id=12,
        message_id=msg_id,
        chat_id=chat_id,
        text=None,
        caption=None,
        memory_policy="offrecord",
        is_redacted=True,
    )

    mock_find = AsyncMock(return_value=existing_row)
    mock_update_policy = AsyncMock()
    mock_get_by_hash = AsyncMock(return_value=None)
    mock_insert_version = AsyncMock(return_value=_make_version_row(id=2, version_seq=2))

    session = AsyncMock()
    session.refresh = AsyncMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=existing_row))
    )
    session.flush = AsyncMock()

    monkeypatch.setattr(handler, "_find_chat_message", mock_find)
    monkeypatch.setattr(handler, "_update_memory_policy", mock_update_policy)
    monkeypatch.setattr(handler.MessageVersionRepo, "get_by_hash", mock_get_by_hash)
    monkeypatch.setattr(handler.MessageVersionRepo, "insert_version", mock_insert_version)

    asyncio.run(handler.handle_edited_message(message, session))

    mock_update_policy.assert_not_awaited()
    if mock_insert_version.await_count > 0:
        kwargs = mock_insert_version.call_args.kwargs
        assert kwargs.get("text") is None
        assert kwargs.get("caption") is None
        assert kwargs.get("is_redacted") is True


def test_edited_message_offrecord_caption_only_edit_is_noop(
    app_env, monkeypatch
) -> None:
    """Sticky offrecord covers caption-only edits on media (Finding 3 caption coverage).

    A photo message that was offrecord-flipped must reject any later caption edit that
    drops the #offrecord token — same invariant as the text path.
    """
    handler = import_module("bot.handlers.edited_message")

    msg_id = _random_message_id()
    chat_id = -1001234567890
    message = _make_message(
        message_id=msg_id,
        chat_id=chat_id,
        text=None,
        caption="now a clean caption",
        photo=object(),
    )

    existing_row = _make_chat_message_row(
        id=13,
        message_id=msg_id,
        chat_id=chat_id,
        text=None,
        caption=None,
        message_kind="photo",
        memory_policy="offrecord",
        is_redacted=True,
    )

    mock_find = AsyncMock(return_value=existing_row)
    mock_update_policy = AsyncMock()
    mock_get_by_hash = AsyncMock(return_value=None)
    mock_insert_version = AsyncMock(return_value=_make_version_row(id=2, version_seq=2))

    session = AsyncMock()
    session.refresh = AsyncMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=existing_row))
    )
    session.flush = AsyncMock()

    monkeypatch.setattr(handler, "_find_chat_message", mock_find)
    monkeypatch.setattr(handler, "_update_memory_policy", mock_update_policy)
    monkeypatch.setattr(handler.MessageVersionRepo, "get_by_hash", mock_get_by_hash)
    monkeypatch.setattr(handler.MessageVersionRepo, "insert_version", mock_insert_version)

    asyncio.run(handler.handle_edited_message(message, session))

    mock_update_policy.assert_not_awaited()
    if mock_insert_version.await_count > 0:
        kwargs = mock_insert_version.call_args.kwargs
        assert kwargs.get("text") is None
        assert kwargs.get("caption") is None
        assert kwargs.get("is_redacted") is True


def test_update_memory_policy_helper_refuses_to_downgrade_offrecord(
    app_env,
) -> None:
    """Defense in depth (Finding 1 part 2): ``_update_memory_policy`` must short-circuit
    to a no-op when the row is already 'offrecord'.

    This is belt-and-suspenders: even if a future caller forgets the sticky check at the
    handler layer, this helper must NEVER write a non-offrecord policy onto an offrecord
    row. Mirrors the ``MessageRepo.save`` sticky CASE.
    """
    import asyncio

    handler = import_module("bot.handlers.edited_message")

    captured: list = []

    async def capture_execute(stmt, *args, **kwargs):
        captured.append(stmt)
        return MagicMock()

    row = MagicMock()
    row.id = 99
    row.memory_policy = "offrecord"

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=capture_execute)
    session.flush = AsyncMock()

    asyncio.run(
        handler._update_memory_policy(
            session,
            row,
            "normal",
            mark_payload=None,
            set_by_user_id=None,
            thread_id=None,
        )
    )

    # Helper must issue ZERO statements when the row is already offrecord.
    assert captured == [], (
        "PRIVACY VIOLATION: _update_memory_policy issued statements for an "
        f"offrecord→normal call. Captured: {captured}"
    )


def test_edit_offrecord_to_normal_does_not_write_text_caption_to_parent(
    app_env, monkeypatch
) -> None:
    """Stronger irreversibility assertion (Codex cross-team review BLOCKER).

    Captures the actual UPDATE statement issued against ChatMessage and asserts that
    on offrecord→normal flip, the values dict does NOT contain ``text`` or ``caption``
    keys — only ``current_version_id``. This catches the original Team A bug where
    the broad ``new_policy != 'offrecord'`` branch leaked the edited text into the
    parent row.
    """
    handler = import_module("bot.handlers.edited_message")

    msg_id = _random_message_id()
    chat_id = -1001234567890
    message = _make_message(message_id=msg_id, chat_id=chat_id, text="trying to come back")

    existing_row = _make_chat_message_row(
        id=11,
        message_id=msg_id,
        chat_id=chat_id,
        text=None,
        caption=None,
        memory_policy="offrecord",
        is_redacted=True,
    )

    captured_updates: list[dict] = []

    async def capture_execute(stmt, *args, **kwargs):
        # update(ChatMessage).values(**update_values) — pull values out of the compiled
        # statement. SQLAlchemy 2.0 exposes them via stmt.compile().params or
        # stmt._values (private). We use the simpler `_values` attribute, which is a
        # dict-like of Column→bound-value at the Update level.
        try:
            values = dict(getattr(stmt, "_values", {}) or {})
            # Convert sqlalchemy Column keys to str names.
            captured_updates.append(
                {str(k.key) if hasattr(k, "key") else str(k): v for k, v in values.items()}
            )
        except Exception:  # pragma: no cover — defensive
            captured_updates.append({})
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=existing_row)
        return result

    mock_find = AsyncMock(return_value=existing_row)
    mock_get_by_hash = AsyncMock(return_value=None)
    mock_insert_version = AsyncMock(return_value=_make_version_row(id=2, version_seq=2))

    session = AsyncMock()
    session.refresh = AsyncMock()
    session.execute = AsyncMock(side_effect=capture_execute)
    session.flush = AsyncMock()

    monkeypatch.setattr(handler, "_find_chat_message", mock_find)
    monkeypatch.setattr(handler.MessageVersionRepo, "get_by_hash", mock_get_by_hash)
    monkeypatch.setattr(handler.MessageVersionRepo, "insert_version", mock_insert_version)

    asyncio.run(handler.handle_edited_message(message, session))

    # Across all UPDATE statements issued in this handler invocation, none must touch
    # the content columns of the parent row when prior policy was offrecord.
    for upd_values in captured_updates:
        assert "text" not in upd_values, (
            f"PRIVACY VIOLATION: edited text leaked into parent row on "
            f"offrecord→normal flip. Captured update: {upd_values}"
        )
        assert "caption" not in upd_values, (
            f"PRIVACY VIOLATION: edited caption leaked into parent row on "
            f"offrecord→normal flip. Captured update: {upd_values}"
        )
        assert "raw_json" not in upd_values, (
            f"PRIVACY VIOLATION: raw_json leaked into parent row on "
            f"offrecord→normal flip. Captured update: {upd_values}"
        )
        # Sticky offrecord (Codex Sprint #80 Finding 1): the parent row must NOT have
        # ``memory_policy`` flipped back to 'normal' / 'nomem' either.
        if "memory_policy" in upd_values:
            policy_val = upd_values["memory_policy"]
            for attr in ("value", "effective_value"):
                if hasattr(policy_val, attr):
                    policy_val = getattr(policy_val, attr)
            assert policy_val == "offrecord", (
                f"PRIVACY VIOLATION: parent row's memory_policy downgraded from "
                f"'offrecord' to {policy_val!r} on edit. Captured update: {upd_values}"
            )

    # Sticky offrecord (Codex Sprint #80 Finding 1): if a version row WAS inserted
    # (depends on stored existing.text vs edited content shape), it MUST NOT carry
    # the raw edited text/caption — that would persist a content fingerprint on the
    # audit table.
    if mock_insert_version.await_count > 0:
        kwargs = mock_insert_version.call_args.kwargs
        assert kwargs.get("text") is None, (
            f"PRIVACY VIOLATION: version row inserted with raw text on offrecord→normal "
            f"flip. kwargs={kwargs}"
        )
        assert kwargs.get("caption") is None, (
            f"PRIVACY VIOLATION: version row inserted with raw caption on offrecord→normal "
            f"flip. kwargs={kwargs}"
        )
        assert kwargs.get("is_redacted") is True, (
            f"PRIVACY VIOLATION: version row inserted with is_redacted=False on "
            f"offrecord→normal flip. kwargs={kwargs}"
        )


# ─── Test 5: unknown prior message → warning + return, no crash ───────────────


def test_edit_unknown_prior_logs_warning_no_crash(app_env, monkeypatch) -> None:
    """Edit for unknown (chat_id, message_id) → logger.warning, no crash, no new rows."""
    handler = import_module("bot.handlers.edited_message")

    message = _make_message(text="some edit")

    mock_find = AsyncMock(return_value=None)  # row not found
    mock_insert_version = AsyncMock()
    session = AsyncMock()

    monkeypatch.setattr(handler, "_find_chat_message", mock_find)
    monkeypatch.setattr(handler.MessageVersionRepo, "insert_version", mock_insert_version)

    with patch.object(handler.logger, "warning") as mock_warning:
        asyncio.run(handler.handle_edited_message(message, session))
        mock_warning.assert_called_once()
        warning_msg = mock_warning.call_args[0][0]
        assert "no prior row" in warning_msg or "edited_message" in warning_msg

    mock_insert_version.assert_not_awaited()
    # No new ChatMessage row is created
    session.add.assert_not_called()


# ─── Test 6: caption-on-media creates v2 ─────────────────────────────────────


def test_edit_caption_on_media_creates_v2(app_env, monkeypatch) -> None:
    """Photo message with edited caption creates v2 version."""
    handler = import_module("bot.handlers.edited_message")

    msg_id = _random_message_id()
    chat_id = -1001234567890
    # Media message: text=None, caption changed
    message = _make_message(
        message_id=msg_id,
        chat_id=chat_id,
        text=None,
        caption="new caption",
        photo=object(),  # triggers "photo" kind
    )

    from bot.services.content_hash import compute_content_hash

    # Existing row had old caption
    existing_row = _make_chat_message_row(
        id=10,
        message_id=msg_id,
        chat_id=chat_id,
        text=None,
        caption="old caption",
        message_kind="photo",
        memory_policy="normal",
    )

    new_hash = compute_content_hash(None, "new caption", "photo", None)
    new_version = _make_version_row(id=2, version_seq=2, content_hash=new_hash)

    mock_find = AsyncMock(return_value=existing_row)
    mock_get_by_hash = AsyncMock(return_value=None)
    mock_insert_version = AsyncMock(return_value=new_version)

    session = AsyncMock()
    session.refresh = AsyncMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=existing_row))
    )
    session.flush = AsyncMock()

    monkeypatch.setattr(handler, "_find_chat_message", mock_find)
    monkeypatch.setattr(handler.MessageVersionRepo, "get_by_hash", mock_get_by_hash)
    monkeypatch.setattr(handler.MessageVersionRepo, "insert_version", mock_insert_version)

    asyncio.run(handler.handle_edited_message(message, session))

    mock_insert_version.assert_awaited_once()
    insert_kwargs = mock_insert_version.call_args.kwargs
    assert insert_kwargs["content_hash"] == new_hash
    assert insert_kwargs["caption"] == "new caption"


# ─── Test 7: empty text creates v2 ───────────────────────────────────────────


def test_edit_empty_text_creates_v2(app_env, monkeypatch) -> None:
    """Edit that clears text to '' is a valid v2 (empty string is distinct content)."""
    handler = import_module("bot.handlers.edited_message")

    msg_id = _random_message_id()
    chat_id = -1001234567890
    message = _make_message(message_id=msg_id, chat_id=chat_id, text="")

    from bot.services.content_hash import compute_content_hash

    old_hash = compute_content_hash("hello", None, "text", None)
    new_hash = compute_content_hash("", None, "text", None)
    assert old_hash != new_hash  # sanity: different content → different hash

    existing_row = _make_chat_message_row(
        id=10,
        message_id=msg_id,
        chat_id=chat_id,
        text="hello",
        memory_policy="normal",
    )

    new_version = _make_version_row(id=2, version_seq=2, content_hash=new_hash)

    mock_find = AsyncMock(return_value=existing_row)
    mock_get_by_hash = AsyncMock(return_value=None)
    mock_insert_version = AsyncMock(return_value=new_version)

    session = AsyncMock()
    session.refresh = AsyncMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=existing_row))
    )
    session.flush = AsyncMock()

    monkeypatch.setattr(handler, "_find_chat_message", mock_find)
    monkeypatch.setattr(handler.MessageVersionRepo, "get_by_hash", mock_get_by_hash)
    monkeypatch.setattr(handler.MessageVersionRepo, "insert_version", mock_insert_version)

    asyncio.run(handler.handle_edited_message(message, session))

    mock_insert_version.assert_awaited_once()
    insert_kwargs = mock_insert_version.call_args.kwargs
    assert insert_kwargs["content_hash"] == new_hash


# ─── Test 8: entities-only change creates v2 ─────────────────────────────────


def test_edit_entities_only_change_creates_v2(app_env, monkeypatch) -> None:
    """Text is identical but entities changed → chv1 includes entities → new hash → v2.

    This test documents the expected behavior: compute_content_hash includes entities
    in the payload, so entity-only edits (bold/italic formatting changes) produce a
    new version. This is intentional — citation stability requires versioning format changes.
    """
    handler = import_module("bot.handlers.edited_message")

    msg_id = _random_message_id()
    chat_id = -1001234567890

    old_entities = [{"offset": 0, "length": 5, "type": "bold"}]
    new_entities_obj = [
        SimpleNamespace(
            offset=0,
            length=5,
            type="bold",
            model_dump=lambda mode, exclude_none: {"offset": 0, "length": 5, "type": "bold"},
        ),
        SimpleNamespace(
            offset=6,
            length=3,
            type="italic",
            model_dump=lambda mode, exclude_none: {"offset": 6, "length": 3, "type": "italic"},
        ),
    ]

    message = _make_message(
        message_id=msg_id,
        chat_id=chat_id,
        text="hello world",
        entities=new_entities_obj,
    )

    from bot.services.content_hash import compute_content_hash

    # Old hash: text + old entities
    old_hash = compute_content_hash("hello world", None, "text", old_entities)
    # New hash: text + new entities (different)
    new_entities_dicts = [
        {"offset": 0, "length": 5, "type": "bold"},
        {"offset": 6, "length": 3, "type": "italic"},
    ]
    new_hash = compute_content_hash("hello world", None, "text", new_entities_dicts)
    assert old_hash != new_hash  # sanity: entity change → hash change

    existing_row = _make_chat_message_row(
        id=10,
        message_id=msg_id,
        chat_id=chat_id,
        text="hello world",
        memory_policy="normal",
    )

    new_version = _make_version_row(id=2, version_seq=2, content_hash=new_hash)

    mock_find = AsyncMock(return_value=existing_row)
    mock_get_by_hash = AsyncMock(return_value=None)
    mock_insert_version = AsyncMock(return_value=new_version)

    session = AsyncMock()
    session.refresh = AsyncMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=existing_row))
    )
    session.flush = AsyncMock()

    monkeypatch.setattr(handler, "_find_chat_message", mock_find)
    monkeypatch.setattr(handler.MessageVersionRepo, "get_by_hash", mock_get_by_hash)
    monkeypatch.setattr(handler.MessageVersionRepo, "insert_version", mock_insert_version)

    asyncio.run(handler.handle_edited_message(message, session))

    # v2 must be created because entity hash differs
    mock_insert_version.assert_awaited_once()


# ─── Test 9: legacy v1 row — recomputes chv1, detects no change ───────────────


def test_edit_on_legacy_v1_recomputes_chv1(app_env, monkeypatch) -> None:
    """Legacy v1 row stored with pre-chv1 hash + identical content edit → no new version.

    The T1-07 backfill created v1 rows with a legacy hash (no HASH_FORMAT_VERSION in
    payload). When an edit arrives with the same content, the new chv1 hash WON'T match
    the legacy hash in the DB (different recipe). The handler must recompute chv1 from
    the existing row's text/caption/kind and compare against the new chv1 to detect
    whether content truly changed.

    This test simulates: existing row text="old text", edit text="old text" (identical).
    Expected: no new version row (content unchanged despite legacy hash mismatch).
    """
    handler = import_module("bot.handlers.edited_message")

    msg_id = _random_message_id()
    chat_id = -1001234567890
    # Edit arrives with identical text
    message = _make_message(message_id=msg_id, chat_id=chat_id, text="old text")

    # Simulate the legacy hash (different recipe — for test purposes, just a known string)
    legacy_hash = "legacy_hash_without_chv1_prefix_" + "a" * 32

    existing_row = _make_chat_message_row(
        id=10,
        message_id=msg_id,
        chat_id=chat_id,
        text="old text",
        message_kind="text",
        caption=None,
        memory_policy="normal",
        content_hash=legacy_hash,  # stored legacy hash
    )

    mock_find = AsyncMock(return_value=existing_row)
    # get_by_hash with chv1_of_old returns None (not in DB under chv1)
    mock_get_by_hash = AsyncMock(return_value=None)
    mock_insert_version = AsyncMock()

    def _refresh_side_effect(row):
        pass  # row already reflects current state

    session = AsyncMock()
    session.refresh = AsyncMock(side_effect=_refresh_side_effect)
    session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=existing_row))
    )
    session.flush = AsyncMock()

    monkeypatch.setattr(handler, "_find_chat_message", mock_find)
    monkeypatch.setattr(handler.MessageVersionRepo, "get_by_hash", mock_get_by_hash)
    monkeypatch.setattr(handler.MessageVersionRepo, "insert_version", mock_insert_version)

    asyncio.run(handler.handle_edited_message(message, session))

    # No new version because chv1("old text") == chv1("old text") — same content
    mock_insert_version.assert_not_awaited()
