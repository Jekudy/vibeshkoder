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
