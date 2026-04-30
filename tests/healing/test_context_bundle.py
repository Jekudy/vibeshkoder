from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from ops.healing import context_bundle


def test_assemble_contains_all_sections(monkeypatch: Any, tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "healthcheck-log.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"generated_at": "2026-04-30T00:00:00Z", "is_red": False}),
                json.dumps({"generated_at": "2026-04-30T03:00:00Z", "is_red": True}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (state_dir / "snapshots").mkdir()
    (state_dir / "snapshots" / "latest.json").write_text(
        '{"prod_image_sha":"sha-caebb519"}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("COOLIFY_BASE_URL", "https://coolify.example.invalid")
    monkeypatch.setenv("COOLIFY_API_TOKEN", "coolify-token")
    monkeypatch.setenv("COOLIFY_APP_UUID", "app-uuid")
    monkeypatch.setenv("HEALING_BOT_CONTAINER", "vibe-gatekeeper-bot")

    def fake_run(command: list[str]) -> str:
        joined = " ".join(command)
        if joined == "git log --oneline -50":
            return "a2b6008 docs(healing): autonomous healing system design spec\n"
        if joined == "git log -5 --format=%H":
            return "a2b6008\n"
        if joined.startswith("git show --stat"):
            return " docs/file.md | 10 +++++-----\n"
        if joined.startswith("docker logs"):
            return "container log line\n"
        raise AssertionError(f"unexpected command: {joined}")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/applications/app-uuid":
            return httpx.Response(
                200,
                json={
                    "status": "running",
                    "restart_count": 12,
                    "last_online_at": "2026-04-30T00:00:00Z",
                    "environment_variables": [{"key": "BOT_TOKEN", "value": "hidden"}],
                },
            )
        if request.url.path == "/api/v1/applications/app-uuid/deployments":
            return httpx.Response(200, json={"data": [{"status": "success"}]})
        return httpx.Response(404)

    monkeypatch.setattr(context_bundle, "_run_command", fake_run)
    monkeypatch.setattr(
        context_bundle,
        "_build_client",
        lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )

    bundle = context_bundle.assemble({"coolify_status": "red"}, str(state_dir))

    assert "## Signal" in bundle
    assert "## Healthcheck history (24h)" in bundle
    assert "## Recent commits" in bundle
    assert "## Last 5 commit diffs (stat only)" in bundle
    assert "## Coolify state" in bundle
    assert "## Container logs" in bundle
    assert "## Last 3 deployments" in bundle
    assert "## Snapshot reference" in bundle
    assert "BOT_TOKEN" in bundle
    assert "hidden" not in bundle
