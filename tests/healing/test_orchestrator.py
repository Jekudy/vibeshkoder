"""Tests for ops.healing.orchestrator._run and scope guard.

Sprint 1A: ensure child failures surface stdout/stderr to parent stderr so
GitHub Actions logs capture the real error, not just `exit status 1`.

Sprint S6: synthetic-signal scope guard — is_real_bug_signal() must reject
synthetic / low-severity / untrusted-source payloads before any PR is opened.
"""

import json
import subprocess
import sys

import pytest

from ops.healing.orchestrator import _run, is_real_bug_signal, run_healing, HealingConfig


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


def test_run_redacts_command_args_on_failure(capsys: pytest.CaptureFixture[str]) -> None:
    cmd = [sys.executable, "-c", "import sys; sys.exit(1)", "--signal-json", '{"secret":"xxx"}', "--token", "abc"]
    with pytest.raises(subprocess.CalledProcessError):
        _run(cmd)
    captured = capsys.readouterr()
    assert '"secret":"xxx"' not in captured.err
    assert "abc" not in captured.err
    assert "[+4 args redacted]" in captured.err


def test_run_success_does_not_emit_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    result = _run([sys.executable, "-c", "print('ok')"])
    captured = capsys.readouterr()
    assert result.returncode == 0
    assert result.stdout == "ok\n"
    assert "[ops.healing._run]" not in captured.err


# ---------------------------------------------------------------------------
# Sprint S6: is_real_bug_signal scope guard tests
# ---------------------------------------------------------------------------

def _make_real_payload(**overrides: object) -> dict:
    base: dict = {
        "incident_id": "INC-1234",
        "severity": "high",
        "source": "sentry",
        "reason": "500 errors spike on /api/recall",
    }
    base.update(overrides)
    return base


def test_scope_guard_accepts_real_signal() -> None:
    payload = _make_real_payload()
    assert is_real_bug_signal(payload) is True


def test_scope_guard_accepts_critical_severity() -> None:
    payload = _make_real_payload(severity="critical")
    assert is_real_bug_signal(payload) is True


def test_scope_guard_rejects_missing_incident_id() -> None:
    payload = _make_real_payload()
    del payload["incident_id"]
    assert is_real_bug_signal(payload) is False


def test_scope_guard_rejects_null_incident_id() -> None:
    payload = _make_real_payload(incident_id=None)
    assert is_real_bug_signal(payload) is False


def test_scope_guard_rejects_low_severity() -> None:
    payload = _make_real_payload(severity="low")
    assert is_real_bug_signal(payload) is False


def test_scope_guard_rejects_medium_severity() -> None:
    payload = _make_real_payload(severity="medium")
    assert is_real_bug_signal(payload) is False


def test_scope_guard_rejects_unknown_source() -> None:
    payload = _make_real_payload(source="local-test-runner")
    assert is_real_bug_signal(payload) is False


def test_scope_guard_rejects_synthetic_flag() -> None:
    payload = _make_real_payload(_synthetic=True)
    assert is_real_bug_signal(payload) is False


def test_scope_guard_rejects_test_payload_flag() -> None:
    payload = _make_real_payload(test_payload=True)
    assert is_real_bug_signal(payload) is False


def test_scope_guard_accepts_coolify_source() -> None:
    payload = _make_real_payload(source="coolify")
    assert is_real_bug_signal(payload) is True


def test_scope_guard_accepts_prod_monitor_source() -> None:
    payload = _make_real_payload(source="prod-monitor")
    assert is_real_bug_signal(payload) is True


def test_run_healing_returns_no_action_on_synthetic_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    """When signal is synthetic, run_healing must return no_action verdict without opening any PRs."""
    monkeypatch.delenv("HEALING_DRY_RUN", raising=False)
    synthetic_payload = json.dumps({
        "incident_id": None,
        "severity": "low",
        "source": "local-test-runner",
        "reason": "synthetic verification run",
        "_synthetic": True,
    })
    result = run_healing(synthetic_payload, HealingConfig())
    assert result.verdict == "no_action"
    assert result.escalated is False
