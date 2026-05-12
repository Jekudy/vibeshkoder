"""T6-03 — verify ``bot/__main__.py`` registers the Phase 6 admin_extract router.

PHASE6_PLAN.md §7 T6-03 + T6-02 addendum: ``admin_extract.router`` MUST be
included via ``dp.include_routers(...)`` adjacent to ``admin.router``.

The check is AST-level (rather than importing ``bot.__main__`` and running
its async ``main()``) so it works without the asyncio event-loop boot.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = REPO_ROOT / "bot" / "__main__.py"


def _parse_main_module() -> ast.Module:
    return ast.parse(MAIN_PATH.read_text(encoding="utf-8"))


def test_admin_extract_imported_from_bot_handlers() -> None:
    """``admin_extract`` MUST appear in the ``from bot.handlers import ...`` block."""
    tree = _parse_main_module()
    imported_handler_names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "bot.handlers":
            for alias in node.names:
                imported_handler_names.append(alias.name)
    assert "admin_extract" in imported_handler_names, (
        "T6-03: ``admin_extract`` must be imported from ``bot.handlers`` in "
        f"bot/__main__.py — currently imported handlers: {imported_handler_names}"
    )


def test_admin_extract_router_included_in_dispatcher() -> None:
    """``admin_extract.router`` MUST be passed to ``dp.include_routers(...)``."""
    tree = _parse_main_module()
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Match ``dp.include_routers(...)`` or ``dp.include_router(...)``.
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr not in ("include_routers", "include_router"):
            continue
        for arg in node.args:
            if (
                isinstance(arg, ast.Attribute)
                and isinstance(arg.value, ast.Name)
                and arg.value.id == "admin_extract"
                and arg.attr == "router"
            ):
                found = True
                break
        if found:
            break
    assert found, (
        "T6-03: ``admin_extract.router`` must appear as an argument to "
        "``dp.include_routers(...)`` in bot/__main__.py"
    )
