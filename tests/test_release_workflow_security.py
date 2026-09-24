"""Security contract for privileged GHCR release workflow triggers."""

from pathlib import Path


RELEASE_WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"


def test_release_follows_only_push_ci_from_this_repository() -> None:
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert "github.event.workflow_run.event == 'push'" in workflow
    assert "github.event.workflow_run.head_repository.full_name == github.repository" in workflow
    assert "github.event.workflow_run.head_branch == 'main'" in workflow


def test_release_deploys_exact_application_sha_only_after_images_are_pushed() -> None:
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert "deploy-bot:" in workflow
    deployment = workflow.split("  deploy-bot:", 1)[1]
    assert "needs: build-and-push" in deployment
    assert "runs-on: [self-hosted, shkoder-vps]" in deployment
    assert "RELEASE_SHA: ${{ github.event.workflow_run.head_sha }}" in deployment
    assert "ref: ${{ github.event.workflow_run.head_sha }}" in deployment
    assert 'python3 ops/compose/deploy.py "$RELEASE_SHA"' in deployment
    assert "git ls-remote" in workflow
    assert "docker buildx imagetools create" in workflow
    assert "${{ env.IMAGE_BOT }}:main" not in workflow
