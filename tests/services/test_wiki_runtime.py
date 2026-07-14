from __future__ import annotations

from pathlib import Path

import pytest


def test_wiki_runtime_config_is_strict_and_parses_vps_denylist() -> None:
    from bot.services.wiki_runtime import load_wiki_runtime_config

    result = load_wiki_runtime_config(
        {
            "WIKI_STATIC_PUBLISH_DIR": "/var/lib/shkoder/wiki-current",
            "WIKI_SITE_TITLE": "Шкодер Wiki",
            "WIKI_FORBIDDEN_ORIGINS_JSON": '["187.77.98.73","bot.internal.example"]',
        },
        require_forbidden_origins=True,
    )

    assert result.publish_dir == Path("/var/lib/shkoder/wiki-current")
    assert result.site_title == "Шкодер Wiki"
    assert result.forbidden_origins == ("187.77.98.73", "bot.internal.example")


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {"WIKI_STATIC_PUBLISH_DIR": "relative/wiki"},
        {"WIKI_STATIC_PUBLISH_DIR": "/"},
        {
            "WIKI_STATIC_PUBLISH_DIR": "/tmp/wiki",
            "WIKI_FORBIDDEN_ORIGINS_JSON": "not-json",
        },
        {
            "WIKI_STATIC_PUBLISH_DIR": "/tmp/wiki",
            "WIKI_FORBIDDEN_ORIGINS_JSON": '[" ok "]',
        },
    ],
)
def test_wiki_runtime_config_rejects_unsafe_or_missing_values(
    environ: dict[str, str],
) -> None:
    from bot.services.wiki_runtime import load_wiki_runtime_config

    with pytest.raises(ValueError):
        load_wiki_runtime_config(environ, require_forbidden_origins=False)


def test_public_wiki_requires_explicit_vps_origin_denylist() -> None:
    from bot.services.wiki_runtime import load_wiki_runtime_config

    with pytest.raises(ValueError, match="VPS origins"):
        load_wiki_runtime_config(
            {"WIKI_STATIC_PUBLISH_DIR": "/tmp/wiki"},
            require_forbidden_origins=True,
        )
