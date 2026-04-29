"""Logical rollback for Telegram Desktop import runs (T2-NEW-G / issue #104).

Rollback removes only rows owned by one import run. The ownership selector is the
FK chain ``chat_messages.raw_update_id -> telegram_updates.id`` plus
``telegram_updates.ingestion_run_id`` and the synthetic-update guard
``telegram_updates.update_id IS NULL``.

NO message text, captions, entities, or raw payload bodies are logged or returned.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from bot.services.import_chunking import acquire_advisory_lock

logger = logging.getLogger(__name__)

_IMPORT_RUN_TYPES = frozenset({"import"})


class IngestionRunNotFoundError(ValueError):
    """Raised when the requested ingestion_runs row does not exist."""


class InvalidRollbackRunError(ValueError):
    """Raised when the requested run is not an import-apply run."""


class DownstreamDependentsError(ValueError):
    """Raised when derived rows already depend on the import run."""


@dataclass(frozen=True)
class RollbackReport:
    """Outcome of one rollback attempt.

    When ``idempotent_skip`` is true, delete counts echo the prior rollback audit row
    for operator visibility; this caller did not delete additional rows.
    """

    original_run_id: int
    chat_messages_deleted: int
    telegram_updates_deleted: int
    message_versions_cascade_deleted: int
    audit_run_id: int
    idempotent_skip: bool


async def rollback_ingestion_run(
    session: AsyncSession,
    ingestion_run_id: int,
) -> RollbackReport:
    """Rollback one import run by deleting its synthetic-update-owned rows.

    The function commits on success and rolls back on any exception. Idempotency is
    enforced under the per-run advisory lock by checking for an existing
    ``run_type='rolled_back'`` audit row whose ``stats_json.original_run_id`` matches.
    """
    connection = await session.connection()

    try:
        original = await _load_original_run(connection, ingestion_run_id)
        _validate_original_run(original)
    except BaseException:
        await session.rollback()
        raise

    async with acquire_advisory_lock(connection, ingestion_run_id):
        try:
            existing_audit = await _find_existing_rollback_audit(
                connection,
                ingestion_run_id,
            )
            if existing_audit is not None:
                report = _build_idempotent_report(
                    ingestion_run_id,
                    audit_id=existing_audit[0],
                    stats=existing_audit[1],
                )
                # Commit inside acquire_advisory_lock(): pinned connection keeps the lock; see import_apply.py.
                await session.commit()
                logger.info(
                    "import_rollback: idempotent no-op",
                    extra={
                        "original_run_id": ingestion_run_id,
                        "audit_run_id": report.audit_run_id,
                    },
                )
                return report

            await _check_no_downstream_dependents(connection, ingestion_run_id)

            chat_messages_count = await _count_import_chat_messages(
                connection,
                ingestion_run_id,
            )
            message_versions_count = await _count_import_message_versions(
                connection,
                ingestion_run_id,
            )

            race_report: RollbackReport | None = None
            async with connection.begin_nested():
                chat_delete_result = await connection.execute(
                    text(
                        """
                        DELETE FROM chat_messages cm
                         WHERE EXISTS (
                               SELECT 1
                                 FROM telegram_updates tu
                                WHERE tu.id = cm.raw_update_id
                                  AND tu.ingestion_run_id = :id
                                  AND tu.update_id IS NULL
                         )
                        """
                    ),
                    {"id": ingestion_run_id},
                )
                chat_messages_deleted = _require_rowcount(chat_delete_result)

                update_delete_result = await connection.execute(
                    text(
                        """
                        DELETE FROM telegram_updates
                         WHERE ingestion_run_id = :id
                           AND update_id IS NULL
                        """
                    ),
                    {"id": ingestion_run_id},
                )
                telegram_updates_deleted = _require_rowcount(update_delete_result)

                audit_savepoint = await connection.begin_nested()
                try:
                    audit_run_id = await _insert_rollback_audit(
                        connection,
                        original_run_id=ingestion_run_id,
                        chat_messages_deleted=chat_messages_deleted,
                        telegram_updates_deleted=telegram_updates_deleted,
                        message_versions_cascade_deleted=message_versions_count,
                    )
                except IntegrityError:
                    await audit_savepoint.rollback()
                    race_audit = await _find_existing_rollback_audit(
                        connection,
                        ingestion_run_id,
                    )
                    if race_audit is None:
                        raise

                    race_report = _build_idempotent_report(
                        ingestion_run_id,
                        audit_id=race_audit[0],
                        stats=race_audit[1],
                    )
                else:
                    await audit_savepoint.commit()

            # Commit inside acquire_advisory_lock(): pinned connection keeps the lock; see import_apply.py.
            await session.commit()
            if race_report is not None:
                logger.warning(
                    "rollback race resolved via unique-index fallback",
                    extra={
                        "original_run_id": ingestion_run_id,
                        "audit_run_id": race_report.audit_run_id,
                        "chat_messages_deleted": race_report.chat_messages_deleted,
                        "telegram_updates_deleted": race_report.telegram_updates_deleted,
                        "message_versions_cascade_deleted": (
                            race_report.message_versions_cascade_deleted
                        ),
                    },
                )
                return race_report
        except BaseException:
            await session.rollback()
            raise

    logger.info(
        "import_rollback: completed",
        extra={
            "original_run_id": ingestion_run_id,
            "audit_run_id": audit_run_id,
            "chat_messages_deleted": chat_messages_deleted,
            "telegram_updates_deleted": telegram_updates_deleted,
            "message_versions_cascade_deleted": message_versions_count,
            "chat_messages_count_before_delete": chat_messages_count,
        },
    )

    return RollbackReport(
        original_run_id=ingestion_run_id,
        chat_messages_deleted=chat_messages_deleted,
        telegram_updates_deleted=telegram_updates_deleted,
        message_versions_cascade_deleted=message_versions_count,
        audit_run_id=audit_run_id,
        idempotent_skip=False,
    )


async def _check_no_downstream_dependents(
    connection: AsyncConnection,
    ingestion_run_id: int,
) -> None:
    """Placeholder for Phase 4+ derived-row protection.

    TODO(Phase 4): check extracted facts, search rows, evidence bundles, cards,
    summaries, graph sync rows, and other derived layers before allowing rollback.
    """
    _ = connection
    _ = ingestion_run_id


async def _load_original_run(
    connection: AsyncConnection,
    ingestion_run_id: int,
) -> dict:
    result = await connection.execute(
        text(
            """
            SELECT id, run_type, status
              FROM ingestion_runs
             WHERE id = :id
             LIMIT 1
            """
        ),
        {"id": ingestion_run_id},
    )
    row = result.mappings().first()
    if row is None:
        raise IngestionRunNotFoundError("ingestion_run not found")
    return dict(row)


def _validate_original_run(run: dict) -> None:
    run_type = run["run_type"]
    if run_type not in _IMPORT_RUN_TYPES:
        raise InvalidRollbackRunError(
            f"ingestion_run {run['id']} has run_type={run_type!r}; "
            "rollback_ingestion_run accepts only import runs"
        )


async def _find_existing_rollback_audit(
    connection: AsyncConnection,
    ingestion_run_id: int,
) -> tuple[int, dict[str, Any]] | None:
    result = await connection.execute(
        text(
            """
            SELECT id, stats_json
              FROM ingestion_runs
             WHERE run_type = 'rolled_back'
               AND stats_json::jsonb ->> 'original_run_id' = :original_run_id
             ORDER BY id ASC
             LIMIT 1
            """
        ),
        {"original_run_id": str(ingestion_run_id)},
    )
    row = result.mappings().first()
    if row is None:
        return None
    stats = row["stats_json"]
    if isinstance(stats, str):
        stats = json.loads(stats)
    if not isinstance(stats, dict):
        raise RuntimeError("rollback audit row stats_json is not an object")
    return int(row["id"]), stats


def _build_idempotent_report(
    original_run_id: int,
    *,
    audit_id: int,
    stats: dict[str, Any],
) -> RollbackReport:
    chat_messages_deleted, telegram_updates_deleted, message_versions_deleted = (
        _extract_rollback_counts(stats)
    )
    return RollbackReport(
        original_run_id=original_run_id,
        chat_messages_deleted=chat_messages_deleted,
        telegram_updates_deleted=telegram_updates_deleted,
        message_versions_cascade_deleted=message_versions_deleted,
        audit_run_id=audit_id,
        idempotent_skip=True,
    )


def _extract_rollback_counts(stats: dict[str, Any]) -> tuple[int, int, int]:
    try:
        return (
            int(stats["chat_messages_deleted"]),
            int(stats["telegram_updates_deleted"]),
            int(stats["message_versions_cascade_deleted"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("rollback audit row stats_json is missing rollback counts") from exc


async def _count_import_chat_messages(
    connection: AsyncConnection,
    ingestion_run_id: int,
) -> int:
    result = await connection.execute(
        text(
            """
            SELECT COUNT(*)
              FROM chat_messages cm
              JOIN telegram_updates tu ON tu.id = cm.raw_update_id
             WHERE tu.ingestion_run_id = :id
               AND tu.update_id IS NULL
            """
        ),
        {"id": ingestion_run_id},
    )
    return int(result.scalar_one())


async def _count_import_message_versions(
    connection: AsyncConnection,
    ingestion_run_id: int,
) -> int:
    result = await connection.execute(
        text(
            """
            SELECT COUNT(*)
              FROM message_versions mv
              JOIN chat_messages cm ON cm.id = mv.chat_message_id
              JOIN telegram_updates tu ON tu.id = cm.raw_update_id
             WHERE tu.ingestion_run_id = :id
               AND tu.update_id IS NULL
            """
        ),
        {"id": ingestion_run_id},
    )
    return int(result.scalar_one())


async def _insert_rollback_audit(
    connection: AsyncConnection,
    *,
    original_run_id: int,
    chat_messages_deleted: int,
    telegram_updates_deleted: int,
    message_versions_cascade_deleted: int,
) -> int:
    now = datetime.now(tz=timezone.utc)
    stats = {
        "original_run_id": original_run_id,
        "chat_messages_deleted": chat_messages_deleted,
        "telegram_updates_deleted": telegram_updates_deleted,
        "message_versions_cascade_deleted": message_versions_cascade_deleted,
        "rolled_back_at": now.isoformat(),
    }
    result = await connection.execute(
        text(
            """
            INSERT INTO ingestion_runs (
                run_type,
                source_name,
                status,
                stats_json,
                started_at,
                finished_at
            )
            VALUES (
                'rolled_back',
                :source_name,
                'completed',
                CAST(:stats_json AS JSONB),
                :started_at,
                :finished_at
            )
            RETURNING id
            """
        ),
        {
            "source_name": f"rollback:{original_run_id}",
            "stats_json": json.dumps(stats),
            "started_at": now,
            "finished_at": now,
        },
    )
    return int(result.scalar_one())


def _require_rowcount(result: CursorResult) -> int:
    rowcount = result.rowcount
    if rowcount is None or rowcount < 0:
        raise RuntimeError("database did not report rollback delete rowcount")
    return int(rowcount)
