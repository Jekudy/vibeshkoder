"""T6-02 ``/admin/extract`` handler + window parser tests.

PHASE6_PLAN.md §7 T6-02 acceptance criteria:

* Admin-only Telegram handler ``/admin/extract --window <ISO8601>..<ISO8601>``.
* Window parser validates ISO8601, non-empty range, ≤ 30 days length.
* Calls ``run_extraction_pass`` directly, bypassing the scheduler flag.
* Records ExtractionRun with operator user_id (logged audit marker).
* Returns summary message: candidates emitted, run_status,
  llm_usage_ledger entry id.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.usefixtures("app_env")


# ─── Window parser tests ─────────────────────────────────────────────────────


def test_parse_extract_window_valid_iso8601_range() -> None:
    """ISO8601 ``<start>..<end>`` parses to a (start, end) tuple of
    timezone-aware datetimes."""
    from bot.handlers.admin_extract import parse_extract_window

    raw = "2026-05-12T00:00:00+00:00..2026-05-13T00:00:00+00:00"
    start, end = parse_extract_window(raw)
    assert start == datetime(2026, 5, 12, tzinfo=timezone.utc)
    assert end == datetime(2026, 5, 13, tzinfo=timezone.utc)


def test_parse_extract_window_iso8601_with_z_suffix() -> None:
    """``Z`` suffix is normalized to ``+00:00`` UTC."""
    from bot.handlers.admin_extract import parse_extract_window

    raw = "2026-05-12T00:00:00Z..2026-05-13T00:00:00Z"
    start, end = parse_extract_window(raw)
    assert start.tzinfo is not None
    assert end.tzinfo is not None


def test_parse_extract_window_rejects_invalid_iso8601() -> None:
    from bot.handlers.admin_extract import (
        WindowParseError,
        parse_extract_window,
    )

    with pytest.raises(WindowParseError):
        parse_extract_window("not-a-date..2026-05-13T00:00:00Z")
    with pytest.raises(WindowParseError):
        parse_extract_window("2026-05-12T00:00:00Z..bad")
    with pytest.raises(WindowParseError):
        parse_extract_window("just-one-date")


def test_parse_extract_window_rejects_empty_range() -> None:
    """Window where ``end <= start`` is rejected."""
    from bot.handlers.admin_extract import (
        WindowParseError,
        parse_extract_window,
    )

    # Exactly equal — empty range.
    with pytest.raises(WindowParseError):
        parse_extract_window(
            "2026-05-12T00:00:00Z..2026-05-12T00:00:00Z"
        )
    # End < start — inverted.
    with pytest.raises(WindowParseError):
        parse_extract_window(
            "2026-05-13T00:00:00Z..2026-05-12T00:00:00Z"
        )


def test_parse_extract_window_rejects_over_30_days() -> None:
    """LLM budget guard: max window length is 30 days."""
    from bot.handlers.admin_extract import (
        WindowParseError,
        parse_extract_window,
    )

    with pytest.raises(WindowParseError):
        parse_extract_window(
            "2026-01-01T00:00:00Z..2026-02-15T00:00:00Z"
        )


def test_parse_extract_window_accepts_exactly_30_days() -> None:
    """Boundary case: exactly 30 days is accepted."""
    from bot.handlers.admin_extract import parse_extract_window

    start, end = parse_extract_window(
        "2026-01-01T00:00:00Z..2026-01-31T00:00:00Z"
    )
    assert (end - start).days == 30


# ─── /admin/extract handler tests ────────────────────────────────────────────


@pytest.fixture
def fake_admin_message() -> MagicMock:
    """Construct a Telegram Message mock with admin sender + private chat."""
    msg = MagicMock()
    msg.from_user = MagicMock()
    msg.from_user.id = 149820031  # match ADMIN_IDS from conftest.app_env
    msg.from_user.username = "admin_user"
    msg.from_user.first_name = "Admin"
    msg.chat = MagicMock()
    msg.chat.type = "private"
    msg.chat.id = 149820031  # private chat = user id
    msg.answer = AsyncMock()
    msg.reply = AsyncMock()
    return msg


@pytest.fixture
def fake_nonadmin_message() -> MagicMock:
    msg = MagicMock()
    msg.from_user = MagicMock()
    msg.from_user.id = 99999  # NOT in ADMIN_IDS
    msg.from_user.username = "regular_user"
    msg.from_user.first_name = "Reg"
    msg.chat = MagicMock()
    msg.chat.type = "private"
    msg.chat.id = 99999
    msg.answer = AsyncMock()
    msg.reply = AsyncMock()
    return msg


@pytest.fixture
def fake_command_object_factory():
    def _make(args: str | None) -> MagicMock:
        cmd = MagicMock()
        cmd.args = args
        return cmd
    return _make


async def test_admin_extract_rejects_non_admin(
    db_session,
    fake_nonadmin_message,
    fake_command_object_factory,
) -> None:
    """Non-admin senders MUST NOT trigger the pass; handler must return
    without invoking ``run_extraction_pass``."""
    import bot.handlers.admin_extract as h_module

    called = {"count": 0}

    async def fake_run_pass(*args, **kwargs):
        called["count"] += 1
        raise AssertionError("run_extraction_pass MUST NOT be called for non-admin")

    h_module.run_extraction_pass = fake_run_pass  # type: ignore[attr-defined]
    cmd = fake_command_object_factory(
        "--window 2026-05-12T00:00:00Z..2026-05-13T00:00:00Z"
    )

    await h_module.cmd_admin_extract(
        fake_nonadmin_message,
        cmd,
        session=db_session,
        gateway=MagicMock(),
    )

    # Either no-op or explicit error — either way, no pass invocation.
    assert called["count"] == 0


async def test_admin_extract_calls_run_extraction_pass_with_window(
    db_session,
    fake_admin_message,
    fake_command_object_factory,
) -> None:
    """Admin invocation parses the window and calls
    ``run_extraction_pass`` directly (bypassing the scheduler flag)."""
    import bot.handlers.admin_extract as h_module
    from bot.services.extractor import ExtractionResult

    captured: dict = {}

    async def fake_run_pass(session, *, window_start, window_end, gateway, **kwargs):
        captured["window_start"] = window_start
        captured["window_end"] = window_end
        captured["operator_user_id"] = kwargs.get("operator_user_id")
        import uuid

        return ExtractionResult(
            extraction_run_id=uuid.uuid4(),
            run_status="completed",
            candidate_count=3,
            llm_usage_ledger_id=42,
        )

    h_module.run_extraction_pass = fake_run_pass  # type: ignore[attr-defined]

    cmd = fake_command_object_factory(
        "--window 2026-05-12T00:00:00Z..2026-05-13T00:00:00Z"
    )

    await h_module.cmd_admin_extract(
        fake_admin_message,
        cmd,
        session=db_session,
        gateway=MagicMock(),
    )

    assert "window_start" in captured
    assert captured["window_start"] == datetime(2026, 5, 12, tzinfo=timezone.utc)
    assert captured["window_end"] == datetime(2026, 5, 13, tzinfo=timezone.utc)
    # Operator user id is recorded as audit marker.
    assert captured["operator_user_id"] == 149820031

    # Admin received a summary reply mentioning candidate_count + run_status
    # + llm_usage_ledger entry id.
    assert fake_admin_message.answer.await_count + fake_admin_message.reply.await_count >= 1


async def test_admin_extract_returns_summary_with_run_metadata(
    db_session,
    fake_admin_message,
    fake_command_object_factory,
) -> None:
    """The admin reply must mention ``candidate_count``, ``run_status``, and
    ``llm_usage_ledger_id`` from the ``ExtractionResult``."""
    import bot.handlers.admin_extract as h_module
    from bot.services.extractor import ExtractionResult

    async def fake_run_pass(session, *, window_start, window_end, gateway, **kwargs):
        import uuid

        return ExtractionResult(
            extraction_run_id=uuid.uuid4(),
            run_status="completed",
            candidate_count=7,
            llm_usage_ledger_id=123,
        )

    h_module.run_extraction_pass = fake_run_pass  # type: ignore[attr-defined]
    cmd = fake_command_object_factory(
        "--window 2026-05-12T00:00:00Z..2026-05-13T00:00:00Z"
    )

    await h_module.cmd_admin_extract(
        fake_admin_message,
        cmd,
        session=db_session,
        gateway=MagicMock(),
    )

    # Inspect every call to .answer / .reply and confirm summary content.
    call_texts: list[str] = []
    for call in fake_admin_message.answer.await_args_list:
        if call.args:
            call_texts.append(str(call.args[0]))
    for call in fake_admin_message.reply.await_args_list:
        if call.args:
            call_texts.append(str(call.args[0]))
    summary = "\n".join(call_texts)
    assert "7" in summary  # candidate_count
    assert "completed" in summary  # run_status
    assert "123" in summary  # llm_usage_ledger_id


async def test_admin_extract_rejects_missing_window(
    db_session,
    fake_admin_message,
    fake_command_object_factory,
) -> None:
    """``/admin/extract`` without ``--window`` must reject and NOT run."""
    import bot.handlers.admin_extract as h_module

    called = {"count": 0}

    async def fake_run_pass(*args, **kwargs):
        called["count"] += 1
        return None

    h_module.run_extraction_pass = fake_run_pass  # type: ignore[attr-defined]

    cmd = fake_command_object_factory(None)  # no args
    await h_module.cmd_admin_extract(
        fake_admin_message,
        cmd,
        session=db_session,
        gateway=MagicMock(),
    )
    assert called["count"] == 0


async def test_admin_extract_rejects_invalid_window_format(
    db_session,
    fake_admin_message,
    fake_command_object_factory,
) -> None:
    """Bad ISO8601 → admin receives an error message, pass NOT called."""
    import bot.handlers.admin_extract as h_module

    called = {"count": 0}

    async def fake_run_pass(*args, **kwargs):
        called["count"] += 1
        return None

    h_module.run_extraction_pass = fake_run_pass  # type: ignore[attr-defined]

    cmd = fake_command_object_factory("--window not-a-date..also-not")
    await h_module.cmd_admin_extract(
        fake_admin_message,
        cmd,
        session=db_session,
        gateway=MagicMock(),
    )
    assert called["count"] == 0
    # Admin received an error reply.
    assert (
        fake_admin_message.answer.await_count + fake_admin_message.reply.await_count
    ) >= 1


async def test_admin_extract_rejects_window_over_30_days(
    db_session,
    fake_admin_message,
    fake_command_object_factory,
) -> None:
    """LLM budget guard: > 30 day windows rejected."""
    import bot.handlers.admin_extract as h_module

    called = {"count": 0}

    async def fake_run_pass(*args, **kwargs):
        called["count"] += 1
        return None

    h_module.run_extraction_pass = fake_run_pass  # type: ignore[attr-defined]

    cmd = fake_command_object_factory(
        "--window 2026-01-01T00:00:00Z..2026-03-01T00:00:00Z"
    )
    await h_module.cmd_admin_extract(
        fake_admin_message,
        cmd,
        session=db_session,
        gateway=MagicMock(),
    )
    assert called["count"] == 0
