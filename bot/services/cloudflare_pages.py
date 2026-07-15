"""Fail-closed Cloudflare Pages publisher for immutable wiki generations.

The publisher never reads Wrangler output.  Its durable PostgreSQL audit row is
committed on a dedicated connection, so a caller rollback cannot erase evidence of
an attempted deployment.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
import re
import shutil
import subprocess
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, Protocol, TypeAlias
from urllib.parse import urlsplit

import httpx
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from bot.services.wiki_static_export import (
    PUBLIC_GENERATION_MANIFEST_PATH,
    StaticExportError,
    audit_static_tree,
)

_NODE_BINARY = Path("/usr/local/bin/node")
_WRANGLER_SCRIPT = Path("/opt/wrangler/node_modules/wrangler/bin/wrangler.js")
_PUBLISH_TIMEOUT_SECONDS = 180.0
_SMOKE_TIMEOUT_SECONDS = 15.0
_MANIFEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_PROJECT_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,253}[a-z0-9])?\Z")
_BRANCH_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._/-]{0,254})?\Z")
_SAFE_ERROR_CLASS_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,254}\Z")

_LOCK_SQL = text("SELECT pg_advisory_lock(:lock_id)")
_UNLOCK_SQL = text("SELECT pg_advisory_unlock(:lock_id)")
_FIND_SUCCESS_SQL = text(
    """
    SELECT id, deployment_url
      FROM wiki_static_deployments
     WHERE manifest_sha256 = :manifest_sha256
       AND project = :project
       AND branch = :branch
       AND status = 'succeeded'
     ORDER BY id DESC
     LIMIT 1
    """
)
_ABANDON_PENDING_SQL = text(
    """
    UPDATE wiki_static_deployments
       SET status = 'failed',
           error_code = 'abandoned_pending',
           error_class = 'AbandonedPending',
           finished_at = now(),
           updated_at = now()
     WHERE project = :project
       AND branch = :branch
       AND status = 'pending'
    """
)
_INSERT_PENDING_SQL = text(
    """
    INSERT INTO wiki_static_deployments (
        manifest_sha256, project, branch, status
    )
    VALUES (:manifest_sha256, :project, :branch, 'pending')
    RETURNING id
    """
)
_MARK_FAILED_SQL = text(
    """
    UPDATE wiki_static_deployments
       SET status = 'failed',
           deployment_url = NULL,
           error_code = :error_code,
           error_class = :error_class,
           finished_at = now(),
           updated_at = now()
     WHERE id = :audit_id
       AND status = 'pending'
    """
)
_MARK_SUCCEEDED_SQL = text(
    """
    UPDATE wiki_static_deployments
       SET status = 'succeeded',
           deployment_url = :deployment_url,
           error_code = NULL,
           error_class = NULL,
           finished_at = now(),
           updated_at = now()
     WHERE id = :audit_id
       AND status = 'pending'
    """
)


class _Process(Protocol):
    async def wait(self) -> int: ...

    def kill(self) -> None: ...


ProcessExec: TypeAlias = Callable[..., Awaitable[_Process]]
SmokeCheck: TypeAlias = Callable[[str, bytes], Awaitable[bool]]


@dataclass(frozen=True)
class CloudflarePagesConfig:
    """Validated deployment config; the API token is excluded from repr."""

    api_token: str = field(repr=False)
    account_id: str
    project: str
    branch: str
    base_url: str

    def __post_init__(self) -> None:
        _require_nonempty_secret(self.api_token, name="CLOUDFLARE_API_TOKEN")
        _require_safe_identifier(
            self.account_id,
            name="CLOUDFLARE_ACCOUNT_ID",
            pattern=re.compile(r"[A-Za-z0-9_-]{1,255}\Z"),
        )
        _require_safe_identifier(
            self.project,
            name="CLOUDFLARE_PAGES_PROJECT",
            pattern=_PROJECT_RE,
        )
        _require_safe_identifier(
            self.branch,
            name="CLOUDFLARE_PAGES_BRANCH",
            pattern=_BRANCH_RE,
        )
        if ".." in self.branch.split("/") or "//" in self.branch:
            raise ValueError("CLOUDFLARE_PAGES_BRANCH is invalid")
        object.__setattr__(self, "base_url", _normalize_public_base_url(self.base_url))


@dataclass(frozen=True)
class CloudflarePublishResult:
    status: Literal["succeeded", "skipped"]
    audit_id: int
    manifest_sha256: str
    deployment_url: str


class CloudflarePagesPublishError(RuntimeError):
    """Safe publication error carrying only an error code and durable audit id."""

    def __init__(self, error_code: str, audit_id: int | None) -> None:
        self.error_code = error_code
        self.audit_id = audit_id
        super().__init__(f"cloudflare pages publication failed: {error_code}")


def load_cloudflare_pages_config(
    environ: Mapping[str, str] | None = None,
) -> CloudflarePagesConfig:
    """Load all required Cloudflare settings at once and fail on any missing value."""

    values = os.environ if environ is None else environ
    required_names = (
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_ACCOUNT_ID",
        "CLOUDFLARE_PAGES_PROJECT",
        "WIKI_PUBLIC_BASE_URL",
    )
    missing = [name for name in required_names if not values.get(name)]
    if missing:
        raise ValueError(f"missing required environment variable: {', '.join(missing)}")
    return CloudflarePagesConfig(
        api_token=values["CLOUDFLARE_API_TOKEN"],
        account_id=values["CLOUDFLARE_ACCOUNT_ID"],
        project=values["CLOUDFLARE_PAGES_PROJECT"],
        branch=values.get("CLOUDFLARE_PAGES_BRANCH", "main"),
        base_url=values["WIKI_PUBLIC_BASE_URL"],
    )


async def publish_static_generation(
    generation_dir: Path,
    *,
    expected_manifest_sha256: str,
    config: CloudflarePagesConfig | None = None,
    db_engine: AsyncEngine | None = None,
    forbidden_origins: Iterable[str] = (),
    process_exec: ProcessExec = asyncio.create_subprocess_exec,
    smoke_check: SmokeCheck | None = None,
) -> CloudflarePublishResult:
    """Publish one immutable generation unless the live target already matches.

    A session-level advisory lock serializes deployments to the same project and
    branch.  Historical success is only an optimization hint: the current live
    generation marker must still match before an upload can be skipped.  Both
    canonical and disposable deploy trees are audited before spawning Wrangler;
    the disposable tree is audited again before smoke verification.
    """

    if not _MANIFEST_RE.fullmatch(expected_manifest_sha256):
        raise ValueError("expected_manifest_sha256 must be lowercase SHA-256 hex")
    resolved_config = config or load_cloudflare_pages_config()
    resolved_engine = db_engine or _default_engine()
    resolved_smoke_check = smoke_check or _public_smoke_check
    resolved_forbidden_origins = tuple(forbidden_origins)
    identity = {
        "manifest_sha256": expected_manifest_sha256,
        "project": resolved_config.project,
        "branch": resolved_config.branch,
    }

    async with resolved_engine.connect() as connection:
        async with _deployment_lock(connection, resolved_config):
            await _abandon_pending(connection, identity)
            existing = await _find_success(connection, identity)
            try:
                resolved_generation = Path(generation_dir).resolve(strict=True)
                expected_generation_marker = (
                    resolved_generation / PUBLIC_GENERATION_MANIFEST_PATH
                ).read_bytes()
            except OSError as exc:
                audit_id = await _begin_attempt(connection, identity)
                await _mark_failed(
                    connection,
                    audit_id=audit_id,
                    error_code="static_audit_failed",
                    error_class=_safe_error_class(exc),
                )
                raise CloudflarePagesPublishError("static_audit_failed", audit_id) from None

            try:
                actual_manifest_sha256 = audit_static_tree(
                    resolved_generation,
                    forbidden_origins=resolved_forbidden_origins,
                )
            except (StaticExportError, OSError) as exc:
                audit_id = await _begin_attempt(connection, identity)
                await _mark_failed(
                    connection,
                    audit_id=audit_id,
                    error_code="static_audit_failed",
                    error_class=_safe_error_class(exc),
                )
                raise CloudflarePagesPublishError("static_audit_failed", audit_id) from None
            if actual_manifest_sha256 != expected_manifest_sha256:
                audit_id = await _begin_attempt(connection, identity)
                await _mark_failed(
                    connection,
                    audit_id=audit_id,
                    error_code="static_manifest_mismatch",
                    error_class="StaticManifestMismatch",
                )
                raise CloudflarePagesPublishError("static_manifest_mismatch", audit_id)

            if existing is not None and await _live_target_matches(
                resolved_smoke_check,
                smoke_url=_smoke_url(resolved_config.base_url, expected_manifest_sha256),
                expected_generation_marker=expected_generation_marker,
            ):
                return _success_result(
                    status="skipped",
                    audit_id=existing[0],
                    manifest_sha256=expected_manifest_sha256,
                    deployment_url=existing[1],
                )

            audit_id = await _begin_attempt(connection, identity)
            process: _Process | None = None
            wrangler_workdir: TemporaryDirectory[str] | None = None
            try:
                try:
                    _validate_pinned_runtime()
                except RuntimeError:
                    await _mark_failed(
                        connection,
                        audit_id=audit_id,
                        error_code="runtime_missing",
                        error_class="RuntimeUnavailable",
                    )
                    raise CloudflarePagesPublishError("runtime_missing", audit_id) from None

                await _audit_attempt_tree(
                    connection,
                    audit_id=audit_id,
                    root=resolved_generation,
                    expected_manifest_sha256=expected_manifest_sha256,
                    forbidden_origins=resolved_forbidden_origins,
                )

                try:
                    wrangler_workdir = TemporaryDirectory(
                        prefix=".wrangler-",
                        dir=resolved_generation.parent,
                    )
                    wrangler_cwd = Path(wrangler_workdir.name).resolve(strict=True)
                    deploy_copy = shutil.copytree(
                        resolved_generation,
                        wrangler_cwd / "site",
                        symlinks=True,
                    )
                except OSError as exc:
                    await _mark_failed(
                        connection,
                        audit_id=audit_id,
                        error_code="static_audit_failed",
                        error_class=_safe_error_class(exc),
                    )
                    raise CloudflarePagesPublishError("static_audit_failed", audit_id) from None

                await _audit_attempt_tree(
                    connection,
                    audit_id=audit_id,
                    root=deploy_copy,
                    expected_manifest_sha256=expected_manifest_sha256,
                    forbidden_origins=resolved_forbidden_origins,
                )

                try:
                    process = await process_exec(
                        str(_NODE_BINARY),
                        str(_WRANGLER_SCRIPT),
                        "pages",
                        "deploy",
                        str(deploy_copy),
                        f"--project-name={resolved_config.project}",
                        f"--branch={resolved_config.branch}",
                        "--no-bundle",
                        cwd=wrangler_cwd,
                        env={
                            "CLOUDFLARE_API_TOKEN": resolved_config.api_token,
                            "CLOUDFLARE_ACCOUNT_ID": resolved_config.account_id,
                            "HOME": "/home/appuser",
                            "PATH": "/usr/local/bin:/usr/bin:/bin",
                        },
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        shell=False,
                    )
                except OSError as exc:
                    await _mark_failed(
                        connection,
                        audit_id=audit_id,
                        error_code="spawn_failed",
                        error_class=_safe_error_class(exc),
                    )
                    raise CloudflarePagesPublishError("spawn_failed", audit_id) from None

                try:
                    exit_code = await asyncio.wait_for(
                        process.wait(),
                        timeout=_PUBLISH_TIMEOUT_SECONDS,
                    )
                except TimeoutError:
                    process.kill()
                    await process.wait()
                    await _mark_failed(
                        connection,
                        audit_id=audit_id,
                        error_code="process_timeout",
                        error_class="TimeoutError",
                    )
                    raise CloudflarePagesPublishError("process_timeout", audit_id) from None
                if exit_code != 0:
                    await _mark_failed(
                        connection,
                        audit_id=audit_id,
                        error_code="process_exit_nonzero",
                        error_class="ProcessExitNonZero",
                    )
                    raise CloudflarePagesPublishError("process_exit_nonzero", audit_id)

                await _audit_attempt_tree(
                    connection,
                    audit_id=audit_id,
                    root=deploy_copy,
                    expected_manifest_sha256=expected_manifest_sha256,
                    forbidden_origins=resolved_forbidden_origins,
                )

                try:
                    smoke_passed = await asyncio.wait_for(
                        resolved_smoke_check(
                            _smoke_url(resolved_config.base_url, expected_manifest_sha256),
                            expected_generation_marker,
                        ),
                        timeout=_SMOKE_TIMEOUT_SECONDS,
                    )
                except TimeoutError:
                    await _mark_failed(
                        connection,
                        audit_id=audit_id,
                        error_code="smoke_timeout",
                        error_class="TimeoutError",
                    )
                    raise CloudflarePagesPublishError("smoke_timeout", audit_id) from None
                except httpx.HTTPError as exc:
                    await _mark_failed(
                        connection,
                        audit_id=audit_id,
                        error_code="smoke_http_error",
                        error_class=_safe_error_class(exc),
                    )
                    raise CloudflarePagesPublishError("smoke_http_error", audit_id) from None
                if smoke_passed is not True:
                    await _mark_failed(
                        connection,
                        audit_id=audit_id,
                        error_code="smoke_failed",
                        error_class="SmokeMismatch",
                    )
                    raise CloudflarePagesPublishError("smoke_failed", audit_id)

                return await _complete_success(
                    connection,
                    audit_id=audit_id,
                    manifest_sha256=expected_manifest_sha256,
                    deployment_url=resolved_config.base_url,
                )
            except asyncio.CancelledError:
                cleanup_task = asyncio.create_task(
                    _cancel_process_and_mark_failed(
                        connection,
                        process=process,
                        audit_id=audit_id,
                    )
                )
                await asyncio.shield(cleanup_task)
                raise
            finally:
                if wrangler_workdir is not None:
                    wrangler_workdir.cleanup()


def _default_engine() -> AsyncEngine:
    from bot.db.engine import engine

    return engine


async def _audit_attempt_tree(
    connection: AsyncConnection,
    *,
    audit_id: int,
    root: Path,
    expected_manifest_sha256: str,
    forbidden_origins: Iterable[str],
) -> None:
    try:
        actual_manifest_sha256 = audit_static_tree(
            root,
            forbidden_origins=forbidden_origins,
        )
    except (StaticExportError, OSError) as exc:
        await _mark_failed(
            connection,
            audit_id=audit_id,
            error_code="static_audit_failed",
            error_class=_safe_error_class(exc),
        )
        raise CloudflarePagesPublishError("static_audit_failed", audit_id) from None
    if actual_manifest_sha256 != expected_manifest_sha256:
        await _mark_failed(
            connection,
            audit_id=audit_id,
            error_code="static_manifest_mismatch",
            error_class="StaticManifestMismatch",
        )
        raise CloudflarePagesPublishError("static_manifest_mismatch", audit_id)


def _require_nonempty_secret(value: str, *, name: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty trimmed string")


def _require_safe_identifier(value: str, *, name: str, pattern: re.Pattern[str]) -> None:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"{name} is invalid")


def _normalize_public_base_url(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("WIKI_PUBLIC_BASE_URL must be a non-empty trimmed URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or parsed.port not in {None, 443}
    ):
        raise ValueError("WIKI_PUBLIC_BASE_URL must be an HTTPS origin")

    hostname = parsed.hostname.casefold().rstrip(".")
    if (
        hostname == "localhost"
        or hostname.endswith(".localhost")
        or hostname.endswith(".local")
        or hostname.endswith(".internal")
    ):
        raise ValueError("WIKI_PUBLIC_BASE_URL must use a public hostname")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("WIKI_PUBLIC_BASE_URL must not use a private IP address")

    port = ":443" if parsed.port == 443 else ""
    return f"https://{hostname}{port}"


def _deployment_lock_id(config: CloudflarePagesConfig) -> int:
    payload = f"cloudflare-pages\0{config.project}\0{config.branch}".encode()
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big", signed=True)


@asynccontextmanager
async def _deployment_lock(
    connection: AsyncConnection,
    config: CloudflarePagesConfig,
):
    lock_id = _deployment_lock_id(config)
    await connection.execute(_LOCK_SQL, {"lock_id": lock_id})
    try:
        yield
    finally:
        if connection.in_transaction():
            await connection.rollback()
        try:
            unlocked = (await connection.execute(_UNLOCK_SQL, {"lock_id": lock_id})).scalar_one()
            await connection.commit()
        except SQLAlchemyError:
            await connection.invalidate()
            raise
        if unlocked is not True:
            await connection.invalidate()
            raise RuntimeError("cloudflare pages advisory lock was not held")


async def _find_success(
    connection: AsyncConnection,
    identity: Mapping[str, str],
) -> tuple[int, str] | None:
    row = (await connection.execute(_FIND_SUCCESS_SQL, identity)).one_or_none()
    if row is None:
        return None
    if row.deployment_url is None:
        raise RuntimeError("succeeded deployment is missing deployment_url")
    return int(row.id), str(row.deployment_url)


async def _abandon_pending(
    connection: AsyncConnection,
    identity: Mapping[str, str],
) -> None:
    await connection.execute(_ABANDON_PENDING_SQL, identity)
    await connection.commit()


async def _begin_attempt(
    connection: AsyncConnection,
    identity: Mapping[str, str],
) -> int:
    audit_id = int((await connection.execute(_INSERT_PENDING_SQL, identity)).scalar_one())
    await connection.commit()
    return audit_id


async def _live_target_matches(
    smoke_check: SmokeCheck,
    *,
    smoke_url: str,
    expected_generation_marker: bytes,
) -> bool:
    """Return true only when the public target proves it serves these exact bytes."""

    return (
        await asyncio.wait_for(
            smoke_check(smoke_url, expected_generation_marker),
            timeout=_SMOKE_TIMEOUT_SECONDS,
        )
        is True
    )


def _smoke_url(base_url: str, manifest_sha256: str) -> str:
    return (
        f"{base_url}/{PUBLIC_GENERATION_MANIFEST_PATH}"
        f"?manifest={manifest_sha256}&probe={uuid.uuid4().hex}"
    )


async def _cancel_process_and_mark_failed(
    connection: AsyncConnection,
    *,
    process: _Process | None,
    audit_id: int,
) -> None:
    if process is not None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
    await _mark_failed(
        connection,
        audit_id=audit_id,
        error_code="process_cancelled",
        error_class="CancelledError",
    )


async def _mark_failed(
    connection: AsyncConnection,
    *,
    audit_id: int,
    error_code: str,
    error_class: str | None,
) -> None:
    result = await connection.execute(
        _MARK_FAILED_SQL,
        {
            "audit_id": audit_id,
            "error_code": error_code,
            "error_class": error_class,
        },
    )
    if result.rowcount != 1:
        await connection.rollback()
        raise RuntimeError("static deployment audit row is not pending")
    await connection.commit()


async def _complete_success(
    connection: AsyncConnection,
    *,
    audit_id: int,
    manifest_sha256: str,
    deployment_url: str,
) -> CloudflarePublishResult:
    result = await connection.execute(
        _MARK_SUCCEEDED_SQL,
        {"audit_id": audit_id, "deployment_url": deployment_url},
    )
    if result.rowcount != 1:
        await connection.rollback()
        raise RuntimeError("static deployment audit row is not pending")
    await connection.commit()
    return _success_result(
        status="succeeded",
        audit_id=audit_id,
        manifest_sha256=manifest_sha256,
        deployment_url=deployment_url,
    )


def _success_result(
    *,
    status: Literal["succeeded", "skipped"],
    audit_id: int,
    manifest_sha256: str,
    deployment_url: str,
) -> CloudflarePublishResult:
    return CloudflarePublishResult(
        status=status,
        audit_id=audit_id,
        manifest_sha256=manifest_sha256,
        deployment_url=deployment_url,
    )


def _validate_pinned_runtime() -> None:
    if not _NODE_BINARY.is_file() or not os.access(_NODE_BINARY, os.X_OK):
        raise RuntimeError("pinned node runtime is unavailable")
    if not _WRANGLER_SCRIPT.is_file():
        raise RuntimeError("pinned wrangler runtime is unavailable")


def _safe_error_class(exc: BaseException) -> str:
    name = type(exc).__name__
    return name if _SAFE_ERROR_CLASS_RE.fullmatch(name) else "UnexpectedError"


async def _public_smoke_check(url: str, expected_payload: bytes) -> bool:
    timeout = httpx.Timeout(_SMOKE_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        async with client.stream(
            "GET",
            url,
            headers={"Accept": "application/json"},
        ) as response:
            if response.status_code != 200:
                return False
            payload = bytearray()
            async for chunk in response.aiter_bytes():
                payload.extend(chunk)
                if len(payload) > len(expected_payload):
                    return False
            return bytes(payload) == expected_payload


__all__ = [
    "CloudflarePagesConfig",
    "CloudflarePagesPublishError",
    "CloudflarePublishResult",
    "ProcessExec",
    "SmokeCheck",
    "load_cloudflare_pages_config",
    "publish_static_generation",
]
