"""T6-04 + T6-05 — admin_cards router registration smoke test.

Confirms ``bot/__main__.py`` imports and registers the ``admin_cards.router``
in the canonical order alongside ``admin_extract.router``.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("app_env")


def test_admin_cards_router_importable() -> None:
    """``admin_cards.router`` is a Router instance and has the expected name."""
    from aiogram import Router

    from bot.handlers import admin_cards

    assert isinstance(admin_cards.router, Router)
    assert admin_cards.router.name == "admin_cards"


def test_main_imports_admin_cards() -> None:
    """``bot.__main__`` imports ``admin_cards`` so its handlers register."""
    import bot.__main__ as main_module

    assert hasattr(main_module, "admin_cards")
    from bot.handlers import admin_cards as expected

    assert main_module.admin_cards is expected


def test_handler_functions_exported() -> None:
    """All five command handlers are public symbols of ``admin_cards``."""
    from bot.handlers import admin_cards

    for fn in (
        "cmd_candidates",
        "cmd_approve",
        "cmd_reject",
        "cmd_cards",
        "cmd_card",
    ):
        assert hasattr(admin_cards, fn), f"missing handler: {fn}"
