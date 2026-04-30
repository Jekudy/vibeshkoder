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
