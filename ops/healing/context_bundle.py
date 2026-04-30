from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx


@dataclass(frozen=True)
class ChunkingConfig:
    history_entries: int = 8
    recent_commits: int = 50
    diffstat_commits: int = 5
    log_tail_lines: int = 500
    deployments: int = 3


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise KeyError(f"missing required env var: {name}")
    return value


def _run_command(command: list[str]) -> str:
    return subprocess.run(
        command,
        check=True,
        text=True,
        capture_output=True,
    ).stdout


def _build_client() -> httpx.Client:
    return httpx.Client(timeout=20)


def _read_recent_history(state_branch_dir: Path, config: ChunkingConfig) -> list[dict[str, Any]]:
    path = state_branch_dir / "healthcheck-log.jsonl"
    if not path.exists():
        return []
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError("healthcheck history row must be a JSON object")
        generated_at = value.get("generated_at") or value.get("ts")
        if isinstance(generated_at, str):
            try:
                parsed = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
            except ValueError:
                parsed = datetime.now(UTC)
            if parsed < cutoff:
                continue
        rows.append(value)
    return rows[-config.history_entries :]


def _recent_commits(config: ChunkingConfig) -> str:
    return _run_command(["git", "log", "--oneline", f"-{config.recent_commits}"]).strip()


def _last_diffstats(config: ChunkingConfig) -> str:
    shas = _run_command(["git", "log", f"-{config.diffstat_commits}", "--format=%H"]).splitlines()
    chunks: list[str] = []
    for sha in shas:
        chunks.append(
            _run_command(
                [
                    "git",
                    "show",
                    "--stat",
                    "--oneline",
                    "--no-renames",
                    "--format=fuller",
                    "-1",
                    sha,
                ]
            ).strip()
        )
    return "\n\n".join(chunk for chunk in chunks if chunk)


def _coolify_json(path: str) -> Any:
    base_url = _require_env("COOLIFY_BASE_URL").rstrip("/")
    token = _require_env("COOLIFY_API_TOKEN")
    with _build_client() as client:
        response = client.get(
            f"{base_url}{path}",
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        return response.json()


def _coolify_state(app_uuid: str) -> dict[str, Any]:
    payload = _coolify_json(f"/api/v1/applications/{app_uuid}")
    if not isinstance(payload, dict):
        raise ValueError("Coolify app response must be a JSON object")
    env_items = payload.get("environment_variables", [])
    env_keys: list[str] = []
    if isinstance(env_items, list):
        for item in env_items:
            if isinstance(item, dict) and "key" in item:
                env_keys.append(str(item["key"]))
    return {
        "status": payload.get("status"),
        "restart_count": payload.get("restart_count"),
        "last_online_at": payload.get("last_online_at"),
        "env_keys": sorted(env_keys),
    }


def _last_deployments(app_uuid: str, config: ChunkingConfig) -> Any:
    payload = _coolify_json(f"/api/v1/applications/{app_uuid}/deployments")
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return payload["data"][: config.deployments]
    if isinstance(payload, list):
        return payload[: config.deployments]
    return payload


def _container_logs(config: ChunkingConfig) -> str:
    container = _require_env("HEALING_BOT_CONTAINER")
    return _run_command(["docker", "logs", "--tail", str(config.log_tail_lines), container]).strip()


def _snapshot_reference(state_branch_dir: Path) -> str:
    latest = state_branch_dir / "snapshots" / "latest.json"
    if latest.exists():
        return str(latest)
    return "No snapshot file found in state branch checkout."


def _json_block(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True)


def assemble(
    signal: dict[str, Any],
    state_branch_dir: str,
    config: ChunkingConfig = ChunkingConfig(),
) -> str:
    app_uuid = _require_env("COOLIFY_APP_UUID")
    state_dir = Path(state_branch_dir)
    sections = [
        ("Signal", _json_block(signal)),
        ("Healthcheck history (24h)", _json_block(_read_recent_history(state_dir, config))),
        ("Recent commits", _recent_commits(config)),
        ("Last 5 commit diffs (stat only)", _last_diffstats(config)),
        ("Coolify state", _json_block(_coolify_state(app_uuid))),
        ("Container logs", _container_logs(config)),
        ("Last 3 deployments", _json_block(_last_deployments(app_uuid, config))),
        ("Snapshot reference", _snapshot_reference(state_dir)),
    ]
    rendered = ["# Autonomous Healing Context Bundle"]
    for title, body in sections:
        rendered.append(f"## {title}")
        rendered.append("```")
        rendered.append(body)
        rendered.append("```")
    return "\n\n".join(rendered) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Assemble a healing context bundle.")
    parser.add_argument("--signal-json", required=True)
    parser.add_argument("--state-branch-dir", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    signal = json.loads(args.signal_json)
    if not isinstance(signal, dict):
        raise ValueError("--signal-json must decode to a JSON object")
    bundle = assemble(signal, args.state_branch_dir)
    Path(args.output).write_text(bundle, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
