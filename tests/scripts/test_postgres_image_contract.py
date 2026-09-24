from __future__ import annotations

import re
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_postgres_image_is_exact_pg15_alpine_parent_with_verified_pgvector() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile.postgres").read_text(encoding="utf-8")

    assert (
        "FROM postgres@sha256:1c52f5ad23db5d7648a63634444af76de48e63b860fccbe3e3a5458b2812eaed"
    ) in dockerfile
    assert "PGVECTOR_VERSION=0.8.2" in dockerfile
    assert (
        "PGVECTOR_TARBALL_SHA256=69f4019389af05dc1c9548deb8628e62878e6e207c03907f2b8af2016472cdaa"
    ) in dockerfile
    assert "sha256sum -c -" in dockerfile
    assert "apk del .pgvector-build" in dockerfile


def test_ci_smokes_image_and_release_publishes_commit_bound_db_image() -> None:
    ci_text = (PROJECT_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    ci = yaml.safe_load(ci_text)
    release_text = (PROJECT_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    release = yaml.safe_load(release_text)

    assert "postgres-image-smoke" in ci["jobs"]
    assert "docker build --file Dockerfile.postgres" in ci_text
    assert "CREATE EXTENSION vector" in ci_text
    assert "server_version_num" in ci_text
    assert "::vector <=>" in ci_text
    assert "alembic upgrade 090" in ci_text
    assert "alembic downgrade 089" in ci_text
    assert "alembic upgrade head" in ci_text
    assert "TEST_DATABASE_URL" in ci_text
    postgres_smoke_script = next(
        step["run"]
        for step in ci["jobs"]["postgres-image-smoke"]["steps"]
        if step.get("name") == "Verify PostgreSQL and pgvector"
    )
    assert re.search(
        r'"SELECT version_num FROM alembic_version"\)" = "092"',
        postgres_smoke_script,
    )
    assert "ck_semantic_attempts_state" in ci_text
    assert release["env"]["IMAGE_DB"] == "ghcr.io/jekudy/vibe-gatekeeper-postgres"
    assert "file: ./Dockerfile.postgres" in release_text
    assert "sha-${{ github.event.workflow_run.head_sha }}" in release_text
