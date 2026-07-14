"""Operator-facing help for explicit memory reconciliation commands."""

from __future__ import annotations

import pytest

from bot.cli import main


pytestmark = pytest.mark.usefixtures("app_env")


@pytest.mark.parametrize(
    ("command", "required_fragments"),
    [
        (
            "memory_reconcile_extraction",
            (
                "--run-id",
                "--action",
                "safe_retry",
                "risk_accepted_retry",
                "abandon",
                "--accept-possible-duplicate-cost",
                "--accept-memory-gap",
                "--reason",
                "--evidence-hash",
            ),
        ),
        (
            "memory_reconcile_image",
            (
                "--message-media-id",
                "--action",
                "risk_accepted_retry",
                "abandon",
                "--accept-possible-duplicate-cost",
                "--accept-memory-gap",
                "--reason",
                "--evidence-hash",
            ),
        ),
    ],
)
def test_reconciliation_cli_help_documents_irreversible_acceptance_flags(
    command: str,
    required_fragments: tuple[str, ...],
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as caught:
        main([command, "--help"])
    assert caught.value.code == 0
    help_text = capsys.readouterr().out
    for fragment in required_fragments:
        assert fragment in help_text
