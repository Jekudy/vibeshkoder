from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from functools import lru_cache

import gspread
from google.oauth2.service_account import Credentials
from sqlalchemy import select

from bot.config import settings
from bot.db.engine import async_session
from bot.db.models import Intro, QuestionnaireAnswer, User
from bot.html_escape import html_escape

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

HEADERS = [
    "Telegram ID",
    "Username",
    "Имя",
    "Локация",
    "Откуда узнал",
    "Опыт",
    "Проекты",
    "Самое сложное",
    "Цели",
    "Кто поручился",
    "Статус",
]

# Maps question_index (0-6) to sheet column index (0-based within HEADERS).
# question 0 = Имя (col 2), question 1 = Локация (col 3), etc.
_Q_INDEX_TO_COL = {
    0: 2,   # Имя
    1: 3,   # Локация
    2: 4,   # Откуда узнал
    3: 5,   # Опыт
    4: 6,   # Проекты
    5: 7,   # Самое сложное
    6: 8,   # Цели
}


def _is_configured() -> bool:
    return bool(settings.GOOGLE_SHEETS_CREDS_FILE and settings.GOOGLE_SHEET_ID)


@lru_cache(maxsize=1)
def _get_client() -> gspread.Client | None:
    """Return a cached gspread client, or None if credentials are not configured."""
    if not _is_configured():
        logger.debug("Google Sheets credentials not configured — skipping.")
        return None
    try:
        creds = Credentials.from_service_account_file(
            settings.GOOGLE_SHEETS_CREDS_FILE, scopes=SCOPES
        )
        return gspread.authorize(creds)
    except Exception:
        logger.exception("Failed to create Google Sheets client")
        return None


def _get_sheet() -> gspread.Worksheet | None:
    """Return the first worksheet, ensuring headers exist."""
    client = _get_client()
    if client is None:
        return None
    try:
        spreadsheet = client.open_by_key(settings.GOOGLE_SHEET_ID)
        worksheet = spreadsheet.sheet1
        # Ensure headers are present in row 1
        existing = worksheet.row_values(1)
        if existing != HEADERS:
            worksheet.update([HEADERS], "A1")
        return worksheet
    except Exception:
        logger.exception("Failed to open Google Sheet")
        return None


def _find_row_by_telegram_id(
    worksheet: gspread.Worksheet, telegram_id: int
) -> int | None:
    """Return 1-based row number for the given Telegram ID, or None."""
    try:
        cell = worksheet.find(str(telegram_id), in_column=1)
        if cell is not None:
            return cell.row
    except gspread.exceptions.CellNotFound:
        pass
    except Exception:
        logger.exception("Error searching for Telegram ID %s in sheet", telegram_id)
    return None


def _sync_row_to_sheet(
    worksheet: gspread.Worksheet,
    telegram_id: int,
    username: str,
    answers_by_index: dict[int, str],
    vouched_by: str,
    status: str = "",
) -> int:
    """Write or update a row in the sheet. Returns the 1-based row number."""
    row_num = _find_row_by_telegram_id(worksheet, telegram_id)

    row_data = [""] * len(HEADERS)
    row_data[0] = str(telegram_id)
    row_data[1] = username
    for q_idx, col_idx in _Q_INDEX_TO_COL.items():
        row_data[col_idx] = answers_by_index.get(q_idx, "")
    row_data[9] = vouched_by  # Кто поручился
    row_data[10] = status      # Статус

    if row_num is not None:
        # Update existing row
        cell_range = f"A{row_num}:{gspread.utils.rowcol_to_a1(row_num, len(HEADERS)).split('!')[0]}"
        # Use row_num to build proper range
        end_col = chr(ord("A") + len(HEADERS) - 1)  # 'K' for 11 columns
        cell_range = f"A{row_num}:{end_col}{row_num}"
        worksheet.update([row_data], cell_range)
    else:
        # Append new row
        worksheet.append_row(row_data, value_input_option="USER_ENTERED")
        # Find the newly appended row number
        row_num = len(worksheet.get_all_values())

    return row_num


def _row_content_hash(row: list[str]) -> str:
    """Compute a hash of the answer columns (indices 2-8) for change detection.

    Non-cryptographic — only used to compare sheet row content against local DB
    state. SHA-256 chosen over MD5 to satisfy the security gate; collision
    strength is not actually relevant here.
    """
    parts = "|".join(row[2:9] if len(row) > 8 else [])
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()


