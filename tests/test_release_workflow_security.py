"""Security contract for privileged GHCR release workflow triggers."""

from pathlib import Path


RELEASE_WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"


def test_release_follows_only_push_ci_from_this_repository() -> None:
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert "github.event.workflow_run.event == 'push'" in workflow
    assert "github.event.workflow_run.head_repository.full_name == github.repository" in workflow
    assert "github.event.workflow_run.head_branch == 'main'" in workflow


def test_release_deploys_exact_bot_sha_only_after_images_are_pushed() -> None:
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert "deploy-bot:" in workflow
    assert "needs: build-and-push" in workflow
    assert "runs-on: [self-hosted, shkoder-vps]" in workflow
    assert "IMAGE_TAG: sha-${{ github.event.workflow_run.head_sha }}" in workflow
    assert "docker_registry_image_tag" in workflow
    assert '"$api/deploy"' in workflow
    assert '"$api/deployments/$deployment_uuid"' in workflow
    assert 'if [[ "$actual_tag" != "$IMAGE_TAG" ]]' in workflow
