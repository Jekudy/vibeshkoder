"""T2-NEW-F: Import apply chunking / rate-limit config tests (issue #102).

Tests are grouped into:
- Offline tests: ChunkingConfig, load_chunking_config (no DB required)
- DB-backed tests: acquire_advisory_lock (skip without postgres)
- CLI wiring tests: assert chunk_size from env is passed to run_apply (mocked)
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("app_env")


# ─── Test 1: load_chunking_config defaults ────────────────────────────────────


def test_load_chunking_config_defaults() -> None:
    """No env vars → defaults: chunk_size=500, sleep_ms=100, advisory_lock=True."""
    from bot.services.import_chunking import load_chunking_config

    config = load_chunking_config(env={})

    assert config.chunk_size == 500
    assert config.sleep_between_chunks_ms == 100
    assert config.use_advisory_lock is True


# ─── Test 2: load_chunking_config honours env vars ───────────────────────────


def test_load_chunking_config_honours_env_vars() -> None:
    """Env vars are read and type-coerced correctly."""
    from bot.services.import_chunking import load_chunking_config

    env = {
        "IMPORT_APPLY_CHUNK_SIZE": "250",
        "IMPORT_APPLY_SLEEP_MS": "50",
        "IMPORT_APPLY_ADVISORY_LOCK": "false",
    }
    config = load_chunking_config(env=env)

    assert config.chunk_size == 250
    assert config.sleep_between_chunks_ms == 50
    assert config.use_advisory_lock is False


def test_load_chunking_config_advisory_lock_true_variants() -> None:
    """Env var '1', 'true', 'True', 'yes' all parse as True."""
    from bot.services.import_chunking import load_chunking_config

    for truthy in ("1", "true", "True", "yes", "YES"):
        config = load_chunking_config(env={"IMPORT_APPLY_ADVISORY_LOCK": truthy})
        assert config.use_advisory_lock is True, f"Expected True for {truthy!r}"

    for falsy in ("0", "false", "False", "no", "NO"):
        config = load_chunking_config(env={"IMPORT_APPLY_ADVISORY_LOCK": falsy})
        assert config.use_advisory_lock is False, f"Expected False for {falsy!r}"


# ─── Test 3: load_chunking_config validation ─────────────────────────────────


def test_load_chunking_config_rejects_zero_chunk_size() -> None:
    """chunk_size=0 is out of range [1, 10000] — must raise ValueError."""
    from bot.services.import_chunking import load_chunking_config

    with pytest.raises(ValueError, match="chunk_size"):
        load_chunking_config(env={"IMPORT_APPLY_CHUNK_SIZE": "0"})


def test_load_chunking_config_rejects_negative_chunk_size() -> None:
    """chunk_size=-1 is out of range — must raise ValueError."""
    from bot.services.import_chunking import load_chunking_config

    with pytest.raises(ValueError, match="chunk_size"):
        load_chunking_config(env={"IMPORT_APPLY_CHUNK_SIZE": "-1"})


def test_load_chunking_config_rejects_excessive_chunk_size() -> None:
    """chunk_size=10001 exceeds max 10000 — must raise ValueError."""
    from bot.services.import_chunking import load_chunking_config

    with pytest.raises(ValueError, match="chunk_size"):
        load_chunking_config(env={"IMPORT_APPLY_CHUNK_SIZE": "10001"})


def test_load_chunking_config_accepts_boundary_chunk_sizes() -> None:
    """chunk_size=1 and chunk_size=10000 are both valid (boundary inclusive)."""
    from bot.services.import_chunking import load_chunking_config

    config_min = load_chunking_config(env={"IMPORT_APPLY_CHUNK_SIZE": "1"})
    assert config_min.chunk_size == 1

    config_max = load_chunking_config(env={"IMPORT_APPLY_CHUNK_SIZE": "10000"})
    assert config_max.chunk_size == 10000


def test_load_chunking_config_rejects_negative_sleep_ms() -> None:
    """sleep_ms=-1 is out of range [0, 60000] — must raise ValueError."""
    from bot.services.import_chunking import load_chunking_config

    with pytest.raises(ValueError, match="sleep_between_chunks_ms"):
        load_chunking_config(env={"IMPORT_APPLY_SLEEP_MS": "-1"})


def test_load_chunking_config_rejects_excessive_sleep_ms() -> None:
    """sleep_ms=60001 exceeds max 60000 — must raise ValueError."""
    from bot.services.import_chunking import load_chunking_config

    with pytest.raises(ValueError, match="sleep_between_chunks_ms"):
        load_chunking_config(env={"IMPORT_APPLY_SLEEP_MS": "60001"})


def test_load_chunking_config_accepts_zero_sleep_ms() -> None:
    """sleep_ms=0 is valid (no sleep between chunks)."""
    from bot.services.import_chunking import load_chunking_config

    config = load_chunking_config(env={"IMPORT_APPLY_SLEEP_MS": "0"})
    assert config.sleep_between_chunks_ms == 0


def test_load_chunking_config_rejects_non_int_chunk_size() -> None:
    """Non-integer IMPORT_APPLY_CHUNK_SIZE must raise ValueError."""
    from bot.services.import_chunking import load_chunking_config

    with pytest.raises(ValueError, match="chunk_size"):
        load_chunking_config(env={"IMPORT_APPLY_CHUNK_SIZE": "abc"})


def test_load_chunking_config_rejects_non_int_sleep_ms() -> None:
    """Non-integer IMPORT_APPLY_SLEEP_MS must raise ValueError."""
    from bot.services.import_chunking import load_chunking_config

    with pytest.raises(ValueError, match="sleep_between_chunks_ms"):
        load_chunking_config(env={"IMPORT_APPLY_SLEEP_MS": "fast"})


# ─── Test 3b: ChunkingConfig.__post_init__ validation ────────────────────────


def test_chunking_config_rejects_invalid_chunk_size_in_constructor() -> None:
    """ChunkingConfig constructor rejects chunk_size outside [1, 10000]."""
    from bot.services.import_chunking import ChunkingConfig

    with pytest.raises(ValueError, match="chunk_size"):
        ChunkingConfig(chunk_size=0, sleep_between_chunks_ms=100, use_advisory_lock=True)

    with pytest.raises(ValueError, match="chunk_size"):
        ChunkingConfig(chunk_size=-1, sleep_between_chunks_ms=100, use_advisory_lock=True)

    with pytest.raises(ValueError, match="chunk_size"):
        ChunkingConfig(chunk_size=10001, sleep_between_chunks_ms=100, use_advisory_lock=True)


def test_chunking_config_rejects_invalid_sleep_ms_in_constructor() -> None:
    """ChunkingConfig constructor rejects sleep_between_chunks_ms outside [0, 60000]."""
    from bot.services.import_chunking import ChunkingConfig

    with pytest.raises(ValueError, match="sleep_between_chunks_ms"):
        ChunkingConfig(chunk_size=500, sleep_between_chunks_ms=-1, use_advisory_lock=True)

    with pytest.raises(ValueError, match="sleep_between_chunks_ms"):
        ChunkingConfig(chunk_size=500, sleep_between_chunks_ms=60001, use_advisory_lock=True)


def test_chunking_config_accepts_boundary_values() -> None:
    """ChunkingConfig accepts boundary values: chunk_size=1, 10000; sleep_ms=0, 60000."""
    from bot.services.import_chunking import ChunkingConfig

    c = ChunkingConfig(chunk_size=1, sleep_between_chunks_ms=0, use_advisory_lock=False)
    assert c.chunk_size == 1
    assert c.sleep_between_chunks_ms == 0

    c2 = ChunkingConfig(chunk_size=10000, sleep_between_chunks_ms=60000, use_advisory_lock=True)
    assert c2.chunk_size == 10000
    assert c2.sleep_between_chunks_ms == 60000


def test_cli_chunk_size_zero_rejected() -> None:
    """CLI --chunk-size 0 must produce non-zero exit (via ChunkingConfig.__post_init__)."""
    import io
    import sys
    from pathlib import Path
    from unittest.mock import AsyncMock, MagicMock, patch

    FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "td_export"
    SMALL_CHAT = FIXTURE_DIR / "small_chat.json"

    mock_decision = MagicMock()
    mock_decision.mode = "start_fresh"
    mock_decision.ingestion_run_id = 9
    mock_decision.last_processed_export_msg_id = None
    mock_decision.reason = "new run"

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    run_apply_mock = AsyncMock(return_value=None)

    from types import ModuleType

    fake_module = ModuleType("bot.services.import_apply")
    fake_module.run_apply = run_apply_mock

    from bot.cli import main

    buf_err = io.StringIO()
    old_stderr = sys.stderr
    sys.stderr = buf_err

    exit_code = None
    try:
        with (
            patch("bot.services.import_checkpoint.init_or_resume_run", new=AsyncMock(return_value=mock_decision)),
            patch("bot.db.engine.async_session", return_value=mock_session),
            patch.dict("sys.modules", {"bot.services.import_apply": fake_module}),
        ):
            try:
                exit_code = main(["import_apply", str(SMALL_CHAT), "--chunk-size", "0"])
            except SystemExit as e:
                exit_code = e.code
    finally:
        sys.stderr = old_stderr

    assert exit_code not in (0, None), (
        f"--chunk-size 0 must produce a non-zero exit; got {exit_code!r}. "
        f"stderr: {buf_err.getvalue()!r}"
    )


def test_cli_chunk_size_negative_rejected() -> None:
    """CLI --chunk-size -1 must produce non-zero exit."""
    import io
    import sys
    from pathlib import Path
    from unittest.mock import AsyncMock, MagicMock, patch

    FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "td_export"
    SMALL_CHAT = FIXTURE_DIR / "small_chat.json"

    mock_decision = MagicMock()
    mock_decision.mode = "start_fresh"
    mock_decision.ingestion_run_id = 10
    mock_decision.last_processed_export_msg_id = None
    mock_decision.reason = "new run"

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    run_apply_mock = AsyncMock(return_value=None)

    from types import ModuleType

    fake_module = ModuleType("bot.services.import_apply")
    fake_module.run_apply = run_apply_mock

    from bot.cli import main

    buf_err = io.StringIO()
    old_stderr = sys.stderr
    sys.stderr = buf_err

    exit_code = None
    try:
        with (
            patch("bot.services.import_checkpoint.init_or_resume_run", new=AsyncMock(return_value=mock_decision)),
            patch("bot.db.engine.async_session", return_value=mock_session),
            patch.dict("sys.modules", {"bot.services.import_apply": fake_module}),
        ):
            try:
                exit_code = main(["import_apply", str(SMALL_CHAT), "--chunk-size", "-1"])
            except SystemExit as e:
                exit_code = e.code
    finally:
        sys.stderr = old_stderr

    assert exit_code not in (0, None), (
        f"--chunk-size -1 must produce a non-zero exit; got {exit_code!r}"
    )


def test_cli_chunk_size_excessive_rejected() -> None:
    """CLI --chunk-size 10001 must produce non-zero exit."""
    import io
    import sys
    from pathlib import Path
    from unittest.mock import AsyncMock, MagicMock, patch

    FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "td_export"
    SMALL_CHAT = FIXTURE_DIR / "small_chat.json"

    mock_decision = MagicMock()
    mock_decision.mode = "start_fresh"
    mock_decision.ingestion_run_id = 11
    mock_decision.last_processed_export_msg_id = None
    mock_decision.reason = "new run"

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    run_apply_mock = AsyncMock(return_value=None)

    from types import ModuleType

    fake_module = ModuleType("bot.services.import_apply")
    fake_module.run_apply = run_apply_mock

    from bot.cli import main

    buf_err = io.StringIO()
    old_stderr = sys.stderr
    sys.stderr = buf_err

    exit_code = None
    try:
        with (
            patch("bot.services.import_checkpoint.init_or_resume_run", new=AsyncMock(return_value=mock_decision)),
            patch("bot.db.engine.async_session", return_value=mock_session),
            patch.dict("sys.modules", {"bot.services.import_apply": fake_module}),
        ):
            try:
                exit_code = main(["import_apply", str(SMALL_CHAT), "--chunk-size", "10001"])
            except SystemExit as e:
                exit_code = e.code
    finally:
        sys.stderr = old_stderr

    assert exit_code not in (0, None), (
        f"--chunk-size 10001 must produce a non-zero exit; got {exit_code!r}"
    )


def test_cli_chunk_size_overrides_invalid_env() -> None:
    """CLI --chunk-size 100 succeeds even when IMPORT_APPLY_CHUNK_SIZE=abc (invalid env).

    Fix 2: CLI override is applied BEFORE load_chunking_config validation.
    """
    import io
    import sys
    from pathlib import Path
    from unittest.mock import AsyncMock, MagicMock, patch

    from bot.services.import_chunking import ChunkingConfig

    FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "td_export"
    SMALL_CHAT = FIXTURE_DIR / "small_chat.json"

    mock_decision = MagicMock()
    mock_decision.mode = "start_fresh"
    mock_decision.ingestion_run_id = 12
    mock_decision.last_processed_export_msg_id = None
    mock_decision.reason = "new run"

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    run_apply_mock = AsyncMock(return_value=None)

    from types import ModuleType

    fake_module = ModuleType("bot.services.import_apply")
    fake_module.run_apply = run_apply_mock

    from bot.cli import main

    buf_err = io.StringIO()
    old_stderr = sys.stderr
    sys.stderr = buf_err

    exit_code = None
    try:
        with (
            patch("bot.services.import_checkpoint.init_or_resume_run", new=AsyncMock(return_value=mock_decision)),
            patch("bot.db.engine.async_session", return_value=mock_session),
            # env has invalid IMPORT_APPLY_CHUNK_SIZE but CLI provides valid --chunk-size
            patch.dict("os.environ", {"IMPORT_APPLY_CHUNK_SIZE": "abc"}),
            patch.dict("sys.modules", {"bot.services.import_apply": fake_module}),
        ):
            try:
                exit_code = main(["import_apply", str(SMALL_CHAT), "--chunk-size", "100"])
            except SystemExit as e:
                exit_code = e.code
    finally:
        sys.stderr = old_stderr

    assert run_apply_mock.called, (
        f"run_apply must be called when --chunk-size 100 overrides invalid env. "
        f"exit_code={exit_code!r}, stderr={buf_err.getvalue()!r}"
    )
    kwargs = run_apply_mock.call_args.kwargs if run_apply_mock.call_args.kwargs else {}
    chunking_config = kwargs.get("chunking_config")
    assert isinstance(chunking_config, ChunkingConfig)
    assert chunking_config.chunk_size == 100


# ─── Test 4: acquire_advisory_lock happy path (DB-backed) ────────────────────


async def test_acquire_advisory_lock_happy_path(postgres_engine) -> None:
    """Lock is taken on enter, work executes, lock is released on exit.

    Uses a single AsyncConnection (connection-scoped lock semantics).
    Verifies that work inside the context manager completes normally and
    the context manager exits without raising.
    """
    from bot.services.import_chunking import acquire_advisory_lock

    ingestion_run_id = 12345
    work_done = []

    async with postgres_engine.connect() as conn:
        async with acquire_advisory_lock(conn, ingestion_run_id):
            work_done.append("step1")
            work_done.append("step2")

    assert work_done == ["step1", "step2"]


async def test_acquire_advisory_lock_releases_on_exception(postgres_engine) -> None:
    """Lock is released (pg_advisory_unlock) even when the body raises.

    Verifies from a SEPARATE DB connection that the lock is truly released —
    not merely re-acquirable from the same connection (stacked locks would
    allow same-connection re-acquisition even if unlock was missed).
    """
    from bot.services.import_chunking import _derive_lock_id, acquire_advisory_lock
    from sqlalchemy import text

    ingestion_run_id = 99991
    lock_id = _derive_lock_id(ingestion_run_id)

    # Acquire and raise inside conn1 — should release on exception exit.
    async with postgres_engine.connect() as conn1:
        with pytest.raises(RuntimeError, match="boom"):
            async with acquire_advisory_lock(conn1, ingestion_run_id):
                raise RuntimeError("boom")

    # Verify from a SEPARATE connection that the lock is released.
    async with postgres_engine.connect() as conn2:
        result = await conn2.execute(
            text("SELECT pg_try_advisory_lock(:id)"), {"id": lock_id}
        )
        assert result.scalar() is True, "lock should be releasable from another connection"
        # Cleanup: release the lock we just acquired on conn2.
        await conn2.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": lock_id})


# ─── Test 5: acquire_advisory_lock — deterministic lock_id ───────────────────


def test_advisory_lock_id_deterministic() -> None:
    """Same ingestion_run_id → same lock_id; different ids → different lock_ids."""
    from bot.services.import_chunking import _derive_lock_id

    id1 = _derive_lock_id(42)
    id2 = _derive_lock_id(42)
    id3 = _derive_lock_id(43)

    assert id1 == id2, "Same ingestion_run_id must produce same lock_id"
    assert id1 != id3, "Different ingestion_run_ids must produce different lock_ids"

    # lock_id must be a valid PostgreSQL int8 (fits in signed 64-bit int)
    assert -(2**63) <= id1 <= (2**63 - 1)
    assert -(2**63) <= id3 <= (2**63 - 1)


def test_advisory_lock_id_is_int() -> None:
    """_derive_lock_id must return a Python int."""
    from bot.services.import_chunking import _derive_lock_id

    lock_id = _derive_lock_id(0)
    assert isinstance(lock_id, int)


# ─── Test 6: CLI wiring — env IMPORT_APPLY_CHUNK_SIZE overrides default ──────


def _make_fake_import_apply_module(run_apply_mock):
    """Create a fake bot.services.import_apply module with the given run_apply mock."""
    from types import ModuleType

    fake_module = ModuleType("bot.services.import_apply")
    fake_module.run_apply = run_apply_mock
    return fake_module


def test_cli_import_apply_passes_chunking_config_to_run_apply() -> None:
    """CLI: IMPORT_APPLY_CHUNK_SIZE=100 → ChunkingConfig.chunk_size=100 passed to run_apply.

    Verifies the CLI wires ChunkingConfig into the lazy run_apply call.
    run_apply is mocked via sys.modules injection; we assert it was called with the
    expected chunking_config kwarg.
    """
    import io
    import sys
    from pathlib import Path
    from unittest.mock import AsyncMock, MagicMock, patch

    from bot.services.import_chunking import ChunkingConfig

    FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "td_export"
    SMALL_CHAT = FIXTURE_DIR / "small_chat.json"

    mock_decision = MagicMock()
    mock_decision.mode = "start_fresh"
    mock_decision.ingestion_run_id = 7
    mock_decision.last_processed_export_msg_id = None
    mock_decision.reason = "new run"

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    run_apply_mock = AsyncMock(return_value=None)
    fake_module = _make_fake_import_apply_module(run_apply_mock)

    from bot.cli import main

    buf_out = io.StringIO()
    buf_err = io.StringIO()
    old_stdout, old_stderr = sys.stdout, sys.stderr
    sys.stdout = buf_out
    sys.stderr = buf_err

    try:
        with (
            patch("bot.services.import_checkpoint.init_or_resume_run", new=AsyncMock(return_value=mock_decision)),
            patch("bot.db.engine.async_session", return_value=mock_session),
            patch.dict("os.environ", {"IMPORT_APPLY_CHUNK_SIZE": "100"}),
            patch.dict("sys.modules", {"bot.services.import_apply": fake_module}),
        ):
            try:
                main(["import_apply", str(SMALL_CHAT)])
            except SystemExit:
                pass
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr

    # run_apply was called
    assert run_apply_mock.called, "run_apply must be called when apply is available"

    # Extract the chunking_config kwarg
    call_kwargs = run_apply_mock.call_args
    assert call_kwargs is not None
    kwargs = call_kwargs.kwargs if call_kwargs.kwargs else {}
    # chunking_config should be a ChunkingConfig with chunk_size=100 (env override)
    chunking_config = kwargs.get("chunking_config")
    assert chunking_config is not None, f"run_apply must receive chunking_config kwarg. Got kwargs: {kwargs}"
    assert isinstance(chunking_config, ChunkingConfig)
    assert chunking_config.chunk_size == 100, (
        f"chunk_size should be 100 (from env IMPORT_APPLY_CHUNK_SIZE=100), "
        f"got {chunking_config.chunk_size}"
    )


def test_cli_import_apply_chunk_size_cli_arg_overrides_env() -> None:
    """--chunk-size CLI arg overrides IMPORT_APPLY_CHUNK_SIZE env var."""
    import io
    import sys
    from pathlib import Path
    from unittest.mock import AsyncMock, MagicMock, patch

    from bot.services.import_chunking import ChunkingConfig

    FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "td_export"
    SMALL_CHAT = FIXTURE_DIR / "small_chat.json"

    mock_decision = MagicMock()
    mock_decision.mode = "start_fresh"
    mock_decision.ingestion_run_id = 8
    mock_decision.last_processed_export_msg_id = None
    mock_decision.reason = "new run"

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    run_apply_mock = AsyncMock(return_value=None)
    fake_module = _make_fake_import_apply_module(run_apply_mock)

    from bot.cli import main

    buf_out = io.StringIO()
    buf_err = io.StringIO()
    old_stdout, old_stderr = sys.stdout, sys.stderr
    sys.stdout = buf_out
    sys.stderr = buf_err

    try:
        with (
            patch("bot.services.import_checkpoint.init_or_resume_run", new=AsyncMock(return_value=mock_decision)),
            patch("bot.db.engine.async_session", return_value=mock_session),
            patch.dict("os.environ", {"IMPORT_APPLY_CHUNK_SIZE": "300"}),
            patch.dict("sys.modules", {"bot.services.import_apply": fake_module}),
        ):
            try:
                # CLI arg --chunk-size 75 should override env IMPORT_APPLY_CHUNK_SIZE=300
                main(["import_apply", str(SMALL_CHAT), "--chunk-size", "75"])
            except SystemExit:
                pass
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr

    assert run_apply_mock.called, "run_apply must be called"
    call_kwargs = run_apply_mock.call_args
    kwargs = call_kwargs.kwargs if call_kwargs.kwargs else {}
    chunking_config = kwargs.get("chunking_config")
    assert chunking_config is not None, f"run_apply must receive chunking_config kwarg. Got: {kwargs}"
    assert isinstance(chunking_config, ChunkingConfig)
    assert chunking_config.chunk_size == 75, (
        f"--chunk-size 75 CLI arg must override env IMPORT_APPLY_CHUNK_SIZE=300. "
        f"Got chunk_size={chunking_config.chunk_size}"
    )