# ── Async public API ─────────────────────────────────────────────────


async def sync_intro_to_sheet(
    user_id: int,
    answers_by_index: dict[int, str],
    username: str,
    vouched_by: str,
) -> None:
    """Push a single intro to Google Sheets (called after intro is saved)."""
    if not _is_configured():
        logger.debug("Sheets not configured — skipping sync_intro_to_sheet")
        return

    def _do_sync() -> int | None:
        worksheet = _get_sheet()
        if worksheet is None:
            return None
        return _sync_row_to_sheet(
            worksheet,
            telegram_id=user_id,
            username=username,
            answers_by_index=answers_by_index,
            vouched_by=vouched_by,
            status="есть интро",
        )

    try:
        row_num = await asyncio.to_thread(_do_sync)
        if row_num is not None:
            # Store sheets_row_number back to DB
            async with async_session() as session:
                result = await session.execute(
                    select(Intro).where(Intro.user_id == user_id)
                )
                intro = result.scalar_one_or_none()
                if intro is not None:
                    intro.sheets_row_number = row_num
                    intro.last_synced_at = datetime.now(timezone.utc)
                    await session.commit()
            logger.info("Synced intro for user %s to sheet row %s", user_id, row_num)
    except Exception:
        logger.exception("Failed to sync intro to sheet for user %s", user_id)


async def sync_all_from_sheet() -> None:
    """Pull changes from sheet into local DB (sheet is source of truth)."""
    if not _is_configured():
        logger.debug("Sheets not configured — skipping sync_all_from_sheet")
        return

    def _read_all_rows() -> list[list[str]]:
        worksheet = _get_sheet()
        if worksheet is None:
            return []
        return worksheet.get_all_values()

    try:
        all_rows = await asyncio.to_thread(_read_all_rows)
    except Exception:
        logger.exception("Failed to read rows from Google Sheet")
        return

    if len(all_rows) <= 1:
        # Only headers or empty
        return

    data_rows = all_rows[1:]  # skip header

    async with async_session() as session:
        for row_idx, row in enumerate(data_rows, start=2):  # row 2 is first data row
            if len(row) < 1 or not row[0].strip():
                continue

            try:
                telegram_id = int(row[0].strip())
            except ValueError:
                logger.warning("Invalid Telegram ID in sheet row %d: %s", row_idx, row[0])
                continue

            # Pad row to full width
            while len(row) < len(HEADERS):
                row.append("")

            sheet_hash = _row_content_hash(row)

            # Look up the local intro
            result = await session.execute(
                select(Intro).where(Intro.user_id == telegram_id)
            )
            intro = result.scalar_one_or_none()

            if intro is None:
                # No local intro for this sheet row — skip (we don't create intros
                # from the sheet alone; they must go through the questionnaire).
                continue

            # Build local hash from current answers for comparison
            qa_result = await session.execute(
                select(QuestionnaireAnswer).where(
                    QuestionnaireAnswer.user_id == telegram_id,
                    QuestionnaireAnswer.is_current.is_(True),
                )
            )
            local_answers = {
                qa.question_index: qa.answer_text
                for qa in qa_result.scalars().all()
            }
            local_row_values = [""] * 7
            for q_idx in range(7):
                local_row_values[q_idx] = local_answers.get(q_idx, "")
            local_hash = hashlib.sha256("|".join(local_row_values).encode("utf-8")).hexdigest()

            if sheet_hash == local_hash:
                # No changes — just update row number
                intro.sheets_row_number = row_idx
                continue

            # Sheet has different content — update local DB
            logger.info(
                "Sheet row %d differs from local for user %s — updating DB",
                row_idx,
                telegram_id,
            )

            # Update individual QuestionnaireAnswer records
            for q_idx, col_idx in _Q_INDEX_TO_COL.items():
                new_value = row[col_idx].strip()
                if q_idx in local_answers and local_answers[q_idx] != new_value:
                    qa_update_result = await session.execute(
                        select(QuestionnaireAnswer).where(
                            QuestionnaireAnswer.user_id == telegram_id,
                            QuestionnaireAnswer.question_index == q_idx,
                            QuestionnaireAnswer.is_current.is_(True),
                        )
                    )
                    qa_record = qa_update_result.scalar_one_or_none()
                    if qa_record is not None:
                        qa_record.answer_text = new_value

            # Rebuild intro_text from the sheet values
            from bot.texts import INTRO_TEMPLATE

            intro.intro_text = INTRO_TEMPLATE.format(
                name=html_escape(row[2].strip()) or "—",
                location=html_escape(row[3].strip()) or "—",
                source=html_escape(row[4].strip()) or "—",
                experience=html_escape(row[5].strip()) or "—",
                projects=html_escape(row[6].strip()) or "—",
                hardest=html_escape(row[7].strip()) or "—",
                goals=html_escape(row[8].strip()) or "—",
            )

            # Update vouched_by if changed
            if row[9].strip() and row[9].strip() != intro.vouched_by_name:
                intro.vouched_by_name = row[9].strip()

            intro.sheets_row_number = row_idx
            intro.last_synced_at = datetime.now(timezone.utc)

        await session.commit()

    logger.debug("sync_all_from_sheet completed — processed %d data rows", len(data_rows))


