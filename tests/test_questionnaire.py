"""Tests for questionnaire FSM logic and repo interactions.

No pytest-asyncio available — all async code runs via asyncio.run() inside
sync test functions. Tests exercise the questionnaire repo layer directly,
as well as the pure helper build_intro_preview(), rather than dispatching
through the full aiogram Router (which requires a real Dispatcher + FSMContext
setup outside the scope of a safety-net).
"""
from __future__ import annotations

import asyncio
import os

# Ensure env is set before bot imports
os.environ.setdefault("BOT_TOKEN", "123456:test-token")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///test.db")
os.environ.setdefault("WEB_PASSWORD", "test-pass")
os.environ.setdefault("DEV_MODE", "true")


# ── Helpers ──────────────────────────────────────────────────────────────────


def _run(coro):
    return asyncio.run(coro)


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestBuildIntroPreview:
    """Pure-function tests for build_intro_preview."""

    def test_all_answers_formatted(self, app_env):
        from bot.db.models import QuestionnaireAnswer
        from bot.handlers.questionnaire import build_intro_preview

        answers = [
            QuestionnaireAnswer(question_index=i, answer_text=f"answer{i}")
            for i in range(7)
        ]
        preview = build_intro_preview(answers)
        for i in range(7):
            assert f"answer{i}" in preview

    def test_missing_answers_use_dash(self, app_env):
        from bot.handlers.questionnaire import build_intro_preview

        # Pass empty list — all placeholders should be '—'
        preview = build_intro_preview([])
        assert "—" in preview
        # Should not raise
        assert isinstance(preview, str)

    def test_partial_answers_fill_remaining_with_dash(self, app_env):
        from bot.db.models import QuestionnaireAnswer
        from bot.handlers.questionnaire import build_intro_preview

        answers = [
            QuestionnaireAnswer(question_index=0, answer_text="Иван"),
            QuestionnaireAnswer(question_index=1, answer_text="Москва"),
        ]
        preview = build_intro_preview(answers)
        assert "Иван" in preview
        assert "Москва" in preview
        assert "—" in preview  # remaining questions use dash


class TestQuestionnaireRepo:
    """Tests for QuestionnaireRepo using in-memory SQLite."""

    def test_save_and_retrieve_all_7_answers(self, session_factory_sqlite):
        """Happy path: saving 7 answers and reading them back."""
        from bot.db.models import User
        from bot.db.repos.application import ApplicationRepo
        from bot.db.repos.questionnaire import QuestionnaireRepo
        from bot.texts import QUESTIONS

        async def _run_test():
            async with session_factory_sqlite() as session:
                user = User(id=100, first_name="Tester")
                session.add(user)
                await session.flush()

                app = await ApplicationRepo.create(session, 100)
                for idx, question_text in enumerate(QUESTIONS):
                    await QuestionnaireRepo.save_answer(
                        session,
                        user_id=100,
                        application_id=app.id,
                        question_index=idx,
                        question_text=question_text,
                        answer_text=f"Ответ {idx}",
                    )
                await session.commit()

                answers = await QuestionnaireRepo.get_answers(session, 100, application_id=app.id)
                assert len(answers) == 7
                for i, answer in enumerate(answers):
                    assert answer.question_index == i
                    assert answer.answer_text == f"Ответ {i}"

        _run(_run_test())

    def test_cancel_mid_flow_deletes_answers(self, session_factory_sqlite):
        """Mid-flow cancel: deleting answers leaves no rows for the user."""
        from bot.db.models import User
        from bot.db.repos.application import ApplicationRepo
        from bot.db.repos.questionnaire import QuestionnaireRepo
        from bot.texts import QUESTIONS

        async def _run_test():
            async with session_factory_sqlite() as session:
                user = User(id=200, first_name="Canceller")
                session.add(user)
                await session.flush()

                app = await ApplicationRepo.create(session, 200)
                # Save 3 answers (partial flow)
                for idx in range(3):
                    await QuestionnaireRepo.save_answer(
                        session,
                        user_id=200,
                        application_id=app.id,
                        question_index=idx,
                        question_text=QUESTIONS[idx],
                        answer_text=f"Partial {idx}",
                    )
                await session.flush()

                # Simulate cancel: delete all answers
                await QuestionnaireRepo.delete_answers(session, 200, application_id=app.id)
                await session.commit()

                remaining = await QuestionnaireRepo.get_answers(session, 200, application_id=app.id)
                assert len(remaining) == 0

        _run(_run_test())

    def test_restart_clears_previous_answers(self, session_factory_sqlite):
        """Restart: after deleting answers, a fresh set can be saved."""
        from bot.db.models import User
        from bot.db.repos.application import ApplicationRepo
        from bot.db.repos.questionnaire import QuestionnaireRepo
        from bot.texts import QUESTIONS

        async def _run_test():
            async with session_factory_sqlite() as session:
                user = User(id=300, first_name="Restarter")
                session.add(user)
                await session.flush()

                app = await ApplicationRepo.create(session, 300)

                # First pass: save 2 answers, then delete (restart)
                for idx in range(2):
                    await QuestionnaireRepo.save_answer(
                        session,
                        user_id=300,
                        application_id=app.id,
                        question_index=idx,
                        question_text=QUESTIONS[idx],
                        answer_text=f"Old {idx}",
                    )
                await QuestionnaireRepo.delete_answers(session, 300, application_id=app.id)

                # Second pass: save all 7
                for idx, question_text in enumerate(QUESTIONS):
                    await QuestionnaireRepo.save_answer(
                        session,
                        user_id=300,
                        application_id=app.id,
                        question_index=idx,
                        question_text=question_text,
                        answer_text=f"New {idx}",
                    )
                await session.commit()

                answers = await QuestionnaireRepo.get_answers(session, 300, application_id=app.id)
                assert len(answers) == 7
                assert all(a.answer_text.startswith("New") for a in answers)

        _run(_run_test())

    def test_get_last_answered_index(self, session_factory_sqlite):
        """get_last_answered_index returns the highest question index saved."""
        from bot.db.models import User
        from bot.db.repos.application import ApplicationRepo
        from bot.db.repos.questionnaire import QuestionnaireRepo
        from bot.texts import QUESTIONS

        async def _run_test():
            async with session_factory_sqlite() as session:
                user = User(id=400, first_name="Indexer")
                session.add(user)
                await session.flush()

                app = await ApplicationRepo.create(session, 400)
                for idx in range(4):
                    await QuestionnaireRepo.save_answer(
                        session,
                        user_id=400,
                        application_id=app.id,
                        question_index=idx,
                        question_text=QUESTIONS[idx],
                        answer_text=f"Answer {idx}",
                    )
                await session.flush()

                last_idx = await QuestionnaireRepo.get_last_answered_index(
                    session, 400, app.id
                )
                assert last_idx == 3

        _run(_run_test())
