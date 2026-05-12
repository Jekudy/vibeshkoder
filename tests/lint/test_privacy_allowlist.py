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
    # Output format is path:content (no line number) after the line-number-
    # resilience fix.  The path prefix is sufficient to confirm the violation.
    assert "bot/handlers/echo.py:" in result.stdout


def test_word_boundary_avoids_substring_false_positive(tmp_path: Path) -> None:
    init_repo(tmp_path)
    write_file(tmp_path, "bot/services/foo.py", 'TEXT = "informed"\n')
    commit_all(tmp_path)

    result = run_lint(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr


def test_line_shift_does_not_trigger_false_positive(tmp_path: Path) -> None:
    """Inserting a line before an existing marker must NOT cause a false-positive
    violation.  The baseline comparison must be resilient to line-number changes
    (path:content match, not path:lineno:content match).

    This requires two commits so that find_base_ref can resolve a real base_ref:
    - commit 1 (base): file has marker at line 1
    - commit 2 (HEAD): a new innocent line is prepended → marker shifts to line 2

    With path:lineno:content comparison, the shifted marker won't match the
    baseline and will be reported as a new violation.  With path:content
    comparison, it matches and no violation is reported.

    Uses alembic/versions/ which is not in the narrowed allowlist (only the
    4 leakage globs are), so the baseline-diff path is exercised.
    """
    init_repo(tmp_path)

    # Commit 1 (base): marker at line 1 in a path outside the 4-glob allowlist.
    # Split the word across concatenation so this test file itself does not match
    # the privacy pattern (same technique used in build_pattern for the regex).
    marker = "for" + "gotten"
    write_file(
        tmp_path,
        "alembic/versions/001_init.py",
        f'COMMENT = "The policy is {marker} here"\n',
    )
    commit_all(tmp_path)

    # Commit 2 (HEAD): insert an innocent line before — marker shifts to line 2.
    write_file(
        tmp_path,
        "alembic/versions/001_init.py",
        f'# header\nCOMMENT = "The policy is {marker} here"\n',
    )
    commit_all(tmp_path)

    result = run_lint(tmp_path)

    assert result.returncode == 0, (
        "line shift caused a false-positive violation:\n" + result.stdout + result.stderr
    )


def test_narrowed_allowlist_only_permits_four_leakage_globs(tmp_path: Path) -> None:
    """docs/ and source paths must NOT be in the allowlist; they must fail if they
    contain a new marker that is not in the baseline.

    The new file must be committed (tracked) so that git ls-files includes it.
    We use two commits so find_base_ref resolves HEAD^ as the base:
    - commit 1 (base): empty repo
    - commit 2 (HEAD): docs file with new marker
    """
    init_repo(tmp_path)
    # Commit 1 (base): empty repo.
    commit_all(tmp_path)

    # Commit 2 (HEAD): NEW marker in a docs file — not in allowlist, not in base.
    # Split the word so this test file itself does not match the privacy pattern.
    marker = "for" + "gotten"
    write_file(
        tmp_path,
        "docs/some-doc.md",
        f"This policy is {marker}.\n",
    )
    commit_all(tmp_path)

    result = run_lint(tmp_path)

    assert result.returncode == 1, (
        "docs/ path with new marker should fail but passed:\n"
        + result.stdout
        + result.stderr
    )
