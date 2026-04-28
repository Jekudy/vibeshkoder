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
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def _cmd_import_dry_run(args: argparse.Namespace) -> int:
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


def _cmd_import_apply(args: argparse.Namespace) -> int:
    """Synchronous entry-point that delegates to the async implementation."""
    return asyncio.run(_cmd_import_apply_async(args))


async def _cmd_import_apply_async(args: argparse.Namespace) -> int:
    """Apply a Telegram Desktop export to the DB.

    Step 1: Resolve path + compute source_hash.
    Step 2: Call init_or_resume_run to get the resume decision.
    Step 3: Dispatch based on decision mode.
    Step 4: Lazily import bot.services.import_apply.run_apply (Stream Delta #103).
            If ImportError → exit 4 with operator-facing message.
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

        # start_fresh or resume_existing → try to call the apply path (#103).
        try:
            from bot.services.import_apply import run_apply  # type: ignore[import]
        except ImportError:
            print(
                "import_apply not yet implemented (#103) — "
                "checkpoint/resume infrastructure (#101) is ready.",
                file=sys.stderr,
            )
            return 4

        # Apply path is available (future #103 case).
        ingestion_run_id = decision.ingestion_run_id
        resume_point = decision.last_processed_export_msg_id
        chunk_size = args.chunk_size

        try:
            await run_apply(
                session,
                ingestion_run_id=ingestion_run_id,
                resume_point=resume_point,
                chunk_size=chunk_size,
            )
        except Exception as exc:
            # Fix 5: if run_apply raised a DB error, the session may be in
            # PendingRollback state. Calling finalize_run on an aborted tx would
            # raise a secondary exception masking the original.
            # Strategy: rollback primary session, open a FRESH session for finalize,
            # swallow finalize errors (log only), then re-raise the ORIGINAL exception.
            original_exc = exc
            try:
                await session.rollback()
            except Exception as rb_err:
                logger.warning("rollback failed after run_apply error: %s", rb_err)

            try:
                async with _db_engine.async_session() as fresh_session:
                    await finalize_run(
                        fresh_session,
                        ingestion_run_id=ingestion_run_id,
                        final_status="failed",
                        error_payload={"error_type": type(original_exc).__name__, "message": str(original_exc)},
                    )
                    await fresh_session.commit()
            except Exception as fin_err:
                logger.warning(
                    "finalize_run failed for run %s after apply error (original: %s): %s",
                    ingestion_run_id,
                    original_exc,
                    fin_err,
                )
            raise original_exc

        await session.commit()

    return 0


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
        default=500,
        dest="chunk_size",
        help="Number of messages per DB transaction chunk (default: 500).",
    )
    p_apply.set_defaults(func=_cmd_import_apply)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
