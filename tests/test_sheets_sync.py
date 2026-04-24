"""Tests for Google Sheets sync (bot.services.sheets).

No pytest-asyncio available — all async code runs via asyncio.run().
gspread is mocked at the module level via monkeypatch.setattr so no
real credentials or network calls are made.

Three scenarios:
- Status column write: worksheet.update called with correct range.
- New-user row insert: worksheet.append_row called for new intro.
- Existing-user row update: worksheet.update called for existing row.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.db.models import Base


def _run(coro):
    return asyncio.run(coro)


def _make_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())
    return factory, engine


def _make_worksheet_mock(existing_row: int | None = None, all_values: list | None = None):
    """Build a MagicMock that behaves like a gspread Worksheet.

    gspread 6.x: find() returns None when not found (no exception raised).
    """
    ws = MagicMock()

    # find() returns a cell with .row, or None when not found
    if existing_row is not None:
        cell_mock = MagicMock()
        cell_mock.row = existing_row
        ws.find.return_value = cell_mock
    else:
        ws.find.return_value = None  # gspread 6.x: returns None, not exception

    ws.row_values.return_value = []  # Empty row 1 → headers not written yet
    ws.update = MagicMock()
    ws.append_row = MagicMock()
    ws.get_all_values.return_value = all_values or []
    return ws


class TestStatusColumnWrite:
    """worksheet.update is called with the correct range for status update."""

    def test_existing_row_uses_update(self, app_env, monkeypatch):
        """_sync_row_to_sheet calls worksheet.update when row exists."""
        # Import the function under test
        from bot.services import sheets as sheets_mod

        ws = _make_worksheet_mock(existing_row=3)

        # Call the sync function directly
        row_num = sheets_mod._sync_row_to_sheet(
            ws,
            telegram_id=12345,
            username="@tester",
            answers_by_index={0: "Name", 1: "City"},
            vouched_by="@voucher",
            status="есть интро",
        )

        ws.update.assert_called_once()
        call_args = ws.update.call_args
        # First positional arg: row data list; second: range string like 'A3:K3'
        cell_range = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("range_name", "")
        assert "3" in str(cell_range)
        assert row_num == 3

    def test_status_column_value_in_row(self, app_env):
        """Row data written to sheet includes the status value at index 10."""
        from bot.services import sheets as sheets_mod

        ws = _make_worksheet_mock(existing_row=2)

        sheets_mod._sync_row_to_sheet(
            ws,
            telegram_id=99999,
            username="@someone",
            answers_by_index={},
            vouched_by="@vch",
            status="CUSTOM_STATUS",
        )

        call_args = ws.update.call_args
        # First arg is [[row_data]]
        row_data = call_args.args[0][0]
        assert row_data[10] == "CUSTOM_STATUS"


class TestNewUserRowInsert:
    """worksheet.append_row is called for a user not yet in the sheet."""

    def test_new_user_appends_row(self, app_env):
        """_sync_row_to_sheet calls append_row when no existing row found.

        gspread 6.x: find() returns None when not found (no exception).
        """
        from bot.services import sheets as sheets_mod

        ws = MagicMock()
        ws.find.return_value = None  # Not found
        ws.append_row = MagicMock()
        ws.update = MagicMock()
        ws.get_all_values.return_value = [
            ["Telegram ID", "Username"],  # header row
            ["99998", "@existing"],       # 1 existing row
        ]

        sheets_mod._sync_row_to_sheet(
            ws,
            telegram_id=11111,
            username="@newuser",
            answers_by_index={0: "NewName"},
            vouched_by="@vch",
            status="",
        )

        ws.append_row.assert_called_once()
        call_args = ws.append_row.call_args
        row_data = call_args.args[0]
        assert str(11111) in row_data
        assert "@newuser" in row_data

    def test_new_user_telegram_id_in_first_column(self, app_env):
        """New row data has Telegram ID at index 0."""
        from bot.services import sheets as sheets_mod

        ws = MagicMock()
        ws.find.return_value = None  # Not found
        ws.append_row = MagicMock()
        ws.get_all_values.return_value = [["hdr"]]

        sheets_mod._sync_row_to_sheet(
            ws,
            telegram_id=77777,
            username="@test",
            answers_by_index={},
            vouched_by="@vch",
        )

        row_data = ws.append_row.call_args.args[0]
        assert row_data[0] == "77777"


class TestExistingUserRowUpdate:
    """worksheet.update is called with correct data for an existing user."""

    def test_existing_user_updates_correct_row(self, app_env):
        """Row number in the update call matches the cell found by find()."""
        from bot.services import sheets as sheets_mod

        ws = _make_worksheet_mock(existing_row=5)

        sheets_mod._sync_row_to_sheet(
            ws,
            telegram_id=55555,
            username="@existing",
            answers_by_index={0: "UpdatedName", 3: "NewExp"},
            vouched_by="@vch",
            status="active",
        )

        ws.update.assert_called_once()
        # Range should contain row 5
        call_args = ws.update.call_args
        range_arg = call_args.args[1] if len(call_args.args) > 1 else ""
        assert "5" in str(range_arg)

    def test_existing_user_answers_in_correct_columns(self, app_env):
        """Answer columns (2-8) in the update data match _Q_INDEX_TO_COL mapping."""
        from bot.services.sheets import _Q_INDEX_TO_COL
        from bot.services import sheets as sheets_mod

        ws = _make_worksheet_mock(existing_row=4)

        sheets_mod._sync_row_to_sheet(
            ws,
            telegram_id=44444,
            username="@col_test",
            answers_by_index={0: "NameVal", 1: "CityVal", 6: "GoalVal"},
            vouched_by="@vch",
        )

        call_args = ws.update.call_args
        row_data = call_args.args[0][0]
        # Check mapping: q_index 0 → col 2, q_index 1 → col 3, q_index 6 → col 8
        assert row_data[_Q_INDEX_TO_COL[0]] == "NameVal"
        assert row_data[_Q_INDEX_TO_COL[1]] == "CityVal"
        assert row_data[_Q_INDEX_TO_COL[6]] == "GoalVal"
