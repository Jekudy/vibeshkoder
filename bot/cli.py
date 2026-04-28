"""Bot CLI entry points (run via `python -m bot.cli <subcommand> [args...]`).

Currently supported subcommands:
    import_dry_run <export_path>  — parse a Telegram Desktop export JSON and print stats
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _cmd_import_dry_run(args: argparse.Namespace) -> int:
    from bot.services.import_parser import parse_export

    path = Path(args.export_path).expanduser().resolve()
    if not path.is_file():
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        return 2

    try:
        report = parse_export(path)
    except FileNotFoundError as e:
        print(f"ERROR: file not found: {e}", file=sys.stderr)
        return 2
    except (ValueError, json.JSONDecodeError) as e:
        print(f"ERROR: parse failed: {e}", file=sys.stderr)
        return 1

    from dataclasses import asdict

    payload = asdict(report)
    # datetime values are not JSON-serialisable by default; convert to ISO strings.
    if payload.get("date_range_start"):
        payload["date_range_start"] = payload["date_range_start"].isoformat()
    if payload.get("date_range_end"):
        payload["date_range_end"] = payload["date_range_end"].isoformat()

    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bot.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_import = sub.add_parser(
        "import_dry_run",
        help="Parse a Telegram Desktop export JSON and print stats (no DB writes).",
    )
    p_import.add_argument("export_path", type=str)
    p_import.set_defaults(func=_cmd_import_dry_run)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
