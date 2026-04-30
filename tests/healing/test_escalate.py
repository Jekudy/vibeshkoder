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
