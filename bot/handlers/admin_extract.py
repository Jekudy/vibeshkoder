"""Admin Telegram handler ``/admin/extract`` (T6-02).

PHASE6_PLAN.md §7 T6-02 acceptance criteria:

* Admin-only handler invoked via ``/admin/extract --window
  <ISO8601-start>..<ISO8601-end>``.
* Window parser validates ISO8601 + range non-empty + length ≤ 30 days
  (LLM budget guard).
* Calls ``run_extraction_pass`` directly, **bypassing the scheduler
  flag** (operator-explicit backfill per Q5).
* Records the ExtractionRun with the operator's user_id as a structured
  audit marker.
* Replies to admin with a summary: ``candidate_count``, ``run_status``,
  ``llm_usage_ledger_id``.

The handler reuses the same admin gating pattern as ``bot/handlers/admin.py``
(``message.from_user.id in settings.ADMIN_IDS``, ``PrivateChatFilter``).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.repos.llm_usage_ledger import LedgerRepo
from bot.filters.chat_type import PrivateChatFilter
from bot.services.extractor import run_extraction_pass
from bot.services.llm_gateway import (
    LiveExtractCandidatesGateway,
    load_gateway_config,
    resolve_provider,
)

logger = logging.getLogger(__name__)

router = Router(name="admin_extract")


MAX_WINDOW_DAYS = 30


class WindowParseError(ValueError):
    """Raised when ``--window <ISO8601>..<ISO8601>`` fails validation."""


def parse_extract_window(raw: str) -> tuple[datetime, datetime]:
    """Parse ``<ISO8601-start>..<ISO8601-end>`` and validate.

    Validation rules (PHASE6_PLAN.md §7 T6-02):

    * Both halves must parse via ``datetime.fromisoformat`` (post-Python
      3.11, this accepts the ``Z`` suffix and full ISO8601 variants).
    * The range must be non-empty: ``end > start``.
    * The length must be ≤ ``MAX_WINDOW_DAYS`` (30 days).

    Returns a ``(start, end)`` tuple of timezone-aware datetimes.

    Raises ``WindowParseError`` on any failure. The error message is
    human-readable so the admin handler can surface it directly.
    """
    if not raw or ".." not in raw:
        raise WindowParseError("expected '<ISO8601-start>..<ISO8601-end>'")
    parts = raw.split("..", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise WindowParseError("missing start or end of window range")

    raw_start, raw_end = parts[0].strip(), parts[1].strip()

    try:
        start = _parse_iso8601(raw_start)
    except ValueError as exc:
        raise WindowParseError(f"invalid ISO8601 start: {raw_start!r}: {exc}") from exc
    try:
        end = _parse_iso8601(raw_end)
    except ValueError as exc:
        raise WindowParseError(f"invalid ISO8601 end: {raw_end!r}: {exc}") from exc

    if end <= start:
        raise WindowParseError(
            f"window range is empty or inverted: end={end.isoformat()} <= start={start.isoformat()}"
        )

    if (end - start) > timedelta(days=MAX_WINDOW_DAYS):
        raise WindowParseError(
            f"window exceeds maximum {MAX_WINDOW_DAYS} days; got "
            f"{(end - start).days} days (LLM budget guard)"
        )

    return start, end


def _parse_iso8601(raw: str) -> datetime:
    """Normalize ``Z`` suffix → ``+00:00`` and call ``fromisoformat``.

    Python 3.11+ ``fromisoformat`` accepts ``Z``; this helper keeps the
    code robust on older minor versions in case the runtime drifts.
    """
    cleaned = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
        # Reject naive datetimes — admin must supply UTC explicitly to
        # avoid timezone ambiguity in the audit trail.
        raise ValueError("datetime missing timezone offset; supply '+00:00' or 'Z'")
    return dt


@router.message(Command("admin_extract"), PrivateChatFilter())
async def cmd_admin_extract(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
) -> None:
    """Admin-only ``/admin_extract --window <start>..<end>`` handler.

    NOTE: The Telegram command word is ``admin_extract`` (underscore) —
    Telegram does not allow slashes in command names, so the
    ``/admin/extract`` syntax from PHASE6_PLAN.md §5.C maps to
    ``/admin_extract --window ...`` at the wire layer. The semantic
    behaviour matches the plan exactly.

    Wiring:

    * Admin guard: ``message.from_user.id in settings.ADMIN_IDS`` (same
      pattern as ``/stats`` and ``/force_refresh`` in
      ``bot/handlers/admin.py``).
    * Private chat only (``PrivateChatFilter``).
    * Window parser → ``run_extraction_pass`` direct invocation
      (scheduler flag is NOT checked here — operator-explicit backfill
      per PHASE6_PLAN.md Q5).
    * **Gateway DI (T6-03)**: the handler constructs
      ``LiveExtractCandidatesGateway`` locally from env-derived config,
      mirroring the Phase 5 QA precedent (``bot/handlers/qa.py:332-343``).
      No aiogram middleware DI is used; the Protocol seam from T6-02
      keeps tests injectable via ``monkeypatch``.
    * Returns summary text with ``candidate_count`` + ``run_status`` +
      ``llm_usage_ledger_id``.
    """
    if message.from_user is None:
        return

    if message.from_user.id not in settings.ADMIN_IDS:
        # Silent no-op (same shape as /stats handler) — non-admin
        # discoverability is intentionally suppressed.
        return

    raw_args = (command.args or "").strip()
    if not raw_args:
        await message.answer(
            "Использование: <code>/admin_extract --window "
            "&lt;ISO8601-start&gt;..&lt;ISO8601-end&gt;</code>",
            parse_mode="HTML",
        )
        return

    # Strip ``--window `` prefix if present (we accept both
    # ``/admin_extract --window X..Y`` and ``/admin_extract X..Y``).
    window_raw = raw_args
    if window_raw.startswith("--window"):
        window_raw = window_raw[len("--window") :].strip()
    if not window_raw:
        await message.answer(
            "Ошибка: ожидалось значение --window. Использование: "
            "<code>/admin_extract --window "
            "&lt;ISO8601-start&gt;..&lt;ISO8601-end&gt;</code>",
            parse_mode="HTML",
        )
        return

    try:
        window_start, window_end = parse_extract_window(window_raw)
    except WindowParseError as exc:
        await message.answer(f"Ошибка окна: {exc}", parse_mode=None)
        logger.info(
            "admin_extract_window_parse_failed",
            extra={
                "admin_user_id": message.from_user.id,
                "raw_args": raw_args,
                "error": str(exc),
            },
        )
        return

    logger.info(
        "admin_extract_invoked",
        extra={
            "admin_user_id": message.from_user.id,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
        },
    )

    # T6-03 design §3: build the gateway locally (Phase 5 precedent).
    cfg = load_gateway_config()
    provider = resolve_provider(cfg.provider, deepseek_max_tokens=8_192)
    gateway = LiveExtractCandidatesGateway(ledger_repo=LedgerRepo(), provider=provider, config=cfg)

    result = await run_extraction_pass(
        session,
        window_start=window_start,
        window_end=window_end,
        gateway=gateway,
        operator_user_id=message.from_user.id,
        source_chat_id=settings.COMMUNITY_CHAT_ID,
    )

    summary_lines = [
        "<b>Extraction pass</b>",
        f"window: <code>{window_start.isoformat()}</code> .. <code>{window_end.isoformat()}</code>",
        f"run_status: <code>{result.run_status}</code>",
        f"candidate_count: <code>{result.candidate_count}</code>",
        f"llm_usage_ledger_id: <code>{result.llm_usage_ledger_id}</code>",
        f"extraction_run_id: <code>{result.extraction_run_id}</code>",
    ]
    if result.failure_reason:
        summary_lines.append(f"failure_reason: <code>{result.failure_reason}</code>")
    await message.answer("\n".join(summary_lines), parse_mode="HTML")
