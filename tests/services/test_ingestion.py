"""T1-04 ingestion service tests.

Covers the BLOCKER cross-cutting requirement from AUTHORIZED_SCOPE.md:
- raw archive flag defaults OFF → record_update returns None, no row written
- when flag ON, raw row persisted via TelegramUpdateRepo
- detect_policy stub is called and returns 'normal' → row stored with is_redacted=False
- duplicate update_id is no-op (relies on TelegramUpdateRepo idempotency)
- raw_hash is deterministic
- get_or_create_live_run creates if missing, attaches if exists
"""

from __future__ import annotations

import itertools
import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

pytestmark = pytest.mark.usefixtures("app_env")

# Deterministic update_id generator. Each test session gets a fresh sequence starting at
# the high range that never collides with real Telegram update_ids in fixtures.
_test_update_id_counter = itertools.count(start=8_000_000_000)


def _next_update_id() -> int:
    return next(_test_update_id_counter)


def _make_message_update(
    update_id: int | None = None,
    text: str = "hello",
    chat_id: int = -1_001_234_567_890,
    message_id: int = 42,
    user_id: int = 12345,
):
    """Build an aiogram ``Update`` carrying a Message — enough fields for the
    ingestion service to extract chat/message ids, text, caption, and update_id."""
    from aiogram.types import Chat, Message, Update, User

    when = datetime.now(timezone.utc)
    chat = Chat(id=chat_id, type="supergroup", title="dev")
    user = User(id=user_id, is_bot=False, first_name="Probe")
    msg = Message(
        message_id=message_id,
        date=when,
        chat=chat,
        from_user=user,
        text=text,
    )
    return Update(update_id=update_id if update_id is not None else _next_update_id(), message=msg)


# ─── flag gating ────────────────────────────────────────────────────────────────────────


async def test_record_update_returns_none_when_flag_off(db_session) -> None:
    from bot.db.models import TelegramUpdate
    from bot.services.ingestion import record_update

    update = _make_message_update(update_id=_next_update_id())
    result = await record_update(db_session, update)

    assert result is None
    rows = await db_session.execute(
        select(TelegramUpdate).where(TelegramUpdate.update_id == update.update_id)
    )
    assert rows.scalar_one_or_none() is None


async def test_record_update_inserts_row_when_flag_on(db_session) -> None:
    from bot.db.repos.feature_flag import FeatureFlagRepo
    from bot.services.ingestion import RAW_ARCHIVE_FLAG, record_update

    await FeatureFlagRepo.set_enabled(db_session, RAW_ARCHIVE_FLAG, enabled=True)

    update = _make_message_update(update_id=_next_update_id(), text="hi")
    row = await record_update(db_session, update)

    assert row is not None
    assert row.update_id == update.update_id
    assert row.update_type == "message"
    assert row.chat_id == -1_001_234_567_890
    assert row.message_id == 42
    assert row.is_redacted is False  # T1-04 stub never returns 'offrecord'
    assert row.redaction_reason is None
    assert row.raw_hash is not None and len(row.raw_hash) == 64  # sha256 hex


async def test_record_update_idempotent_on_duplicate_update_id(db_session) -> None:
    from bot.db.repos.feature_flag import FeatureFlagRepo
    from bot.services.ingestion import RAW_ARCHIVE_FLAG, record_update

    await FeatureFlagRepo.set_enabled(db_session, RAW_ARCHIVE_FLAG, enabled=True)

    update = _make_message_update(update_id=_next_update_id(), text="first")
    first = await record_update(db_session, update)
    second = await record_update(db_session, update)

    assert first is not None and second is not None
    assert first.id == second.id


# ─── deterministic raw_hash ────────────────────────────────────────────────────────────


def test_compute_raw_hash_is_deterministic(app_env) -> None:
    from bot.services.ingestion import _compute_raw_hash

    a = _compute_raw_hash({"x": 1, "y": [1, 2, 3]})
    b = _compute_raw_hash({"y": [1, 2, 3], "x": 1})  # different key order
    assert a == b
    assert len(a) == 64


def test_compute_raw_hash_rejects_non_json_values(app_env) -> None:
    """Raw hashing must fail fast instead of stringifying unknown payload objects."""
    from aiogram.client.default import Default

    from bot.services.ingestion import _compute_raw_hash

    with pytest.raises(TypeError):
        _compute_raw_hash({"unexpected": Default("parse_mode")})


def test_telegram_serializer_resolves_nested_aiogram_defaults(app_env) -> None:
    """Telegram link previews carry aiogram Default sentinels in omitted fields."""
    from aiogram.types import LinkPreviewOptions

    from bot.services.telegram_serialization import serialize_telegram_object

    update = _make_message_update(text="https://example.test/evidence")
    assert update.message is not None
    update = update.model_copy(
        update={
            "message": update.message.model_copy(
                update={"link_preview_options": LinkPreviewOptions(is_disabled=False)}
            )
        }
    )

    payload = serialize_telegram_object(update)

    assert payload["message"]["link_preview_options"] == {"is_disabled": False}
    json.dumps(payload)


