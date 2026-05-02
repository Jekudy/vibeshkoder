from __future__ import annotations

import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "lint_privacy_check.sh"


def run_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True)


def init_repo(repo: Path) -> None:
    run_git(repo, "init")
    run_git(repo, "config", "user.email", "privacy-lint@example.test")
    run_git(repo, "config", "user.name", "Privacy Lint")


def commit_all(repo: Path) -> None:
    run_git(repo, "add", ".")
    run_git(repo, "commit", "--allow-empty", "-m", "seed")


def run_lint(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )


def write_file(repo: Path, relative_path: str, content: str) -> None:
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_empty_repo_passes(tmp_path: Path) -> None:
    init_repo(tmp_path)
    commit_all(tmp_path)

    result = run_lint(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr


def test_allowed_seed_file_passes(tmp_path: Path) -> None:
    init_repo(tmp_path)
    write_file(
        tmp_path,
        "tests/fixtures/eval_seeds/leakage_offrecord_v1.jsonl",
        '{"text": "' + "#" + "off" + "record" + '"}\n',
    )
    commit_all(tmp_path)

    result = run_lint(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr


def test_unauthorized_source_file_fails(tmp_path: Path) -> None:
    init_repo(tmp_path)
    write_file(
        tmp_path,
        "bot/handlers/echo.py",
        'TEXT = "' + "#" + "off" + "record" + '"\n',
    )
    commit_all(tmp_path)

    result = run_lint(tmp_path)

    assert result.returncode == 1
    assert "bot/handlers/echo.py:1:" in result.stdout


def test_word_boundary_avoids_substring_false_positive(tmp_path: Path) -> None:
    init_repo(tmp_path)
    write_file(tmp_path, "bot/services/foo.py", 'TEXT = "informed"\n')
    commit_all(tmp_path)

    result = run_lint(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
