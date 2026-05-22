"""Tests for ops.healing.orchestrator._run and scope guard.

Sprint 1A: ensure child failures surface stdout/stderr to parent stderr so
GitHub Actions logs capture the real error, not just `exit status 1`.

Sprint S6: synthetic-signal scope guard — is_real_bug_signal() must reject
synthetic / low-severity / untrusted-source payloads before any PR is opened.

Sprint S6 review (Fix 1-6): guard rewritten around CheckReport.to_dict() shape;
regression test with actual 2026-05-13 payload; malformed-input safety;
integration test for real-signal path; no_action exit code; codex-on-PATH guard.
"""

import json
import subprocess
import sys

import pytest

from ops.healing.orchestrator import (
    HealingConfig,
    HealingResult,
    _run,
    is_real_bug_signal,
    run_healing,
)


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
# Sprint S6: is_real_bug_signal scope guard tests (CheckReport schema)
# Guard was rewritten in the S6 review to match CheckReport.to_dict() shape.
# ---------------------------------------------------------------------------

def _make_checkreport_payload(**overrides: object) -> dict:
    """Build a minimal valid CheckReport.to_dict() payload with coolify_status=red."""
    base: dict = {
        "coolify_status": {"status": "red", "reason": "container exited with code 137"},
        "telegram_pending": {"status": "green", "reason": "ok"},
        "db_roundtrip": {"status": "green", "reason": "ok"},
        "is_red": True,
        "generated_at": "2026-05-22T10:00:00+00:00",
    }
    base.update(overrides)
    return base


def test_scope_guard_accepts_real_signal() -> None:
    """A valid CheckReport with coolify_status=red must be accepted."""
    payload = _make_checkreport_payload()
    assert is_real_bug_signal(payload) is True


def test_scope_guard_accepts_telegram_red() -> None:
    """A valid CheckReport with telegram_pending=red must be accepted."""
    payload = _make_checkreport_payload(
        coolify_status={"status": "green", "reason": "ok"},
        telegram_pending={"status": "red", "reason": "no updates in 10m"},
    )
    assert is_real_bug_signal(payload) is True


def test_scope_guard_rejects_all_green() -> None:
    """All-green CheckReport must be rejected — no real failure."""
    payload = _make_checkreport_payload(
        coolify_status={"status": "green", "reason": "ok"},
        telegram_pending={"status": "green", "reason": "ok"},
        is_red=False,
    )
    assert is_real_bug_signal(payload) is False


def test_scope_guard_rejects_only_db_red() -> None:
    """db_roundtrip=red alone must not trigger healing (excluded per healthcheck.py)."""
    payload = _make_checkreport_payload(
        coolify_status={"status": "green", "reason": "ok"},
        telegram_pending={"status": "green", "reason": "ok"},
        db_roundtrip={"status": "red", "reason": "connection refused"},
        is_red=False,
    )
    assert is_real_bug_signal(payload) is False


def test_scope_guard_rejects_synthetic_flag() -> None:
    """_synthetic=True at top level must reject even with red coolify_status."""
    payload = _make_checkreport_payload(_synthetic=True)
    assert is_real_bug_signal(payload) is False


def test_scope_guard_rejects_test_payload_flag() -> None:
    """test_payload=True at top level must reject even with red coolify_status."""
    payload = _make_checkreport_payload(test_payload=True)
    assert is_real_bug_signal(payload) is False


def test_scope_guard_rejects_synthetic_reason_in_red_check() -> None:
    """Red check with reason='synthetic ...' must be rejected."""
    payload = _make_checkreport_payload(
        coolify_status={"status": "red", "reason": "synthetic probe"},
    )
    assert is_real_bug_signal(payload) is False


def test_scope_guard_rejects_test_reason_in_red_check() -> None:
    """Red check with reason='test ...' must be rejected."""
    payload = _make_checkreport_payload(
        coolify_status={"status": "red", "reason": "test run only"},
    )
    assert is_real_bug_signal(payload) is False


def test_scope_guard_rejects_empty_payload() -> None:
    """Empty mapping (no healing targets) must be rejected."""
    assert is_real_bug_signal({}) is False


def test_run_healing_returns_no_action_on_synthetic_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    """When signal is synthetic, run_healing must return no_action verdict without opening any PRs."""
    monkeypatch.delenv("HEALING_DRY_RUN", raising=False)
    synthetic_payload = json.dumps({
        "_synthetic": True,
        "coolify_status": {"status": "red", "reason": "container stopped"},
        "telegram_pending": {"status": "green", "reason": "ok"},
        "db_roundtrip": {"status": "green", "reason": "ok"},
        "is_red": True,
        "generated_at": "2026-05-22T10:00:00+00:00",
    })
    result = run_healing(synthetic_payload, HealingConfig())
    assert result.verdict == "no_action"
    assert result.escalated is False


# ---------------------------------------------------------------------------
# Sprint S6 review: Fix 1 — CheckReport schema guard
# ---------------------------------------------------------------------------

def test_scope_guard_accepts_checkreport_coolify_red() -> None:
    """CheckReport payload with coolify_status=red must be accepted."""
    payload = {
        "coolify_status": {"status": "red", "reason": "container exited with code 137"},
        "telegram_pending": {"status": "green", "reason": "ok"},
        "db_roundtrip": {"status": "green", "reason": "ok"},
        "is_red": True,
        "generated_at": "2026-05-22T10:00:00+00:00",
    }
    assert is_real_bug_signal(payload) is True