async def test_record_update_persists_link_preview_without_defaults(db_session) -> None:
    """Regression: raw archive ON must accept real updates containing link previews."""
    from aiogram.types import LinkPreviewOptions

    from bot.db.repos.feature_flag import FeatureFlagRepo
    from bot.services.ingestion import RAW_ARCHIVE_FLAG, record_update

    await FeatureFlagRepo.set_enabled(db_session, RAW_ARCHIVE_FLAG, enabled=True)
    update = _make_message_update(text="https://example.test/evidence")
    assert update.message is not None
    update = update.model_copy(
        update={
            "message": update.message.model_copy(
                update={"link_preview_options": LinkPreviewOptions(is_disabled=False)}
            )
        }
    )

    row = await record_update(db_session, update)

    assert row is not None
    assert row.raw_json["message"]["link_preview_options"] == {"is_disabled": False}
    assert "Default(" not in json.dumps(row.raw_json)


# ─── get_or_create_live_run ─────────────────────────────────────────────────────────────


async def test_get_or_create_live_run_creates_when_missing(db_session) -> None:
    from bot.services.ingestion import get_or_create_live_run

    run = await get_or_create_live_run(db_session)
    assert run.run_type == "live"
    assert run.status == "running"


async def test_get_or_create_live_run_attaches_when_exists(db_session) -> None:
    from bot.services.ingestion import get_or_create_live_run

    first = await get_or_create_live_run(db_session)
    second = await get_or_create_live_run(db_session)
    assert first.id == second.id


async def test_live_ingestion_run_created_on_startup(db_session) -> None:
    """§3.8: startup logic creates exactly one live run with run_type='live', status='running'.

    Models the dp['live_ingestion_run_id'] wiring in bot/__main__.py::on_startup.
    """
    from bot.services.ingestion import get_or_create_live_run

    # Simulate what on_startup does: call get_or_create_live_run and cache its id.
    live_run = await get_or_create_live_run(db_session)
    cached_id = live_run.id

    assert cached_id is not None
    assert live_run.run_type == "live"
    assert live_run.status == "running"

    # Idempotent: second call (e.g. bot restart) returns same id, no duplicate rows.
    second_run = await get_or_create_live_run(db_session)
    assert second_run.id == cached_id


# ─── stub detector wired ────────────────────────────────────────────────────────────────


async def test_record_update_calls_detect_policy_stub(db_session, monkeypatch) -> None:
    """Verify the stub detector is actually invoked. When T1-12 swaps the stub for the
    real detector, this test still passes — it only asserts the wiring."""
    from bot.db.repos.feature_flag import FeatureFlagRepo
    from bot.services import ingestion

    await FeatureFlagRepo.set_enabled(db_session, ingestion.RAW_ARCHIVE_FLAG, enabled=True)

    calls: list[tuple[str | None, str | None]] = []

    def _spy_detect(text, caption):
        calls.append((text, caption))
        return ("normal", None)

    monkeypatch.setattr(ingestion, "detect_policy", _spy_detect)

    update = _make_message_update(update_id=_next_update_id(), text="payload")
    await ingestion.record_update(db_session, update)

    assert len(calls) == 1
    assert calls[0] == ("payload", None)


async def test_record_update_calls_detect_policy_BEFORE_insert(db_session, monkeypatch) -> None:
    """Critical privacy invariant from AUTHORIZED_SCOPE §`#offrecord` ordering rule:
    ``detect_policy`` MUST be called BEFORE ``TelegramUpdateRepo.insert``. If the order
    swaps (insert first, detect after), an `#offrecord` message would have its raw_json
    persisted unredacted before redaction logic runs.

    This test pins the order via call-sequence spies. T1-12 swap of the detector keeps
    this contract intact."""
    from bot.db.repos import telegram_update as tu_repo_module
    from bot.db.repos.feature_flag import FeatureFlagRepo
    from bot.services import ingestion

    await FeatureFlagRepo.set_enabled(db_session, ingestion.RAW_ARCHIVE_FLAG, enabled=True)

    call_order: list[str] = []

    def _spy_detect(text, caption):
        call_order.append("detect_policy")
        return ("normal", None)

    original_insert = tu_repo_module.TelegramUpdateRepo.insert

    @staticmethod
    async def _spy_insert(session, **kwargs):
        call_order.append("insert")
        return await original_insert(session, **kwargs)

    monkeypatch.setattr(ingestion, "detect_policy", _spy_detect)
    monkeypatch.setattr(tu_repo_module.TelegramUpdateRepo, "insert", _spy_insert)

    update = _make_message_update(update_id=_next_update_id(), text="payload")
    await ingestion.record_update(db_session, update)

    assert call_order == ["detect_policy", "insert"], (
        f"#offrecord ordering rule violated: expected detect_policy before insert, got {call_order}"
    )


# ─── classifier helpers ─────────────────────────────────────────────────────────────────


def test_classify_update_type_message(app_env) -> None:
    from bot.services.ingestion import _classify_update_type

    assert _classify_update_type(_make_message_update(update_id=1)) == "message"


