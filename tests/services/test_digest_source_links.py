"""Input validation bindings for digest source-link resolution."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.usefixtures("app_env")


async def test_boolean_position_is_rejected_before_source_lookup() -> None:
    from bot.services.digest_publisher import resolve_digest_source_links

    session = AsyncMock()
    with pytest.raises(ValueError, match="citations are malformed"):
        await resolve_digest_source_links(
            session,
            citations=[{"kind": "message_version", "id": 1, "position": False}],
            source_chat_id=-1001234567890,
        )
    session.execute.assert_not_awaited()


async def test_multiple_sources_for_one_item_are_allowed() -> None:
    from bot.services.digest_publisher import resolve_digest_source_links

    session = AsyncMock()
    result = MagicMock()
    result.all.return_value = [
        ("1", -1001234567890, 101),
        ("2", -1001234567890, 102),
    ]
    session.execute.return_value = result
    links = await resolve_digest_source_links(
        session,
        citations=[
            {"kind": "message_version", "id": 1, "position": 0},
            {"kind": "message_version", "id": 2, "position": 0},
        ],
        source_chat_id=-1001234567890,
    )
    assert links == {
        "[[mv:1]]": "https://t.me/c/1234567890/101",
        "[[mv:2]]": "https://t.me/c/1234567890/102",
    }
