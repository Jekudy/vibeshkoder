"""Bot CLI entry points (run via `python -m bot.cli <subcommand> [args...]`).

Currently supported subcommands:
    import_dry_run <export_path>  — parse a Telegram Desktop export JSON and print stats
    import_apply <export_path> [--resume] [--chunk-size N]
                                  — apply a Telegram Desktop export to the DB
                                    (checkpoint/resume infrastructure ready; apply logic in #103)
    rollback_ingestion_run <id>   — logically rollback one import run by ingestion_run_id
    memory_backfill --chat-id ID  — bounded extraction + automatic promotion over history
    memory_reconcile_extraction   — explicitly resolve one ambiguous extraction attempt
    memory_reconcile_image        — explicitly resolve one ambiguous image description
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any, cast

from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger(__name__)


def _cmd_import_dry_run(args: argparse.Namespace) -> int:
    """Entry point for import_dry_run subcommand.

    Without --with-db: parses offline, prints JSON report.
    With --with-db: opens DB session, enriches with duplicate/reply stats, prints
    operator-readable summary.
    """
    if getattr(args, "with_db", False):
        return asyncio.run(_cmd_import_dry_run_with_db(args))
    return _cmd_import_dry_run_offline(args)


def _cmd_import_dry_run_offline(args: argparse.Namespace) -> int:
    path = Path(args.export_path).expanduser().resolve()
    if not path.exists():
        print(f"ERROR: export path not found: {path}", file=sys.stderr)
        return 2

    is_html = _is_html_export_path(path)
    try:
        excluded_author_names = _load_excluded_author_names(args) if is_html else frozenset()
        if is_html:
            from bot.services.import_html_parser import parse_html_export

            report = parse_html_export(
                path,
                excluded_author_names=excluded_author_names,
            )
        else:
            from bot.services.import_parser import parse_export

            report = parse_export(path)
    except FileNotFoundError as e:
        print(f"ERROR: file not found: {e}", file=sys.stderr)
        return 2
    except (ValueError, json.JSONDecodeError) as e:
        print(f"ERROR: parse failed: {e}", file=sys.stderr)
        return 1

    from dataclasses import asdict

    payload = asdict(report)
    # datetime values are not JSON-serialisable by default; convert to ISO strings.
    if payload.get("date_range_start"):
        payload["date_range_start"] = payload["date_range_start"].isoformat()
    if payload.get("date_range_end"):
        payload["date_range_end"] = payload["date_range_end"].isoformat()

    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


async def _cmd_import_dry_run_with_db(args: argparse.Namespace) -> int:
    """DB-aware dry-run: enriches report with duplicate / broken-reply stats."""
    path = Path(args.export_path).expanduser().resolve()
    if not path.exists():
        print(f"ERROR: export path not found: {path}", file=sys.stderr)
        return 2

    is_html = _is_html_export_path(path)
    explicit_chat_id = cast(int | None, getattr(args, "chat_id", None))
    if is_html:
        from bot.services.import_html_parser import (
            HtmlExportValidationError,
            discover_html_pages,
        )

        try:
            discover_html_pages(path)
            excluded_author_names = _load_excluded_author_names(args)
        except HtmlExportValidationError as exc:
            print(f"ERROR: invalid HTML export: {exc}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"ERROR: invalid excluded-author config: {exc}", file=sys.stderr)
            return 2
        if explicit_chat_id is None:
            print(
                "ERROR: --chat-id is required for DB-aware Telegram HTML dry-run.",
                file=sys.stderr,
            )
            return 2
        chat_id = explicit_chat_id
    else:
        try:
            chat_id = _read_chat_id_from_envelope(path)
        except (ValueError, OSError) as e:
            print(f"ERROR: could not read chat_id from export envelope: {e}", file=sys.stderr)
            return 2
        if explicit_chat_id is not None and explicit_chat_id != chat_id:
            print(
                "ERROR: --chat-id does not match the JSON export envelope id.",
                file=sys.stderr,
            )
            return 2

    import bot.db.engine as _db_engine

    try:
        async with _db_engine.async_session() as session:
            if is_html:
                from bot.services.import_dry_run import parse_html_export_with_db

                report = await parse_html_export_with_db(
                    path,
                    session,
                    chat_id,
                    excluded_author_names=excluded_author_names,
                )
            else:
                from bot.services.import_dry_run import parse_export_with_db

                report = await parse_export_with_db(path, session, chat_id)
            # Do NOT commit: parse_export_with_db creates a synthetic dry_run run
            # that must be rolled back. The async_session context manager rolls back
            # on exit without commit.
    except FileNotFoundError as e:
        print(f"ERROR: file not found: {e}", file=sys.stderr)
        return 2
    except (ValueError, json.JSONDecodeError) as e:
        print(f"ERROR: parse failed: {e}", file=sys.stderr)
        return 1

    # Operator-readable summary
    policy = report.policy_marker_counts
    offrecord_count = policy.get("offrecord", 0)
    nomem_count = policy.get("nomem", 0)
    print(
        f"{report.db_duplicate_count} duplicates would be skipped, "
        f"{report.db_rehydrate_count} hidden/legacy rows would be restored, "
        f"{offrecord_count} offrecord messages, "
        f"{nomem_count} nomem, "
        f"{report.db_broken_reply_count} broken reply chains."
    )
    print("Complete history: legacy tombstones and policy markers do not skip messages.")
    print(
        f"Excluded author:  {report.excluded_author_message_count} exact-match messages "
        f"would be retained raw-only ({report.excluded_author_message_counts})."
    )
    return 0


def _cmd_import_apply(args: argparse.Namespace) -> int:
    """Synchronous entry-point that delegates to the async implementation."""
    return asyncio.run(_cmd_import_apply_async(args))


def _cmd_rollback_ingestion_run(args: argparse.Namespace) -> int:
    """Synchronous entry-point for logical import rollback."""
    return asyncio.run(_cmd_rollback_ingestion_run_async(args))


def _cmd_memory_backfill(args: argparse.Namespace) -> int:
    """Synchronous entry-point for the bounded one-off memory backfill."""
    return asyncio.run(_cmd_memory_backfill_async(args))


def _cmd_memory_reconcile_extraction(args: argparse.Namespace) -> int:
    return asyncio.run(_cmd_memory_reconcile_extraction_async(args))


def _cmd_memory_reconcile_image(args: argparse.Namespace) -> int:
    return asyncio.run(_cmd_memory_reconcile_image_async(args))


def _parse_backfill_utc(value: str) -> Any:
    """Parse an explicit UTC ISO8601 bound without accepting local time."""
    from datetime import datetime, timedelta, timezone

    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise ValueError("expected an ISO8601 UTC datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("expected an ISO8601 UTC datetime with Z or +00:00")
    return parsed.astimezone(timezone.utc)


def _print_memory_backfill_progress(window: Any) -> None:
    """Print only bounded operational metadata; source content is forbidden."""
    run_id = str(window.extraction_run_id) if window.extraction_run_id else "none"
    print(
        f"window={window.window_number}/{window.window_count} "
        f"start={window.window_start.isoformat()} end={window.window_end.isoformat()} "
        f"run_id={run_id} candidates={window.candidate_count} "
        f"promotions={window.promotion_count} "
        f"promoted={window.promoted_count} resumed={int(window.resumed)}"
    )


async def _cmd_memory_backfill_async(args: argparse.Namespace) -> int:
    from bot.db.engine import async_session
    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from bot.services.llm_gateway import (
        LiveExtractCandidatesGateway,
        load_gateway_config,
        resolve_provider,
    )
    from bot.services.memory_backfill import (
        MemoryBackfillConfigurationError,
        MemoryBackfillWindowError,
        run_memory_backfill,
    )

    raw_start = cast(str | None, getattr(args, "start", None))
    raw_end = cast(str | None, getattr(args, "end", None))
    if (raw_start is None) != (raw_end is None):
        print(
            "ERROR: --start and --end must be supplied together or both omitted.",
            file=sys.stderr,
        )
        return 2
    try:
        start = _parse_backfill_utc(raw_start) if raw_start is not None else None
        end = _parse_backfill_utc(raw_end) if raw_end is not None else None
    except ValueError:
        print(
            "ERROR: --start/--end must be ISO8601 UTC values using Z or +00:00.",
            file=sys.stderr,
        )
        return 2

    try:
        config = load_gateway_config()
        gateway = LiveExtractCandidatesGateway(
            ledger_repo=LedgerRepo(),
            provider=resolve_provider(config.provider, deepseek_max_tokens=8_192),
            config=config,
        )
        report = await run_memory_backfill(
            session_factory=async_session,
            gateway=gateway,
            source_chat_id=args.chat_id,
            start=start,
            end=end,
            window_hours=args.window_hours,
            max_windows=args.max_windows,
            actor_user_id=args.actor_user_id,
            progress=_print_memory_backfill_progress,
        )
    except MemoryBackfillConfigurationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except MemoryBackfillWindowError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except (ValueError, SQLAlchemyError) as exc:
        # Configuration/DB failures are surfaced class-only: provider or driver
        # messages may contain URLs, credentials, or response fragments.
        print(f"ERROR: memory backfill setup failed ({type(exc).__name__}).", file=sys.stderr)
        return 2

    print(
        f"completed_windows={report.completed_window_count} "
        f"resumed_windows={report.resumed_window_count} "
        f"candidates={report.candidate_count} promoted={report.promoted_count} "
        f"start={report.range_start.isoformat()} end={report.range_end.isoformat()}"
    )
    return 0


async def _cmd_memory_reconcile_extraction_async(args: argparse.Namespace) -> int:
    from bot.db.engine import async_session
    from bot.services.memory_reconciliation import (
        MemoryReconciliationError,
        reconcile_extraction_run,
    )

    try:
        result = await reconcile_extraction_run(
            session_factory=async_session,
            run_id=args.run_id,
            action=args.action,
            actor_user_id=args.actor_user_id,
            reason=args.reason,
            evidence_hash=args.evidence_hash,
            accept_possible_duplicate_cost=args.accept_possible_duplicate_cost,
            accept_memory_gap=args.accept_memory_gap,
        )
    except MemoryReconciliationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except (SQLAlchemyError, RuntimeError) as exc:
        print(
            f"ERROR: extraction reconciliation failed ({type(exc).__name__}).",
            file=sys.stderr,
        )
        return 1
    print(f"reconciled target={result.target_type} id={result.target_id} action={result.action}")
    return 0


async def _cmd_memory_reconcile_image_async(args: argparse.Namespace) -> int:
    from bot.db.engine import async_session
    from bot.services.memory_reconciliation import (
        MemoryReconciliationError,
        reconcile_image_description,
    )

    try:
        result = await reconcile_image_description(
            session_factory=async_session,
            message_media_id=args.message_media_id,
            action=args.action,
            actor_user_id=args.actor_user_id,
            reason=args.reason,
            evidence_hash=args.evidence_hash,
            accept_possible_duplicate_cost=args.accept_possible_duplicate_cost,
            accept_memory_gap=args.accept_memory_gap,
        )
    except MemoryReconciliationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except (SQLAlchemyError, RuntimeError) as exc:
        print(
            f"ERROR: image reconciliation failed ({type(exc).__name__}).",
            file=sys.stderr,
        )
        return 1
    print(f"reconciled target={result.target_type} id={result.target_id} action={result.action}")
    return 0


async def _cmd_import_apply_async(args: argparse.Namespace) -> int:
    """Apply a Telegram Desktop export to the DB.

    Step 1: Resolve path + compute source_hash.
    Step 2: Call init_or_resume_run to get the resume decision.
    Step 3: Dispatch based on decision mode.
    Step 4: Run bot.services.import_apply.run_apply (Stream Delta #103).
    """
    from bot.services.import_checkpoint import finalize_run, init_or_resume_run

    path = Path(args.export_path).expanduser().resolve()
    if not path.exists():
        print(f"ERROR: export path not found: {path}", file=sys.stderr)
        return 2

    is_html = _is_html_export_path(path)
    explicit_chat_id = cast(int | None, getattr(args, "chat_id", None))
    if is_html and explicit_chat_id is None:
        print(
            "ERROR: --chat-id is required for Telegram HTML exports because HTML "
            "does not contain the numeric chat id.",
            file=sys.stderr,
        )
        return 2

    try:
        excluded_author_names = _load_excluded_author_names(args)
    except ValueError as exc:
        print(f"ERROR: invalid excluded-author config: {exc}", file=sys.stderr)
        return 2
    if is_html and not excluded_author_names:
        print(
            "ERROR: HTML import requires --exclude-author-name or "
            "IMPORT_EXCLUDED_AUTHOR_NAMES_JSON.",
            file=sys.stderr,
        )
        return 2

    from bot.services.import_html_parser import (
        HtmlExportValidationError,
        count_html_author_matches,
    )

    try:
        excluded_author_message_counts = (
            count_html_author_matches(path, excluded_author_names) if is_html else {}
        )
        unmatched_names = [
            name for name, count in excluded_author_message_counts.items() if count == 0
        ]
        if unmatched_names:
            print(
                "ERROR: exact excluded author name(s) not found in HTML export: "
                + ", ".join(unmatched_names),
                file=sys.stderr,
            )
            return 2
        source_hash = _hash_import_source(
            path,
            excluded_author_names=excluded_author_names,
        )
    except HtmlExportValidationError as exc:
        print(f"ERROR: invalid HTML export: {exc}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"ERROR: could not read export source: {e}", file=sys.stderr)
        return 2

    if is_html:
        assert explicit_chat_id is not None  # guarded above; narrows for static typing
        chat_id = explicit_chat_id
    else:
        # Extract chat_id from the export envelope (first parse of JSON header).
        try:
            chat_id = _read_chat_id_from_envelope(path)
        except (ValueError, OSError) as e:
            print(f"ERROR: could not read chat_id from export envelope: {e}", file=sys.stderr)
            return 2
        if explicit_chat_id is not None and explicit_chat_id != chat_id:
            print(
                "ERROR: --chat-id does not match the JSON export envelope id.",
                file=sys.stderr,
            )
            return 2

    import_run_config = {
        "source_adapter": "telegram_html" if is_html else "telegram_json",
        "excluded_author_names": sorted(excluded_author_names),
        "excluded_author_message_count": sum(excluded_author_message_counts.values()),
        "excluded_author_message_counts": excluded_author_message_counts,
    }

    # Open async session and decide on start/resume/block.
    # Import via the engine module so tests can patch bot.db.engine.async_session.
    import bot.db.engine as _db_engine

    async with _db_engine.async_session() as session:
        # Feature-flag gate: memory.import.apply.enabled (default OFF). Read BEFORE
        # creating an ingestion_run so a disabled run leaves zero footprint.
        from bot.db.repos.feature_flag import FeatureFlagRepo

        if not await FeatureFlagRepo.get(session, IMPORT_APPLY_FLAG):
            print(
                "import apply disabled (feature flag "
                f"'{IMPORT_APPLY_FLAG}' is OFF). Enable via SQL before retrying."
            )
            return 0

        decision = await init_or_resume_run(
            session,
            source_path=str(path),
            source_hash=source_hash,
            chat_id=chat_id,
            resume=args.resume,
        )

        if decision.mode in ("start_fresh", "resume_existing"):
            assert decision.ingestion_run_id is not None
            await _save_import_run_config(
                session,
                ingestion_run_id=decision.ingestion_run_id,
                config=import_run_config,
            )

        # Commit the run row immediately (Fix 4): ensures the partial unique index
        # sees the new 'running' row before run_apply starts, AND that a hard-kill
        # before the first checkpoint still leaves a recoverable 'running' row in DB.
        if decision.mode in ("start_fresh", "resume_existing"):
            await session.commit()

        print(f"[import_apply] decision: {decision.mode} — {decision.reason}")

        if decision.mode == "block_partial_present":
            print(
                f"ERROR: {decision.reason}\n"
                "Use --resume to continue the partial run, or finalize the prior run manually.",
                file=sys.stderr,
            )
            return 3

        # start_fresh or resume_existing → import the apply path (#103).
        from bot.services.import_apply import run_apply

        # Load chunking config from env; CLI --chunk-size overrides the env var.
        # IMPORTANT: apply CLI override BEFORE calling load_chunking_config so that an
        # invalid IMPORT_APPLY_CHUNK_SIZE env var does not block a valid --chunk-size arg.
        from bot.services.import_chunking import load_chunking_config

        _env = os.environ.copy()
        if args.chunk_size is not None:
            _env["IMPORT_APPLY_CHUNK_SIZE"] = str(args.chunk_size)
        try:
            chunking_config = load_chunking_config(env=_env)
        except ValueError as exc:
            print(f"ERROR: invalid chunking config: {exc}", file=sys.stderr)
            return 2

        assert decision.ingestion_run_id is not None
        ingestion_run_id = decision.ingestion_run_id
        resume_point = decision.last_processed_export_msg_id

        try:
            apply_kwargs: dict[str, Any] = {
                "ingestion_run_id": ingestion_run_id,
                "resume_point": resume_point,
                "chunking_config": chunking_config,
            }
            if excluded_author_names:
                apply_kwargs["excluded_author_names"] = excluded_author_names
            report = await run_apply(session, **apply_kwargs)
        except (ValueError, RuntimeError, OSError, SQLAlchemyError, json.JSONDecodeError) as exc:
            # Fix 5: if run_apply raised a DB error, the session may be in
            # PendingRollback state. Calling finalize_run on an aborted tx would
            # raise a secondary exception masking the original.
            # Strategy: rollback primary session, open a FRESH session for finalize,
            # swallow finalize errors (log only), then re-raise the ORIGINAL exception.
            original_exc = exc
            await _finalize_failed_apply(
                _db_engine,
                session=session,
                finalize_run=finalize_run,
                ingestion_run_id=ingestion_run_id,
                original_exc=original_exc,
            )
            print(
                f"ERROR: import apply failed: {type(original_exc).__name__}",
                file=sys.stderr,
            )
            return 5
        except BaseException as exc:
            await _finalize_failed_apply(
                _db_engine,
                session=session,
                finalize_run=finalize_run,
                ingestion_run_id=ingestion_run_id,
                original_exc=exc,
            )
            raise

        # Mark the run completed with final stats. finalize_run is idempotent.
        await finalize_run(
            session,
            ingestion_run_id=ingestion_run_id,
            final_status="completed",
        )
        # Persist final stats (counts) into stats_json via deep-merge.
        await _save_apply_final_stats(session, report)
        await session.commit()

        # Operator-readable summary
        print(
            f"[import_apply] run {ingestion_run_id} completed: "
            f"applied={report.applied_count}, "
            f"duplicate={report.skipped_duplicate_count}, "
            f"tombstone={report.skipped_tombstone_count}, "
            f"governance={report.skipped_governance_count}, "
            f"excluded_author={report.skipped_excluded_author_count}, "
            f"errors={report.error_count}, "
            f"chunks={report.chunks_processed}"
        )

    return 0


async def _cmd_rollback_ingestion_run_async(args: argparse.Namespace) -> int:
    from bot.services.import_rollback import (
        DownstreamDependentsError,
        IngestionRunNotFoundError,
        InvalidRollbackRunError,
        rollback_ingestion_run,
    )

    import bot.db.engine as _db_engine

    try:
        async with _db_engine.async_session() as session:
            report = await rollback_ingestion_run(session, args.ingestion_run_id)
    except InvalidRollbackRunError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except IngestionRunNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    except DownstreamDependentsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 4

    suffix = " (idempotent no-op; prior audit counts shown)" if report.idempotent_skip else ""
    print(f"rollback_ingestion_run {report.original_run_id}{suffix}")
    print(f"chat_messages_deleted: {report.chat_messages_deleted}")
    print(f"telegram_updates_deleted: {report.telegram_updates_deleted}")
    print(f"message_versions_cascade_deleted: {report.message_versions_cascade_deleted}")
    print(f"audit_run_id: {report.audit_run_id}")
    return 0


# Feature flag key controlling whether import apply is allowed to run. Default OFF.
# Operators flip it via SQL once the apply path is verified in their env.
IMPORT_APPLY_FLAG = "memory.import.apply.enabled"


async def _save_apply_final_stats(session, report) -> None:
    """Deep-merge the apply report's counts into ingestion_runs.stats_json.

    Mirrors save_checkpoint's deep-merge so other operator-set keys survive.
    Called once at the end of a successful run.
    """
    from sqlalchemy import text

    patch = {
        "applied_count": report.applied_count,
        "rehydrated_count": report.rehydrated_count,
        "skipped_duplicate_count": report.skipped_duplicate_count,
        "skipped_tombstone_count": report.skipped_tombstone_count,
        "tombstone_skip_export_msg_ids": report.tombstone_skip_export_msg_ids,
        "skipped_governance_count": report.skipped_governance_count,
        "skipped_excluded_author_count": report.skipped_excluded_author_count,
        "excluded_author_names": report.excluded_author_names,
        "excluded_author_message_counts": report.excluded_author_message_counts,
        "skipped_resume_count": report.skipped_resume_count,
        "skipped_service_count": report.skipped_service_count,
        "skipped_overlap_count": report.skipped_overlap_count,
        "error_count": report.error_count,
        "error_export_msg_ids": report.error_export_msg_ids,
        "chunks_processed": report.chunks_processed,
        "last_processed_export_msg_id": report.last_processed_export_msg_id,
    }
    await session.execute(
        text(
            """
            UPDATE ingestion_runs
               SET stats_json = COALESCE(stats_json::jsonb, '{}'::jsonb) || CAST(:patch AS jsonb)
             WHERE id = :id
            """
        ),
        {"id": report.ingestion_run_id, "patch": json.dumps(patch)},
    )


async def _save_import_run_config(
    session,
    *,
    ingestion_run_id: int,
    config: dict[str, Any],
) -> None:
    """Deep-merge content-free adapter preflight metadata into config_json."""
    from sqlalchemy import text

    await session.execute(
        text(
            """
            UPDATE ingestion_runs
               SET config_json = COALESCE(config_json::jsonb, '{}'::jsonb)
                                 || CAST(:patch AS jsonb)
             WHERE id = :id
            """
        ),
        {"id": ingestion_run_id, "patch": json.dumps(config)},
    )


async def _finalize_failed_apply(
    db_engine_module,
    *,
    session,
    finalize_run,
    ingestion_run_id: int,
    original_exc: BaseException,
) -> None:
    """Persist best-effort failure state after run_apply raises."""
    partial_report = getattr(original_exc, "import_apply_report", None)
    try:
        await session.rollback()
    except SQLAlchemyError as rb_err:
        logger.warning(
            "rollback failed after run_apply error",
            extra={
                "ingestion_run_id": ingestion_run_id,
                "error_class": type(rb_err).__name__,
                "error_taxonomy": "import_apply_primary_rollback_failed",
            },
        )

    try:
        async with db_engine_module.async_session() as fresh_session:
            if partial_report is not None:
                try:
                    await _save_apply_final_stats(fresh_session, partial_report)
                except (ValueError, RuntimeError, SQLAlchemyError) as stats_err:
                    logger.warning(
                        "saving partial import_apply stats failed",
                        extra={
                            "ingestion_run_id": ingestion_run_id,
                            "original_error_class": type(original_exc).__name__,
                            "error_class": type(stats_err).__name__,
                            "error_taxonomy": "import_apply_partial_stats_persist_failed",
                        },
                    )
                    try:
                        await fresh_session.rollback()
                    except SQLAlchemyError as rb_err:
                        logger.warning(
                            "rollback failed after partial import_apply stats error",
                            extra={
                                "ingestion_run_id": ingestion_run_id,
                                "error_class": type(rb_err).__name__,
                                "error_taxonomy": "import_apply_partial_stats_rollback_failed",
                            },
                        )

            await finalize_run(
                fresh_session,
                ingestion_run_id=ingestion_run_id,
                final_status="failed",
                error_payload={
                    "error_type": type(original_exc).__name__,
                    "message": "import apply runtime error",
                },
            )
            await fresh_session.commit()
    except (ValueError, SQLAlchemyError) as fin_err:
        logger.warning(
            "finalize_run failed after import_apply error",
            extra={
                "ingestion_run_id": ingestion_run_id,
                "original_error_class": type(original_exc).__name__,
                "error_class": type(fin_err).__name__,
                "error_taxonomy": "import_apply_finalize_failed",
            },
        )


def _read_chat_id_from_envelope(path: Path) -> int:
    """Read the top-level ``id`` field from the export JSON.

    Performs a full ``json.load`` of the file. For typical Telegram Desktop exports
    the envelope fields (``id``, ``name``, ``type``) appear at the top of the JSON
    object, so the parser will encounter them early; however, the full file is still
    loaded into memory before returning. A future ticket should add streaming extraction
    (e.g. via ``ijson``) if very large exports are expected. For now full load is
    acceptable given exports are typically a few hundred MB at most and this function
    is called once at CLI startup.

    Raises ValueError if the envelope cannot be parsed or has no ``id``.
    """
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Export JSON is not valid: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("Export envelope is not a JSON object")

    chat_id = data.get("id")
    if not isinstance(chat_id, int):
        raise ValueError(
            f"Export envelope has no integer 'id' field (got {type(chat_id).__name__!r}). "
            "Ensure this is a single-chat Telegram Desktop export."
        )
    return chat_id


def _is_html_export_path(path: Path) -> bool:
    return path.is_dir() or path.suffix.lower() == ".html"


def _load_excluded_author_names(args: argparse.Namespace) -> frozenset[str]:
    from bot.services.import_author_exclusion import load_import_excluded_author_names

    return load_import_excluded_author_names(
        env=os.environ,
        cli_names=cast(
            list[str] | None,
            getattr(args, "excluded_author_names", None),
        ),
    )


def _hash_import_source(
    path: Path,
    *,
    excluded_author_names: frozenset[str] = frozenset(),
) -> str:
    """Hash exactly the source pages consumed by the selected import adapter."""
    digest = hashlib.sha256()
    if not _is_html_export_path(path):
        # Preserve the original JSON source-hash contract byte-for-byte so existing
        # resumable ingestion_runs keep matching after HTML support is deployed.
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        return _salt_import_hash(digest, excluded_author_names)

    from bot.services.import_html_parser import discover_html_pages

    files = discover_html_pages(path)
    for source_file in files:
        # Include page names and boundaries so concatenation cannot collide.
        encoded_name = source_file.name.encode("utf-8")
        digest.update(len(encoded_name).to_bytes(4, byteorder="big"))
        digest.update(encoded_name)
        with source_file.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(len(chunk).to_bytes(4, byteorder="big"))
                digest.update(chunk)
        digest.update((0).to_bytes(4, byteorder="big"))
    return _salt_import_hash(digest, excluded_author_names)


def _salt_import_hash(
    digest: Any,
    excluded_author_names: frozenset[str],
) -> str:
    if not excluded_author_names:
        return digest.hexdigest()
    salted = hashlib.sha256()
    salted.update(b"shkoder-import-author-exclusions-v1\0")
    salted.update(digest.digest())
    for name in sorted(excluded_author_names):
        encoded = name.encode("utf-8")
        salted.update(len(encoded).to_bytes(4, byteorder="big"))
        salted.update(encoded)
    return salted.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bot.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # import_dry_run subcommand
    p_import = sub.add_parser(
        "import_dry_run",
        help="Parse a Telegram Desktop export JSON and print stats (no DB writes).",
    )
    p_import.add_argument("export_path", type=str)
    p_import.add_argument(
        "--chat-id",
        type=int,
        default=None,
        dest="chat_id",
        help="Required for DB-aware HTML dry-run; HTML does not contain a numeric chat id.",
    )
    p_import.add_argument(
        "--exclude-author-name",
        action="append",
        default=None,
        dest="excluded_author_names",
        help=(
            "Exact HTML author display name to count as raw-only. Repeat for multiple "
            "names; IMPORT_EXCLUDED_AUTHOR_NAMES_JSON is also supported."
        ),
    )
    p_import.add_argument(
        "--with-db",
        action="store_true",
        default=False,
        dest="with_db",
        help=(
            "Enrich report with DB-backed stats: duplicate detection and broken "
            "reply chain count. Prints operator-readable summary instead of JSON."
        ),
    )
    p_import.set_defaults(func=_cmd_import_dry_run)

    # import_apply subcommand
    p_apply = sub.add_parser(
        "import_apply",
        help="Apply a Telegram Desktop export to the DB (checkpoint/resume aware).",
    )
    p_apply.add_argument("export_path", type=str, help="Path to export JSON or HTML directory.")
    p_apply.add_argument(
        "--chat-id",
        type=int,
        default=None,
        dest="chat_id",
        help="Required for HTML apply; HTML does not contain a numeric chat id.",
    )
    p_apply.add_argument(
        "--exclude-author-name",
        action="append",
        default=None,
        dest="excluded_author_names",
        help=(
            "Exact author display name excluded from normalized import while raw provenance "
            "is retained. Repeat for multiple names; required for HTML unless configured "
            "by IMPORT_EXCLUDED_AUTHOR_NAMES_JSON."
        ),
    )
    p_apply.add_argument(
        "--resume",
        action="store_true",
        default=False,
        help="Resume a partial run. Required when a prior partial run exists.",
    )
    p_apply.add_argument(
        "--chunk-size",
        type=int,
        default=None,
        dest="chunk_size",
        help=(
            "Number of messages per DB transaction chunk. "
            "Overrides IMPORT_APPLY_CHUNK_SIZE env var. Default: 500 (from env or built-in)."
        ),
    )
    p_apply.set_defaults(func=_cmd_import_apply)

    # rollback_ingestion_run subcommand
    p_rollback = sub.add_parser(
        "rollback_ingestion_run",
        help="Logically rollback one import run by ingestion_run_id.",
    )
    p_rollback.add_argument("ingestion_run_id", type=int)
    p_rollback.set_defaults(func=_cmd_rollback_ingestion_run)

    # memory_backfill subcommand
    p_backfill = sub.add_parser(
        "memory_backfill",
        help=(
            "Run bounded historical extraction and automatic candidate promotion "
            "for one Telegram chat."
        ),
    )
    p_backfill.add_argument(
        "--chat-id",
        type=int,
        required=True,
        dest="chat_id",
        help="Telegram source chat id (required; all windows remain isolated to it).",
    )
    p_backfill.add_argument(
        "--start",
        type=str,
        default=None,
        help="Inclusive ISO8601 UTC event-time bound; omit with --end to use DB min/max.",
    )
    p_backfill.add_argument(
        "--end",
        type=str,
        default=None,
        help="Exclusive ISO8601 UTC event-time bound; omit with --start to use DB min/max.",
    )
    p_backfill.add_argument(
        "--window-hours",
        type=int,
        default=24,
        dest="window_hours",
        help="Hours per provider request window (1..168; default: 24).",
    )
    p_backfill.add_argument(
        "--max-windows",
        type=int,
        default=400,
        dest="max_windows",
        help="Fail before work if the range needs more windows (1..400; default: 400).",
    )
    p_backfill.add_argument(
        "--actor-user-id",
        type=int,
        default=None,
        dest="actor_user_id",
        help=(
            "Existing Telegram user id used for automatic approvals. Falls back only "
            "to required MEMORY_AUTOMATION_ACTOR_USER_ID."
        ),
    )
    p_backfill.set_defaults(func=_cmd_memory_backfill)

    p_reconcile_extraction = sub.add_parser(
        "memory_reconcile_extraction",
        help="Resolve one non-completed semantic extraction attempt with an audit row.",
    )
    p_reconcile_extraction.add_argument(
        "--run-id",
        type=uuid.UUID,
        required=True,
        dest="run_id",
        help="Extraction run UUID to reconcile.",
    )
    p_reconcile_extraction.add_argument(
        "--action",
        choices=("safe_retry", "risk_accepted_retry", "abandon"),
        required=True,
        help="safe_retry, risk_accepted_retry, or abandon.",
    )
    p_reconcile_extraction.add_argument(
        "--actor-user-id",
        type=int,
        required=True,
        dest="actor_user_id",
        help="Existing Telegram user id recorded as the operator.",
    )
    p_reconcile_extraction.add_argument(
        "--reason",
        required=True,
        help="Bounded operational reason (1..500 characters; no provider payloads or secrets).",
    )
    p_reconcile_extraction.add_argument(
        "--evidence-hash",
        default=None,
        dest="evidence_hash",
        help="Optional lowercase SHA-256 of external audit evidence; never the raw payload.",
    )
    p_reconcile_extraction.add_argument(
        "--accept-possible-duplicate-cost",
        action="store_true",
        default=False,
        dest="accept_possible_duplicate_cost",
        help="Required only for risk_accepted_retry; acknowledges possible duplicate spend.",
    )
    p_reconcile_extraction.add_argument(
        "--accept-memory-gap",
        action="store_true",
        default=False,
        dest="accept_memory_gap",
        help="Required only for abandon; acknowledges the permanent memory gap.",
    )
    p_reconcile_extraction.set_defaults(func=_cmd_memory_reconcile_extraction)

    p_reconcile_image = sub.add_parser(
        "memory_reconcile_image",
        help="Resolve one ambiguous processing image-description claim with an audit row.",
    )
    p_reconcile_image.add_argument(
        "--message-media-id",
        type=int,
        required=True,
        dest="message_media_id",
        help="Positive message_media id whose description is still processing.",
    )
    p_reconcile_image.add_argument(
        "--action",
        choices=("risk_accepted_retry", "abandon"),
        required=True,
        help="risk_accepted_retry or abandon.",
    )
    p_reconcile_image.add_argument(
        "--actor-user-id",
        type=int,
        required=True,
        dest="actor_user_id",
        help="Existing Telegram user id recorded as the operator.",
    )
    p_reconcile_image.add_argument(
        "--reason",
        required=True,
        help="Bounded operational reason (1..500 characters; no provider payloads or secrets).",
    )
    p_reconcile_image.add_argument(
        "--evidence-hash",
        default=None,
        dest="evidence_hash",
        help="Optional lowercase SHA-256 of external audit evidence; never the raw payload.",
    )
    p_reconcile_image.add_argument(
        "--accept-possible-duplicate-cost",
        action="store_true",
        default=False,
        dest="accept_possible_duplicate_cost",
        help="Required only for risk_accepted_retry; acknowledges possible duplicate spend.",
    )
    p_reconcile_image.add_argument(
        "--accept-memory-gap",
        action="store_true",
        default=False,
        dest="accept_memory_gap",
        help="Required only for abandon; acknowledges the permanent image-memory gap.",
    )
    p_reconcile_image.set_defaults(func=_cmd_memory_reconcile_image)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
