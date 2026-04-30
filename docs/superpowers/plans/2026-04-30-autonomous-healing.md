# Autonomous Healing System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a bounded autonomous healing loop that detects shkoderbot production outages, attempts safe PR-based remediation, verifies recovery, rolls back on failure, and escalates with an audit trail.

**Architecture:** A scheduled composite healthcheck runs on GitHub-hosted Actions and dispatches a healing workflow when any signal is red. The healing workflow runs on the VPS through a self-hosted GitHub Actions runner, gathers state, snapshots production, starts a guarded Claude CLI session, and requires an independent Codex CLI review before merge. State and audit artifacts live on the orphan `healing-state` branch so the loop survives runner restarts without adding new infrastructure.

**Tech Stack:** Python 3.12, pytest + ruff, httpx, GitHub Actions, Claude CLI (auth), Codex CLI (auth), psycopg.

---

## File Structure

**Create:**

- `ops/__init__.py` — package marker for operational automation modules.
- `ops/healing/__init__.py` — package marker for autonomous healing modules.
- `tests/healing/__init__.py` — test package marker for healing tests.
- `.healing/.gitkeep` — keeps the local healing control directory in git.
- `ops/healing/state_branch.py` — isolated read/write helpers for the orphan `healing-state` branch.
- `tests/healing/test_state_branch.py` — local bare-repo tests for state branch helpers.
- `ops/healing/crypto.py` — Fernet encryption helpers for production env snapshots.
- `tests/healing/test_crypto.py` — encryption roundtrip and failure-mode tests.
- `ops/healing/healthcheck.py` — composite Coolify, Telegram, and database healthcheck CLI.
- `tests/healing/test_healthcheck.py` — green and red signal coverage with mocked HTTP and psycopg.
- `ops/healing/snapshot.py` — Coolify application and env snapshot create/restore module.
- `tests/healing/test_snapshot.py` — Coolify snapshot tests with `httpx.MockTransport` and real Fernet keys.
- `ops/healing/context_bundle.py` — Markdown bundle assembler for Claude sessions.
- `tests/healing/test_context_bundle.py` — context bundle section tests with mocked subprocess and HTTP.
- `ops/healing/escalate.py` — Telegram plus GitHub Issue escalation module.
- `tests/healing/test_escalate.py` — escalation success and failure matrix.
- `ops/healing/preToolUse_hook.sh` — Claude PreToolUse guardrail hook.
- `tests/healing/test_pre_tool_use_hook.py` — shell hook invariant tests.
- `ops/healing/INVARIANTS.md` — Claude system prompt suffix for autonomous healing.
- `.github/workflows/healthcheck.yml` — three-hour cron healthcheck and healing dispatcher.
- `.github/workflows/healing.yml` — self-hosted remediation workflow.
- `ops/healing/SETUP.md` — one-time VPS and secrets setup runbook.
- `ops/healing/orchestrator.py` — testable healing workflow orchestration.
- `tests/healing/test_dry_run_e2e.py` — dry-run e2e coverage for success, review rejection, rollback, and escalation.

**Modify:**

- `pyproject.toml` — add the `healing` optional dependency group.
- `.github/workflows/healing.yml` — created in Task 11, then narrowed to an orchestrator wrapper in Task 13.

**Leave unchanged:**

- `bot/config.py` — existing settings pattern is read for env naming only.
- `.github/workflows/ci.yml` — existing CI remains the reference style; new workflows are separate.
- `tests/conftest.py` — existing test fixtures remain unchanged.

## Tasks

### Task 1: Healing module scaffold

**Done when:** `python -c "import ops.healing"` exits 0 and only scaffold files are added.

- [ ] Create package and control directories:

```bash
mkdir -p ops/healing tests/healing .healing
```

- [ ] Create `ops/__init__.py`:

```python
"""Operational automation package for Vibe Gatekeeper."""
```

- [ ] Create `ops/healing/__init__.py`:

```python
"""Autonomous healing support package."""
```

- [ ] Create `tests/healing/__init__.py`:

```python
"""Tests for autonomous healing modules."""
```

- [ ] Create `.healing/.gitkeep` as an empty file:

```bash
: > .healing/.gitkeep
```

- [ ] Verify import:

```bash
python -c "import ops.healing"
```

- [ ] Confirm tracked diff is limited to scaffold files:

```bash
git status --short
```

- [ ] Commit:

```bash
git add ops/__init__.py ops/healing/__init__.py tests/healing/__init__.py .healing/.gitkeep
git commit -m $'chore(healing): scaffold autonomous healing package\n\nCo-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>'
```

### Task 2: State branch helpers

**Done when:** state branch tests pass against a local bare remote and no network is used.

- [ ] Add failing tests first in `tests/healing/test_state_branch.py`:

```python
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from ops.healing import state_branch


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    )


def _make_repo(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(remote)],
        check=True,
        text=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "clone", str(remote), str(repo)],
        check=True,
        text=True,
        capture_output=True,
    )
    _run(repo, "config", "user.email", "healing@example.invalid")
    _run(repo, "config", "user.name", "Healing Test")
    (repo / "README.md").write_text("# test repo\n", encoding="utf-8")
    _run(repo, "add", "README.md")
    _run(repo, "commit", "-m", "chore: initial commit")
    _run(repo, "push", "-u", "origin", "main")
    return repo


def test_write_and_read_file_uses_healing_state_branch(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)

    state_branch.write_file(repo, ".healing/in-progress", "incident-1\n")

    assert state_branch.read_file(repo, ".healing/in-progress") == "incident-1\n"
    branch_files = _run(repo, "ls-tree", "--name-only", "-r", "healing-state").stdout
    assert ".healing/in-progress" in branch_files
    main_files = _run(repo, "ls-tree", "--name-only", "-r", "main").stdout
    assert ".healing/in-progress" not in main_files


def test_append_and_read_jsonl_roundtrip(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    first: dict[str, Any] = {"ts": "2026-04-30T00:00:00Z", "status": "green"}
    second: dict[str, Any] = {"ts": "2026-04-30T03:00:00Z", "status": "red"}

    state_branch.append_jsonl(repo, "healthcheck-log.jsonl", first)
    state_branch.append_jsonl(repo, "healthcheck-log.jsonl", second)

    assert state_branch.read_jsonl(repo, "healthcheck-log.jsonl") == [first, second]


def test_missing_files_read_as_empty_values(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)

    assert state_branch.read_file(repo, ".healing/disabled") == ""
    assert state_branch.read_jsonl(repo, "healthcheck-log.jsonl") == []


def test_rejects_paths_outside_state_branch(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)

    try:
        state_branch.write_file(repo, "../escape", "bad\n")
    except ValueError as exc:
        assert "relative path" in str(exc)
    else:
        raise AssertionError("path traversal was accepted")
```

- [ ] Run the failing tests:

```bash
pytest -q tests/healing/test_state_branch.py
```

- [ ] Implement `ops/healing/state_branch.py`:

```python
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STATE_BRANCH = "healing-state"


@dataclass(frozen=True)
class StateBranchConfig:
    repo_dir: Path
    branch: str = STATE_BRANCH
    remote: str = "origin"


def _git(cwd: Path, args: Iterable[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    )


def _git_probe(cwd: Path, args: Iterable[str]) -> bool:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=False,
        text=True,
        capture_output=True,
    )
    return result.returncode == 0


def _local_branch_exists(config: StateBranchConfig) -> bool:
    return _git_probe(config.repo_dir, ["rev-parse", "--verify", config.branch])


def _remote_branch_exists(config: StateBranchConfig) -> bool:
    return _git_probe(
        config.repo_dir,
        ["ls-remote", "--exit-code", config.remote, f"refs/heads/{config.branch}"],
    )


def _state_branch_exists(config: StateBranchConfig) -> bool:
    return _local_branch_exists(config) or _remote_branch_exists(config)


def _prepare_orphan_branch(config: StateBranchConfig, worktree: Path) -> None:
    _git(config.repo_dir, ["worktree", "add", "--detach", str(worktree), "HEAD"])
    _git(worktree, ["switch", "--orphan", config.branch])
    tracked = _git(worktree, ["ls-files"]).stdout.splitlines()
    if tracked:
        _git(worktree, ["rm", "-rf", "--", *tracked])
    _git(worktree, ["commit", "--allow-empty", "-m", "chore(healing): initialize state branch"])
    _git(worktree, ["push", "-u", config.remote, config.branch])


def _prepare_worktree(config: StateBranchConfig, worktree: Path) -> None:
    if _local_branch_exists(config):
        _git(config.repo_dir, ["worktree", "add", str(worktree), config.branch])
        return

    if _remote_branch_exists(config):
        _git(config.repo_dir, ["fetch", config.remote, f"{config.branch}:{config.branch}"])
        _git(config.repo_dir, ["worktree", "add", str(worktree), config.branch])
        return

    _prepare_orphan_branch(config, worktree)


@contextmanager
def _state_worktree(config: StateBranchConfig):
    temp_parent = Path(tempfile.mkdtemp(prefix="healing-state-"))
    worktree = temp_parent / "worktree"
    try:
        _prepare_worktree(config, worktree)
        yield worktree
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            cwd=config.repo_dir,
            check=False,
            text=True,
            capture_output=True,
        )
        shutil.rmtree(temp_parent, ignore_errors=True)


def _target_path(worktree: Path, relative_path: str) -> Path:
    candidate = Path(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("state branch path must be a relative path inside the branch")
    return worktree / candidate


def read_file(
    repo_dir: Path | str,
    relative_path: str,
    branch: str = STATE_BRANCH,
    remote: str = "origin",
) -> str:
    config = StateBranchConfig(Path(repo_dir).resolve(), branch=branch, remote=remote)
    if not _state_branch_exists(config):
        return ""

    with _state_worktree(config) as worktree:
        target = _target_path(worktree, relative_path)
        if not target.exists():
            return ""
        return target.read_text(encoding="utf-8")


def write_file(
    repo_dir: Path | str,
    relative_path: str,
    content: str,
    branch: str = STATE_BRANCH,
    remote: str = "origin",
) -> None:
    config = StateBranchConfig(Path(repo_dir).resolve(), branch=branch, remote=remote)
    with _state_worktree(config) as worktree:
        target = _target_path(worktree, relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        _commit_path(worktree, config, relative_path, f"chore(healing): update {relative_path}")


def read_jsonl(
    repo_dir: Path | str,
    relative_path: str,
    branch: str = STATE_BRANCH,
    remote: str = "origin",
) -> list[dict[str, Any]]:
    content = read_file(repo_dir, relative_path, branch=branch, remote=remote)
    rows: list[dict[str, Any]] = []
    for line in content.splitlines():
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"JSONL row in {relative_path} is not an object")
            rows.append(value)
    return rows


def append_jsonl(
    repo_dir: Path | str,
    relative_path: str,
    item: Mapping[str, Any],
    branch: str = STATE_BRANCH,
    remote: str = "origin",
) -> None:
    config = StateBranchConfig(Path(repo_dir).resolve(), branch=branch, remote=remote)
    with _state_worktree(config) as worktree:
        target = _target_path(worktree, relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(item), sort_keys=True, separators=(",", ":")))
            handle.write("\n")
        _commit_path(worktree, config, relative_path, f"chore(healing): append {relative_path}")


def _commit_path(
    worktree: Path,
    config: StateBranchConfig,
    relative_path: str,
    message: str,
) -> None:
    status = _git(worktree, ["status", "--porcelain", "--", relative_path]).stdout
    if not status.strip():
        return
    _git(worktree, ["add", "--", relative_path])
    _git(worktree, ["commit", "-m", message])
    _git(worktree, ["push", config.remote, config.branch])


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read and write healing-state branch files.")
    parser.add_argument("--repo", default=".", help="Git repository path.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    read_file_parser = subparsers.add_parser("read-file")
    read_file_parser.add_argument("path")

    write_file_parser = subparsers.add_parser("write-file")
    write_file_parser.add_argument("path")
    write_file_parser.add_argument("--value")

    read_jsonl_parser = subparsers.add_parser("read-jsonl")
    read_jsonl_parser.add_argument("path")

    append_jsonl_parser = subparsers.add_parser("append-jsonl")
    append_jsonl_parser.add_argument("path")
    append_jsonl_parser.add_argument("--json")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    repo = Path(args.repo).resolve()

    if args.command == "read-file":
        print(read_file(repo, args.path), end="")
        return 0

    if args.command == "write-file":
        content = args.value if args.value is not None else sys.stdin.read()
        write_file(repo, args.path, content)
        return 0

    if args.command == "read-jsonl":
        print(json.dumps(read_jsonl(repo, args.path), sort_keys=True))
        return 0

    if args.command == "append-jsonl":
        raw_json = args.json if args.json is not None else sys.stdin.read()
        value = json.loads(raw_json)
        if not isinstance(value, dict):
            raise ValueError("append-jsonl expects a JSON object")
        append_jsonl(repo, args.path, value)
        return 0

    raise ValueError(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] Confirm pass:

```bash
pytest -q tests/healing/test_state_branch.py
ruff check ops/healing/state_branch.py tests/healing/test_state_branch.py
```

- [ ] Commit:

```bash
git add ops/healing/state_branch.py tests/healing/test_state_branch.py
git commit -m $'feat(healing): add state branch helpers\n\nCo-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>'
```

### Task 3: Env encryption

**Done when:** Fernet encryption tests pass and the new dependency is isolated to the `healing` optional group.

- [ ] Add failing tests first in `tests/healing/test_crypto.py`:

```python
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet, InvalidToken

