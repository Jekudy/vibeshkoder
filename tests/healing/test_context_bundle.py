from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

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


def test_last_deployments_returns_empty_on_404(capsys: pytest.CaptureFixture[str], monkeypatch: Any) -> None:
    """Sprint 1B: Coolify /deployments endpoint may not exist in current Coolify version.

    Degrade to empty list + stderr marker rather than crashing context_bundle.
    """
    def fake_coolify_json(path: str) -> Any:
        request = httpx.Request("GET", f"http://example/{path}")
        response = httpx.Response(404, request=request)
        raise httpx.HTTPStatusError("404", request=request, response=response)

    monkeypatch.setattr(context_bundle, "_coolify_json", fake_coolify_json)
    result = context_bundle._last_deployments("abc-uuid", context_bundle.ChunkingConfig())
    assert result == []
    err = capsys.readouterr().err
    assert "Coolify deployments endpoint 404" in err
    assert "abc-uuid" in err


def test_last_deployments_propagates_non_404(monkeypatch: Any) -> None:
    """Non-404 HTTP errors must still propagate — we only mask the missing endpoint."""
    def fake_coolify_json(path: str) -> Any:
        request = httpx.Request("GET", f"http://example/{path}")
        response = httpx.Response(500, request=request)
        raise httpx.HTTPStatusError("500", request=request, response=response)

    monkeypatch.setattr(context_bundle, "_coolify_json", fake_coolify_json)
    with pytest.raises(httpx.HTTPStatusError):
        context_bundle._last_deployments("abc-uuid", context_bundle.ChunkingConfig())
