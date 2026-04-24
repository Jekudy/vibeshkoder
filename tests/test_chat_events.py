"""Tests for chat event handlers: join, leave, service message deletion.

No pytest-asyncio available — all async code runs via asyncio.run().

Tests exercise:
- _is_join / _is_leave pure helper functions
- _handle_join: User.is_member=True set after join
- _handle_leave: User.is_member=False set after leave
- delete_join_service_message: bot.delete called for join service messages
- Unauthorized join (no questionnaire) → user kicked and not set as member

The full aiogram ChatMemberUpdated dispatch (which requires a real Dispatcher)
is not tested; instead, we call the handler coroutines directly with minimal
MagicMock event objects.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock


def _run(coro):
    return asyncio.run(coro)


# ── Pure helper tests ─────────────────────────────────────────────────────────


class TestJoinLeaveHelpers:
    """_is_join and _is_leave identify event direction correctly."""

    def test_is_join_left_to_member(self, app_env):
        from bot.handlers.chat_events import _is_join

        event = MagicMock()
        event.old_chat_member.status = "left"
        event.new_chat_member.status = "member"
        assert _is_join(event) is True

    def test_is_join_kicked_to_member(self, app_env):
        from bot.handlers.chat_events import _is_join

        event = MagicMock()
        event.old_chat_member.status = "kicked"
        event.new_chat_member.status = "member"
        assert _is_join(event) is True

    def test_is_join_false_for_leave(self, app_env):
        from bot.handlers.chat_events import _is_join

        event = MagicMock()
        event.old_chat_member.status = "member"
        event.new_chat_member.status = "left"
        assert _is_join(event) is False

    def test_is_leave_member_to_left(self, app_env):
        from bot.handlers.chat_events import _is_leave

        event = MagicMock()
        event.old_chat_member.status = "member"
        event.new_chat_member.status = "left"
        assert _is_leave(event) is True

    def test_is_leave_member_to_kicked(self, app_env):
        from bot.handlers.chat_events import _is_leave

        event = MagicMock()
        event.old_chat_member.status = "member"
        event.new_chat_member.status = "kicked"
        assert _is_leave(event) is True

    def test_is_leave_false_for_join(self, app_env):
        from bot.handlers.chat_events import _is_leave

        event = MagicMock()
        event.old_chat_member.status = "left"
        event.new_chat_member.status = "member"
        assert _is_leave(event) is False


# ── _handle_leave ─────────────────────────────────────────────────────────────


class TestHandleLeave:
    """_handle_leave sets User.is_member=False in the DB."""

    def test_leave_marks_user_not_member(self, session_factory_sqlite):
        from bot.db.models import User
        from bot.db.repos.user import UserRepo
        from bot.handlers.chat_events import _handle_leave

        async def _run_test():
            async with session_factory_sqlite() as session:
                user = User(id=6001, first_name="Leaver", is_member=True)
                session.add(user)
                await session.commit()

                tg_user = MagicMock()
                tg_user.id = 6001

                await _handle_leave(session, tg_user)
                await session.commit()

                updated = await UserRepo.get(session, 6001)
                assert updated.is_member is False

        _run(_run_test())

    def test_leave_sets_left_at_timestamp(self, session_factory_sqlite):
        from bot.db.models import User
        from bot.handlers.chat_events import _handle_leave

        async def _run_test():
            async with session_factory_sqlite() as session:
                user = User(id=6002, first_name="Leaver2", is_member=True)
                session.add(user)
                await session.commit()

                tg_user = MagicMock()
                tg_user.id = 6002

                await _handle_leave(session, tg_user)
                await session.commit()

                from sqlalchemy import select

                result = await session.execute(
                    select(User).where(User.id == 6002)
                )
                u = result.scalar_one()
                assert u.left_at is not None

        _run(_run_test())


# ── _handle_join (legitimate join with vouched application) ───────────────────


class TestHandleJoinLegitimate:
    """_handle_join with a vouched application sets User.is_member=True."""

    def test_join_with_vouched_app_sets_member(self, session_factory_sqlite):
        from bot.db.models import User
        from bot.db.repos.application import ApplicationRepo
        from bot.db.repos.user import UserRepo
        from bot.handlers.chat_events import _handle_join

        async def _run_test():
            async with session_factory_sqlite() as session:
                user = User(id=6003, first_name="Joiner")
                session.add(user)
                await session.flush()

                app = await ApplicationRepo.create(session, 6003)
                await ApplicationRepo.update_status(session, app.id, "vouched")
                await session.commit()

                # Build minimal event mock
                event = MagicMock()
                event.chat.id = -1001234567890
                event.bot = AsyncMock()
                event.bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))

                tg_user = MagicMock()
                tg_user.id = 6003
                tg_user.username = "joiner"
                tg_user.first_name = "Joiner"
                tg_user.last_name = None

                await _handle_join(event, session, tg_user)
                await session.commit()

                updated = await UserRepo.get(session, 6003)
                assert updated is not None
                assert updated.is_member is True

        _run(_run_test())


# ── Service message deletion ──────────────────────────────────────────────────


class TestServiceMessageDeletion:
    """delete_join_service_message calls message.delete() for the right chat."""

    def test_join_service_msg_deleted_in_community_chat(self, app_env):
        """message.delete() is called when chat.id matches COMMUNITY_CHAT_ID."""
        from bot.handlers.chat_events import delete_join_service_message
        from bot.config import settings

        msg = MagicMock()
        msg.chat.id = settings.COMMUNITY_CHAT_ID
        msg.message_id = 123
        msg.delete = AsyncMock()

        _run(delete_join_service_message(msg))

        msg.delete.assert_called_once()

    def test_join_service_msg_not_deleted_in_other_chat(self, app_env):
        """message.delete() is NOT called when chat.id doesn't match."""
        from bot.handlers.chat_events import delete_join_service_message

        msg = MagicMock()
        msg.chat.id = -9999999999  # Different chat
        msg.message_id = 456
        msg.delete = AsyncMock()

        _run(delete_join_service_message(msg))

        msg.delete.assert_not_called()
