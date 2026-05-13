"""Tests for ops.healing.orchestrator._run.

Sprint 1A: ensure child failures surface stdout/stderr to parent stderr so
GitHub Actions logs capture the real error, not just `exit status 1`.
"""

import subprocess
import sys

import pytest

from ops.healing.orchestrator import _run


def test_run_surfaces_child_stderr_on_failure(capsys: pytest.CaptureFixture[str]) -> None:
    # Use a command that exits non-zero and writes to stderr.
    failing = [sys.executable, "-c", "import sys; sys.stderr.write('TEST_CHILD_STDERR'); sys.exit(2)"]
    with pytest.raises(subprocess.CalledProcessError):
        _run(failing)
    captured = capsys.readouterr()
    assert "TEST_CHILD_STDERR" in captured.err
    assert "[ops.healing._run]" in captured.err


def test_run_surfaces_child_stdout_on_failure(capsys: pytest.CaptureFixture[str]) -> None:
    failing = [sys.executable, "-c", "import sys; print('TEST_CHILD_STDOUT'); sys.exit(3)"]
    with pytest.raises(subprocess.CalledProcessError):
        _run(failing)
    captured = capsys.readouterr()
    assert "TEST_CHILD_STDOUT" in captured.err  # we redirect stdout into stderr surface
    assert "exit=3" in captured.err


def test_run_success_does_not_emit_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    result = _run([sys.executable, "-c", "print('ok')"])
    captured = capsys.readouterr()
    assert result.returncode == 0
    assert result.stdout == "ok\n"
    assert "[ops.healing._run]" not in captured.err
