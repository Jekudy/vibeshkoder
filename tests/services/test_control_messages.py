import json

import pytest
from sqlalchemy import text

from bot.services.control_messages import control_message_excludes_sql_fragment


async def _sql_allows(
    db_session,
    *,
    entities: object | None,
    message_text: str | None = None,
) -> bool:
    result = await db_session.execute(
        text(
            "SELECT "
            + control_message_excludes_sql_fragment("mv")
            + " AS allowed FROM (VALUES (CAST(:entities AS jsonb), NULL::text, "
            "CAST(:message_text AS text), NULL::text)) "
            "AS mv(entities_json, normalized_text, text, caption)"
        ),
        {"entities": None if entities is None else json.dumps(entities), "message_text": message_text},
    )
    return bool(result.scalar_one())


@pytest.mark.parametrize(
    "entities",
    [
        [{"type": "bot_command", "offset": 0, "length": 11}],
        '[{"type":"bot_command","offset":0,"length":11}]',
    ],
)
async def test_sql_excludes_telegram_bot_command_at_offset_zero(
    db_session, entities: object
) -> None:
    assert not await _sql_allows(
        db_session,
        entities=entities,
        message_text="ordinary",
    )


@pytest.mark.parametrize("command", ["/digest_now", "/digest_now weekly", "/help@TestBot"])
async def test_sql_excludes_canonical_command_text_fallback(db_session, command: str) -> None:
    assert not await _sql_allows(db_session, entities=None, message_text=command)


async def test_sql_allows_non_command_and_nonzero_command_entity(db_session) -> None:
    assert await _sql_allows(db_session, entities=None, message_text="ordinary")
    assert await _sql_allows(
        db_session,
        entities=[{"type": "bot_command", "offset": 5, "length": 4}],
        message_text="prefix /help",
    )


def test_sql_alias_must_be_identifier() -> None:
    with pytest.raises(ValueError):
        control_message_excludes_sql_fragment("mv; DROP TABLE messages")
