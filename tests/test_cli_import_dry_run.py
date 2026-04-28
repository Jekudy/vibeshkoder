"""CLI smoke tests for `python -m bot.cli import_dry_run` (T2-01 / issue #94).

All tests are offline: no DB, no network. Exercises bot/cli.py via the public
main() entry point (not subprocess), capturing stdout.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "td_export"
SMALL_CHAT = FIXTURE_DIR / "small_chat.json"


def _invoke(argv: list[str]) -> tuple[int, str]:
    """Invoke bot.cli.main with the given argv and capture stdout.

    Returns (return_code, stdout_text).
    """
    from bot.cli import main

    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        rc = main(argv)
    finally:
        sys.stdout = old_stdout
    return rc, buf.getvalue()


def test_cli_runs_on_small_chat():
    rc, stdout = _invoke(["import_dry_run", str(SMALL_CHAT)])
    assert rc == 0, f"Expected return code 0, got {rc}. Output: {stdout!r}"
    payload = json.loads(stdout)
    assert payload["total_messages"] == 6


def test_cli_returns_exit2_on_missing_file():
    """Documented contract: file-not-found → exit code 2 (distinct from parse-error 1)."""
    rc, _ = _invoke(["import_dry_run", "/nonexistent/path/export.json"])
    assert rc == 2


def test_cli_returns_exit1_on_invalid_json(tmp_path: Path):
    """Documented contract: parse error → exit code 1 (distinct from file-not-found 2)."""
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid", encoding="utf-8")
    rc, _ = _invoke(["import_dry_run", str(bad)])
    assert rc == 1


# ---------------------------------------------------------------------------
# Fix 6 — CLI wraps FileNotFoundError from parse_export (race condition)
# ---------------------------------------------------------------------------

def test_cli_returns_exit2_on_race_deleted_file(tmp_path: Path):
    """parse_export raising FileNotFoundError mid-call must map to exit code 2 (file-not-found)."""
    from unittest.mock import patch

    # Use a real-looking path that passes is_file() check but then simulate
    # parse_export raising FileNotFoundError (race: deleted between check and open).
    with patch("bot.services.import_parser.parse_export", side_effect=FileNotFoundError("race deleted")):
        rc, _ = _invoke(["import_dry_run", str(SMALL_CHAT)])
    assert rc == 2
