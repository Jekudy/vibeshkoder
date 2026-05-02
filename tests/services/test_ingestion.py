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