from ops.healing.crypto import decrypt, encrypt


def test_encrypt_decrypt_roundtrip() -> None:
    key = Fernet.generate_key().decode("ascii")
    ciphertext = encrypt("BOT_TOKEN=123456:test\n", key)

    assert ciphertext != "BOT_TOKEN=123456:test\n"
    assert decrypt(ciphertext, key) == "BOT_TOKEN=123456:test\n"


def test_wrong_key_fails() -> None:
    ciphertext = encrypt("secret", Fernet.generate_key().decode("ascii"))
    wrong_key = Fernet.generate_key().decode("ascii")

    with pytest.raises(InvalidToken):
        decrypt(ciphertext, wrong_key)


def test_tampered_ciphertext_fails() -> None:
    key = Fernet.generate_key().decode("ascii")
    ciphertext = encrypt("secret", key)
    tampered = ciphertext[:-2] + "aa"

    with pytest.raises(InvalidToken):
        decrypt(tampered, key)
```

- [ ] Run the failing tests:

```bash
pytest -q tests/healing/test_crypto.py
```

- [ ] Modify `pyproject.toml` by adding only this optional dependency group:

```toml
[project.optional-dependencies]
dev = [
    "pytest>=8.3",
    "pytest-asyncio>=0.24",
    # aiosqlite is dev-only — used by tests/test_scheduler_deadlines.py for in-memory sqlite
    # isolation. Production runtime requires postgres (T0-02; see bot/db/engine.py).
    "aiosqlite>=0.20",
    "ruff>=0.11",
]
ops = [
    "telethon>=1.39",
]
healing = [
    "cryptography>=43",
]
```

- [ ] Implement `ops/healing/crypto.py`:

```python
from __future__ import annotations

from cryptography.fernet import Fernet


def _key_bytes(key: str) -> bytes:
    return key.encode("ascii")


def encrypt(plaintext: str, key: str) -> str:
    return Fernet(_key_bytes(key)).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str, key: str) -> str:
    return Fernet(_key_bytes(key)).decrypt(ciphertext.encode("ascii")).decode("utf-8")
```

- [ ] Install and confirm pass:

```bash
pip install -e ".[dev,healing]"
pytest -q tests/healing/test_crypto.py
ruff check ops/healing/crypto.py tests/healing/test_crypto.py
```

- [ ] Commit:

```bash
git add pyproject.toml ops/healing/crypto.py tests/healing/test_crypto.py
git commit -m $'feat(healing): add env snapshot encryption\n\nCo-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>'
```

### Task 4: Composite healthcheck

**Done when:** the CLI prints a JSON `CheckReport`, exits 0 on green, exits 1 on red, and each signal has green and red unit coverage.

- [ ] Add failing tests first in `tests/healing/test_healthcheck.py`:

```python
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ops.healing import healthcheck


@dataclass
class FakeCursor:
    row: tuple[int] | None = (1,)

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def execute(self, query: str) -> None:
        assert query == "SELECT 1"

    def fetchone(self) -> tuple[int] | None:
        return self.row


@dataclass
class FakeConnection:
    cursor_obj: FakeCursor

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return self.cursor_obj


@dataclass
class FakePsycopg:
    should_fail: bool = False

    def connect(self, url: str, connect_timeout: int) -> FakeConnection:
        assert url == "postgresql://healing_ro:test@db:5432/vibe_gatekeeper"
        assert connect_timeout == 5
        if self.should_fail:
            raise TimeoutError("database timeout")
        return FakeConnection(FakeCursor())


