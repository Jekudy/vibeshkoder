"""Fail-closed bindings for forget-cascade digest redaction."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

pytestmark = pytest.mark.usefixtures("app_env")


async def test_malformed_position_neutralizes_db_and_telegram(db_session) -> None:
    from bot.db.models import Digest
    from bot.services.digest_redactor import REDACTED_DIGEST_BODY, redact_digest_for_forget

    now = datetime.now(timezone.utc)
    digest = Digest(
        type="daily",
        window_start=now - timedelta(days=1),
        window_end=now,
        body_markdown="TL;DR.\n\n- secret text [[mv:1]]",
        citations=[{"kind": "message_version", "id": 1, "position": "broken"}],
        status="posted",
        posted_chat_id=-1001234567890,
        posted_message_id=123,
        posted_at=now,
    )
    db_session.add(digest)
    await db_session.flush()
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()

    await redact_digest_for_forget(
        db_session,
        digest_id=digest.id,
        affected_mvids={1},
        affected_card_source_ids=set(),
        bot=bot,
    )

    row = (
        (
            await db_session.execute(
                text("SELECT body_markdown, citations, status FROM digests WHERE id = :id"),
                {"id": digest.id},
            )
        )
        .mappings()
        .one()
    )
    assert row == {"body_markdown": REDACTED_DIGEST_BODY, "citations": [], "status": "redacted"}
    bot.edit_message_text.assert_awaited_once()
    assert "secret text" not in bot.edit_message_text.call_args.kwargs["text"]


async def test_no_matching_citation_is_a_noop(db_session) -> None:
    from bot.db.models import Digest
    from bot.services.digest_redactor import redact_digest_for_forget

    now = datetime.now(timezone.utc)
    citations = [{"kind": "message_version", "id": 2, "position": 0}]
    digest = Digest(
        type="daily",
        window_start=now - timedelta(days=1),
        window_end=now,
        body_markdown="TL;DR.\n\n- untouched [[mv:2]]",
        citations=citations,
        status="draft",
    )
    db_session.add(digest)
    await db_session.flush()

    await redact_digest_for_forget(
        db_session,
        digest_id=digest.id,
        affected_mvids={1},
        affected_card_source_ids=set(),
        bot=None,
    )

    row = (
        (
            await db_session.execute(
                text("SELECT body_markdown, citations, status FROM digests WHERE id = :id"),
                {"id": digest.id},
            )
        )
        .mappings()
        .one()
    )
    assert row == {
        "body_markdown": "TL;DR.\n\n- untouched [[mv:2]]",
        "citations": citations,
        "status": "draft",
    }


async def test_row_lock_error_propagates() -> None:
    from bot.services.digest_redactor import redact_digest_for_forget

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[MagicMock(), SQLAlchemyError("row lock failed")])

    with pytest.raises(SQLAlchemyError, match="row lock failed"):
        await redact_digest_for_forget(
            session,
            digest_id=1,
            affected_mvids={1},
            affected_card_source_ids=set(),
            bot=None,
        )
