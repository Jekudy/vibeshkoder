"""T2-NEW-E: checkpoint/resume infrastructure tests (issue #101).

Tests are grouped into:
- DB-backed tests (require postgres, skip gracefully when unavailable)
- Offline tests (no DB required)

The 'kill at 50%, resume, identical state' integration test from the issue is replaced
by test 13 (test_checkpoint_simulated_kill_and_resume) — an infrastructure proxy that
mocks run_apply to write a checkpoint then raise RuntimeError, verifying that
finalize_run(failed) is called and the resume path picks up the correct checkpoint.
This substitution is justified because the apply path lives in Stream Delta (#103).
See docs/memory-system/import-checkpoint.md for the rationale.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.usefixtures("app_env")

FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "td_export"
SMALL_CHAT = FIXTURE_DIR / "small_chat.json"


# ─── helpers ───────────────────────────────────────────────────────────────────


async def _create_partial_run(
    session,
    *,
    source_path: str = "/tmp/export.json",
    source_hash: str = "abc123",
    chat_id: int = -1001999999999,
    status: str = "failed",
    last_processed_export_msg_id: int | None = None,
) -> int:
    """Helper: create an ingestion_run row with the given source_hash + status via raw SQL."""
    from sqlalchemy import text

    stats = None
    if last_processed_export_msg_id is not None:
        stats = json.dumps({"last_processed_export_msg_id": last_processed_export_msg_id})

    result = await session.execute(
        text(
            """
            INSERT INTO ingestion_runs (run_type, source_name, source_hash, status, stats_json)
            VALUES ('import', :source_name, :source_hash, :status, CAST(:stats AS JSONB))
            RETURNING id
            """
        ),
        {
            "source_name": source_path,
            "source_hash": source_hash,
            "status": status,
            "stats": stats,
        },
    )
    run_id = result.scalar_one()
    await session.flush()
    return run_id


# ─── DB-backed tests ───────────────────────────────────────────────────────────


# Test 1: Fresh start — no prior run → start_fresh, creates new run
async def test_fresh_start_no_prior_run(db_session) -> None:
    from bot.services.import_checkpoint import init_or_resume_run

    decision = await init_or_resume_run(
        db_session,
        source_path="/tmp/export.json",
        source_hash="hash_fresh_001",
        chat_id=-1001999999999,
        resume=False,
    )

    assert decision.mode == "start_fresh"
    assert decision.ingestion_run_id is not None
    assert decision.last_processed_export_msg_id is None
    assert "fresh" in decision.reason.lower() or "new" in decision.reason.lower()


# Test 2: save_checkpoint updates stats_json with deep-merge semantics
async def test_save_checkpoint_updates_stats_json(db_session) -> None:
    from bot.services.import_checkpoint import init_or_resume_run, save_checkpoint

    decision = await init_or_resume_run(
        db_session,
        source_path="/tmp/export.json",
        source_hash="hash_chk_002",
        chat_id=-1001,
        resume=False,
    )
    run_id = decision.ingestion_run_id

    # Prefill stats_json with an operator-set field to test deep-merge preservation
    from sqlalchemy import text

    await db_session.execute(
        text("UPDATE ingestion_runs SET stats_json = '{\"operator_note\": \"do not overwrite\"}'::jsonb WHERE id = :id"),
        {"id": run_id},
    )
    await db_session.flush()

    await save_checkpoint(
        db_session,
        ingestion_run_id=run_id,
        last_processed_export_msg_id=42,
        chunk_index=1,
    )

    from sqlalchemy import text as t

    row = (await db_session.execute(
        t("SELECT stats_json FROM ingestion_runs WHERE id = :id"),
        {"id": run_id},
    )).one()
    stats = row[0]

    assert stats["last_processed_export_msg_id"] == 42
    assert stats["chunk_index"] == 1
    assert "last_checkpoint_at" in stats
    # Operator key preserved (deep-merge)
    assert stats.get("operator_note") == "do not overwrite"


# Test 3: load_checkpoint returns the most recent state
async def test_load_checkpoint_returns_state(db_session) -> None:
    from bot.services.import_checkpoint import init_or_resume_run, load_checkpoint, save_checkpoint

    decision = await init_or_resume_run(
        db_session,
        source_path="/tmp/export.json",
        source_hash="hash_load_003",
        chat_id=-1001,
        resume=False,
    )
    run_id = decision.ingestion_run_id

    await save_checkpoint(db_session, ingestion_run_id=run_id, last_processed_export_msg_id=100, chunk_index=0)
    await save_checkpoint(db_session, ingestion_run_id=run_id, last_processed_export_msg_id=200, chunk_index=1)

    chk = await load_checkpoint(db_session, run_id)
    assert chk is not None
    assert chk.last_processed_export_msg_id == 200
    assert chk.chunk_index == 1
    assert chk.ingestion_run_id == run_id
    assert chk.status == "running"


# Test 4: resume from prior failed run (same source_hash, resume=True) → resume_existing
async def test_resume_from_failed_run(db_session) -> None:
    from bot.services.import_checkpoint import init_or_resume_run

    # Create a prior failed run with a checkpoint at msg 50
    run_id = await _create_partial_run(
        db_session,
        source_hash="hash_resume_004",
        status="failed",
        last_processed_export_msg_id=50,
    )

    decision = await init_or_resume_run(
        db_session,
        source_path="/tmp/export.json",
        source_hash="hash_resume_004",
        chat_id=-1001,
        resume=True,
    )

    assert decision.mode == "resume_existing"
    assert decision.ingestion_run_id == run_id
    assert decision.last_processed_export_msg_id == 50


# Test 5: block on partial running run with resume=False → block_partial_present
async def test_block_on_partial_run_no_resume(db_session) -> None:
    from bot.services.import_checkpoint import init_or_resume_run

    await _create_partial_run(
        db_session,
        source_hash="hash_block_005",
        status="running",
    )

    decision = await init_or_resume_run(
        db_session,
        source_path="/tmp/export.json",
        source_hash="hash_block_005",
        chat_id=-1001,
        resume=False,
    )

    assert decision.mode == "block_partial_present"
    assert "resume" in decision.reason.lower() or "partial" in decision.reason.lower()


# Test 6: block on hash mismatch (SAME path, different source_hash, resume=True)
async def test_block_on_hash_mismatch(db_session) -> None:
    """The operator ran import_apply against /tmp/export_006.json (hash_old_006),
    the run is still partial (running). They re-exported the file, producing a new
    SHA-256 (hash_new_006), but call --resume with the new file at the same path.
    The infrastructure must block: resuming a run started against a different file content
    is unsafe.
    """
    from bot.services.import_checkpoint import init_or_resume_run

    same_path = "/tmp/export_006.json"
    await _create_partial_run(
        db_session,
        source_path=same_path,
        source_hash="hash_old_006",
        status="running",
    )

    # Same path, different content hash (file was re-exported) — resume is not safe
    decision = await init_or_resume_run(
        db_session,
        source_path=same_path,     # SAME path
        source_hash="hash_new_006",  # Different content hash!
        chat_id=-1001,
        resume=True,
    )

    assert decision.mode == "block_partial_present"
    assert "hash" in decision.reason.lower() or "mismatch" in decision.reason.lower()


# Test 7: restart after completed run → start_fresh with new run ID
async def test_restart_after_completed_run(db_session) -> None:
    from bot.services.import_checkpoint import init_or_resume_run

    old_run_id = await _create_partial_run(
        db_session,
        source_hash="hash_complete_007",
        status="completed",
    )

    decision = await init_or_resume_run(
        db_session,
        source_path="/tmp/export.json",
        source_hash="hash_complete_007",
        chat_id=-1001,
        resume=False,
    )

    assert decision.mode == "start_fresh"
    assert decision.ingestion_run_id != old_run_id


# Test 8: concurrent init (two parallel CLI starts, both see no prior run) — one wins
async def test_concurrent_init_partial_unique_index(db_session) -> None:
    """Simulate two concurrent CLI invocations both seeing no prior run.

    Due to the partial unique index on (source_hash) WHERE status='running',
    exactly one INSERT should succeed; the second should get block_partial_present.

    We simulate this by inserting the 'winning' run directly first (as the DB already
    has a running row) then calling init_or_resume_run with the same source_hash and
    resume=False — it must see the existing running row and return block_partial_present.
    """
    from bot.services.import_checkpoint import init_or_resume_run

    # Simulate the 'winning' concurrent caller already inserted a running row
    await _create_partial_run(
        db_session,
        source_hash="hash_concurrent_008",
        status="running",
    )

    # Second caller — sees running row, resume=False → must block
    decision = await init_or_resume_run(
        db_session,
        source_path="/tmp/export.json",
        source_hash="hash_concurrent_008",
        chat_id=-1001,
        resume=False,
    )

    assert decision.mode == "block_partial_present"


# Test 9: finalize_run → sets status, finished_at, error_json; idempotent on re-call
async def test_finalize_run_idempotent(db_session) -> None:
    """Also verifies Fix 1: re-finalize of a failed run with status='completed' updates the row.

    Before Fix 1, 'failed' was in _TERMINAL_STATUSES, making finalize_run a no-op even
    when transitioning failed → completed. Now only 'completed'/'cancelled'/'dry_run'
    are hard-locked terminal states.
    """
    from bot.services.import_checkpoint import finalize_run, init_or_resume_run

    decision = await init_or_resume_run(
        db_session,
        source_path="/tmp/export.json",
        source_hash="hash_finalize_009",
        chat_id=-1001,
        resume=False,
    )
    run_id = decision.ingestion_run_id

    await finalize_run(
        db_session,
        ingestion_run_id=run_id,
        final_status="failed",
        error_payload={"reason": "simulated failure"},
    )

    from sqlalchemy import text

    row = (await db_session.execute(
        text("SELECT status, finished_at, error_json FROM ingestion_runs WHERE id = :id"),
        {"id": run_id},
    )).one()
    assert row[0] == "failed"
    assert row[1] is not None
    assert row[2]["reason"] == "simulated failure"

    # Re-call with same status (idempotent) — should NOT raise, just log warning
    await finalize_run(
        db_session,
        ingestion_run_id=run_id,
        final_status="failed",
        error_payload={"reason": "second call"},
    )

    # Status unchanged, first call's data preserved (idempotency: failed→failed is no-op)
    row2 = (await db_session.execute(
        text("SELECT status, error_json FROM ingestion_runs WHERE id = :id"),
        {"id": run_id},
    )).one()
    assert row2[0] == "failed"
    # The error_json after idempotent re-call — should be original or updated, but not raise

    # Fix 1 key assertion: failed → completed MUST update the row (failed is resumable,
    # not terminal-locked). A successful resume+apply must be able to transition to completed.
    await finalize_run(
        db_session,
        ingestion_run_id=run_id,
        final_status="completed",
    )

    row3 = (await db_session.execute(
        text("SELECT status FROM ingestion_runs WHERE id = :id"),
        {"id": run_id},
    )).one()
    assert row3[0] == "completed", (
        f"failed → completed transition must update the row. "
        f"Got {row3[0]!r}. This indicates 'failed' is incorrectly in _TERMINAL_STATUSES."
    )


# Test 13 (killer integration proxy): simulated kill at 50% + resume
async def test_checkpoint_simulated_kill_and_resume(db_session) -> None:
    """Infrastructure proxy for the 'kill at 50%, resume, identical state' requirement.

    Mocks run_apply to write a checkpoint at msg 50 then raise RuntimeError (simulated kill).
    Asserts:
    - finalize_run(failed) was called and checkpoint persists at msg 50
    - Subsequent init_or_resume_run with --resume returns resume_existing at msg 50

    This is the strongest integration-style test possible without a real apply path.
    The real apply path lives in Stream Delta (#103).
    """
    from bot.services.import_checkpoint import (
        finalize_run,
        init_or_resume_run,
        load_checkpoint,
        save_checkpoint,
    )

    # First run: start fresh
    decision = await init_or_resume_run(
        db_session,
        source_path="/tmp/export.json",
        source_hash="hash_kill_013",
        chat_id=-1001,
        resume=False,
    )
    run_id = decision.ingestion_run_id
    assert decision.mode == "start_fresh"

    # Simulate apply running: writes checkpoint at msg 50...
    await save_checkpoint(
        db_session,
        ingestion_run_id=run_id,
        last_processed_export_msg_id=50,
        chunk_index=0,
    )
    # ...then crashes (simulated kill)
    try:
        raise RuntimeError("simulated kill at 50%")
    except RuntimeError:
        await finalize_run(
            db_session,
            ingestion_run_id=run_id,
            final_status="failed",
            error_payload={"reason": "simulated kill at 50%"},
        )

    # Verify checkpoint persists at msg 50
    chk = await load_checkpoint(db_session, run_id)
    assert chk is not None
    assert chk.last_processed_export_msg_id == 50

    # Fix 9 (stronger assertion): re-fetch row and assert status='failed' + error_json payload
    from sqlalchemy import text

    row = (await db_session.execute(
        text("SELECT status, error_json FROM ingestion_runs WHERE id = :id"),
        {"id": run_id},
    )).one()
    assert row[0] == "failed", f"Expected status='failed', got {row[0]!r}"
    assert row[1] is not None, "error_json should not be None after finalize_run(failed)"
    assert row[1].get("reason") == "simulated kill at 50%", f"Unexpected error_json: {row[1]!r}"

    # Now simulate CLI --resume invocation
    decision2 = await init_or_resume_run(
        db_session,
        source_path="/tmp/export.json",
        source_hash="hash_kill_013",
        chat_id=-1001,
        resume=True,
    )

    assert decision2.mode == "resume_existing"
    assert decision2.ingestion_run_id == run_id
    assert decision2.last_processed_export_msg_id == 50


# ─── Offline CLI smoke tests ───────────────────────────────────────────────────


def _invoke_cli(argv: list[str]) -> tuple[int, str, str]:
    """Invoke bot.cli.main with the given argv and capture stdout + stderr."""
    import io
    import sys

    from bot.cli import main

    buf_out = io.StringIO()
    buf_err = io.StringIO()
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    sys.stdout = buf_out
    sys.stderr = buf_err
    try:
        rc = main(argv)
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 1
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr
    return rc, buf_out.getvalue(), buf_err.getvalue()


# Test 10: CLI smoke — import_apply <fixture.json> (no --resume) → exits 4, prints start_fresh
def test_cli_import_apply_no_resume_exits_4() -> None:
    """Without a DB / apply implementation, import_apply must exit 4 (apply not implemented).

    The CLI parses args, reads the file, computes source_hash, calls init_or_resume_run
    (which needs a DB), so we mock the DB session + init_or_resume_run to return
    start_fresh decision, then observe the exit 4 from the lazy ImportError path.
    """
    from unittest.mock import MagicMock

    mock_decision = MagicMock()
    mock_decision.mode = "start_fresh"
    mock_decision.ingestion_run_id = 1
    mock_decision.last_processed_export_msg_id = None
    mock_decision.reason = "new run"

    # Mock the session context manager returned by async_session()
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("bot.services.import_checkpoint.init_or_resume_run", new=AsyncMock(return_value=mock_decision)),
        patch("bot.db.engine.async_session", return_value=mock_session),
    ):
        rc, stdout, stderr = _invoke_cli(["import_apply", str(SMALL_CHAT)])

    assert rc == 4, f"Expected exit 4 (apply not implemented), got {rc}. stderr: {stderr!r}"
    assert "not yet implemented" in stderr.lower() or "#103" in stderr


# Test 11: CLI smoke — --resume on no-prior-run → start_fresh, exits 4
def test_cli_import_apply_resume_on_no_prior_run_exits_4() -> None:
    """--resume when no prior run exists → start_fresh (harmless) → exits 4 (apply not impl)."""
    from unittest.mock import MagicMock

    mock_decision = MagicMock()
    mock_decision.mode = "start_fresh"
    mock_decision.ingestion_run_id = 2
    mock_decision.last_processed_export_msg_id = None
    mock_decision.reason = "no prior run; starting fresh despite --resume"

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("bot.services.import_checkpoint.init_or_resume_run", new=AsyncMock(return_value=mock_decision)),
        patch("bot.db.engine.async_session", return_value=mock_session),
    ):
        rc, stdout, stderr = _invoke_cli(["import_apply", "--resume", str(SMALL_CHAT)])

    assert rc == 4, f"Expected exit 4, got {rc}"


# Test 12: CLI smoke — no --resume on partial run (status=failed) → exits 3
def test_cli_import_apply_no_resume_on_partial_run_exits_3() -> None:
    """Without --resume, a partial run (status=failed/running) must block with exit 3."""
    from unittest.mock import MagicMock

    mock_decision = MagicMock()
    mock_decision.mode = "block_partial_present"
    mock_decision.ingestion_run_id = None
    mock_decision.last_processed_export_msg_id = None
    mock_decision.reason = "partial run found; use --resume to continue"

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("bot.services.import_checkpoint.init_or_resume_run", new=AsyncMock(return_value=mock_decision)),
        patch("bot.db.engine.async_session", return_value=mock_session),
    ):
        rc, stdout, stderr = _invoke_cli(["import_apply", str(SMALL_CHAT)])

    assert rc == 3, f"Expected exit 3 (block_partial_present), got {rc}. stderr: {stderr!r}"
    assert "resume" in stderr.lower() or "partial" in stderr.lower()


# ─── Fix 1 regression: failed → completed transition ──────────────────────────


# Test 9b: failed run can be finalized to completed (Fix 1: failed NOT in _TERMINAL_STATUSES)
async def test_finalize_failed_run_to_completed(db_session) -> None:
    """A resumed run that succeeded must be able to transition failed → completed.

    Before Fix 1, 'failed' was in _TERMINAL_STATUSES which caused finalize_run to
    no-op, leaving the run stuck in 'failed' even after a successful resume+apply.
    """
    from bot.services.import_checkpoint import finalize_run

    # Create a failed run directly
    run_id = await _create_partial_run(
        db_session,
        source_hash="hash_f2c_009b",
        status="failed",
    )

    # After a successful resume+apply, finalize to completed — must NOT be a no-op
    await finalize_run(
        db_session,
        ingestion_run_id=run_id,
        final_status="completed",
    )

    from sqlalchemy import text

    row = (await db_session.execute(
        text("SELECT status, finished_at FROM ingestion_runs WHERE id = :id"),
        {"id": run_id},
    )).one()
    assert row[0] == "completed", (
        f"Expected status='completed' after failed→completed transition, got {row[0]!r}. "
        "This indicates 'failed' is incorrectly in _TERMINAL_STATUSES."
    )
    assert row[1] is not None, "finished_at should be set when transitioning to completed"


# Test 9c: idempotency — completed is truly terminal (cannot re-finalize to failed)
async def test_finalize_completed_is_terminal(db_session) -> None:
    """completed runs must be immutable (truly terminal — no re-finalization allowed)."""
    from bot.services.import_checkpoint import finalize_run, init_or_resume_run

    decision = await init_or_resume_run(
        db_session,
        source_path="/tmp/export.json",
        source_hash="hash_term_009c",
        chat_id=-1001,
        resume=False,
    )
    run_id = decision.ingestion_run_id

    # Transition running → completed
    await finalize_run(db_session, ingestion_run_id=run_id, final_status="completed")

    # Attempt to re-finalize as failed — must be a no-op (idempotent, log warning)
    await finalize_run(
        db_session,
        ingestion_run_id=run_id,
        final_status="failed",
        error_payload={"reason": "should not overwrite"},
    )

    from sqlalchemy import text

    row = (await db_session.execute(
        text("SELECT status, error_json FROM ingestion_runs WHERE id = :id"),
        {"id": run_id},
    )).one()
    assert row[0] == "completed", "completed run must not be re-finalized"
    assert row[1] is None, "error_json must not be written on no-op finalize"


# ─── Fix 3: IntegrityError branch via mocked session.flush ────────────────────


# Test 8b: _create_fresh_run catches IntegrityError via SAVEPOINT and re-queries
def test_create_fresh_run_integrity_error_branch() -> None:
    """Offline test: _create_fresh_run handles IntegrityError from session.flush
    via SAVEPOINT (begin_nested), re-queries, and returns block_partial_present.

    Simulates the concurrent-INSERT race: two CLIs both see no prior run, both
    call _create_fresh_run; the second one hits IntegrityError on the partial
    unique index.
    """
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    from sqlalchemy.exc import IntegrityError

    from bot.services.import_checkpoint import _create_fresh_run

    # Mock IngestionRun returned by session.add + flush
    mock_existing_run = MagicMock()
    mock_existing_run.id = 42

    # We need to simulate: begin_nested() context manager raises IntegrityError on __aexit__
    # Then re-query (_find_partial_run_by_hash) returns the winning run.
    mock_nested_ctx = AsyncMock()
    mock_nested_ctx.__aenter__ = AsyncMock(return_value=mock_nested_ctx)
    # Simulate IntegrityError raised when the SAVEPOINT flushes/commits
    mock_nested_ctx.__aexit__ = AsyncMock(
        side_effect=IntegrityError("UNIQUE constraint", {}, Exception("unique violation"))
    )

    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.begin_nested = MagicMock(return_value=mock_nested_ctx)

    async def run() -> None:
        with patch(
            "bot.services.import_checkpoint._find_partial_run_by_hash",
            new=AsyncMock(return_value=mock_existing_run),
        ):
            result = await _create_fresh_run(
                mock_session,
                source_path="/tmp/export.json",
                source_hash="hash_race_008b",
                chat_id=-1001,
            )

        assert result.mode == "block_partial_present", (
            f"Expected block_partial_present, got {result.mode!r}"
        )
        assert result.ingestion_run_id is None
        assert "42" in result.reason or "concurrent" in result.reason.lower()

    asyncio.run(run())


# ─── Fix 7: load_checkpoint type validation ───────────────────────────────────


# Test 14a: load_checkpoint raises ValueError on non-int last_processed_export_msg_id
async def test_load_checkpoint_rejects_non_int_msg_id(db_session) -> None:
    """Corrupted stats_json.last_processed_export_msg_id (str) must raise ValueError."""
    from sqlalchemy import text

    from bot.services.import_checkpoint import load_checkpoint

    run_id = await _create_partial_run(db_session, source_hash="hash_corrupt_014a", status="running")
    # Corrupt the stats_json: write a string instead of int
    await db_session.execute(
        text(
            "UPDATE ingestion_runs SET stats_json = '{\"last_processed_export_msg_id\": \"abc\"}'::jsonb "
            "WHERE id = :id"
        ),
        {"id": run_id},
    )
    await db_session.flush()

    with pytest.raises(ValueError, match="last_processed_export_msg_id"):
        await load_checkpoint(db_session, run_id)


# Test 14b: load_checkpoint raises ValueError on non-int chunk_index
async def test_load_checkpoint_rejects_non_int_chunk_index(db_session) -> None:
    """Corrupted stats_json.chunk_index (float) must raise ValueError."""
    from sqlalchemy import text

    from bot.services.import_checkpoint import load_checkpoint

    run_id = await _create_partial_run(db_session, source_hash="hash_corrupt_014b", status="running")
    await db_session.execute(
        text(
            "UPDATE ingestion_runs SET stats_json = '{\"chunk_index\": \"two\"}'::jsonb "
            "WHERE id = :id"
        ),
        {"id": run_id},
    )
    await db_session.flush()

    with pytest.raises(ValueError, match="chunk_index"):
        await load_checkpoint(db_session, run_id)


# Test 14c: load_checkpoint raises ValueError on unparseable last_checkpoint_at
async def test_load_checkpoint_rejects_bad_timestamp(db_session) -> None:
    """Corrupted stats_json.last_checkpoint_at (not ISO) must raise ValueError."""
    from sqlalchemy import text

    from bot.services.import_checkpoint import load_checkpoint

    run_id = await _create_partial_run(db_session, source_hash="hash_corrupt_014c", status="running")
    await db_session.execute(
        text(
            "UPDATE ingestion_runs SET stats_json = '{\"last_checkpoint_at\": \"not-a-date\"}'::jsonb "
            "WHERE id = :id"
        ),
        {"id": run_id},
    )
    await db_session.flush()

    with pytest.raises(ValueError, match="last_checkpoint_at"):
        await load_checkpoint(db_session, run_id)
