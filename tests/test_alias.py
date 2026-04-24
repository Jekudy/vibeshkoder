from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tests.conftest import import_module


def test_normalize_member_tag_trims_emoji_and_limits_length(app_env) -> None:
    alias = import_module("bot.handlers.alias")

    assert alias.normalize_member_tag("  hello🙂world🚀team  ") == "helloworldteam"


def test_normalize_member_tag_empty_after_cleanup_becomes_none(app_env) -> None:
    alias = import_module("bot.handlers.alias")

    assert alias.normalize_member_tag("🙂🚀") is None
    assert alias.normalize_member_tag(" - ") is None


def test_alias_cooldown_window_constant(app_env) -> None:
    alias = import_module("bot.handlers.alias")

    now = datetime.now(timezone.utc)
    assert now + alias.ALIAS_COOLDOWN == now + timedelta(hours=1)


def test_uses_admin_title_for_supergroup_admins(app_env) -> None:
    alias = import_module("bot.handlers.alias")

    assert alias._uses_admin_title("supergroup", "administrator") is True
    assert alias._uses_admin_title("supergroup", "creator") is True
    assert alias._uses_admin_title("group", "administrator") is False
    assert alias._uses_admin_title("supergroup", "member") is False
