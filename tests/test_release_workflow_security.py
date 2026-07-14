"""Security contract for privileged GHCR release workflow triggers."""

from pathlib import Path


RELEASE_WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"


def test_release_follows_only_push_ci_from_this_repository() -> None:
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert "github.event.workflow_run.event == 'push'" in workflow
    assert "github.event.workflow_run.head_repository.full_name == github.repository" in workflow
    assert "github.event.workflow_run.head_branch == 'main'" in workflow
