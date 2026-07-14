"""Migration 083: canonical topic slug on knowledge cards."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.usefixtures("app_env")


async def test_083_topic_slug_column_check_and_index_exist(db_session) -> None:
    column = (
        await db_session.execute(
            text(
                """
                SELECT data_type, is_nullable
                FROM information_schema.columns
                WHERE table_schema='public'
                  AND table_name='knowledge_cards'
                  AND column_name='topic_slug'
                """
            )
        )
    ).one()
    assert column == ("text", "YES")

    check_definition = await db_session.scalar(
        text(
            """
            SELECT pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE conname='ck_knowledge_cards_topic_slug'
            """
        )
    )
    assert check_definition is not None
    assert "lower(topic_slug)" in check_definition
    assert "a-z0-9" in check_definition

    index_definition = await db_session.scalar(
        text(
            """
            SELECT indexdef
            FROM pg_indexes
            WHERE schemaname='public'
              AND indexname='ix_knowledge_cards_topic_slug'
            """
        )
    )
    assert index_definition is not None
    assert "topic_slug" in index_definition

    run_column = (
        await db_session.execute(
            text(
                """
                SELECT data_type, is_nullable
                FROM information_schema.columns
                WHERE table_schema='public'
                  AND table_name='extraction_runs'
                  AND column_name='source_chat_id'
                """
            )
        )
    ).one()
    assert run_column == ("bigint", "YES")
    run_index = await db_session.scalar(
        text(
            """
            SELECT indexdef
            FROM pg_indexes
            WHERE schemaname='public'
              AND indexname='ix_extraction_runs_source_chat_id_window_end'
            """
        )
    )
    assert run_index is not None
    assert "source_chat_id" in run_index
    assert "ingestion_window_end" in run_index


@pytest.mark.parametrize(
    "invalid_slug",
    ["Upper-Case", "under_score", "double--dash", " leading", "trailing-"],
)
async def test_083_rejects_non_lowercase_kebab_slug(
    db_session,
    invalid_slug: str,
) -> None:
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    """
                    INSERT INTO knowledge_cards (
                        id, topic_slug, title, body_markdown, card_status
                    ) VALUES (:id, :slug, 'Title', 'Body', 'draft')
                    """
                ),
                {"id": uuid.uuid4(), "slug": invalid_slug},
            )


async def test_083_allows_null_and_lowercase_kebab_slug(db_session) -> None:
    for slug in (None, "valid-topic-123"):
        await db_session.execute(
            text(
                """
                INSERT INTO knowledge_cards (
                    id, topic_slug, title, body_markdown, card_status
                ) VALUES (:id, :slug, 'Title', 'Body', 'draft')
                """
            ),
            {"id": uuid.uuid4(), "slug": slug},
        )