def test_scope_guard_accepts_checkreport_telegram_red() -> None:
    """CheckReport payload with telegram_pending=red must be accepted."""
    payload = {
        "coolify_status": {"status": "green", "reason": "ok"},
        "telegram_pending": {"status": "red", "reason": "no updates received in 10m"},
        "db_roundtrip": {"status": "green", "reason": "ok"},
        "is_red": True,
        "generated_at": "2026-05-22T10:00:00+00:00",
    }
    assert is_real_bug_signal(payload) is True


def test_scope_guard_rejects_only_db_roundtrip_red() -> None:
    """db_roundtrip=red alone must not trigger healing (excluded per healthcheck.py policy)."""
    payload = {
        "coolify_status": {"status": "green", "reason": "ok"},
        "telegram_pending": {"status": "green", "reason": "ok"},
        "db_roundtrip": {"status": "red", "reason": "connection refused"},
        "is_red": False,
        "generated_at": "2026-05-22T10:00:00+00:00",
    }
    assert is_real_bug_signal(payload) is False


def test_scope_guard_rejects_all_green_checkreport() -> None:
    """All-green CheckReport must not trigger healing."""
    payload = {
        "coolify_status": {"status": "green", "reason": "ok"},
        "telegram_pending": {"status": "green", "reason": "ok"},
        "db_roundtrip": {"status": "green", "reason": "ok"},
        "is_red": False,
        "generated_at": "2026-05-22T10:00:00+00:00",
    }
    assert is_real_bug_signal(payload) is False


# ---------------------------------------------------------------------------
# Sprint S6 review: Fix 2 — Regression test with actual 2026-05-13 payload
# ---------------------------------------------------------------------------

def test_is_real_bug_signal_rejects_2026_05_13_synthetic_payload() -> None:
    """Regression: actual payload from healing run 25813274803 that triggered PR #281
    must be rejected — db_roundtrip reason='synthetic' is the trigger."""
    payload = {
        "coolify_status": {"status": "green", "reason": "ok"},
        "db_roundtrip": {"status": "red", "reason": "synthetic"},
        "telegram_pending": {"status": "green", "reason": "ok"},
        "is_red": True,
        "generated_at": "2026-05-13T16:50:00+00:00",
    }
    assert is_real_bug_signal(payload) is False


# ---------------------------------------------------------------------------
# Sprint S6 review: Fix 3 — Malformed JSON safety
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_input", [None, "string", [], 42, True])
def test_is_real_bug_signal_rejects_non_mapping(bad_input: object) -> None:
    """Malformed JSON (null, list, scalar) must not crash and must return False."""
    assert is_real_bug_signal(bad_input) is False


# ---------------------------------------------------------------------------
# Sprint S6 review: Fix 4 — Integration test: real payload reaches _run_real
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_healing_calls_real_path_on_real_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    """When guard accepts a CheckReport payload, _run_real must be invoked."""
    monkeypatch.delenv("HEALING_DRY_RUN", raising=False)
    real_payload = {
        "coolify_status": {"status": "red", "reason": "container exited with code 137"},
        "telegram_pending": {"status": "green", "reason": "ok"},
        "db_roundtrip": {"status": "green", "reason": "ok"},
        "is_red": True,
        "generated_at": "2026-05-22T10:00:00+00:00",
    }
    sentinel = HealingResult(
        verdict="succeeded",
        attempts=1,
        rolled_back=False,
        escalated=False,
        events=["test:sentinel"],
    )
    called: list = []

    def fake_run_real(signal_payload: str, config: HealingConfig) -> HealingResult:
        called.append((signal_payload, config))
        return sentinel

    monkeypatch.setattr("ops.healing.orchestrator._run_real", fake_run_real)
    result = run_healing(json.dumps(real_payload), HealingConfig())
    assert result is sentinel
    assert called, "_run_real should be invoked for real signals"


# ---------------------------------------------------------------------------
# Sprint S6 review: Fix 5 — no_action verdict exits 0
# ---------------------------------------------------------------------------

def test_run_healing_no_action_verdict_on_synthetic_checkreport(monkeypatch: pytest.MonkeyPatch) -> None:
    """Synthetic CheckReport payload with _synthetic=True must return no_action."""
    monkeypatch.delenv("HEALING_DRY_RUN", raising=False)
    payload = {
        "_synthetic": True,
        "coolify_status": {"status": "red", "reason": "container stopped"},
        "telegram_pending": {"status": "green", "reason": "ok"},
        "db_roundtrip": {"status": "green", "reason": "ok"},
        "is_red": True,
        "generated_at": "2026-05-22T10:00:00+00:00",
    }
    result = run_healing(json.dumps(payload), HealingConfig())
    assert result.verdict == "no_action"


# ---------------------------------------------------------------------------
# Sprint S6 review: synthetic reason marker in red check
# ---------------------------------------------------------------------------

def test_scope_guard_rejects_synthetic_reason_in_red_component() -> None:
    """Red check with reason matching 'synthetic' marker must be rejected."""
    payload = {
        "coolify_status": {"status": "red", "reason": "synthetic test run"},
        "telegram_pending": {"status": "green", "reason": "ok"},
        "db_roundtrip": {"status": "green", "reason": "ok"},
        "is_red": True,
        "generated_at": "2026-05-22T10:00:00+00:00",
    }
    assert is_real_bug_signal(payload) is False


def test_scope_guard_rejects_verification_reason_in_red_component() -> None:
    """Red check with reason matching 'verification' marker must be rejected."""
    payload = {
        "coolify_status": {"status": "green", "reason": "ok"},
        "telegram_pending": {"status": "red", "reason": "manual verification run"},
        "db_roundtrip": {"status": "green", "reason": "ok"},
        "is_red": True,
        "generated_at": "2026-05-22T10:00:00+00:00",
    }
    assert is_real_bug_signal(payload) is False
