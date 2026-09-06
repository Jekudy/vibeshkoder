from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import QuestionnaireAnswer


class QuestionnaireRepo:
    @staticmethod
    async def save_answer(
        session: AsyncSession,
        user_id: int,
        application_id: int,
        field_id: str,
        question_index: int,
        question_text: str,
        answer_text: str,
    ) -> QuestionnaireAnswer:
        answer = QuestionnaireAnswer(
            user_id=user_id,
            application_id=application_id,
            field_id=field_id,
            question_index=question_index,
            question_text=question_text,
            answer_text=answer_text,
            is_current=True,
        )
        session.add(answer)
        await session.flush()
        return answer

    @staticmethod
    async def get_by_application(
        session: AsyncSession, *, application_id: int
    ) -> list[QuestionnaireAnswer]:
        result = await session.execute(
            select(QuestionnaireAnswer)
            .where(
                QuestionnaireAnswer.application_id == application_id,
                QuestionnaireAnswer.is_current.is_(True),
            )
            .order_by(QuestionnaireAnswer.question_index)
        )
        return list(result.scalars().all())
