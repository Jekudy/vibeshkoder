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