def test_extract_chat_and_message_ids_from_message(app_env) -> None:
    from bot.services.ingestion import _extract_chat_and_message_ids

    chat_id, message_id = _extract_chat_and_message_ids(
        _make_message_update(update_id=1, chat_id=-555, message_id=99)
    )
    assert chat_id == -555
    assert message_id == 99


# ─── callback_query coverage (issue #86) ──────────────────────────────────────


def _make_callback_query_update(
    update_id: int | None = None,
    text: str | None = "tap",
    data: str = "btn:1",
    chat_id: int = -1_001_234_567_890,
    message_id: int = 55,
    user_id: int = 12345,
    inaccessible: bool = False,
):
    """Build an aiogram ``Update`` carrying a CallbackQuery with an embedded message
    snapshot — the shape inline-keyboard presses arrive in."""
    from aiogram.types import (
        CallbackQuery,
        Chat,
        InaccessibleMessage,
        Message,
        Update,
        User,
    )

    when = datetime.now(timezone.utc)
    chat = Chat(id=chat_id, type="supergroup", title="dev")
    user = User(id=user_id, is_bot=False, first_name="Probe")
    if inaccessible:
        msg = InaccessibleMessage(chat=chat, message_id=message_id)
    elif text is not None:
        msg = Message(message_id=message_id, date=when, chat=chat, from_user=user, text=text)
    else:
        msg = None
    cb = CallbackQuery(
        id="cb-1",
        from_user=user,
        chat_instance="ci",
        message=msg,
        inline_message_id="imid" if msg is None else None,
        data=data,
    )
    return Update(
        update_id=update_id if update_id is not None else _next_update_id(),
        callback_query=cb,
    )


def test_extract_text_and_caption_from_callback_query(app_env) -> None:
    """Issue #86: policy detection must see the text inside ``callback_query.message`` —
    the snapshot Telegram embeds on every inline-button press."""
    from bot.services.ingestion import _extract_text_and_caption

    update = _make_callback_query_update(update_id=1, text="hi #offrecord")
    assert _extract_text_and_caption(update) == ("hi #offrecord", None)


def test_extract_text_and_caption_callback_query_without_message(app_env) -> None:
    """Inline-mode callbacks (``inline_message_id`` only) and inaccessible messages
    carry no readable text — extraction returns (None, None) instead of crashing."""
    from bot.services.ingestion import _extract_text_and_caption

    assert _extract_text_and_caption(
        _make_callback_query_update(update_id=2, text=None)
    ) == (None, None)
    assert _extract_text_and_caption(
        _make_callback_query_update(update_id=3, inaccessible=True)
    ) == (None, None)


async def test_record_update_offrecord_callback_query_redacted(db_session, monkeypatch) -> None:
    """Issue #86: a ``callback_query`` whose embedded message carries ``#offrecord``
    must have the whole message snapshot scrubbed before ``raw_json`` is persisted.

    Uses a spy detector emulating the real #offrecord detector so the live ordering
    rule (detect → redact → insert) is exercised end-to-end through record_update.
    """
    from bot.db.repos.feature_flag import FeatureFlagRepo
    from bot.services import ingestion

    await FeatureFlagRepo.set_enabled(db_session, ingestion.RAW_ARCHIVE_FLAG, enabled=True)

    calls: list[tuple[str | None, str | None]] = []

    def _spy_detect(text, caption):
        calls.append((text, caption))
        if (text and "#offrecord" in text) or (caption and "#offrecord" in caption):
            return ("offrecord", {"marker": "#offrecord"})
        return ("normal", None)

    monkeypatch.setattr(ingestion, "detect_policy", _spy_detect)

    update = _make_callback_query_update(update_id=_next_update_id(), text="tap #offrecord")
    row = await ingestion.record_update(db_session, update)

    assert row is not None
    assert calls == [("tap #offrecord", None)]
    assert row.is_redacted is True
    assert row.redaction_reason == "offrecord"
    assert "#offrecord" not in json.dumps(row.raw_json)
    msg = row.raw_json["callback_query"]["message"]
    assert "text" not in msg
    assert "entities" not in msg
    # Structural fields survive the redaction:
    assert msg["message_id"] == 55
    assert row.raw_json["callback_query"]["data"] == "btn:1"


async def test_record_update_plain_callback_query_unchanged(db_session) -> None:
    """A normal ``callback_query`` archives verbatim — the fix must not change the
    default path (Phase 13 contract: no token → normal memory, full snapshot kept)."""
    from bot.db.repos.feature_flag import FeatureFlagRepo
    from bot.services.ingestion import RAW_ARCHIVE_FLAG, record_update

    await FeatureFlagRepo.set_enabled(db_session, RAW_ARCHIVE_FLAG, enabled=True)

    update = _make_callback_query_update(update_id=_next_update_id(), text="plain tap")
    row = await record_update(db_session, update)

    assert row is not None
    assert row.update_type == "callback_query"
    assert row.is_redacted is False
    assert row.redaction_reason is None
    assert row.raw_json["callback_query"]["message"]["text"] == "plain tap"
    assert row.raw_json["callback_query"]["data"] == "btn:1"
    assert row.chat_id == -1_001_234_567_890
    assert row.message_id == 55
