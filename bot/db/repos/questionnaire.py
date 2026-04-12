from __future__ import annotations

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import QuestionnaireAnswer


class QuestionnaireRepo:
    @staticmethod
    async def save_answer(
        session: AsyncSession,
        user_id: int,
        application_id: int,
        question_index: int,
        question_text: str,
        answer_text: str,
    ) -> QuestionnaireAnswer:
        answer = QuestionnaireAnswer(
            user_id=user_id,
            application_id=application_id,
            question_index=question_index,
            question_text=question_text,
            answer_text=answer_text,
            is_current=True,
        )
        session.add(answer)
        await session.flush()
        return answer

    @staticmethod
    async def get_answers(
        session: AsyncSession,
        user_id: int,
        application_id: int | None = None,
        current_only: bool = True,
    ) -> list[QuestionnaireAnswer]:
        stmt = select(QuestionnaireAnswer).where(
            QuestionnaireAnswer.user_id == user_id
        )
        if application_id is not None:
            stmt = stmt.where(
                QuestionnaireAnswer.application_id == application_id
            )
        if current_only:
            stmt = stmt.where(QuestionnaireAnswer.is_current.is_(True))
        stmt = stmt.order_by(QuestionnaireAnswer.question_index)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    async def delete_answers(
        session: AsyncSession,
        user_id: int,
        application_id: int | None = None,
    ) -> None:
        stmt = delete(QuestionnaireAnswer).where(
            QuestionnaireAnswer.user_id == user_id
        )
        if application_id is not None:
            stmt = stmt.where(
                QuestionnaireAnswer.application_id == application_id
            )
        await session.execute(stmt)
        await session.flush()

    @staticmethod
    async def mark_not_current(
        session: AsyncSession, user_id: int
    ) -> None:
        await session.execute(
            update(QuestionnaireAnswer)
            .where(QuestionnaireAnswer.user_id == user_id)
            .values(is_current=False)
        )
        await session.flush()

    @staticmethod
    async def get_last_answered_index(
        session: AsyncSession, user_id: int, application_id: int
    ) -> int | None:
        result = await session.execute(
            select(QuestionnaireAnswer.question_index)
            .where(
                QuestionnaireAnswer.user_id == user_id,
                QuestionnaireAnswer.application_id == application_id,
                QuestionnaireAnswer.is_current.is_(True),
            )
            .order_by(QuestionnaireAnswer.question_index.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()
