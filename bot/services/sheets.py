from __future__ import annotations

import asyncio
import logging
import threading
import time

import gspread
from google.auth.exceptions import GoogleAuthError
from google.oauth2.service_account import Credentials

from bot.config import settings
from bot.services.intro_contract import get_intro_catalog

logger = logging.getLogger(__name__)


class SheetProjectionError(RuntimeError):
    """Expected failure while writing the non-canonical Google Sheet projection."""


SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_CLIENT_TTL_SECONDS = 300
_CATALOG = get_intro_catalog("intro-v2")
HEADERS = [
    "Telegram ID",
    "Username",
    *(field.sheet_header for field in _CATALOG),
    "Кто поручился",
    "Статус",
]

_client: gspread.Client | None = None
_client_ts = 0.0
_client_lock = threading.Lock()


def _is_configured() -> bool:
    return bool(settings.GOOGLE_SHEETS_CREDS_FILE and settings.GOOGLE_SHEET_ID)


def _get_client() -> gspread.Client | None:
    """Return a TTL-cached client, or ``None`` when Sheets is not configured."""
    global _client, _client_ts

    now = time.monotonic()
    with _client_lock:
        if not _is_configured():
            _client = None
            _client_ts = now
            return None
        if _client is not None and now - _client_ts <= _CLIENT_TTL_SECONDS:
            return _client
        credentials = Credentials.from_service_account_file(
            settings.GOOGLE_SHEETS_CREDS_FILE, scopes=SCOPES
        )
        _client = gspread.authorize(credentials)
        _client_ts = now
        return _client


def _get_sheet() -> gspread.Worksheet | None:
    """Return the projection worksheet, or ``None`` when Sheets is not configured."""
    client = _get_client()
    if client is None:
        return None
    worksheet = client.open_by_key(settings.GOOGLE_SHEET_ID).sheet1
    if worksheet.row_values(1) != HEADERS:
        worksheet.update([HEADERS], "A1")
    return worksheet


def _find_row_by_telegram_id(worksheet: gspread.Worksheet, user_id: int) -> int | None:
    """Return the 1-based row containing ``user_id``, if it exists."""
    try:
        return worksheet.find(str(user_id), in_column=1).row
    except gspread.exceptions.CellNotFound:
        return None


def _project_row(
    worksheet: gspread.Worksheet,
    *,
    user_id: int,
    username: str | None,
    vouched_by: str,
    answers_by_field_id: dict[str, str],
) -> None:
    expected_field_ids = {field.field_id for field in _CATALOG}
    if set(answers_by_field_id) != expected_field_ids:
        raise SheetProjectionError("Answers must contain every catalog field exactly once")
    row = [
        str(user_id),
        username or "",
        *(answers_by_field_id[field.field_id] for field in _CATALOG),
        vouched_by,
        "есть интро",
    ]
    row_number = _find_row_by_telegram_id(worksheet, user_id)
    if row_number is None:
        worksheet.append_row(row, value_input_option="RAW")
        return
    end_column = gspread.utils.rowcol_to_a1(1, len(HEADERS)).rstrip("1")
    worksheet.update([row], f"A{row_number}:{end_column}{row_number}", raw=True)


async def project_intro_to_sheet(
    *,
    user_id: int,
    application_id: int,
    username: str | None,
    vouched_by: str,
    answers_by_field_id: dict[str, str],
) -> None:
    """Project one published application to Google Sheets without touching canonical data."""
    if not _is_configured():
        logger.debug("Google Sheets is not configured — skipping intro projection")
        return

    def write_projection() -> None:
        worksheet = _get_sheet()
        if worksheet is None:
            return
        _project_row(
            worksheet,
            user_id=user_id,
            username=username,
            vouched_by=vouched_by,
            answers_by_field_id=answers_by_field_id,
        )

    try:
        await asyncio.to_thread(write_projection)
    except (gspread.exceptions.GSpreadException, GoogleAuthError, OSError) as error:
        raise SheetProjectionError("Google Sheet projection failed") from error