async def full_sync() -> None:
    """Full bi-directional sync, called by the scheduler every 5 min.

    1. Push local intros that don't have sheets_row_number yet.
    2. Pull changes from sheet (sheet is source of truth).
    3. Update status column for all members.
    """
    if not _is_configured():
        logger.debug("Sheets not configured — skipping full_sync")
        return

    logger.debug("Starting full Google Sheets sync")

    # ── Step 1: Push local intros missing from sheet ─────────────────
    async with async_session() as session:
        result = await session.execute(
            select(Intro, User)
            .join(User, Intro.user_id == User.id)
            .where(Intro.sheets_row_number.is_(None))
        )
        rows_to_push = result.all()

    for intro, user in rows_to_push:
        # Fetch current answers
        async with async_session() as session:
            qa_result = await session.execute(
                select(QuestionnaireAnswer).where(
                    QuestionnaireAnswer.user_id == intro.user_id,
                    QuestionnaireAnswer.is_current.is_(True),
                )
            )
            answers_by_index = {
                qa.question_index: qa.answer_text
                for qa in qa_result.scalars().all()
            }

        username = f"@{user.username}" if user.username else ""
        await sync_intro_to_sheet(
            user_id=intro.user_id,
            answers_by_index=answers_by_index,
            username=username,
            vouched_by=intro.vouched_by_name,
        )

    # ── Step 2: Pull changes from sheet ──────────────────────────────
    await sync_all_from_sheet()

    # ── Step 3: Update status column in sheet for all members ────────
    await _update_status_column()

    logger.debug("Full Google Sheets sync completed")


async def _update_status_column() -> None:
    """Update the Status column for every row based on the User.is_member flag."""
    if not _is_configured():
        return

    def _do_update() -> list[tuple[int, int]]:
        worksheet = _get_sheet()
        if worksheet is None:
            return []

        all_rows = worksheet.get_all_values()
        if len(all_rows) <= 1:
            return []

        # Collect telegram IDs and their row numbers
        updates: list[tuple[int, int]] = []  # (row_num, telegram_id)
        for row_idx, row in enumerate(all_rows[1:], start=2):
            if not row or not row[0].strip():
                continue
            try:
                tid = int(row[0].strip())
                updates.append((row_idx, tid))
            except ValueError:
                continue

        return updates

    try:
        updates = await asyncio.to_thread(_do_update)
    except Exception:
        logger.exception("Failed to read sheet for status update")
        return

    if not updates:
        return

    # Look up membership status and intro existence for all users
    telegram_ids = [tid for _, tid in updates]
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.id.in_(telegram_ids))
        )
        _ = {u.id: u for u in result.scalars().all()}

        intro_result = await session.execute(
            select(Intro.user_id).where(Intro.user_id.in_(telegram_ids))
        )
        users_with_intro = {row[0] for row in intro_result.all()}

    # Build batch of status updates
    status_cells: list[tuple[int, str]] = []
    for row_num, tid in updates:
        has_intro = tid in users_with_intro
        status = "есть интро" if has_intro else "нет интро"
        status_cells.append((row_num, status))

    def _write_statuses() -> None:
        worksheet = _get_sheet()
        if worksheet is None:
            return
        # Status is column 11 (K)
        batch = []
        for row_num, status in status_cells:
            batch.append({"range": f"K{row_num}", "values": [[status]]})
        if batch:
            worksheet.batch_update(batch)

    try:
        await asyncio.to_thread(_write_statuses)
        logger.debug("Updated status column for %d rows", len(status_cells))
    except Exception:
        logger.exception("Failed to update status column in sheet")
