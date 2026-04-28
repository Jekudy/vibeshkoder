"""Bot CLI entry points (run via `python -m bot.cli <subcommand> [args...]`).

Currently supported subcommands:
    import_dry_run <export_path>  — parse a Telegram Desktop export JSON and print stats
    import_apply <export_path> [--resume] [--chunk-size N]
                                  — apply a Telegram Desktop export to the DB
                                    (checkpoint/resume infrastructure ready; apply logic in #103)
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
from pathlib import Path

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
    from bot.services.import_parser import parse_export

    path = Path(args.export_path).expanduser().resolve()
    if not path.is_file():
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        return 2

    try:
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
    from bot.services.import_dry_run import parse_export_with_db

    path = Path(args.export_path).expanduser().resolve()
    if not path.is_file():
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        return 2

    # Read chat_id from the export envelope (needed to scope DB queries).
    try:
        chat_id = _read_chat_id_from_envelope(path)
    except (ValueError, OSError) as e:
        print(f"ERROR: could not read chat_id from export envelope: {e}", file=sys.stderr)
        return 2

    import bot.db.engine as _db_engine

    try:
        async with _db_engine.async_session() as session:
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
        f"{offrecord_count} offrecord messages, "
        f"{nomem_count} nomem, "
        f"{report.db_broken_reply_count} broken reply chains."
    )
    print(
        f"Tombstone skip:   {report.tombstone_skip_count} messages match existing tombstones "
        f"(would be skipped on apply)"
    )
    return 0


def _cmd_import_apply(args: argparse.Namespace) -> int:
    """Synchronous entry-point that delegates to the async implementation."""
    return asyncio.run(_cmd_import_apply_async(args))


async def _cmd_import_apply_async(args: argparse.Namespace) -> int:
    """Apply a Telegram Desktop export to the DB.

    Step 1: Resolve path + compute source_hash.
    Step 2: Call init_or_resume_run to get the resume decision.
    Step 3: Dispatch based on decision mode.
    Step 4: Run bot.services.import_apply.run_apply (Stream Delta #103).
    """
    from bot.services.import_checkpoint import finalize_run, init_or_resume_run

    path = Path(args.export_path).expanduser().resolve()
    if not path.is_file():
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        return 2

    # Compute SHA-256 of the export file using streaming reads (1 MB chunks) to
    # avoid OOM on large exports. path.read_bytes() on a multi-GB file would crash.
    try:
        h = hashlib.sha256()
        with path.open("rb") as _fh:
            while _chunk := _fh.read(1024 * 1024):
                h.update(_chunk)
        source_hash = h.hexdigest()
    except OSError as e:
        print(f"ERROR: could not read file: {e}", file=sys.stderr)
        return 2

    # Extract chat_id from the export envelope (first parse of JSON header).
    try:
        chat_id = _read_chat_id_from_envelope(path)
    except (ValueError, OSError) as e:
        print(f"ERROR: could not read chat_id from export envelope: {e}", file=sys.stderr)
        return 2

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

        ingestion_run_id = decision.ingestion_run_id
        resume_point = decision.last_processed_export_msg_id

        try:
            report = await run_apply(
                session,
                ingestion_run_id=ingestion_run_id,
                resume_point=resume_point,
                chunking_config=chunking_config,
            )
        except (ValueError, RuntimeError, OSError, SQLAlchemyError, json.JSONDecodeError) as exc:
            # Fix 5: if run_apply raised a DB error, the session may be in
            # PendingRollback state. Calling finalize_run on an aborted tx would
            # raise a secondary exception masking the original.
            # Strategy: rollback primary session, open a FRESH session for finalize,
            # swallow finalize errors (log only), then re-raise the ORIGINAL exception.
            original_exc = exc
            try:
                await session.rollback()
            except SQLAlchemyError as rb_err:
                logger.warning("rollback failed after run_apply error: %s", rb_err)

            try:
                async with _db_engine.async_session() as fresh_session:
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
                    "finalize_run failed for run %s after apply error (original_type=%s): %s",
                    ingestion_run_id,
                    type(original_exc).__name__,
                    fin_err,
                )
            print(
                f"ERROR: import apply failed: {type(original_exc).__name__}",
                file=sys.stderr,
            )
            return 5

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
            f"errors={report.error_count}, "
            f"chunks={report.chunks_processed}"
        )

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
        "skipped_duplicate_count": report.skipped_duplicate_count,
        "skipped_tombstone_count": report.skipped_tombstone_count,
        "skipped_governance_count": report.skipped_governance_count,
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
    p_apply.add_argument("export_path", type=str, help="Path to the export JSON file.")
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

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