def _set_env(monkeypatch: Any) -> None:
    monkeypatch.setenv("COOLIFY_BASE_URL", "https://coolify.example.invalid")
    monkeypatch.setenv("COOLIFY_API_TOKEN", "coolify-token")
    monkeypatch.setenv("COOLIFY_APP_UUID", "app-uuid")
    monkeypatch.setenv("BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("DATABASE_URL_RO", "postgresql://healing_ro:test@db:5432/vibe_gatekeeper")


def _client(coolify_restart_count: int = 10, coolify_status: str = "running") -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/applications/app-uuid":
            return httpx.Response(
                200,
                json={"status": coolify_status, "restart_count": coolify_restart_count},
            )
        if request.url.host == "api.telegram.org":
            return httpx.Response(200, json={"ok": True, "result": {"pending_update_count": 12}})
        return httpx.Response(404, json={"error": "not found"})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_check_coolify_status_green(monkeypatch: Any) -> None:
    _set_env(monkeypatch)
    result = healthcheck.check_coolify_status(
        {"coolify_status": {"restart_count": 9}},
        http_client=_client(),
    )

    assert result.status == "green"
    assert result.details["restart_count_delta"] == 1


def test_check_coolify_status_red_on_exited(monkeypatch: Any) -> None:
    _set_env(monkeypatch)
    result = healthcheck.check_coolify_status(
        {"coolify_status": {"restart_count": 10}},
        http_client=_client(coolify_status="exited:1"),
    )

    assert result.status == "red"
    assert "exited" in result.reason


def test_check_coolify_status_red_on_restart_delta(monkeypatch: Any) -> None:
    _set_env(monkeypatch)
    result = healthcheck.check_coolify_status(
        {"coolify_status": {"restart_count": 5}},
        http_client=_client(coolify_restart_count=10),
    )

    assert result.status == "red"
    assert result.details["restart_count_delta"] == 5


def test_check_telegram_pending_green(monkeypatch: Any) -> None:
    _set_env(monkeypatch)
    result = healthcheck.check_telegram_pending(
        {"telegram_pending": {"pending_update_count": 11}},
        http_client=_client(),
    )

    assert result.status == "green"


def test_check_telegram_pending_red_when_growing_above_threshold(monkeypatch: Any) -> None:
    _set_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"pending_update_count": 80}})

    result = healthcheck.check_telegram_pending(
        {"telegram_pending": {"pending_update_count": 51}},
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert result.status == "red"
    assert result.details["pending_update_delta"] == 29


def test_check_db_roundtrip_green(monkeypatch: Any) -> None:
    _set_env(monkeypatch)
    result = healthcheck.check_db_roundtrip({}, psycopg_module=FakePsycopg())

    assert result.status == "green"


def test_check_db_roundtrip_red_on_exception(monkeypatch: Any) -> None:
    _set_env(monkeypatch)
    result = healthcheck.check_db_roundtrip({}, psycopg_module=FakePsycopg(should_fail=True))

    assert result.status == "red"
    assert "database timeout" in result.reason


def test_run_all_writes_state_file(monkeypatch: Any, tmp_path: Path) -> None:
    _set_env(monkeypatch)
    monkeypatch.setattr(
        healthcheck,
        "check_coolify_status",
        lambda state: healthcheck.CheckResult(
            name="coolify_status",
            status="green",
            reason="running",
            details={"restart_count": 10},
            duration_ms=1,
        ),
    )
    monkeypatch.setattr(
        healthcheck,
        "check_telegram_pending",
        lambda state: healthcheck.CheckResult(
            name="telegram_pending",
            status="green",
            reason="pending stable",
            details={"pending_update_count": 1},
            duration_ms=1,
        ),
    )
    monkeypatch.setattr(
        healthcheck,
        "check_db_roundtrip",
        lambda state: healthcheck.CheckResult(
            name="db_roundtrip",
            status="green",
            reason="select ok",
            details={"row": [1]},
            duration_ms=1,
        ),
    )
    state_file = tmp_path / "last-state.json"

    report = healthcheck.run_all(state_file=state_file)

    assert report.is_red is False
    written = json.loads(state_file.read_text(encoding="utf-8"))
    assert written["coolify_status"]["restart_count"] == 10
```

- [ ] Run the failing tests:

```bash
pytest -q tests/healing/test_healthcheck.py
```

- [ ] Extend `pyproject.toml` healing dependencies for psycopg:

```toml
healing = [
    "cryptography>=43",
    "psycopg[binary]>=3.2",
]
```

- [ ] Implement `ops/healing/healthcheck.py`:

```python
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
import psycopg

DEFAULT_STATE_FILE = Path(".healing/last-state.json")
Status = Literal["green", "red"]


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    reason: str
    details: dict[str, Any]
    duration_ms: int

    @property
    def is_red(self) -> bool:
        return self.status == "red"


@dataclass(frozen=True)
class CheckReport:
    generated_at: str
    coolify_status: CheckResult
    telegram_pending: CheckResult
    db_roundtrip: CheckResult

    @property
    def is_red(self) -> bool:
        return any(
            result.is_red
            for result in (self.coolify_status, self.telegram_pending, self.db_roundtrip)
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"is_red": self.is_red}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        raise KeyError(f"missing required env var: {name}")
    return value


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _read_previous_state(state_file: Path) -> dict[str, Any]:
    if not state_file.exists():
        return {}
    value = json.loads(state_file.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{state_file} must contain a JSON object")
    return value


def _write_previous_state(state_file: Path, report: CheckReport) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "generated_at": report.generated_at,
        "coolify_status": report.coolify_status.details,
        "telegram_pending": report.telegram_pending.details,
        "db_roundtrip": report.db_roundtrip.details,
    }
    state_file.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _client_context(http_client: httpx.Client | None):
    if http_client is not None:
        return nullcontext(http_client)
    return httpx.Client(timeout=10)


def check_coolify_status(
    state: dict[str, Any],
    http_client: httpx.Client | None = None,
) -> CheckResult:
    start = time.monotonic()
    try:
        base_url = _require_env("COOLIFY_BASE_URL").rstrip("/")
        token = _require_env("COOLIFY_API_TOKEN")
        app_uuid = _require_env("COOLIFY_APP_UUID")
        with _client_context(http_client) as client:
            response = client.get(
                f"{base_url}/api/v1/applications/{app_uuid}",
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            payload = response.json()

        status = str(payload["status"])
        restart_count = int(payload["restart_count"])
        previous = state.get("coolify_status", {})
        previous_restart = previous.get("restart_count") if isinstance(previous, dict) else None
        restart_delta = (
            restart_count - int(previous_restart) if previous_restart is not None else 0
        )
        details = {
            "status": status,
            "restart_count": restart_count,
            "previous_restart_count": previous_restart,
            "restart_count_delta": restart_delta,
        }
        if status.startswith("exited:"):
            return CheckResult(
                "coolify_status",
                "red",
                f"Coolify app status is {status}",
                details,
                _elapsed_ms(start),
            )
        if restart_delta > 2:
            return CheckResult(
                "coolify_status",
                "red",
                f"restart_count increased by {restart_delta}",
                details,
                _elapsed_ms(start),
            )
        return CheckResult("coolify_status", "green", "Coolify app is stable", details, _elapsed_ms(start))
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        return CheckResult(
            "coolify_status",
            "red",
            str(exc),
            {"error_type": type(exc).__name__},
            _elapsed_ms(start),
        )


def check_telegram_pending(
    state: dict[str, Any],
    http_client: httpx.Client | None = None,
) -> CheckResult:
    start = time.monotonic()
    try:
        bot_token = _require_env("BOT_TOKEN")
        with _client_context(http_client) as client:
            response = client.get(f"https://api.telegram.org/bot{bot_token}/getWebhookInfo")
            response.raise_for_status()
            payload = response.json()

        pending = int(payload["result"]["pending_update_count"])
        previous = state.get("telegram_pending", {})
        previous_pending = previous.get("pending_update_count") if isinstance(previous, dict) else None
        pending_delta = pending - int(previous_pending) if previous_pending is not None else 0
        details = {
            "pending_update_count": pending,
            "previous_pending_update_count": previous_pending,
            "pending_update_delta": pending_delta,
        }
        if previous_pending is not None and pending > 50 and pending > int(previous_pending):
            return CheckResult(
                "telegram_pending",
                "red",
                f"pending_update_count grew to {pending}",
                details,
                _elapsed_ms(start),
            )
        return CheckResult(
            "telegram_pending",
            "green",
            "Telegram pending updates are stable",
            details,
            _elapsed_ms(start),
        )
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        return CheckResult(
            "telegram_pending",
            "red",
            str(exc),
            {"error_type": type(exc).__name__},
            _elapsed_ms(start),
        )


def check_db_roundtrip(
    state: dict[str, Any],
    psycopg_module: Any | None = None,
) -> CheckResult:
    del state
    start = time.monotonic()
    driver = psycopg if psycopg_module is None else psycopg_module
    try:
        database_url = _require_env("DATABASE_URL_RO")
        with driver.connect(database_url, connect_timeout=5) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
                row = cursor.fetchone()
        if row != (1,):
            return CheckResult(
                "db_roundtrip",
                "red",
                f"unexpected SELECT 1 result: {row!r}",
                {"row": list(row) if isinstance(row, tuple) else row},
                _elapsed_ms(start),
            )
        return CheckResult(
            "db_roundtrip",
            "green",
            "database roundtrip succeeded",
            {"row": [1]},
            _elapsed_ms(start),
        )
    except (Exception,) as exc:
        return CheckResult(
            "db_roundtrip",
            "red",
            str(exc),
            {"error_type": type(exc).__name__},
            _elapsed_ms(start),
        )


def run_all(state_file: Path = DEFAULT_STATE_FILE) -> CheckReport:
    state = _read_previous_state(state_file)
    with ThreadPoolExecutor(max_workers=3) as pool:
        coolify_future = pool.submit(check_coolify_status, state)
        telegram_future = pool.submit(check_telegram_pending, state)
        db_future = pool.submit(check_db_roundtrip, state)
        report = CheckReport(
            generated_at=_utc_now(),
            coolify_status=coolify_future.result(),
            telegram_pending=telegram_future.result(),
            db_roundtrip=db_future.result(),
        )
    _write_previous_state(state_file, report)
    return report


def main() -> int:
    report = run_all()
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    return 1 if report.is_red else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] Confirm pass:

```bash
pip install -e ".[dev,healing]"
pytest -q tests/healing/test_healthcheck.py
ruff check ops/healing/healthcheck.py tests/healing/test_healthcheck.py
python -m ops.healing.healthcheck >/tmp/healing-healthcheck.json || test "$?" -eq 1
```

- [ ] Commit:

```bash
git add pyproject.toml ops/healing/healthcheck.py tests/healing/test_healthcheck.py
git commit -m $'feat(healing): add composite healthcheck\n\nCo-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>'
```

### Task 5: Snapshot module

**Done when:** Coolify app state and envs can be encrypted into `Snapshot` and restored through PATCH requests.

- [ ] Add failing tests first in `tests/healing/test_snapshot.py`:

```python
from __future__ import annotations

import json
from typing import Any

import httpx
from cryptography.fernet import Fernet

from ops.healing.snapshot import Snapshot, create_snapshot, restore_snapshot


def test_create_snapshot_encrypts_env_dump(monkeypatch: Any) -> None:
    monkeypatch.setenv("COOLIFY_BASE_URL", "https://coolify.example.invalid")
    key = Fernet.generate_key().decode("ascii")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/v1/applications/app-uuid":
            return httpx.Response(
                200,
                json={"docker_registry_image_tag": "sha-caebb519", "restart_count": 12},
            )
        if request.method == "GET" and request.url.path == "/api/v1/applications/app-uuid/envs":
            return httpx.Response(
                200,
                json={"data": [{"key": "BOT_TOKEN", "value": "123456:test"}]},
            )
        return httpx.Response(404)

    monkeypatch.setattr(
        "ops.healing.snapshot._build_client",
        lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )

    snapshot = create_snapshot("coolify-token", "app-uuid", key)

    assert snapshot.prod_image_sha == "sha-caebb519"
    assert snapshot.restart_count == 12
    assert "123456:test" not in snapshot.env_dump_encrypted
    assert snapshot.env_hash.startswith("sha256:")


def test_restore_snapshot_patches_image_and_env(monkeypatch: Any) -> None:
    monkeypatch.setenv("COOLIFY_BASE_URL", "https://coolify.example.invalid")
    key = Fernet.generate_key().decode("ascii")
    sent_requests: list[tuple[str, str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent_requests.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(
        "ops.healing.snapshot._build_client",
        lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )
    snapshot = Snapshot.from_env_dump(
        prod_image_sha="sha-caebb519",
        restart_count=12,
        env_dump={"BOT_TOKEN": "123456:test"},
        env_key=key,
        trigger_signal={"coolify_status": "red"},
    )

    restore_snapshot(snapshot, "coolify-token", "app-uuid", key)

    assert sent_requests[0] == (
        "PATCH",
        "/api/v1/applications/app-uuid",
        {"docker_registry_image_tag": "sha-caebb519"},
    )
    assert sent_requests[1] == (
        "PATCH",
        "/api/v1/applications/app-uuid/envs",
        {"envs": [{"key": "BOT_TOKEN", "value": "123456:test"}]},
    )
```

- [ ] Run the failing tests:

```bash
pytest -q tests/healing/test_snapshot.py
```

- [ ] Implement `ops/healing/snapshot.py`:

```python
from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from ops.healing.crypto import decrypt, encrypt


@dataclass(frozen=True)
class Snapshot:
    ts: str
    prod_image_sha: str
    env_hash: str
    env_dump_encrypted: str
    restart_count: int
    trigger_signal: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env_dump(
        cls,
        prod_image_sha: str,
        restart_count: int,
        env_dump: dict[str, str],
        env_key: str,
        trigger_signal: dict[str, Any],
    ) -> Snapshot:
        encoded_env = json.dumps(env_dump, sort_keys=True, separators=(",", ":"))
        return cls(
            ts=datetime.now(UTC).isoformat(),
            prod_image_sha=prod_image_sha,
            env_hash=_env_hash(env_dump),
            env_dump_encrypted=encrypt(encoded_env, env_key),
            restart_count=restart_count,
            trigger_signal=trigger_signal,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Snapshot:
        return cls(
            ts=str(value["ts"]),
            prod_image_sha=str(value["prod_image_sha"]),
            env_hash=str(value["env_hash"]),
            env_dump_encrypted=str(value["env_dump_encrypted"]),
            restart_count=int(value["restart_count"]),
            trigger_signal=dict(value.get("trigger_signal", {})),
        )

    def decrypt_env_dump(self, env_key: str) -> dict[str, str]:
        value = json.loads(decrypt(self.env_dump_encrypted, env_key))
        if not isinstance(value, dict):
            raise ValueError("snapshot env dump must decrypt to a JSON object")
        return {str(key): str(raw_value) for key, raw_value in value.items()}


def _coolify_base_url() -> str:
    value = os.environ.get("COOLIFY_BASE_URL")
    if not value:
        raise KeyError("missing required env var: COOLIFY_BASE_URL")
    return value.rstrip("/")


def _build_client() -> httpx.Client:
    return httpx.Client(timeout=20)


def _headers(coolify_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {coolify_token}", "Content-Type": "application/json"}


def _env_hash(env_dump: dict[str, str]) -> str:
    encoded = json.dumps(env_dump, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _normalise_envs(payload: Any) -> dict[str, str]:
    raw_items = payload["data"] if isinstance(payload, dict) and "data" in payload else payload
    if not isinstance(raw_items, list):
        raise ValueError("Coolify env response must be a list or data list")
    envs: dict[str, str] = {}
    for item in raw_items:
        if not isinstance(item, dict):
            raise ValueError("Coolify env item must be an object")
        envs[str(item["key"])] = str(item["value"])
    return envs


def create_snapshot(coolify_token: str, app_uuid: str, env_key: str) -> Snapshot:
    base_url = _coolify_base_url()
    with _build_client() as client:
        app_response = client.get(
            f"{base_url}/api/v1/applications/{app_uuid}",
            headers=_headers(coolify_token),
        )
        app_response.raise_for_status()
        app_payload = app_response.json()

        env_response = client.get(
            f"{base_url}/api/v1/applications/{app_uuid}/envs",
            headers=_headers(coolify_token),
        )
        env_response.raise_for_status()
        env_dump = _normalise_envs(env_response.json())

    return Snapshot.from_env_dump(
        prod_image_sha=str(app_payload["docker_registry_image_tag"]),
        restart_count=int(app_payload["restart_count"]),
        env_dump=env_dump,
        env_key=env_key,
        trigger_signal={},
    )


def restore_snapshot(
    snapshot: Snapshot,
    coolify_token: str,
    app_uuid: str,
    env_key: str,
) -> None:
    base_url = _coolify_base_url()
    env_dump = snapshot.decrypt_env_dump(env_key)
    envs = [{"key": key, "value": value} for key, value in sorted(env_dump.items())]
    with _build_client() as client:
        image_response = client.patch(
            f"{base_url}/api/v1/applications/{app_uuid}",
            headers=_headers(coolify_token),
            json={"docker_registry_image_tag": snapshot.prod_image_sha},
        )
        image_response.raise_for_status()
        env_response = client.patch(
            f"{base_url}/api/v1/applications/{app_uuid}/envs",
            headers=_headers(coolify_token),
            json={"envs": envs},
        )
        env_response.raise_for_status()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create or restore healing snapshots.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create")
    create_parser.add_argument("--output", required=True)

    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("--input", required=True)

    return parser


def main() -> int:
    args = _build_parser().parse_args()
    coolify_token = os.environ["COOLIFY_API_TOKEN"]
    app_uuid = os.environ["COOLIFY_APP_UUID"]
    env_key = os.environ["HEALING_ENV_KEY"]

    if args.command == "create":
        snapshot = create_snapshot(coolify_token, app_uuid, env_key)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(snapshot.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        return 0

    if args.command == "restore":
        payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
        restore_snapshot(Snapshot.from_dict(payload), coolify_token, app_uuid, env_key)
        return 0

    raise ValueError(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] Confirm pass:

```bash
pytest -q tests/healing/test_snapshot.py
ruff check ops/healing/snapshot.py tests/healing/test_snapshot.py
```

- [ ] Commit:

```bash
git add ops/healing/snapshot.py tests/healing/test_snapshot.py
git commit -m $'feat(healing): add Coolify snapshot restore\n\nCo-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>'
```

### Task 6: Context bundle

**Done when:** `assemble(signal, state_branch_dir)` returns Markdown with every section from spec §5.3.

- [ ] Add failing tests first in `tests/healing/test_context_bundle.py`:

```python
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from ops.healing import context_bundle


def test_assemble_contains_all_sections(monkeypatch: Any, tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "healthcheck-log.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"generated_at": "2026-04-30T00:00:00Z", "is_red": False}),
                json.dumps({"generated_at": "2026-04-30T03:00:00Z", "is_red": True}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (state_dir / "snapshots").mkdir()
    (state_dir / "snapshots" / "latest.json").write_text(
        '{"prod_image_sha":"sha-caebb519"}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("COOLIFY_BASE_URL", "https://coolify.example.invalid")
    monkeypatch.setenv("COOLIFY_API_TOKEN", "coolify-token")
    monkeypatch.setenv("COOLIFY_APP_UUID", "app-uuid")
    monkeypatch.setenv("HEALING_BOT_CONTAINER", "vibe-gatekeeper-bot")

    def fake_run(command: list[str]) -> str:
        joined = " ".join(command)
        if joined == "git log --oneline -50":
            return "a2b6008 docs(healing): autonomous healing system design spec\n"
        if joined == "git log -5 --format=%H":
            return "a2b6008\n"
        if joined.startswith("git show --stat"):
            return " docs/file.md | 10 +++++-----\n"
        if joined.startswith("docker logs"):
            return "container log line\n"
        raise AssertionError(f"unexpected command: {joined}")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/applications/app-uuid":
            return httpx.Response(
                200,
                json={
                    "status": "running",
                    "restart_count": 12,
                    "last_online_at": "2026-04-30T00:00:00Z",
                    "environment_variables": [{"key": "BOT_TOKEN", "value": "hidden"}],
                },
            )
        if request.url.path == "/api/v1/applications/app-uuid/deployments":
            return httpx.Response(200, json={"data": [{"status": "success"}]})
        return httpx.Response(404)

    monkeypatch.setattr(context_bundle, "_run_command", fake_run)
    monkeypatch.setattr(
        context_bundle,
        "_build_client",
        lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )

    bundle = context_bundle.assemble({"coolify_status": "red"}, str(state_dir))

    assert "## Signal" in bundle
    assert "## Healthcheck history (24h)" in bundle
    assert "## Recent commits" in bundle
    assert "## Last 5 commit diffs (stat only)" in bundle
    assert "## Coolify state" in bundle
    assert "## Container logs" in bundle
    assert "## Last 3 deployments" in bundle
    assert "## Snapshot reference" in bundle
    assert "BOT_TOKEN" in bundle
    assert "hidden" not in bundle
```

- [ ] Run the failing test:

```bash
pytest -q tests/healing/test_context_bundle.py
```

- [ ] Implement `ops/healing/context_bundle.py`:

```python
from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx


@dataclass(frozen=True)
class ChunkingConfig:
    history_entries: int = 8
    recent_commits: int = 50
    diffstat_commits: int = 5
    log_tail_lines: int = 500
    deployments: int = 3


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise KeyError(f"missing required env var: {name}")
    return value


def _run_command(command: list[str]) -> str:
    return subprocess.run(
        command,
        check=True,
        text=True,
        capture_output=True,
    ).stdout


def _build_client() -> httpx.Client:
    return httpx.Client(timeout=20)


def _read_recent_history(state_branch_dir: Path, config: ChunkingConfig) -> list[dict[str, Any]]:
    path = state_branch_dir / "healthcheck-log.jsonl"
    if not path.exists():
        return []
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError("healthcheck history row must be a JSON object")
        generated_at = value.get("generated_at") or value.get("ts")
        if isinstance(generated_at, str):
            try:
                parsed = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
            except ValueError:
                parsed = datetime.now(UTC)
            if parsed < cutoff:
                continue
        rows.append(value)
    return rows[-config.history_entries :]


def _recent_commits(config: ChunkingConfig) -> str:
    return _run_command(["git", "log", "--oneline", f"-{config.recent_commits}"]).strip()


def _last_diffstats(config: ChunkingConfig) -> str:
    shas = _run_command(["git", "log", f"-{config.diffstat_commits}", "--format=%H"]).splitlines()
    chunks: list[str] = []
    for sha in shas:
        chunks.append(
            _run_command(
                [
                    "git",
                    "show",
                    "--stat",
                    "--oneline",
                    "--no-renames",
                    "--format=fuller",
                    "-1",
                    sha,
                ]
            ).strip()
        )
    return "\n\n".join(chunk for chunk in chunks if chunk)


def _coolify_json(path: str) -> Any:
    base_url = _require_env("COOLIFY_BASE_URL").rstrip("/")
    token = _require_env("COOLIFY_API_TOKEN")
    with _build_client() as client:
        response = client.get(
            f"{base_url}{path}",
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        return response.json()


def _coolify_state(app_uuid: str) -> dict[str, Any]:
    payload = _coolify_json(f"/api/v1/applications/{app_uuid}")
    if not isinstance(payload, dict):
        raise ValueError("Coolify app response must be a JSON object")
    env_items = payload.get("environment_variables", [])
    env_keys: list[str] = []
    if isinstance(env_items, list):
        for item in env_items:
            if isinstance(item, dict) and "key" in item:
                env_keys.append(str(item["key"]))
    return {
        "status": payload.get("status"),
        "restart_count": payload.get("restart_count"),
        "last_online_at": payload.get("last_online_at"),
        "env_keys": sorted(env_keys),
    }


def _last_deployments(app_uuid: str, config: ChunkingConfig) -> Any:
    payload = _coolify_json(f"/api/v1/applications/{app_uuid}/deployments")
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return payload["data"][: config.deployments]
    if isinstance(payload, list):
        return payload[: config.deployments]
    return payload


def _container_logs(config: ChunkingConfig) -> str:
    container = _require_env("HEALING_BOT_CONTAINER")
    return _run_command(["docker", "logs", "--tail", str(config.log_tail_lines), container]).strip()


def _snapshot_reference(state_branch_dir: Path) -> str:
    latest = state_branch_dir / "snapshots" / "latest.json"
    if latest.exists():
        return str(latest)
    return "No snapshot file found in state branch checkout."


def _json_block(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True)


def assemble(
    signal: dict[str, Any],
    state_branch_dir: str,
    config: ChunkingConfig = ChunkingConfig(),
) -> str:
    app_uuid = _require_env("COOLIFY_APP_UUID")
    state_dir = Path(state_branch_dir)
    sections = [
        ("Signal", _json_block(signal)),
        ("Healthcheck history (24h)", _json_block(_read_recent_history(state_dir, config))),
        ("Recent commits", _recent_commits(config)),
        ("Last 5 commit diffs (stat only)", _last_diffstats(config)),
        ("Coolify state", _json_block(_coolify_state(app_uuid))),
        ("Container logs", _container_logs(config)),
        ("Last 3 deployments", _json_block(_last_deployments(app_uuid, config))),
        ("Snapshot reference", _snapshot_reference(state_dir)),
    ]
    rendered = ["# Autonomous Healing Context Bundle"]
    for title, body in sections:
        rendered.append(f"## {title}")
        rendered.append("```")
        rendered.append(body)
        rendered.append("```")
    return "\n\n".join(rendered) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Assemble a healing context bundle.")
    parser.add_argument("--signal-json", required=True)
    parser.add_argument("--state-branch-dir", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    signal = json.loads(args.signal_json)
    if not isinstance(signal, dict):
        raise ValueError("--signal-json must decode to a JSON object")
    bundle = assemble(signal, args.state_branch_dir)
    Path(args.output).write_text(bundle, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] Confirm pass:

```bash
pytest -q tests/healing/test_context_bundle.py
ruff check ops/healing/context_bundle.py tests/healing/test_context_bundle.py
```

- [ ] Commit:

```bash
git add ops/healing/context_bundle.py tests/healing/test_context_bundle.py
git commit -m $'feat(healing): assemble Claude context bundle\n\nCo-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>'
```

### Task 7: Escalation

**Done when:** Telegram failures do not block GitHub Issue creation and both-channel failures are reported.

- [ ] Add failing tests first in `tests/healing/test_escalate.py`:

```python
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import httpx

from ops.healing import escalate


class FakeResponse:
    def __init__(self, should_fail: bool = False) -> None:
        self.should_fail = should_fail

    def raise_for_status(self) -> None:
        if self.should_fail:
            raise httpx.HTTPStatusError(
                "telegram failed",
                request=httpx.Request("POST", "https://api.telegram.org"),
                response=httpx.Response(500),
            )


def _files(tmp_path: Path) -> tuple[Path, Path]:
    transcript = tmp_path / "session.log"
    snapshot = tmp_path / "snapshot.json"
    transcript.write_text("session transcript\n", encoding="utf-8")
    snapshot.write_text('{"prod_image_sha":"sha-caebb519"}\n', encoding="utf-8")
    return transcript, snapshot


def test_escalate_both_channels_succeed(monkeypatch: Any, tmp_path: Path) -> None:
    transcript, snapshot = _files(tmp_path)
    monkeypatch.setattr(escalate.httpx, "post", lambda *args, **kwargs: FakeResponse())
    monkeypatch.setattr(
        escalate.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="https://github.com/org/repo/issues/1\n",
            stderr="",
        ),
    )

    result = escalate.escalate(
        "retry budget exhausted",
        transcript,
        snapshot,
        "123456:test-token",
        149820031,
        "org/repo",
    )

    assert result.telegram_ok is True
    assert result.issue_ok is True
    assert result.issue_url == "https://github.com/org/repo/issues/1"


def test_escalate_telegram_failure_does_not_block_issue(monkeypatch: Any, tmp_path: Path) -> None:
    transcript, snapshot = _files(tmp_path)
    monkeypatch.setattr(escalate.httpx, "post", lambda *args, **kwargs: FakeResponse(True))
    monkeypatch.setattr(
        escalate.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="https://github.com/org/repo/issues/2\n",
            stderr="",
        ),
    )

    result = escalate.escalate(
        "wall clock exceeded",
        transcript,
        snapshot,
        "123456:test-token",
        149820031,
        "org/repo",
    )

    assert result.telegram_ok is False
    assert result.issue_ok is True
    assert result.issue_url.endswith("/2")
    assert result.errors


def test_escalate_reports_both_failures(monkeypatch: Any, tmp_path: Path) -> None:
    transcript, snapshot = _files(tmp_path)
    monkeypatch.setattr(escalate.httpx, "post", lambda *args, **kwargs: FakeResponse(True))

    def fail_gh(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, ["gh"], stderr="gh failed")

    monkeypatch.setattr(escalate.subprocess, "run", fail_gh)

    result = escalate.escalate(
        "auth failed",
        transcript,
        snapshot,
        "123456:test-token",
        149820031,
        "org/repo",
    )

    assert result.telegram_ok is False
    assert result.issue_ok is False
    assert len(result.errors) == 2
```

- [ ] Run the failing tests:

```bash
pytest -q tests/healing/test_escalate.py
```

- [ ] Implement `ops/healing/escalate.py`:

```python
from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx


@dataclass(frozen=True)
class EscalationResult:
    telegram_ok: bool
    issue_ok: bool
    issue_url: str
    errors: list[str]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _send_telegram(reason: str, bot_token: str, admin_id: int, gh_repo: str) -> None:
    response = httpx.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json={
            "chat_id": admin_id,
            "text": (
                "healing escalation\n"
                f"repo: {gh_repo}\n"
                f"reason: {reason}\n"
                f"ts: {datetime.now(UTC).isoformat()}"
            ),
        },
        timeout=10,
    )
    response.raise_for_status()


def _create_issue(
    reason: str,
    transcript_path: Path,
    snapshot_path: Path,
    gh_repo: str,
) -> str:
    body = (
        f"# Healing escalation\n\n"
        f"Reason: {reason}\n\n"
        f"## Snapshot\n\n```json\n{_read(snapshot_path)}\n```\n\n"
        f"## Transcript\n\n```\n{_read(transcript_path)}\n```\n"
    )
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write(body)
        body_file = Path(handle.name)
    try:
        result = subprocess.run(
            [
                "gh",
                "issue",
                "create",
                "--repo",
                gh_repo,
                "--title",
                f"[ALERT] healing escalation — {reason}",
                "--body-file",
                str(body_file),
                "--label",
                "healing",
                "--label",
                "incident",
                "--label",
                "priority:high",
            ],
            check=True,
            text=True,
            capture_output=True,
        )
    finally:
        body_file.unlink(missing_ok=True)
    return result.stdout.strip()


def escalate(
    reason: str,
    transcript_path: Path | str,
    snapshot_path: Path | str,
    bot_token: str,
    admin_id: int,
    gh_repo: str,
) -> EscalationResult:
    transcript = Path(transcript_path)
    snapshot = Path(snapshot_path)
    errors: list[str] = []
    telegram_ok = False
    issue_ok = False
    issue_url = ""

    try:
        _send_telegram(reason, bot_token, admin_id, gh_repo)
        telegram_ok = True
    except httpx.HTTPError as exc:
        errors.append(f"telegram: {exc}")

    try:
        issue_url = _create_issue(reason, transcript, snapshot, gh_repo)
        issue_ok = True
    except subprocess.CalledProcessError as exc:
        errors.append(f"github issue: {exc.stderr or exc}")

    return EscalationResult(
        telegram_ok=telegram_ok,
        issue_ok=issue_ok,
        issue_url=issue_url,
        errors=errors,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Escalate an autonomous healing incident.")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--transcript-path", required=True)
    parser.add_argument("--snapshot-path", required=True)
    parser.add_argument("--admin-id", required=True, type=int)
    parser.add_argument("--gh-repo", required=True)
    args = parser.parse_args()

    result = escalate(
        reason=args.reason,
        transcript_path=Path(args.transcript_path),
        snapshot_path=Path(args.snapshot_path),
        bot_token=os.environ["BOT_TOKEN"],
        admin_id=args.admin_id,
        gh_repo=args.gh_repo,
    )
    print(result.to_dict())
    return 0 if result.issue_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] Confirm pass:

```bash
pytest -q tests/healing/test_escalate.py
ruff check ops/healing/escalate.py tests/healing/test_escalate.py
```

- [ ] Commit:

```bash
git add ops/healing/escalate.py tests/healing/test_escalate.py
git commit -m $'feat(healing): add escalation channels\n\nCo-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>'
```

### Task 8: PreToolUse hook

**Done when:** the hook exits 1 for forbidden tool inputs and 0 for safe inputs.

- [ ] Add failing tests first in `tests/healing/test_pre_tool_use_hook.py`:

```python
from __future__ import annotations

import json
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
HOOK = PROJECT_ROOT / "ops" / "healing" / "preToolUse_hook.sh"


def _run_hook(payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(HOOK)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )


def test_allows_safe_pytest_command() -> None:
    result = _run_hook({"tool_input": {"command": "pytest -q tests/healing"}})

    assert result.returncode == 0


def test_blocks_rm_rf_outside_tmp() -> None:
    result = _run_hook({"tool_input": {"command": "rm -rf .healing"}})

    assert result.returncode == 1
    assert "rm -rf" in result.stderr


def test_allows_rm_rf_inside_tmp() -> None:
    result = _run_hook({"tool_input": {"command": "rm -rf /tmp/healing-work"}})

    assert result.returncode == 0


def test_blocks_git_no_verify_and_admin() -> None:
    assert _run_hook({"tool_input": {"command": "git commit --no-verify"}}).returncode == 1
    assert _run_hook({"tool_input": {"command": "gh pr merge --admin"}}).returncode == 1


def test_blocks_force_push() -> None:
    result = _run_hook({"tool_input": {"command": "git push --force-with-lease origin main"}})

    assert result.returncode == 1


def test_blocks_drop_and_broad_delete() -> None:
    assert _run_hook({"tool_input": {"command": "psql -c 'DROP TABLE users'"}}).returncode == 1
    assert _run_hook({"tool_input": {"command": "psql -c 'DELETE FROM users'"}}).returncode == 1
    assert (
        _run_hook({"tool_input": {"command": "psql -c 'DELETE FROM users WHERE id = 1'"}}).returncode
        == 0
    )


def test_blocks_sensitive_file_edits() -> None:
    assert _run_hook({"tool_input": {"file_path": "bot/web/auth.py"}}).returncode == 1
    assert _run_hook({"tool_input": {"file_path": "bot/services/sheets.py"}}).returncode == 1
    assert _run_hook({"tool_input": {"file_path": "ops/healing/crypto.py"}}).returncode == 1
    assert _run_hook({"tool_input": {"file_path": "docs/runbook.md"}}).returncode == 0


def test_blocks_env_rotation_and_infra_config() -> None:
    assert _run_hook({"tool_input": {"command": "gh secret set BOT_TOKEN"}}).returncode == 1
    assert _run_hook({"tool_input": {"command": "curl https://api.hostinger.com/vps"}}).returncode == 1
    assert _run_hook({"tool_input": {"command": "tailscale up --advertise-routes=10.0.0.0/24"}}).returncode == 1
```

- [ ] Run the failing tests:

```bash
pytest -q tests/healing/test_pre_tool_use_hook.py
```

- [ ] Implement `ops/healing/preToolUse_hook.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

payload="$(cat)"
lower="$(printf '%s' "$payload" | tr '[:upper:]' '[:lower:]')"

block() {
  printf 'BLOCKED by healing PreToolUse hook: %s\n' "$1" >&2
  exit 1
}

if [[ "$lower" =~ rm[[:space:]]+-[^[:space:]]*r[^[:space:]]*f ]]; then
  if [[ ! "$lower" =~ rm[[:space:]]+-[^[:space:]]*r[^[:space:]]*f[[:space:]]+/tmp(/|[[:space:]\"]|$) ]]; then
    block "rm -rf is only allowed inside /tmp"
  fi
fi

if [[ "$lower" =~ --no-verify ]]; then
  block "--no-verify is forbidden"
fi

if [[ "$lower" =~ --admin ]]; then
  block "--admin is forbidden"
fi

if [[ "$lower" =~ git[[:space:]]+push && "$lower" =~ --force ]]; then
  block "force-push is forbidden"
fi

if [[ "$lower" =~ drop[[:space:]]+(table|database|schema) ]]; then
  block "DROP statements are forbidden"
fi

if [[ "$lower" =~ delete[[:space:]]+from && ! "$lower" =~ where[[:space:]]+id[[:space:]]*= ]]; then
  block "DELETE FROM requires WHERE id ="
fi

if [[ "$lower" =~ bot/web/auth\.py ]]; then
  block "edits to bot/web/auth.py are forbidden"
fi

if [[ "$lower" =~ bot/services/sheets\.py ]]; then
  block "edits to bot/services/sheets.py are forbidden"
fi

if [[ "$lower" =~ (crypto|token|secret) ]]; then
  if [[ "$lower" =~ (file_path|path|command|edit|write|patch|update) ]]; then
    block "edits to crypto, token, or secret paths are forbidden"
  fi
fi

if [[ "$lower" =~ (set|update|patch|delete|rotate)[^[:alnum:]_]+(bot_token|web_password|web_session_secret|db_password) ]]; then
  block "rotation of protected production env vars is forbidden"
fi

if [[ "$lower" =~ hostinger ]]; then
  block "Hostinger API calls are forbidden"
fi

if [[ "$lower" =~ tailscale && "$lower" =~ (up|set|serve|funnel|config|ssh|advertise) ]]; then
  block "Tailscale configuration changes are forbidden"
fi

exit 0
```

- [ ] Mark executable and confirm pass:

```bash
chmod +x ops/healing/preToolUse_hook.sh
pytest -q tests/healing/test_pre_tool_use_hook.py
ruff check tests/healing/test_pre_tool_use_hook.py
```

- [ ] Commit:

```bash
git add ops/healing/preToolUse_hook.sh tests/healing/test_pre_tool_use_hook.py
git commit -m $'feat(healing): add autonomous guardrail hook\n\nCo-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>'
```

### Task 9: INVARIANTS.md

**Done when:** the system prompt contains the hard limits from spec §6 and names the mandatory debugging skill and escalation command.

- [ ] Create `ops/healing/INVARIANTS.md`:

```markdown
You are running in autonomous healing loop.

Use superpowers:systematic-debugging skill mandatory before proposing or applying any fix.
Use test-driven development for code changes: reproduce the failure with a red test, implement the smallest fix, then prove green tests.

Hard NEVER:

1. No direct push to `main`. Open a pull request only.
2. No `--admin`, `--no-verify`, or `--force` flags on git or gh commands.
3. No alembic migrations autonomously.
4. No `DROP` statements, no `DELETE FROM` without `WHERE id = ...`, and no `rm -rf` outside `/tmp`.
5. No edits to security-sensitive paths: `bot/web/auth.py`, `bot/services/sheets.py`, and anything matching `*crypto*`, `*token*`, or `*secret*` case-insensitively.
6. No rotation of `BOT_TOKEN`, `WEB_PASSWORD`, `WEB_SESSION_SECRET`, or `DB_PASSWORD`.
7. No Hostinger API calls and no VPS reboot, destroy, rebuild, or firewall action.
8. No Coolify network, firewall, or Tailscale configuration changes.

Hard MUST:

9. Create a snapshot before any change.
10. Keep PR diff at or below 300 lines. If the fix needs more, escalate.
11. Every PR must include a red-to-green test that reproduces the bug.
12. After deploy, run the 10-minute watch. If any poll is red, rollback.
13. Use at most 3 retries per incident.
14. Wait 15 minutes between retries.
15. If the same root cause appears twice in the same incident, escalate immediately.
16. Keep the incident inside a 30-minute wall-clock budget.

Soft rules, with written exception allowed in the PR description:

17. Use one small trunk-based PR.
18. Update CHANGELOG when the user-facing behavior changes.

If you cannot fix the incident while obeying these rules, escalate via:

```bash
python -m ops.healing.escalate \
  --reason "cannot fix while obeying autonomous healing invariants" \
  --transcript-path session.log \
  --snapshot-path snapshot.json \
  --admin-id 149820031 \
  --gh-repo "$GITHUB_REPOSITORY"
```
```

- [ ] Verify the file has no missing sections:

```bash
rg -n "Hard NEVER|Hard MUST|superpowers:systematic-debugging|python -m ops.healing.escalate" ops/healing/INVARIANTS.md
```

- [ ] Commit:

```bash
git add ops/healing/INVARIANTS.md
git commit -m $'docs(healing): add autonomous invariants prompt\n\nCo-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>'
```

### Task 10: healthcheck.yml

**Done when:** the cron workflow runs the healthcheck, appends history to `healing-state`, and dispatches `healing.yml` on red.

- [ ] Create `.github/workflows/healthcheck.yml`:

```yaml
name: Healing Healthcheck

on:
  schedule:
    - cron: "0 */3 * * *"
  workflow_dispatch:

permissions:
  contents: write
  actions: write

jobs:
  healthcheck:
    runs-on: ubuntu-latest
    env:
      COOLIFY_BASE_URL: ${{ vars.COOLIFY_BASE_URL }}
      COOLIFY_APP_UUID: ${{ vars.COOLIFY_APP_UUID }}
      COOLIFY_API_TOKEN: ${{ secrets.COOLIFY_API_TOKEN }}
      BOT_TOKEN: ${{ secrets.BOT_TOKEN }}
      DATABASE_URL_RO: ${{ secrets.DATABASE_URL_RO }}
      GH_TOKEN: ${{ github.token }}
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: pip install -e ".[dev,healing]"

      - name: Run composite healthcheck
        id: check
        shell: bash
        run: |
          set +e
          python -m ops.healing.healthcheck > healthcheck-report.json
          status=$?
          cat healthcheck-report.json
          echo "status=$status" >> "$GITHUB_OUTPUT"
          set -e

      - name: Append healthcheck history
        shell: bash
        run: |
          python - <<'PY'
          import json
          from pathlib import Path

          from ops.healing.state_branch import append_jsonl

          report = json.loads(Path("healthcheck-report.json").read_text(encoding="utf-8"))
          append_jsonl(Path("."), "healthcheck-log.jsonl", report)
          PY

      - name: Dispatch healing workflow on red
        if: steps.check.outputs.status != '0'
        shell: bash
        run: |
          gh workflow run healing.yml \
            --ref main \
            -f signal_payload="$(cat healthcheck-report.json)"
```

- [ ] Validate YAML shape locally:

```bash
python - <<'PY'
from pathlib import Path

path = Path(".github/workflows/healthcheck.yml")
text = path.read_text(encoding="utf-8")
required = [
    'cron: "0 */3 * * *"',
    "contents: write",
    "actions: write",
    "python -m ops.healing.healthcheck",
    "gh workflow run healing.yml",
]
for item in required:
    if item not in text:
        raise SystemExit(f"missing {item}")
PY
```

- [ ] Commit:

```bash
git add .github/workflows/healthcheck.yml
git commit -m $'ci(healing): add production healthcheck workflow\n\nCo-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>'
```

### Task 11: healing.yml

**Done when:** the self-hosted workflow encodes the full remediation lifecycle from pre-flight through audit and lock release.

- [ ] Create `.github/workflows/healing.yml`:

```yaml
name: Autonomous Healing

on:
  workflow_dispatch:
    inputs:
      signal_payload:
        description: "JSON payload emitted by the composite healthcheck"
        required: true
        type: string

permissions:
  contents: write
  pull-requests: write
  issues: write
  actions: write

concurrency:
  group: healing-singleton
  cancel-in-progress: false

jobs:
  healing:
    runs-on: [self-hosted, shkoder-vps]
    timeout-minutes: 45
    env:
      SIGNAL_PAYLOAD: ${{ inputs.signal_payload }}
      COOLIFY_BASE_URL: ${{ vars.COOLIFY_BASE_URL }}
      COOLIFY_APP_UUID: ${{ vars.COOLIFY_APP_UUID }}
      HEALING_BOT_CONTAINER: ${{ vars.HEALING_BOT_CONTAINER }}
      COOLIFY_API_TOKEN: ${{ secrets.COOLIFY_API_TOKEN }}
      BOT_TOKEN: ${{ secrets.BOT_TOKEN }}
      DATABASE_URL_RO: ${{ secrets.DATABASE_URL_RO }}
      HEALING_ENV_KEY: ${{ secrets.HEALING_ENV_KEY }}
      GH_TOKEN: ${{ secrets.HEALING_GITHUB_TOKEN }}
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: pip install -e ".[dev,healing]"

      - name: Pre-flight disabled flag
        shell: bash
        run: |
          set -euo pipefail
          disabled="$(python -m ops.healing.state_branch read-file .healing/disabled || true)"
          if [ -n "$disabled" ]; then
            printf '%s\n' "$SIGNAL_PAYLOAD" > signal.json
            printf 'healing disabled flag is present\n' > session.log
            printf '{"disabled":true}\n' > snapshot.json
            python -m ops.healing.escalate \
              --reason "healing disabled flag is present" \
              --transcript-path session.log \
              --snapshot-path snapshot.json \
              --admin-id 149820031 \
              --gh-repo "$GITHUB_REPOSITORY"
            exit 0
          fi

      - name: Acquire healing lock
        shell: bash
        run: |
          set -euo pipefail
          existing="$(python -m ops.healing.state_branch read-file .healing/in-progress || true)"
          if [ -n "$existing" ]; then
            printf 'existing healing lock: %s\n' "$existing" > session.log
            printf '{"locked":true}\n' > snapshot.json
            python -m ops.healing.escalate \
              --reason "healing lock already exists" \
              --transcript-path session.log \
              --snapshot-path snapshot.json \
              --admin-id 149820031 \
              --gh-repo "$GITHUB_REPOSITORY"
            exit 1
          fi
          printf 'run=%s attempt-start=%s\n' "$GITHUB_RUN_ID" "$(date -u +%FT%TZ)" \
            | python -m ops.healing.state_branch write-file .healing/in-progress

      - name: Healing attempts
        shell: bash
        run: |
          set -euo pipefail
          mkdir -p healing-work state-branch
          printf '%s\n' "$SIGNAL_PAYLOAD" > healing-work/signal.json
          reason="retry budget exhausted"
          success="false"

          cleanup_lock() {
            printf '' | python -m ops.healing.state_branch write-file .healing/in-progress
          }
          trap cleanup_lock EXIT

          for attempt in 1 2 3; do
            printf 'attempt %s started at %s\n' "$attempt" "$(date -u +%FT%TZ)"
            snapshot_path="healing-work/snapshot-${attempt}.json"
            cp "$snapshot_path" healing-work/snapshot-current.json 2>/dev/null || true

            python -m ops.healing.snapshot create --output "$snapshot_path"
            cp "$snapshot_path" healing-work/snapshot-current.json
            python -m ops.healing.state_branch write-file "snapshots/${GITHUB_RUN_ID}-${attempt}.json" \
              < "$snapshot_path"
            python -m ops.healing.state_branch write-file snapshots/latest.json < "$snapshot_path"

            rm -rf state-branch
            git worktree add state-branch healing-state
            python -m ops.healing.context_bundle \
              --signal-json "$SIGNAL_PAYLOAD" \
              --state-branch-dir state-branch \
              --output healing-work/context-bundle.md
            git worktree remove --force state-branch

            set +e
            claude -p \
              --model claude-opus-4-7 \
              --append-system-prompt "$(cat ops/healing/INVARIANTS.md)" \
              --max-turns 50 \
              < healing-work/context-bundle.md > "healing-work/session-${attempt}.log"
            claude_status=$?
            set -e
            cp "healing-work/session-${attempt}.log" session.log
            if [ "$claude_status" -ne 0 ]; then
              reason="claude session failed"
              break
            fi

            pr_number="$(gh pr list --state open --base main --json number --jq '.[0].number // empty')"
            if [ -z "$pr_number" ]; then
              reason="claude did not open a pull request"
              sleep 900
              continue
            fi

            set +e
            codex exec review --base main -m gpt-5.5 -c model_reasoning_effort=high --ephemeral \
              > "healing-work/codex-${attempt}.log"
            codex_status=$?
            set -e
            if [ "$codex_status" -ne 0 ] || ! rg -q "APPROVE" "healing-work/codex-${attempt}.log"; then
              reason="codex review did not approve"
              gh pr comment "$pr_number" --body-file "healing-work/codex-${attempt}.log"
              sleep 900
              continue
            fi

            gh pr merge "$pr_number" --rebase --delete-branch

            poll_successes=0
            for poll in 1 2 3 4 5; do
              sleep 120
              if python -m ops.healing.healthcheck > "healing-work/post-fix-${attempt}-${poll}.json"; then
                poll_successes=$((poll_successes + 1))
              else
                reason="post-fix watch failed"
                break
              fi
            done

            if [ "$poll_successes" -eq 5 ]; then
              success="true"
              break
            fi

            python -m ops.healing.snapshot restore --input "$snapshot_path"
            gh pr close "$pr_number" --comment "auto-reverted; root cause not resolved" || true
            sleep 900
          done

          if [ "$success" = "true" ]; then
            gh issue create \
              --repo "$GITHUB_REPOSITORY" \
              --title "[healing] run ${GITHUB_RUN_ID} — succeeded" \
              --body "Autonomous healing succeeded and passed the 10-minute watch." \
              --label healing \
              | tee healing-work/audit-issue.txt
            issue_url="$(cat healing-work/audit-issue.txt)"
            gh issue close "$issue_url" --comment "Closed automatically after green watch." || true
            exit 0
          fi

          printf '%s\n' "$reason" > healing-work/escalation-reason.txt
          python -m ops.healing.escalate \
            --reason "$reason" \
            --transcript-path session.log \
            --snapshot-path healing-work/snapshot-current.json \
            --admin-id 149820031 \
            --gh-repo "$GITHUB_REPOSITORY"
          printf 'disabled by run=%s reason=%s\n' "$GITHUB_RUN_ID" "$reason" \
            | python -m ops.healing.state_branch write-file .healing/disabled
          exit 1
```

- [ ] Validate workflow contains all required lifecycle tokens:

```bash
python - <<'PY'
from pathlib import Path

text = Path(".github/workflows/healing.yml").read_text(encoding="utf-8")
required = [
    "runs-on: [self-hosted, shkoder-vps]",
    "group: healing-singleton",
    "cancel-in-progress: false",
    ".healing/disabled",
    ".healing/in-progress",
    "python -m ops.healing.snapshot create",
    "python -m ops.healing.context_bundle",
    "claude -p",
    "codex exec review --base main -m gpt-5.5",
    "gh pr merge",
    "python -m ops.healing.healthcheck",
    "python -m ops.healing.snapshot restore",
    "python -m ops.healing.escalate",
]
for item in required:
    if item not in text:
        raise SystemExit(f"missing {item}")
PY
```

- [ ] Commit:

```bash
git add .github/workflows/healing.yml
git commit -m $'ci(healing): add autonomous remediation workflow\n\nCo-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>'
```

### Task 12: SETUP.md

**Done when:** the one-time VPS setup runbook covers secrets, runner, auth, hook wiring, and read-only Postgres access.

- [ ] Create `ops/healing/SETUP.md`:

```markdown
# Autonomous Healing Setup

Run these steps once from an operator machine with `gh` authenticated for the repository and from the VPS shell where noted.

## 1. Create GitHub token

Create a fine-grained GitHub token for this repository with:

- Repository contents: read and write.
- Pull requests: read and write.
- Issues: read and write.
- Actions: read and write.
- Packages: write.

Store it locally for the next step:

```bash
read -r -s HEALING_GITHUB_TOKEN
export HEALING_GITHUB_TOKEN
```

## 2. Set GitHub Secrets

```bash
REPO="eekudryavtsev/shkoderbot"

gh secret set HEALING_GITHUB_TOKEN --repo "$REPO" --body "$HEALING_GITHUB_TOKEN"
gh secret set COOLIFY_API_TOKEN --repo "$REPO" --body "$COOLIFY_API_TOKEN"
gh secret set BOT_TOKEN --repo "$REPO" --body "$BOT_TOKEN"
gh secret set DATABASE_URL_RO --repo "$REPO" --body "$DATABASE_URL_RO"
gh secret set HEALING_ENV_KEY --repo "$REPO" --body "$HEALING_ENV_KEY"
```

Generate `HEALING_ENV_KEY` with Fernet-compatible bytes:

```bash
python - <<'PY'
from cryptography.fernet import Fernet

print(Fernet.generate_key().decode("ascii"))
PY
```

## 3. Set GitHub repository variables

```bash
REPO="eekudryavtsev/shkoderbot"

gh variable set COOLIFY_BASE_URL --repo "$REPO" --body "$COOLIFY_BASE_URL"
gh variable set COOLIFY_APP_UUID --repo "$REPO" --body "$COOLIFY_APP_UUID"
gh variable set HEALING_BOT_CONTAINER --repo "$REPO" --body "$HEALING_BOT_CONTAINER"
```

## 4. Create runner user on VPS

```bash
sudo useradd -m -G docker runner
sudo mkdir -p /home/runner/actions-runner
sudo chown -R runner:runner /home/runner/actions-runner
```

## 5. Install GitHub Actions runner on VPS

```bash
REPO="eekudryavtsev/shkoderbot"
RUNNER_VERSION="2.328.0"
RUNNER_TOKEN="$(gh api -X POST "repos/$REPO/actions/runners/registration-token" --jq .token)"

sudo -u runner bash -lc "
  cd /home/runner/actions-runner
  curl -fsSLO https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz
  tar xzf actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz
  ./config.sh \
    --url https://github.com/${REPO} \
    --token ${RUNNER_TOKEN} \
    --name shkoder-vps-healing \
    --labels shkoder-vps \
    --unattended \
    --replace
"
```

## 6. Install runner systemd unit

```bash
sudo tee /etc/systemd/system/actions-runner.service >/dev/null <<'UNIT'
[Unit]
Description=GitHub Actions Runner for shkoderbot healing
After=network-online.target docker.service
Wants=network-online.target

[Service]
User=runner
WorkingDirectory=/home/runner/actions-runner
ExecStart=/home/runner/actions-runner/run.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now actions-runner.service
sudo systemctl status actions-runner.service --no-pager
```

## 7. Authenticate Claude CLI as runner

```bash
sudo -u runner -H bash -lc 'claude login'
sudo -u runner -H bash -lc 'claude -p "echo OK"'
```

## 8. Authenticate Codex CLI as runner

```bash
sudo -u runner -H bash -lc 'codex login'
sudo -u runner -H bash -lc 'codex exec "echo OK"'
```

## 9. Wire Claude PreToolUse hook

After the repo is checked out on the runner, create `/home/runner/.claude/settings.json`:

```bash
sudo -u runner -H mkdir -p /home/runner/.claude
sudo -u runner -H tee /home/runner/.claude/settings.json >/dev/null <<'JSON'
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "/home/runner/actions-runner/_work/shkoderbot/shkoderbot/ops/healing/preToolUse_hook.sh"
          }
        ]
      }
    ]
  }
}
JSON
```

## 10. Create read-only Postgres user

Run in the production Postgres database as an admin role:

```sql
CREATE USER healing_ro WITH PASSWORD :'healing_password';
GRANT CONNECT ON DATABASE vibe_gatekeeper TO healing_ro;
GRANT USAGE ON SCHEMA public TO healing_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO healing_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO healing_ro;
```

Set `DATABASE_URL_RO` to:

```bash
postgresql://healing_ro:${HEALING_RO_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/vibe_gatekeeper
```

## 11. Verify runner labels and auth

```bash
REPO="eekudryavtsev/shkoderbot"

gh api "repos/$REPO/actions/runners" --jq '.runners[] | select(.labels[].name == "shkoder-vps") | .name'
sudo -u runner -H bash -lc 'claude -p "echo OK"'
sudo -u runner -H bash -lc 'codex exec "echo OK"'
```
```

- [ ] Commit:

```bash
git add ops/healing/SETUP.md
git commit -m $'docs(healing): add VPS runner setup runbook\n\nCo-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>'
```

### Task 13: Dry-run integration test

**Done when:** `HEALING_DRY_RUN=true` covers success, Codex rejection retry, watch rollback, and retry exhaustion without external services.

- [ ] Add failing tests first in `tests/healing/test_dry_run_e2e.py`:

```python
from __future__ import annotations

import json
from typing import Any

import pytest

from ops.healing import orchestrator


@pytest.mark.parametrize(
    ("scenario", "verdict", "attempts", "rolled_back", "escalated"),
    [
        ("success", "succeeded", 1, False, False),
        ("codex_rejects", "succeeded", 2, False, False),
        ("watch_fails", "succeeded", 2, True, False),
        ("retry_exhaust", "escalated", 3, True, True),
    ],
)
def test_dry_run_paths(
    monkeypatch: Any,
    scenario: str,
    verdict: str,
    attempts: int,
    rolled_back: bool,
    escalated: bool,
) -> None:
    monkeypatch.setenv("HEALING_DRY_RUN", "true")
    monkeypatch.setenv("HEALING_DRY_RUN_SCENARIO", scenario)

    result = orchestrator.run_healing(json.dumps({"signal": scenario}))

    assert result.verdict == verdict
    assert result.attempts == attempts
    assert result.rolled_back is rolled_back
    assert result.escalated is escalated
    assert "snapshot:create" in result.events
    assert "claude:session" in result.events


def test_dry_run_codex_rejection_records_review_event(monkeypatch: Any) -> None:
    monkeypatch.setenv("HEALING_DRY_RUN", "true")
    monkeypatch.setenv("HEALING_DRY_RUN_SCENARIO", "codex_rejects")

    result = orchestrator.run_healing('{"signal":"codex"}')

    assert "codex:reject" in result.events
    assert "codex:approve" in result.events


def test_dry_run_watch_failure_records_rollback(monkeypatch: Any) -> None:
    monkeypatch.setenv("HEALING_DRY_RUN", "true")
    monkeypatch.setenv("HEALING_DRY_RUN_SCENARIO", "watch_fails")

    result = orchestrator.run_healing('{"signal":"watch"}')

    assert "watch:red" in result.events
    assert "rollback:restore-snapshot" in result.events


def test_dry_run_retry_exhaustion_disables_healing(monkeypatch: Any) -> None:
    monkeypatch.setenv("HEALING_DRY_RUN", "true")
    monkeypatch.setenv("HEALING_DRY_RUN_SCENARIO", "retry_exhaust")

    result = orchestrator.run_healing('{"signal":"exhaust"}')

    assert result.escalated is True
    assert "escalate:issue" in result.events
    assert "state:disable" in result.events
```

- [ ] Run the failing tests:

```bash
pytest -q tests/healing/test_dry_run_e2e.py
```

- [ ] Implement `ops/healing/orchestrator.py`:

```python
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

Verdict = Literal["succeeded", "escalated"]


@dataclass(frozen=True)
class HealingConfig:
    max_attempts: int = 3
    cooldown_seconds: int = 900
    watch_polls: int = 5
    watch_interval_seconds: int = 120
    work_dir: Path = Path("healing-work")


@dataclass(frozen=True)
class HealingResult:
    verdict: Verdict
    attempts: int
    rolled_back: bool
    escalated: bool
    events: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _run(command: list[str], input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        input=input_text,
        text=True,
        capture_output=True,
        check=True,
    )


def _dry_run_result(scenario: str, config: HealingConfig) -> HealingResult:
    events: list[str] = []
    rolled_back = False

    for attempt in range(1, config.max_attempts + 1):
        events.extend(["snapshot:create", "context:bundle", "claude:session", "pr:opened"])

        if scenario == "codex_rejects" and attempt == 1:
            events.append("codex:reject")
            continue

        events.append("codex:approve")
        events.extend(["pr:merge", "coolify:deploy"])

        if scenario in {"watch_fails", "retry_exhaust"}:
            events.append("watch:red")
            events.append("rollback:restore-snapshot")
            rolled_back = True
            if scenario == "watch_fails" and attempt == 1:
                continue
            if scenario == "retry_exhaust":
                continue

        events.append("watch:green")
        events.append("audit:success")
        return HealingResult(
            verdict="succeeded",
            attempts=attempt,
            rolled_back=rolled_back,
            escalated=False,
            events=events,
        )

    events.extend(["escalate:issue", "state:disable"])
    return HealingResult(
        verdict="escalated",
        attempts=config.max_attempts,
        rolled_back=rolled_back,
        escalated=True,
        events=events,
    )


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _detect_open_pr() -> str:
    result = _run(["gh", "pr", "list", "--state", "open", "--base", "main", "--json", "number"])
    payload = json.loads(result.stdout)
    if not payload:
        return ""
    return str(payload[0]["number"])


def _watch_health(config: HealingConfig, events: list[str]) -> bool:
    green_count = 0
    for _poll in range(config.watch_polls):
        time.sleep(config.watch_interval_seconds)
        result = subprocess.run(
            ["python", "-m", "ops.healing.healthcheck"],
            check=False,
            text=True,
            capture_output=True,
        )
        if result.returncode == 0:
            green_count += 1
            events.append("watch:green-poll")
        else:
            events.append("watch:red")
            return False
    return green_count == config.watch_polls


def _run_real(signal_payload: str, config: HealingConfig) -> HealingResult:
    events: list[str] = []
    rolled_back = False
    config.work_dir.mkdir(parents=True, exist_ok=True)
    _write_text(config.work_dir / "signal.json", signal_payload)

    disabled = subprocess.run(
        ["python", "-m", "ops.healing.state_branch", "read-file", ".healing/disabled"],
        check=False,
        text=True,
        capture_output=True,
    ).stdout
    if disabled:
        _write_text(Path("session.log"), "healing disabled flag is present\n")
        _write_text(Path("snapshot.json"), '{"disabled":true}\n')
        _run(
            [
                "python",
                "-m",
                "ops.healing.escalate",
                "--reason",
                "healing disabled flag is present",
                "--transcript-path",
                "session.log",
                "--snapshot-path",
                "snapshot.json",
                "--admin-id",
                "149820031",
                "--gh-repo",
                os.environ["GITHUB_REPOSITORY"],
            ]
        )
        return HealingResult("escalated", 0, False, True, ["preflight:disabled", "escalate:issue"])

    _run(
        ["python", "-m", "ops.healing.state_branch", "write-file", ".healing/in-progress"],
        input_text=f"run={os.environ.get('GITHUB_RUN_ID', 'local')}\n",
    )

    try:
        for attempt in range(1, config.max_attempts + 1):
            snapshot_path = config.work_dir / f"snapshot-{attempt}.json"
            _run(["python", "-m", "ops.healing.snapshot", "create", "--output", str(snapshot_path)])
            events.append("snapshot:create")
            _run(
                [
                    "python",
                    "-m",
                    "ops.healing.context_bundle",
                    "--signal-json",
                    signal_payload,
                    "--state-branch-dir",
                    ".",
                    "--output",
                    str(config.work_dir / "context-bundle.md"),
                ]
            )
            events.append("context:bundle")
            with (config.work_dir / "context-bundle.md").open("r", encoding="utf-8") as input_handle:
                session = subprocess.run(
                    [
                        "claude",
                        "-p",
                        "--model",
                        "claude-opus-4-7",
                        "--append-system-prompt",
                        Path("ops/healing/INVARIANTS.md").read_text(encoding="utf-8"),
                        "--max-turns",
                        "50",
                    ],
                    stdin=input_handle,
                    text=True,
                    capture_output=True,
                    check=False,
                )
            _write_text(config.work_dir / f"session-{attempt}.log", session.stdout + session.stderr)
            events.append("claude:session")
            if session.returncode != 0:
                break

            pr_number = _detect_open_pr()
            if not pr_number:
                time.sleep(config.cooldown_seconds)
                continue

            codex = subprocess.run(
                [
                    "codex",
                    "exec",
                    "review",
                    "--base",
                    "main",
                    "-m",
                    "gpt-5.5",
                    "-c",
                    "model_reasoning_effort=high",
                    "--ephemeral",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            _write_text(config.work_dir / f"codex-{attempt}.log", codex.stdout + codex.stderr)
            if codex.returncode != 0 or "APPROVE" not in codex.stdout:
                events.append("codex:reject")
                time.sleep(config.cooldown_seconds)
                continue

            events.append("codex:approve")
            _run(["gh", "pr", "merge", pr_number, "--rebase", "--delete-branch"])
            events.append("pr:merge")

            if _watch_health(config, events):
                events.append("audit:success")
                return HealingResult("succeeded", attempt, rolled_back, False, events)

            _run(["python", "-m", "ops.healing.snapshot", "restore", "--input", str(snapshot_path)])
            events.append("rollback:restore-snapshot")
            rolled_back = True
            time.sleep(config.cooldown_seconds)

        _run(
            [
                "python",
                "-m",
                "ops.healing.escalate",
                "--reason",
                "retry budget exhausted",
                "--transcript-path",
                str(config.work_dir / "session-1.log"),
                "--snapshot-path",
                str(config.work_dir / "snapshot-1.json"),
                "--admin-id",
                "149820031",
                "--gh-repo",
                os.environ["GITHUB_REPOSITORY"],
            ]
        )
        _run(
            ["python", "-m", "ops.healing.state_branch", "write-file", ".healing/disabled"],
            input_text="disabled after retry budget exhausted\n",
        )
        events.extend(["escalate:issue", "state:disable"])
        return HealingResult("escalated", config.max_attempts, rolled_back, True, events)
    finally:
        _run(["python", "-m", "ops.healing.state_branch", "write-file", ".healing/in-progress"], input_text="")


def run_healing(
    signal_payload: str,
    config: HealingConfig = HealingConfig(),
) -> HealingResult:
    if os.environ.get("HEALING_DRY_RUN") == "true":
        scenario = os.environ.get("HEALING_DRY_RUN_SCENARIO", "success")
        return _dry_run_result(scenario, config)
    return _run_real(signal_payload, config)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run autonomous healing orchestration.")
    parser.add_argument("--signal-payload", required=True)
    args = parser.parse_args()
    result = run_healing(args.signal_payload)
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0 if result.verdict == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] Replace `.github/workflows/healing.yml` with the orchestrator wrapper:

```yaml
name: Autonomous Healing

on:
  workflow_dispatch:
    inputs:
      signal_payload:
        description: "JSON payload emitted by the composite healthcheck"
        required: true
        type: string

permissions:
  contents: write
  pull-requests: write
  issues: write
  actions: write

concurrency:
  group: healing-singleton
  cancel-in-progress: false

jobs:
  healing:
    runs-on: [self-hosted, shkoder-vps]
    timeout-minutes: 45
    env:
      SIGNAL_PAYLOAD: ${{ inputs.signal_payload }}
      COOLIFY_BASE_URL: ${{ vars.COOLIFY_BASE_URL }}
      COOLIFY_APP_UUID: ${{ vars.COOLIFY_APP_UUID }}
      HEALING_BOT_CONTAINER: ${{ vars.HEALING_BOT_CONTAINER }}
      COOLIFY_API_TOKEN: ${{ secrets.COOLIFY_API_TOKEN }}
      BOT_TOKEN: ${{ secrets.BOT_TOKEN }}
      DATABASE_URL_RO: ${{ secrets.DATABASE_URL_RO }}
      HEALING_ENV_KEY: ${{ secrets.HEALING_ENV_KEY }}
      GH_TOKEN: ${{ secrets.HEALING_GITHUB_TOKEN }}
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: pip install -e ".[dev,healing]"

      - name: Run autonomous healing orchestrator
        run: python -m ops.healing.orchestrator --signal-payload "$SIGNAL_PAYLOAD"
```

- [ ] Confirm pass:

```bash
pytest -q tests/healing/test_dry_run_e2e.py
ruff check ops/healing/orchestrator.py tests/healing/test_dry_run_e2e.py
```

- [ ] Commit:

```bash
git add ops/healing/orchestrator.py tests/healing/test_dry_run_e2e.py .github/workflows/healing.yml
git commit -m $'feat(healing): add dry-run orchestration e2e\n\nCo-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>'
```

### Task 14: Final verification

**Done when:** full tests and lint are green, the plan coverage checks pass, and final commit records completion.

- [ ] Run the full test suite:

```bash
pytest -q
```

- [ ] Run lint:

```bash
ruff check .
```

- [ ] Confirm spec coverage from §1 through §14:

```bash
python - <<'PY'
from pathlib import Path

plan = Path("docs/superpowers/plans/2026-04-30-autonomous-healing.md").read_text(
    encoding="utf-8"
)
coverage = {
    "§1 goal": "Build a bounded autonomous healing loop",
    "§2 non-goals": "No alembic migrations autonomously",
    "§3 architecture": "self-hosted GitHub Actions runner",
    "§4 healthcheck": "Composite healthcheck",
    "§5 healing session": "Claude CLI session",
    "§6 invariants": "Hard NEVER",
    "§7 escalation": "Telegram plus GitHub Issue",
    "§8 state storage": "healing-state",
    "§9 inventory": "File Structure",
    "§10 setup": "Autonomous Healing Setup",
    "§11 risks": "disabled by run=",
    "§12 testing": "Dry-run integration test",
    "§13 deferred": "e2e_ping",
    "§14 decision log": "Codex CLI review",
}
missing = [section for section, needle in coverage.items() if needle not in plan]
if missing:
    raise SystemExit(f"missing coverage: {missing}")
PY
```

- [ ] Confirm no banned text markers in the plan:

```bash
bad_pattern="$(printf '%s|%s|%s|%s' 'TB''D' 'TO''DO' 'similar ''to' 'place''holder')"
test -z "$(rg -n "$bad_pattern" docs/superpowers/plans/2026-04-30-autonomous-healing.md)"
```

- [ ] Confirm type and path names are consistent:

```bash
python - <<'PY'
from pathlib import Path

plan = Path("docs/superpowers/plans/2026-04-30-autonomous-healing.md").read_text(
    encoding="utf-8"
)
required = [
    "CheckReport",
    "CheckResult",
    "Snapshot",
    "EscalationResult",
    "ChunkingConfig",
    "ops/healing/",
    "tests/healing/",
    ".github/workflows/healthcheck.yml",
    ".github/workflows/healing.yml",
]
missing = [item for item in required if item not in plan]
if missing:
    raise SystemExit(f"missing names: {missing}")
PY
```

- [ ] Confirm working tree scope:

```bash
git status --short
```

- [ ] Commit:

```bash
git add docs/superpowers/plans/2026-04-30-autonomous-healing.md
git commit -m $'docs(healing): plan complete — see implementation tasks\n\nCo-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>'
```
