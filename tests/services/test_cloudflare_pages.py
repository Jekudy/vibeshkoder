"""Cloudflare Pages publisher acceptance tests.

These tests use an isolated PostgreSQL database because publisher audit rows must
commit independently from the caller and advisory locks are PostgreSQL-specific.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine.url import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from bot.services.cloudflare_pages import (
    CloudflarePagesConfig,
    CloudflarePagesPublishError,
    load_cloudflare_pages_config,
    publish_static_generation,
)
from bot.services.wiki_static_export import StaticWikiPage, audit_static_tree, export_static_site
from tests.conftest import DEFAULT_LOCAL_POSTGRES_URL


def _base_test_url() -> URL:
    return make_url(
        os.environ.get("TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or DEFAULT_LOCAL_POSTGRES_URL
    )


def _asyncpg_kwargs(url: URL, *, database: str | None = None) -> dict[str, object]:
    return {
        "user": url.username,
        "password": url.password,
        "host": url.host or "127.0.0.1",
        "port": url.port or 5432,
        "database": database or url.database,
    }


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


async def _create_database(admin_url: URL, database_name: str) -> None:
    connection = await asyncpg.connect(**_asyncpg_kwargs(admin_url, database="postgres"))
    try:
        await connection.execute(f"CREATE DATABASE {_quote_identifier(database_name)}")
    finally:
        await connection.close()


async def _drop_database(admin_url: URL, database_name: str) -> None:
    connection = await asyncpg.connect(**_asyncpg_kwargs(admin_url, database="postgres"))
    try:
        await connection.execute(
            """
            SELECT pg_terminate_backend(pid)
              FROM pg_stat_activity
             WHERE datname = $1 AND pid <> pg_backend_pid()
            """,
            database_name,
        )
        await connection.execute(f"DROP DATABASE IF EXISTS {_quote_identifier(database_name)}")
    finally:
        await connection.close()


_AUDIT_TABLE_DDL = """
CREATE TABLE wiki_static_deployments (
    id BIGSERIAL PRIMARY KEY,
    manifest_sha256 VARCHAR(64) NOT NULL,
    project VARCHAR(255) NOT NULL,
    branch VARCHAR(255) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    deployment_url TEXT NULL,
    error_code VARCHAR(64) NULL,
    error_class VARCHAR(255) NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_wiki_static_deployments_status
        CHECK (status IN ('pending', 'succeeded', 'failed')),
    CONSTRAINT ck_wiki_static_deployments_manifest_sha256
        CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_wiki_static_deployments_terminal_state CHECK (
        (status = 'pending' AND finished_at IS NULL AND deployment_url IS NULL
            AND error_code IS NULL AND error_class IS NULL)
        OR (status = 'succeeded' AND finished_at IS NOT NULL AND deployment_url IS NOT NULL
            AND error_code IS NULL AND error_class IS NULL)
        OR (status = 'failed' AND finished_at IS NOT NULL AND deployment_url IS NULL
            AND error_code IS NOT NULL)
    )
)
"""


@pytest_asyncio.fixture(scope="module")
async def publisher_engine() -> AsyncIterator[AsyncEngine]:
    base_url = _base_test_url()
    database_name = f"shkoder_cf_pages_{uuid.uuid4().hex[:10]}"
    try:
        await _create_database(base_url, database_name)
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"cannot create temporary postgres database: {exc!s}")

    database_url = base_url.set(database=database_name).render_as_string(hide_password=False)
    engine = create_async_engine(database_url, echo=False, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(_AUDIT_TABLE_DDL))
            await connection.execute(
                text(
                    """
                    CREATE INDEX ix_wiki_static_deployments_success_lookup
                    ON wiki_static_deployments (manifest_sha256, project, branch)
                    WHERE status = 'succeeded'
                    """
                )
            )
        yield engine
    finally:
        await engine.dispose()
        await _drop_database(base_url, database_name)


@pytest_asyncio.fixture()
async def clean_audit_rows(publisher_engine: AsyncEngine) -> AsyncIterator[None]:
    async with publisher_engine.begin() as connection:
        await connection.execute(text("DELETE FROM wiki_static_deployments"))
    yield


@pytest.fixture()
def pinned_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    node = tmp_path / "node"
    node.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    node.chmod(0o755)
    wrangler = tmp_path / "wrangler.js"
    wrangler.write_text("// pinned fake runtime\n", encoding="utf-8")

    # ``app_env`` deliberately evicts bot modules from ``sys.modules``.  Pytest
    # may therefore keep this file's imported callable while a later import
    # returns a different module object.  Patch the globals used by the actual
    # callable under test, not whichever module object is currently registered.
    publisher_globals = publish_static_generation.__globals__
    monkeypatch.setitem(publisher_globals, "_NODE_BINARY", node)
    monkeypatch.setitem(publisher_globals, "_WRANGLER_SCRIPT", wrangler)
    return node, wrangler


@pytest.fixture()
def config() -> CloudflarePagesConfig:
    return CloudflarePagesConfig(
        api_token="cf-secret-token",
        account_id="cf-account-id",
        project="shkoder-wiki",
        branch="main",
        base_url="https://wiki.example.test",
    )


def _static_generation(
    tmp_path: Path,
    *,
    body_markdown: str = "Проверенный текст без внешних ссылок. [^mv:1]",
    site_title: str = "Шкодер Wiki",
):
    return export_static_site(
        [
            StaticWikiPage(
                slug="memory-system",
                title="Система памяти",
                body_markdown=body_markdown,
                revision_seq=1,
            )
        ],
        publish_dir=tmp_path / "published",
        site_title=site_title,
        publication_authorized=True,
    )


def _tree_snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(root).as_posix(): None if path.is_dir() else path.read_bytes()
        for path in root.rglob("*")
    }


class _FakeProcess:
    def __init__(
        self,
        *,
        returncode: int,
        gate: asyncio.Event | None = None,
        started: asyncio.Event | None = None,
    ) -> None:
        self.returncode = returncode
        self._gate = gate
        self._started = started
        self.killed = False
        self.wait_calls = 0
        self.reaped = False

    async def wait(self) -> int:
        self.wait_calls += 1
        if self._started is not None:
            self._started.set()
        if self._gate is not None:
            await self._gate.wait()
        self.reaped = True
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        if self._gate is not None:
            self._gate.set()


class _ProcessFactory:
    def __init__(
        self,
        *,
        returncode: int = 0,
        gate: asyncio.Event | None = None,
        started: asyncio.Event | None = None,
    ) -> None:
        self.returncode = returncode
        self.gate = gate
        self.started = started
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.instances: list[_FakeProcess] = []

    async def __call__(self, *args: object, **kwargs: object) -> _FakeProcess:
        self.calls.append((args, kwargs))
        process = _FakeProcess(
            returncode=self.returncode,
            gate=self.gate,
            started=self.started,
        )
        self.instances.append(process)
        return process


async def _smoke_ok(_url: str, _expected_payload: bytes) -> bool:
    return True


async def _fetch_rows(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                """
                SELECT id, manifest_sha256, project, branch, status,
                       deployment_url, error_code, error_class, finished_at
                  FROM wiki_static_deployments
                 ORDER BY id
                """
            )
        )
        return [dict(row) for row in result.mappings().all()]


@pytest.mark.parametrize("mutation", ["unsafe", "safe-but-changed"])
async def test_mutated_generation_fails_audit_before_process_spawn(
    tmp_path: Path,
    publisher_engine: AsyncEngine,
    clean_audit_rows: None,
    pinned_runtime: tuple[Path, Path],
    config: CloudflarePagesConfig,
    mutation: str,
) -> None:
    generation = _static_generation(tmp_path)
    target = generation.generation_dir / "index.html"
    if mutation == "unsafe":
        target.write_text(
            target.read_text(encoding="utf-8") + "https://private-vps.example/admin",
            encoding="utf-8",
        )
    else:
        target.write_text(target.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    process = _ProcessFactory()

    with pytest.raises(CloudflarePagesPublishError) as raised:
        await publish_static_generation(
            generation.generation_dir,
            expected_manifest_sha256=generation.manifest_sha256,
            config=config,
            db_engine=publisher_engine,
            process_exec=process,
            smoke_check=_smoke_ok,
        )

    assert raised.value.error_code in {
        "static_audit_failed",
        "static_manifest_mismatch",
    }
    assert process.calls == []
    rows = await _fetch_rows(publisher_engine)
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert rows[0]["error_code"] == raised.value.error_code
    assert rows[0]["finished_at"] is not None


async def test_empty_generation_is_rejected_by_publisher_before_spawn(
    tmp_path: Path,
    publisher_engine: AsyncEngine,
    clean_audit_rows: None,
    pinned_runtime: tuple[Path, Path],
    config: CloudflarePagesConfig,
) -> None:
    generation = _static_generation(tmp_path)
    (generation.generation_dir / "pages" / "memory-system" / "index.html").unlink()
    (generation.generation_dir / "search-index.json").write_text("[]\n", encoding="utf-8")
    process = _ProcessFactory()

    with pytest.raises(CloudflarePagesPublishError) as raised:
        await publish_static_generation(
            generation.generation_dir,
            expected_manifest_sha256=generation.manifest_sha256,
            config=config,
            db_engine=publisher_engine,
            process_exec=process,
            smoke_check=_smoke_ok,
        )

    assert raised.value.error_code == "static_audit_failed"
    assert process.calls == []
    rows = await _fetch_rows(publisher_engine)
    assert [(row["status"], row["error_code"]) for row in rows] == [
        ("failed", "static_audit_failed")
    ]


async def test_next_target_attempt_abandons_pending_from_different_manifest(
    tmp_path: Path,
    publisher_engine: AsyncEngine,
    clean_audit_rows: None,
    pinned_runtime: tuple[Path, Path],
    config: CloudflarePagesConfig,
) -> None:
    generation = _static_generation(tmp_path)
    stale_manifest = "b" * 64
    assert stale_manifest != generation.manifest_sha256
    async with publisher_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO wiki_static_deployments "
                "(manifest_sha256, project, branch, status) "
                "VALUES (:manifest, :project, :branch, 'pending')"
            ),
            {
                "manifest": stale_manifest,
                "project": config.project,
                "branch": config.branch,
            },
        )

    process = _ProcessFactory()
    result = await publish_static_generation(
        generation.generation_dir,
        expected_manifest_sha256=generation.manifest_sha256,
        config=config,
        db_engine=publisher_engine,
        process_exec=process,
        smoke_check=_smoke_ok,
    )

    assert result.status == "succeeded"
    rows = await _fetch_rows(publisher_engine)
    assert [(row["status"], row["error_code"]) for row in rows] == [
        ("failed", "abandoned_pending"),
        ("succeeded", None),
    ]


async def test_process_failure_is_durable_and_never_captures_output(
    tmp_path: Path,
    publisher_engine: AsyncEngine,
    clean_audit_rows: None,
    pinned_runtime: tuple[Path, Path],
    config: CloudflarePagesConfig,
) -> None:
    generation = _static_generation(tmp_path)
    process = _ProcessFactory(returncode=23)

    with pytest.raises(CloudflarePagesPublishError) as raised:
        await publish_static_generation(
            generation.generation_dir,
            expected_manifest_sha256=generation.manifest_sha256,
            config=config,
            db_engine=publisher_engine,
            process_exec=process,
            smoke_check=_smoke_ok,
        )

    assert raised.value.error_code == "process_exit_nonzero"
    assert len(process.calls) == 1
    args, kwargs = process.calls[0]
    assert config.api_token not in " ".join(str(arg) for arg in args)
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["shell"] is False
    assert kwargs["cwd"] != generation.generation_dir
    assert kwargs["env"] == {
        "CLOUDFLARE_API_TOKEN": config.api_token,
        "CLOUDFLARE_ACCOUNT_ID": config.account_id,
        "HOME": "/home/appuser",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
    }
    rows = await _fetch_rows(publisher_engine)
    assert rows[0]["status"] == "failed"
    assert rows[0]["error_code"] == "process_exit_nonzero"
    assert "secret" not in repr(rows[0]).lower()


async def test_wrangler_workdir_cannot_mutate_immutable_generation(
    tmp_path: Path,
    publisher_engine: AsyncEngine,
    clean_audit_rows: None,
    pinned_runtime: tuple[Path, Path],
    config: CloudflarePagesConfig,
) -> None:
    generation = _static_generation(tmp_path)
    resolved_generation = generation.generation_dir.resolve()
    canonical_before = _tree_snapshot(resolved_generation)
    process_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def wrangler_writes_cache(*args: object, **kwargs: object) -> _FakeProcess:
        process_calls.append((args, kwargs))
        cache_path = Path(kwargs["cwd"]) / ".wrangler" / "cache" / "pages.json"
        cache_path.parent.mkdir(parents=True)
        cache_path.write_text("{}\n", encoding="utf-8")
        deploy_index = Path(args[4]) / "index.html"
        deploy_index.write_bytes(deploy_index.read_bytes())
        return _FakeProcess(returncode=0)

    first = await publish_static_generation(
        generation.generation_dir,
        expected_manifest_sha256=generation.manifest_sha256,
        config=config,
        db_engine=publisher_engine,
        process_exec=wrangler_writes_cache,
        smoke_check=_smoke_ok,
    )
    second = await publish_static_generation(
        generation.generation_dir,
        expected_manifest_sha256=generation.manifest_sha256,
        config=config,
        db_engine=publisher_engine,
        process_exec=wrangler_writes_cache,
        smoke_check=_smoke_ok,
    )

    assert (first.status, second.status) == ("succeeded", "skipped")
    assert len(process_calls) == 1
    args, kwargs = process_calls[0]
    deploy_copy = Path(args[4])
    wrangler_workdir = Path(kwargs["cwd"])
    assert deploy_copy.is_absolute()
    assert deploy_copy != resolved_generation
    assert str(resolved_generation) not in {str(arg) for arg in args}
    assert wrangler_workdir.is_absolute()
    assert deploy_copy.parent == wrangler_workdir
    assert not wrangler_workdir.is_relative_to(resolved_generation)
    assert not wrangler_workdir.exists()
    assert not deploy_copy.exists()
    assert not (resolved_generation / ".wrangler").exists()
    assert _tree_snapshot(resolved_generation) == canonical_before
    assert audit_static_tree(resolved_generation) == generation.manifest_sha256


@pytest.mark.parametrize(
    ("mutation", "expected_error", "expected_class"),
    [
        ("content", "static_audit_failed", "StaticExportSecurityError"),
        ("extra-file", "static_audit_failed", "StaticExportSecurityError"),
        ("valid-other-tree", "static_manifest_mismatch", "StaticManifestMismatch"),
    ],
)
async def test_wrangler_target_mutation_fails_before_smoke_or_success(
    tmp_path: Path,
    publisher_engine: AsyncEngine,
    clean_audit_rows: None,
    pinned_runtime: tuple[Path, Path],
    config: CloudflarePagesConfig,
    mutation: str,
    expected_error: str,
    expected_class: str,
) -> None:
    generation = _static_generation(tmp_path)
    other_generation = _static_generation(tmp_path / "other", site_title="Другая Wiki")
    assert other_generation.manifest_sha256 != generation.manifest_sha256
    resolved_generation = generation.generation_dir.resolve()
    canonical_before = _tree_snapshot(resolved_generation)
    temp_paths: list[Path] = []
    smoke_calls = 0

    async def mutate_deploy_target(*args: object, **kwargs: object) -> _FakeProcess:
        deploy_copy = Path(args[4])
        wrangler_workdir = Path(kwargs["cwd"])
        temp_paths.extend((wrangler_workdir, deploy_copy))
        cache_path = wrangler_workdir / ".wrangler" / "cache" / "pages.json"
        cache_path.parent.mkdir(parents=True)
        cache_path.write_text("{}\n", encoding="utf-8")
        if mutation == "content":
            target = deploy_copy / "index.html"
            target.write_bytes(target.read_bytes() + b"\n")
        elif mutation == "extra-file":
            (deploy_copy / "unexpected.txt").write_text("mutated\n", encoding="utf-8")
        else:
            shutil.rmtree(deploy_copy)
            shutil.copytree(other_generation.generation_dir, deploy_copy)
        return _FakeProcess(returncode=0)

    async def track_smoke(_url: str, _expected_payload: bytes) -> bool:
        nonlocal smoke_calls
        smoke_calls += 1
        return True

    with pytest.raises(CloudflarePagesPublishError) as raised:
        await publish_static_generation(
            generation.generation_dir,
            expected_manifest_sha256=generation.manifest_sha256,
            config=config,
            db_engine=publisher_engine,
            process_exec=mutate_deploy_target,
            smoke_check=track_smoke,
        )

    assert raised.value.error_code == expected_error
    assert smoke_calls == 0
    assert all(not path.exists() for path in temp_paths)
    assert _tree_snapshot(resolved_generation) == canonical_before
    assert audit_static_tree(resolved_generation) == generation.manifest_sha256
    rows = await _fetch_rows(publisher_engine)
    assert [(row["status"], row["error_code"], row["error_class"]) for row in rows] == [
        ("failed", expected_error, expected_class)
    ]


async def test_process_timeout_kills_once_without_retry(
    tmp_path: Path,
    publisher_engine: AsyncEngine,
    clean_audit_rows: None,
    pinned_runtime: tuple[Path, Path],
    config: CloudflarePagesConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = _static_generation(tmp_path)
    process = _ProcessFactory(gate=asyncio.Event())

    monkeypatch.setitem(
        publish_static_generation.__globals__,
        "_PUBLISH_TIMEOUT_SECONDS",
        0.01,
    )
    with pytest.raises(CloudflarePagesPublishError) as raised:
        await publish_static_generation(
            generation.generation_dir,
            expected_manifest_sha256=generation.manifest_sha256,
            config=config,
            db_engine=publisher_engine,
            process_exec=process,
            smoke_check=_smoke_ok,
        )

    assert raised.value.error_code == "process_timeout"
    assert len(process.calls) == 1
    assert len(process.instances) == 1
    assert process.instances[0].killed is True
    rows = await _fetch_rows(publisher_engine)
    assert [(row["status"], row["error_code"]) for row in rows] == [("failed", "process_timeout")]


async def test_cancelled_publish_kills_reaps_and_durably_marks_process_cancelled(
    tmp_path: Path,
    publisher_engine: AsyncEngine,
    clean_audit_rows: None,
    pinned_runtime: tuple[Path, Path],
    config: CloudflarePagesConfig,
) -> None:
    generation = _static_generation(tmp_path)
    process_started = asyncio.Event()
    process = _ProcessFactory(gate=asyncio.Event(), started=process_started)

    publish_task = asyncio.create_task(
        publish_static_generation(
            generation.generation_dir,
            expected_manifest_sha256=generation.manifest_sha256,
            config=config,
            db_engine=publisher_engine,
            process_exec=process,
            smoke_check=_smoke_ok,
        )
    )
    await asyncio.wait_for(process_started.wait(), timeout=5)
    publish_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await publish_task

    assert len(process.instances) == 1
    assert process.instances[0].killed is True
    assert process.instances[0].wait_calls == 2
    assert process.instances[0].reaped is True
    rows = await _fetch_rows(publisher_engine)
    assert [(row["status"], row["error_code"], row["error_class"]) for row in rows] == [
        ("failed", "process_cancelled", "CancelledError")
    ]


async def test_cancelled_during_spawn_durably_fails_without_process_cleanup(
    tmp_path: Path,
    publisher_engine: AsyncEngine,
    clean_audit_rows: None,
    pinned_runtime: tuple[Path, Path],
    config: CloudflarePagesConfig,
) -> None:
    generation = _static_generation(tmp_path)
    spawn_started = asyncio.Event()
    never_return = asyncio.Event()

    async def blocked_spawn(*_args: object, **_kwargs: object) -> _FakeProcess:
        spawn_started.set()
        await never_return.wait()
        raise AssertionError("blocked spawn unexpectedly resumed")

    publish_task = asyncio.create_task(
        publish_static_generation(
            generation.generation_dir,
            expected_manifest_sha256=generation.manifest_sha256,
            config=config,
            db_engine=publisher_engine,
            process_exec=blocked_spawn,
            smoke_check=_smoke_ok,
        )
    )
    await asyncio.wait_for(spawn_started.wait(), timeout=5)
    publish_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await publish_task

    rows = await _fetch_rows(publisher_engine)
    assert [(row["status"], row["error_code"], row["error_class"]) for row in rows] == [
        ("failed", "process_cancelled", "CancelledError")
    ]


async def test_cancelled_during_smoke_durably_fails_and_reaps_existing_process(
    tmp_path: Path,
    publisher_engine: AsyncEngine,
    clean_audit_rows: None,
    pinned_runtime: tuple[Path, Path],
    config: CloudflarePagesConfig,
) -> None:
    generation = _static_generation(tmp_path)
    smoke_started = asyncio.Event()
    never_finish = asyncio.Event()
    process = _ProcessFactory()

    async def blocked_smoke(_url: str, _expected_payload: bytes) -> bool:
        smoke_started.set()
        await never_finish.wait()
        return True

    publish_task = asyncio.create_task(
        publish_static_generation(
            generation.generation_dir,
            expected_manifest_sha256=generation.manifest_sha256,
            config=config,
            db_engine=publisher_engine,
            process_exec=process,
            smoke_check=blocked_smoke,
        )
    )
    await asyncio.wait_for(smoke_started.wait(), timeout=5)
    publish_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await publish_task

    assert len(process.instances) == 1
    assert process.instances[0].killed is True
    assert process.instances[0].wait_calls == 2
    rows = await _fetch_rows(publisher_engine)
    assert [(row["status"], row["error_code"], row["error_class"]) for row in rows] == [
        ("failed", "process_cancelled", "CancelledError")
    ]


async def test_cancelled_during_success_finalization_durably_fails_pending_attempt(
    tmp_path: Path,
    publisher_engine: AsyncEngine,
    clean_audit_rows: None,
    pinned_runtime: tuple[Path, Path],
    config: CloudflarePagesConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = _static_generation(tmp_path)
    finalization_started = asyncio.Event()
    never_finish = asyncio.Event()
    process = _ProcessFactory()

    async def blocked_complete_success(*_args: object, **_kwargs: object):
        finalization_started.set()
        await never_finish.wait()
        raise AssertionError("blocked finalization unexpectedly resumed")

    monkeypatch.setitem(
        publish_static_generation.__globals__,
        "_complete_success",
        blocked_complete_success,
    )
    publish_task = asyncio.create_task(
        publish_static_generation(
            generation.generation_dir,
            expected_manifest_sha256=generation.manifest_sha256,
            config=config,
            db_engine=publisher_engine,
            process_exec=process,
            smoke_check=_smoke_ok,
        )
    )
    await asyncio.wait_for(finalization_started.wait(), timeout=5)
    publish_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await publish_task

    assert len(process.instances) == 1
    assert process.instances[0].killed is True
    rows = await _fetch_rows(publisher_engine)
    assert [(row["status"], row["error_code"], row["error_class"]) for row in rows] == [
        ("failed", "process_cancelled", "CancelledError")
    ]


async def test_success_is_idempotent_and_second_call_skips_upload(
    tmp_path: Path,
    publisher_engine: AsyncEngine,
    clean_audit_rows: None,
    pinned_runtime: tuple[Path, Path],
    config: CloudflarePagesConfig,
) -> None:
    generation = _static_generation(tmp_path)
    process = _ProcessFactory()

    first = await publish_static_generation(
        generation.generation_dir,
        expected_manifest_sha256=generation.manifest_sha256,
        config=config,
        db_engine=publisher_engine,
        process_exec=process,
        smoke_check=_smoke_ok,
    )
    second = await publish_static_generation(
        generation.generation_dir,
        expected_manifest_sha256=generation.manifest_sha256,
        config=config,
        db_engine=publisher_engine,
        process_exec=process,
        smoke_check=_smoke_ok,
    )

    assert first.status == "succeeded"
    assert second.status == "skipped"
    assert first.audit_id == second.audit_id
    assert len(process.calls) == 1
    rows = await _fetch_rows(publisher_engine)
    assert [(row["status"], row["deployment_url"]) for row in rows] == [
        ("succeeded", config.base_url)
    ]


async def test_historical_success_redeploys_when_live_hash_no_longer_matches(
    tmp_path: Path,
    publisher_engine: AsyncEngine,
    clean_audit_rows: None,
    pinned_runtime: tuple[Path, Path],
    config: CloudflarePagesConfig,
) -> None:
    generation_a = _static_generation(
        tmp_path / "a",
        site_title="Wiki A",
    )
    generation_b = _static_generation(
        tmp_path / "b",
        site_title="Wiki B",
    )
    assert generation_a.manifest_sha256 != generation_b.manifest_sha256
    assert (generation_a.generation_dir / "search-index.json").read_bytes() == (
        generation_b.generation_dir / "search-index.json"
    ).read_bytes()
    process = _ProcessFactory()
    smoke_results = iter((True, True, False, True, True))
    smoke_calls: list[tuple[str, bytes]] = []

    async def smoke_sequence(url: str, expected_payload: bytes) -> bool:
        assert "/generation-manifest.json?" in url
        smoke_calls.append((url, expected_payload))
        return next(smoke_results)

    first_a = await publish_static_generation(
        generation_a.generation_dir,
        expected_manifest_sha256=generation_a.manifest_sha256,
        config=config,
        db_engine=publisher_engine,
        process_exec=process,
        smoke_check=smoke_sequence,
    )
    published_b = await publish_static_generation(
        generation_b.generation_dir,
        expected_manifest_sha256=generation_b.manifest_sha256,
        config=config,
        db_engine=publisher_engine,
        process_exec=process,
        smoke_check=smoke_sequence,
    )
    second_a = await publish_static_generation(
        generation_a.generation_dir,
        expected_manifest_sha256=generation_a.manifest_sha256,
        config=config,
        db_engine=publisher_engine,
        process_exec=process,
        smoke_check=smoke_sequence,
    )
    current_a = await publish_static_generation(
        generation_a.generation_dir,
        expected_manifest_sha256=generation_a.manifest_sha256,
        config=config,
        db_engine=publisher_engine,
        process_exec=process,
        smoke_check=smoke_sequence,
    )

    assert (first_a.status, published_b.status, second_a.status) == (
        "succeeded",
        "succeeded",
        "succeeded",
    )
    assert second_a.audit_id != first_a.audit_id
    assert current_a.status == "skipped"
    assert current_a.audit_id == second_a.audit_id
    assert len(process.calls) == 3
    assert len(smoke_calls) == 5
    assert smoke_calls[0][0] != smoke_calls[2][0]
    assert smoke_calls[2][0] != smoke_calls[3][0]
    assert smoke_calls[3][0] != smoke_calls[4][0]
    marker_a = (generation_a.generation_dir / "generation-manifest.json").read_bytes()
    marker_b = (generation_b.generation_dir / "generation-manifest.json").read_bytes()
    assert [payload for _url, payload in smoke_calls] == [
        marker_a,
        marker_b,
        marker_a,
        marker_a,
        marker_a,
    ]
    rows = await _fetch_rows(publisher_engine)
    assert [row["manifest_sha256"] for row in rows] == [
        generation_a.manifest_sha256,
        generation_b.manifest_sha256,
        generation_a.manifest_sha256,
    ]
    assert [row["status"] for row in rows] == ["succeeded", "succeeded", "succeeded"]


async def test_concurrent_same_manifest_uploads_exactly_once(
    tmp_path: Path,
    publisher_engine: AsyncEngine,
    clean_audit_rows: None,
    pinned_runtime: tuple[Path, Path],
    config: CloudflarePagesConfig,
) -> None:
    generation = _static_generation(tmp_path)
    process_started = asyncio.Event()
    release_process = asyncio.Event()
    process = _ProcessFactory(gate=release_process, started=process_started)

    async def publish():
        return await publish_static_generation(
            generation.generation_dir,
            expected_manifest_sha256=generation.manifest_sha256,
            config=config,
            db_engine=publisher_engine,
            process_exec=process,
            smoke_check=_smoke_ok,
        )

    first_task = asyncio.create_task(publish())
    await asyncio.wait_for(process_started.wait(), timeout=5)
    second_task = asyncio.create_task(publish())
    await asyncio.sleep(0)
    release_process.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert {first.status, second.status} == {"succeeded", "skipped"}
    assert first.audit_id == second.audit_id
    assert len(process.calls) == 1
    rows = await _fetch_rows(publisher_engine)
    assert [row["status"] for row in rows] == ["succeeded"]


async def test_smoke_failure_prevents_success_state(
    tmp_path: Path,
    publisher_engine: AsyncEngine,
    clean_audit_rows: None,
    pinned_runtime: tuple[Path, Path],
    config: CloudflarePagesConfig,
) -> None:
    generation = _static_generation(tmp_path)
    process = _ProcessFactory()
    smoke_calls: list[tuple[str, bytes]] = []

    async def smoke_failed(url: str, expected_payload: bytes) -> bool:
        smoke_calls.append((url, expected_payload))
        return False

    with pytest.raises(CloudflarePagesPublishError) as raised:
        await publish_static_generation(
            generation.generation_dir,
            expected_manifest_sha256=generation.manifest_sha256,
            config=config,
            db_engine=publisher_engine,
            process_exec=process,
            smoke_check=smoke_failed,
        )

    assert raised.value.error_code == "smoke_failed"
    assert len(process.calls) == 1
    assert len(smoke_calls) == 1
    smoke_url, expected_payload = smoke_calls[0]
    assert smoke_url.startswith(
        f"{config.base_url}/generation-manifest.json?manifest={generation.manifest_sha256}&probe="
    )
    assert len(smoke_url.rsplit("=", 1)[1]) == 32
    assert expected_payload == (generation.generation_dir / "generation-manifest.json").read_bytes()
    rows = await _fetch_rows(publisher_engine)
    assert rows[0]["status"] == "failed"
    assert rows[0]["deployment_url"] is None


async def test_audit_commit_survives_unrelated_caller_rollback(
    tmp_path: Path,
    publisher_engine: AsyncEngine,
    clean_audit_rows: None,
    pinned_runtime: tuple[Path, Path],
    config: CloudflarePagesConfig,
) -> None:
    generation = _static_generation(tmp_path)
    process = _ProcessFactory()

    async with publisher_engine.connect() as caller_connection:
        caller_transaction = await caller_connection.begin()
        await caller_connection.execute(text("SELECT 1"))
        result = await publish_static_generation(
            generation.generation_dir,
            expected_manifest_sha256=generation.manifest_sha256,
            config=config,
            db_engine=publisher_engine,
            process_exec=process,
            smoke_check=_smoke_ok,
        )
        await caller_transaction.rollback()

    assert result.status == "succeeded"
    assert [row["status"] for row in await _fetch_rows(publisher_engine)] == ["succeeded"]


def test_config_is_fail_fast_and_secret_safe() -> None:
    complete = {
        "CLOUDFLARE_API_TOKEN": "secret-api-token",
        "CLOUDFLARE_ACCOUNT_ID": "account-id",
        "CLOUDFLARE_PAGES_PROJECT": "shkoder-wiki",
        "WIKI_PUBLIC_BASE_URL": "https://wiki.example.test/",
    }
    config = load_cloudflare_pages_config(complete)
    assert config.branch == "main"
    assert config.base_url == "https://wiki.example.test"
    assert complete["CLOUDFLARE_API_TOKEN"] not in repr(config)

    for missing_key in (
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_ACCOUNT_ID",
        "CLOUDFLARE_PAGES_PROJECT",
        "WIKI_PUBLIC_BASE_URL",
    ):
        incomplete = dict(complete)
        del incomplete[missing_key]
        with pytest.raises(ValueError, match=missing_key):
            load_cloudflare_pages_config(incomplete)


@pytest.mark.parametrize(
    "url",
    [
        "http://wiki.example.test",
        "https://localhost",
        "https://127.0.0.1",
        "https://wiki.example.test/path",
        "https://user:password@wiki.example.test",
        "https://wiki.example.test?redirect=https://private-vps.example",
    ],
)
def test_config_rejects_non_public_or_ambiguous_base_url(url: str) -> None:
    with pytest.raises(ValueError, match="WIKI_PUBLIC_BASE_URL"):
        load_cloudflare_pages_config(
            {
                "CLOUDFLARE_API_TOKEN": "secret-api-token",
                "CLOUDFLARE_ACCOUNT_ID": "account-id",
                "CLOUDFLARE_PAGES_PROJECT": "shkoder-wiki",
                "WIKI_PUBLIC_BASE_URL": url,
            }
        )
