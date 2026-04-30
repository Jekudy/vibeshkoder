from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

Verdict = Literal["succeeded", "escalated"]


@dataclass(frozen=True)
class HealingConfig:
    max_attempts: int = 3
    cooldown_seconds: int = 900
    watch_polls: int = 5
    watch_interval_seconds: int = 120
    work_dir: Path = Path("healing-work")


@dataclass(frozen=True)
class HealingResult:
    verdict: Verdict
    attempts: int
    rolled_back: bool
    escalated: bool
    events: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _run(command: list[str], input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        input=input_text,
        text=True,
        capture_output=True,
        check=True,
    )


def _dry_run_result(scenario: str, config: HealingConfig) -> HealingResult:
    events: list[str] = []
    rolled_back = False

    for attempt in range(1, config.max_attempts + 1):
        events.extend(["snapshot:create", "context:bundle", "claude:session", "pr:opened"])

        if scenario == "codex_rejects" and attempt == 1:
            events.append("codex:reject")
            continue

        events.append("codex:approve")
        events.extend(["pr:merge", "coolify:deploy"])

        if scenario in {"watch_fails", "retry_exhaust"}:
            events.append("watch:red")
            events.append("rollback:restore-snapshot")
            rolled_back = True
            if scenario == "watch_fails" and attempt == 1:
                continue
            if scenario == "retry_exhaust":
                continue

        events.append("watch:green")
        events.append("audit:success")
        return HealingResult(
            verdict="succeeded",
            attempts=attempt,
            rolled_back=rolled_back,
            escalated=False,
            events=events,
        )

    events.extend(["escalate:issue", "state:disable"])
    return HealingResult(
        verdict="escalated",
        attempts=config.max_attempts,
        rolled_back=rolled_back,
        escalated=True,
        events=events,
    )


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _detect_open_pr() -> str:
    result = _run(["gh", "pr", "list", "--state", "open", "--base", "main", "--json", "number"])
    payload = json.loads(result.stdout)
    if not payload:
        return ""
    return str(payload[0]["number"])


def _watch_health(config: HealingConfig, events: list[str]) -> bool:
    green_count = 0
    for _poll in range(config.watch_polls):
        time.sleep(config.watch_interval_seconds)
        result = subprocess.run(
            ["python", "-m", "ops.healing.healthcheck"],
            check=False,
            text=True,
            capture_output=True,
        )
        if result.returncode == 0:
            green_count += 1
            events.append("watch:green-poll")
        else:
            events.append("watch:red")
            return False
    return green_count == config.watch_polls


def _run_real(signal_payload: str, config: HealingConfig) -> HealingResult:
    events: list[str] = []
    rolled_back = False
    config.work_dir.mkdir(parents=True, exist_ok=True)
    _write_text(config.work_dir / "signal.json", signal_payload)

    disabled = subprocess.run(
        ["python", "-m", "ops.healing.state_branch", "read-file", ".healing/disabled"],
        check=False,
        text=True,
        capture_output=True,
    ).stdout
    if disabled:
        _write_text(Path("session.log"), "healing disabled flag is present\n")
        _write_text(Path("snapshot.json"), '{"disabled":true}\n')
        _run(
            [
                "python",
                "-m",
                "ops.healing.escalate",
                "--reason",
                "healing disabled flag is present",
                "--transcript-path",
                "session.log",
                "--snapshot-path",
                "snapshot.json",
                "--admin-id",
                "149820031",
                "--gh-repo",
                os.environ["GITHUB_REPOSITORY"],
            ]
        )
        return HealingResult("escalated", 0, False, True, ["preflight:disabled", "escalate:issue"])

    _run(
        ["python", "-m", "ops.healing.state_branch", "write-file", ".healing/in-progress"],
        input_text=f"run={os.environ.get('GITHUB_RUN_ID', 'local')}\n",
    )

    try:
        for attempt in range(1, config.max_attempts + 1):
            snapshot_path = config.work_dir / f"snapshot-{attempt}.json"
            _run(["python", "-m", "ops.healing.snapshot", "create", "--output", str(snapshot_path)])
            events.append("snapshot:create")
            _run(
                [
                    "python",
                    "-m",
                    "ops.healing.context_bundle",
                    "--signal-json",
                    signal_payload,
                    "--state-branch-dir",
                    ".",
                    "--output",
                    str(config.work_dir / "context-bundle.md"),
                ]
            )
            events.append("context:bundle")
            with (config.work_dir / "context-bundle.md").open("r", encoding="utf-8") as input_handle:
                session = subprocess.run(
                    [
                        "claude",
                        "-p",
                        "--model",
                        "claude-opus-4-7",
                        "--append-system-prompt",
                        Path("ops/healing/INVARIANTS.md").read_text(encoding="utf-8"),
                        "--max-turns",
                        "50",
                    ],
                    stdin=input_handle,
                    text=True,
                    capture_output=True,
                    check=False,
                )
            _write_text(config.work_dir / f"session-{attempt}.log", session.stdout + session.stderr)
            events.append("claude:session")
            if session.returncode != 0:
                break

            pr_number = _detect_open_pr()
            if not pr_number:
                time.sleep(config.cooldown_seconds)
                continue

            codex = subprocess.run(
                [
                    "codex",
                    "exec",
                    "review",
                    "--base",
                    "main",
                    "-m",
                    "gpt-5.5",
                    "-c",
                    "model_reasoning_effort=high",
                    "--ephemeral",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            _write_text(config.work_dir / f"codex-{attempt}.log", codex.stdout + codex.stderr)
            if codex.returncode != 0 or "APPROVE" not in codex.stdout:
                events.append("codex:reject")
                time.sleep(config.cooldown_seconds)
                continue

            events.append("codex:approve")
            _run(["gh", "pr", "merge", pr_number, "--rebase", "--delete-branch"])
            events.append("pr:merge")

            if _watch_health(config, events):
                events.append("audit:success")
                return HealingResult("succeeded", attempt, rolled_back, False, events)

            _run(["python", "-m", "ops.healing.snapshot", "restore", "--input", str(snapshot_path)])
            events.append("rollback:restore-snapshot")
            rolled_back = True
            time.sleep(config.cooldown_seconds)

        _run(
            [
                "python",
                "-m",
                "ops.healing.escalate",
                "--reason",
                "retry budget exhausted",
                "--transcript-path",
                str(config.work_dir / "session-1.log"),
                "--snapshot-path",
                str(config.work_dir / "snapshot-1.json"),
                "--admin-id",
                "149820031",
                "--gh-repo",
                os.environ["GITHUB_REPOSITORY"],
            ]
        )
        _run(
            ["python", "-m", "ops.healing.state_branch", "write-file", ".healing/disabled"],
            input_text="disabled after retry budget exhausted\n",
        )
        events.extend(["escalate:issue", "state:disable"])
        return HealingResult("escalated", config.max_attempts, rolled_back, True, events)
    finally:
        _run(["python", "-m", "ops.healing.state_branch", "write-file", ".healing/in-progress"], input_text="")


def run_healing(
    signal_payload: str,
    config: HealingConfig = HealingConfig(),
) -> HealingResult:
    if os.environ.get("HEALING_DRY_RUN") == "true":
        scenario = os.environ.get("HEALING_DRY_RUN_SCENARIO", "success")
        return _dry_run_result(scenario, config)
    return _run_real(signal_payload, config)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run autonomous healing orchestration.")
    parser.add_argument("--signal-payload", required=True)
    args = parser.parse_args()
    result = run_healing(args.signal_payload)
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0 if result.verdict == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
