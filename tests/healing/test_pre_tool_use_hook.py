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
