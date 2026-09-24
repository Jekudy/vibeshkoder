from __future__ import annotations

import json
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


class FakeGh:
    """Dispatching ``gh`` mock: list returns ``open_issues``, create/comment record calls."""

    def __init__(
        self,
        open_issues: list[dict[str, Any]] | None = None,
        *,
        fail_on: str | None = None,
    ) -> None:
        self.open_issues = open_issues if open_issues is not None else []
        self.fail_on = fail_on
        self.created_titles: list[str] = []
        self.commented: list[int] = []
        self._next_issue = 100

    def run(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        command = f"{args[1]} {args[2]}"
        if self.fail_on == command:
            raise subprocess.CalledProcessError(1, args, stderr="gh failed")
        if command == "issue list":
            stdout = json.dumps(self.open_issues)
        elif command == "issue create":
            title = args[args.index("--title") + 1]
            self.created_titles.append(title)
            self._next_issue += 1
            stdout = f"https://github.com/org/repo/issues/{self._next_issue}\n"
        elif command == "issue comment":
            self.commented.append(int(args[3]))
            stdout = ""
        else:
            stdout = ""
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")


def _patch_gh(monkeypatch: Any, fake: FakeGh) -> None:
    monkeypatch.setattr(escalate.subprocess, "run", fake.run)


def test_escalate_both_channels_succeed(monkeypatch: Any, tmp_path: Path) -> None:
    transcript, snapshot = _files(tmp_path)
    monkeypatch.setattr(escalate.httpx, "post", lambda *args, **kwargs: FakeResponse())
    _patch_gh(monkeypatch, FakeGh())

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
    assert result.issue_url == "https://github.com/org/repo/issues/101"


def test_escalate_repeat_comments_on_existing_issue(monkeypatch: Any, tmp_path: Path) -> None:
    transcript, snapshot = _files(tmp_path)
    reason = "retry budget exhausted"
    existing_url = "https://github.com/org/repo/issues/42"
    fake = FakeGh(
        [
            {
                "number": 42,
                "title": f"[ALERT] healing escalation — {reason}",
                "url": existing_url,
            },
            {
                "number": 77,
                "title": "[ALERT] healing escalation — other reason",
                "url": "https://github.com/org/repo/issues/77",
            },
        ]
    )
    monkeypatch.setattr(escalate.httpx, "post", lambda *args, **kwargs: FakeResponse())
    _patch_gh(monkeypatch, fake)

    result = escalate.escalate(
        reason, transcript, snapshot, "123456:test-token", 149820031, "org/repo"
    )

    assert result.issue_ok is True
    assert result.issue_url == existing_url
    assert fake.commented == [42]
    assert fake.created_titles == []


def test_escalate_creates_when_only_closed_match(monkeypatch: Any, tmp_path: Path) -> None:
    transcript, snapshot = _files(tmp_path)
    fake = FakeGh()
    monkeypatch.setattr(escalate.httpx, "post", lambda *args, **kwargs: FakeResponse())
    _patch_gh(monkeypatch, fake)

    result = escalate.escalate(
        "wall clock exceeded",
        transcript,
        snapshot,
        "123456:test-token",
        149820031,
        "org/repo",
    )

    assert result.issue_ok is True
    assert fake.created_titles == ["[ALERT] healing escalation — wall clock exceeded"]
    assert fake.commented == []


def test_escalate_issue_list_failure_does_not_create(monkeypatch: Any, tmp_path: Path) -> None:
    transcript, snapshot = _files(tmp_path)
    fake = FakeGh(fail_on="issue list")
    monkeypatch.setattr(escalate.httpx, "post", lambda *args, **kwargs: FakeResponse())
    _patch_gh(monkeypatch, fake)

    result = escalate.escalate(
        "auth failed",
        transcript,
        snapshot,
        "123456:test-token",
        149820031,
        "org/repo",
    )

    assert result.issue_ok is False
    assert fake.created_titles == []
    assert fake.commented == []


def test_escalate_telegram_failure_does_not_block_issue(monkeypatch: Any, tmp_path: Path) -> None:
    transcript, snapshot = _files(tmp_path)
    monkeypatch.setattr(escalate.httpx, "post", lambda *args, **kwargs: FakeResponse(True))
    _patch_gh(monkeypatch, FakeGh())

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
    assert result.issue_url.endswith("/101")
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
