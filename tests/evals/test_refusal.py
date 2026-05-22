"""Phase 11 §5.3 — refusal / abstention cases for recall.

R5.a/R5.b handler-layer tightening (8.5-C carryover):
- R5.a: non-admin invokes /digest_approve → handler _is_admin gate → silent no-op
  (no message.answer, no approve_digest service call).
- R5.b: non-admin invokes /digest_reject → same silent no-op.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.eval_runner import run_eval_recall
from tests.conftest import import_module
from tests.evals.conftest import SEED_CHAT_ID

pytestmark = pytest.mark.asyncio(loop_scope="class")

# Explicit non-community chat_ids used in R3b/R3c wrong-chat branch tests.
# These must differ from SEED_CHAT_ID so the handler's community-id guard fires.
NON_COMMUNITY_CHAT_ID_PRIVATE = 1099887766    # R3b: private chat wrong-chat path (positive: Telegram private chat IDs > 0)
NON_COMMUNITY_CHAT_ID_GROUP = -1099887767    # R3c: supergroup wrong-chat + forbidden path

CONTENT_TRUNCATE_SQL = text(
    "TRUNCATE TABLE qa_traces, message_versions, chat_messages, forget_events "
    "RESTART IDENTITY CASCADE"
)


def _message(
    *,
    chat_id: int = SEED_CHAT_ID,
    chat_type: str = "supergroup",
    user_id: int = 1001,
    message_id: int = 700,
) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type=chat_type),
        from_user=SimpleNamespace(
            id=user_id,
            username="refusal_user",
            first_name="Refusal",
            last_name=None,
        ),
        message_id=message_id,
        reply=AsyncMock(),
    )


def _command(args: str | None) -> SimpleNamespace:
    return SimpleNamespace(args=args)


async def _truncate_content_tables(session: AsyncSession) -> None:
    await session.execute(CONTENT_TRUNCATE_SQL)
    await session.flush()


async def _insert_excluded_message(
    session: AsyncSession,
    *,
    message_id: int,
    body: str,
    memory_policy: str = "normal",
    chat_redacted: bool = False,
    version_redacted: bool = False,
) -> None:
    user_id = 91001
    now = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)
    content_hash = f"refusal-{message_id}"

    await session.execute(
        text(
            """
            INSERT INTO users (
                id, username, first_name, last_name, is_member, is_admin, is_imported_only
            )
            VALUES (
                :user_id, 'refusal_seed_user', 'Refusal Seed', NULL, TRUE, FALSE, FALSE
            )
            ON CONFLICT (id) DO UPDATE
            SET
                username = EXCLUDED.username,
                first_name = EXCLUDED.first_name,
                is_member = EXCLUDED.is_member,
                is_admin = EXCLUDED.is_admin
            """
        ),
        {"user_id": user_id},
    )

    chat_message_id = (
        await session.execute(
            text(
                """
                INSERT INTO chat_messages (
                    message_id,
                    chat_id,
                    user_id,
                    text,
                    date,
                    raw_json,
                    memory_policy,
                    visibility,
                    is_redacted,
                    content_hash,
                    message_kind
                )
                VALUES (
                    :message_id,
                    :chat_id,
                    :user_id,
                    :body,
                    :date,
                    CAST(:raw_json AS json),
                    :memory_policy,
                    'member',
                    :chat_redacted,
                    :content_hash,
                    'text'
                )
                RETURNING id
                """
            ),
            {
                "message_id": message_id,
                "chat_id": SEED_CHAT_ID,
                "user_id": user_id,
                "body": body,
                "date": now,
                "raw_json": "{}",
                "memory_policy": memory_policy,
                "chat_redacted": chat_redacted,
                "content_hash": content_hash,
            },
        )
    ).scalar_one()

    version_id = (
        await session.execute(
            text(
                """
                INSERT INTO message_versions (
                    chat_message_id,
                    version_seq,
                    text,
                    normalized_text,
                    content_hash,
                    is_redacted,
                    imported_final
                )
                VALUES (
                    :chat_message_id,
                    1,
                    :body,
                    :body,
                    :content_hash,
                    :version_redacted,
                    FALSE
                )
                RETURNING id
                """
            ),
            {
                "chat_message_id": chat_message_id,
                "body": body,
                "content_hash": content_hash,
                "version_redacted": version_redacted,
            },
        )
    ).scalar_one()

    await session.execute(
        text("UPDATE chat_messages SET current_version_id = :version_id WHERE id = :id"),
        {"version_id": version_id, "id": chat_message_id},
    )
    await session.flush()


async def _assert_rows_match_query_without_governance_filter(
    session: AsyncSession,
    *,
    query: str,
    expected_count: int,
) -> None:
    count = (
        await session.execute(
            text(
                """
                SELECT count(*)
                FROM message_versions
                WHERE search_tsv @@ plainto_tsquery('russian', :query)
                """
            ),
            {"query": query},
        )
    ).scalar_one()
    assert count == expected_count


def _patch_handler_db_edges(handler: Any) -> tuple[AsyncMock, AsyncMock, AsyncMock]:
    trace_create = AsyncMock()
    run_qa = AsyncMock()
    persist = AsyncMock(return_value=SimpleNamespace(chat_message=SimpleNamespace(id=1)))
    handler.QaTraceRepo.create = trace_create
    handler.run_qa = run_qa
    handler.persist_message_with_policy = persist
    handler.UserRepo.upsert = AsyncMock()
    return trace_create, run_qa, persist


@pytest.mark.usefixtures("eval_app_env")
class TestRefusal:
    async def test_r1_empty_seed_abstains(self, eval_db_session: AsyncSession) -> None:
        await _truncate_content_tables(eval_db_session)

        bundle, _trace = await run_eval_recall(
            eval_db_session,
            query="любой реальный запрос",
            chat_id=SEED_CHAT_ID,
        )

        assert bundle.abstained is True
        assert bundle.items == ()

    async def test_r2_only_redacted_and_offrecord_seed_abstains(
        self,
        eval_db_session: AsyncSession,
    ) -> None:
        await _truncate_content_tables(eval_db_session)
        query = "секретный отказовый маркер"
        body = "секретный отказовый маркер должен совпадать с полнотекстовым запросом"
        await _insert_excluded_message(
            eval_db_session,
            message_id=801,
            body=body,
            memory_policy="offrecord",
        )
        await _insert_excluded_message(
            eval_db_session,
            message_id=802,
            body=body,
            chat_redacted=True,
        )
        await _insert_excluded_message(
            eval_db_session,
            message_id=803,
            body=body,
            version_redacted=True,
        )
        await _assert_rows_match_query_without_governance_filter(
            eval_db_session,
            query=query,
            expected_count=3,
        )

        bundle, _trace = await run_eval_recall(
            eval_db_session,
            query=query,
            chat_id=SEED_CHAT_ID,
        )

        assert bundle.abstained is True
        assert bundle.items == ()

    async def test_r3a_non_member_refuses_at_handler(self) -> None:
        """R3a (non-community-membership branch): user in community chat without
        is_member/is_admin → handler replies access-denied, never runs run_qa,
        writes an abstained qa_trace."""
        handler = import_module("bot.handlers.qa")
        trace_create, run_qa, _persist = _patch_handler_db_edges(handler)
        handler.FeatureFlagRepo.get = AsyncMock(return_value=True)
        handler.UserRepo.get = AsyncMock(
            return_value=SimpleNamespace(id=1001, is_member=False, is_admin=False)
        )
        message = _message(user_id=1001, message_id=901)

        await handler.recall_handler(message, _command("память"), AsyncMock())

        message.reply.assert_awaited_once_with("Доступ только участникам сообщества.")
        run_qa.assert_not_awaited()
        trace_create.assert_awaited_once()
        assert trace_create.call_args.kwargs["abstained"] is True

    async def test_r3b_wrong_chat_private_replies_usage_hint(self) -> None:
        """R3b (wrong-chat private branch): /recall invoked from a 1:1 private
        chat with the bot → handler replies the community-only usage hint,
        never runs run_qa, writes an abstained qa_trace."""
        handler = import_module("bot.handlers.qa")
        trace_create, run_qa, _persist = _patch_handler_db_edges(handler)
        handler.FeatureFlagRepo.get = AsyncMock(return_value=True)
        # UserRepo.get must NOT be called on the wrong-chat branch (returns early).
        handler.UserRepo.get = AsyncMock(side_effect=AssertionError(
            "UserRepo.get must not be reached on wrong-chat branch"
        ))
        message = _message(
            chat_id=NON_COMMUNITY_CHAT_ID_PRIVATE,
            chat_type="private",
            user_id=1001,
            message_id=902,
        )

        await handler.recall_handler(message, _command("память"), AsyncMock())

        message.reply.assert_awaited_once_with(
            "Команда /recall работает только в community чате."
        )
        run_qa.assert_not_awaited()
        trace_create.assert_awaited_once()
        assert trace_create.call_args.kwargs["abstained"] is True

    async def test_r3c_wrong_chat_forbidden_silent_audit(self) -> None:
        """R3c (wrong-chat + TelegramForbiddenError branch): /recall invoked
        from a chat where the bot lacks send permission → reply attempt is
        swallowed via `except TelegramForbiddenError`, audit qa_trace still
        written for abstention record. Verifies the silent-fallback path in
        bot/handlers/qa.py — without this case a regression could surface
        the exception and break wrong-chat handling for kicked / restricted
        bots."""
        from aiogram.exceptions import TelegramForbiddenError

        handler = import_module("bot.handlers.qa")
        trace_create, run_qa, _persist = _patch_handler_db_edges(handler)
        handler.FeatureFlagRepo.get = AsyncMock(return_value=True)
        handler.UserRepo.get = AsyncMock(side_effect=AssertionError(
            "UserRepo.get must not be reached on wrong-chat branch"
        ))
        message = _message(
            chat_id=NON_COMMUNITY_CHAT_ID_GROUP,
            chat_type="supergroup",
            user_id=1001,
            message_id=903,
        )
        # Simulate the bot lacking can_send_messages — handler must swallow
        # the exception (logger.info) and continue to audit_empty + return.
        message.reply = AsyncMock(
            side_effect=TelegramForbiddenError(method=None, message="kicked")  # type: ignore[arg-type]
        )

        await handler.recall_handler(message, _command("память"), AsyncMock())

        # Reply WAS attempted (then raised); audit must still happen abstained.
        message.reply.assert_awaited_once()
        run_qa.assert_not_awaited()
        trace_create.assert_awaited_once()
        assert trace_create.call_args.kwargs["abstained"] is True

    async def test_r4_empty_query_replies_usage_hint_without_retrieval(self) -> None:
        handler = import_module("bot.handlers.qa")
        trace_create, run_qa, _persist = _patch_handler_db_edges(handler)
        handler.FeatureFlagRepo.get = AsyncMock(return_value=True)
        handler.UserRepo.get = AsyncMock(
            return_value=SimpleNamespace(id=1001, is_member=True, is_admin=False)
        )
        session = AsyncMock()
        message = _message(user_id=1001, message_id=902)

        await handler.recall_handler(message, _command("   "), session)

        message.reply.assert_awaited_once_with(
            "Использование: <code>/recall &lt;вопрос&gt;</code>",
            parse_mode="HTML",
        )
        run_qa.assert_not_awaited()
        trace_create.assert_awaited_once()
        assert trace_create.call_args.kwargs["query"] == ""
        assert trace_create.call_args.kwargs["abstained"] is True
        assert session.mock_calls == []


# ────────────────────────────────────────────────────────────────────────────
# R5.a / R5.b — handler-layer tightening (8.5-C carryover)
#
# These tests verify the handler's _is_admin gate at the Telegram routing
# layer. The service-layer invariants (no DB mutation) are covered by
# test_digest_weekly_review_invariants.py::test_R5a_* / test_R5b_*.
#
# Handler contract: non-admin caller → immediate return, no message.answer,
# no service call. Matches the "silent no-op" precedent for /stats /
# /admin_extract (PHASE8_PLAN.md §3).
# ────────────────────────────────────────────────────────────────────────────


def _non_admin_message(user_id: int = 9_999_999_999) -> SimpleNamespace:
    """Build a minimal Telegram Message mock for a non-admin user."""
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        answer=AsyncMock(),
    )


def _command_object(args: str = "1") -> SimpleNamespace:
    """Build a minimal CommandObject mock."""
    return SimpleNamespace(args=args)


@pytest.mark.usefixtures("eval_app_env")
class TestR5HandlerLayer:
    """R5.a/R5.b: handler-layer admin gate for digest review commands."""

    async def test_r5a_non_admin_digest_approve_handler_silent_no_op(self) -> None:
        """R5.a handler-layer: non-admin calls cmd_digest_approve → silent return,
        no message.answer, approve_digest service NOT called."""
        handler = import_module("bot.handlers.digest")
        message = _non_admin_message()
        bot = AsyncMock()
        session = AsyncMock()
        command = _command_object("1")

        # Ensure the non-admin user is not in ADMIN_IDS.
        from bot.config import settings
        assert message.from_user.id not in set(settings.ADMIN_IDS), (
            "R5.a precondition: test user must NOT be in ADMIN_IDS"
        )

        with patch.object(handler, "approve_digest", new_callable=AsyncMock) as mock_svc:
            await handler.cmd_digest_approve(message, bot, session, command)

        # Silent no-op: no answer sent.
        message.answer.assert_not_called()
        # Service layer never reached.
        mock_svc.assert_not_awaited()

    async def test_r5b_non_admin_digest_reject_handler_silent_no_op(self) -> None:
        """R5.b handler-layer: non-admin calls cmd_digest_reject → silent return,
        no message.answer, reject_digest service NOT called."""
        handler = import_module("bot.handlers.digest")
        message = _non_admin_message()
        session = AsyncMock()
        command = _command_object("1 причина")

        # Ensure the non-admin user is not in ADMIN_IDS.
        from bot.config import settings
        assert message.from_user.id not in set(settings.ADMIN_IDS), (
            "R5.b precondition: test user must NOT be in ADMIN_IDS"
        )

        with patch.object(handler, "reject_digest", new_callable=AsyncMock) as mock_svc:
            await handler.cmd_digest_reject(message, session, command)

        # Silent no-op: no answer sent.
        message.answer.assert_not_called()
        # Service layer never reached.
        mock_svc.assert_not_awaited()
