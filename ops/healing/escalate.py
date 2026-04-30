from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx


@dataclass(frozen=True)
class EscalationResult:
    telegram_ok: bool
    issue_ok: bool
    issue_url: str
    errors: list[str]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _send_telegram(reason: str, bot_token: str, admin_id: int, gh_repo: str) -> None:
    response = httpx.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json={
            "chat_id": admin_id,
            "text": (
                "healing escalation\n"
                f"repo: {gh_repo}\n"
                f"reason: {reason}\n"
                f"ts: {datetime.now(UTC).isoformat()}"
            ),
        },
        timeout=10,
    )
    response.raise_for_status()


def _create_issue(
    reason: str,
    transcript_path: Path,
    snapshot_path: Path,
    gh_repo: str,
) -> str:
    body = (
        f"# Healing escalation\n\n"
        f"Reason: {reason}\n\n"
        f"## Snapshot\n\n```json\n{_read(snapshot_path)}\n```\n\n"
        f"## Transcript\n\n```\n{_read(transcript_path)}\n```\n"
    )
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write(body)
        body_file = Path(handle.name)
    try:
        result = subprocess.run(
            [
                "gh",
                "issue",
                "create",
                "--repo",
                gh_repo,
                "--title",
                f"[ALERT] healing escalation — {reason}",
                "--body-file",
                str(body_file),
                "--label",
                "healing",
                "--label",
                "incident",
                "--label",
                "priority:high",
            ],
            check=True,
            text=True,
            capture_output=True,
        )
    finally:
        body_file.unlink(missing_ok=True)
    return result.stdout.strip()


def escalate(
    reason: str,
    transcript_path: Path | str,
    snapshot_path: Path | str,
    bot_token: str,
    admin_id: int,
    gh_repo: str,
) -> EscalationResult:
    transcript = Path(transcript_path)
    snapshot = Path(snapshot_path)
    errors: list[str] = []
    telegram_ok = False
    issue_ok = False
    issue_url = ""

    try:
        _send_telegram(reason, bot_token, admin_id, gh_repo)
        telegram_ok = True
    except httpx.HTTPError as exc:
        errors.append(f"telegram: {exc}")

    try:
        issue_url = _create_issue(reason, transcript, snapshot, gh_repo)
        issue_ok = True
    except subprocess.CalledProcessError as exc:
        errors.append(f"github issue: {exc.stderr or exc}")

    return EscalationResult(
        telegram_ok=telegram_ok,
        issue_ok=issue_ok,
        issue_url=issue_url,
        errors=errors,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Escalate an autonomous healing incident.")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--transcript-path", required=True)
    parser.add_argument("--snapshot-path", required=True)
    parser.add_argument("--admin-id", required=True, type=int)
    parser.add_argument("--gh-repo", required=True)
    args = parser.parse_args()

    result = escalate(
        reason=args.reason,
        transcript_path=Path(args.transcript_path),
        snapshot_path=Path(args.snapshot_path),
        bot_token=os.environ["BOT_TOKEN"],
        admin_id=args.admin_id,
        gh_repo=args.gh_repo,
    )
    print(result.to_dict())
    return 0 if result.issue_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
