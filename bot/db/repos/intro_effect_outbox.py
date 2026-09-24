from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import IntroEffectOutbox


class IntroEffectOutboxRepo:
    @staticmethod
    async def enqueue_once(session: AsyncSession, *, application_id: int, effect_kind: str) -> None:
        await session.execute(
            insert(IntroEffectOutbox)
            .values(application_id=application_id, effect_kind=effect_kind, status="pending")
            .on_conflict_do_nothing(index_elements=("application_id", "effect_kind"))
        )
        await session.flush()

    @staticmethod
    async def ensure_projection_pending(session: AsyncSession, *, application_id: int) -> None:
        effect = await session.scalar(
            select(IntroEffectOutbox)
            .where(
                IntroEffectOutbox.application_id == application_id,
                IntroEffectOutbox.effect_kind == "sheet_projection",
            )
            .with_for_update()
        )
        if effect is None:
            session.add(
                IntroEffectOutbox(
                    application_id=application_id,
                    effect_kind="sheet_projection",
                    status="pending",
                )
            )
        elif effect.status in {"sent", "failed", "stale"}:
            effect.status = "pending"
            effect.attempt_count = 0
            effect.attempt_started_at = None
            effect.last_error = None
            effect.completed_at = None
        await session.flush()

    @staticmethod
    async def claim_pending(
        session: AsyncSession,
        limit: int = 10,
        *,
        include_sheet: bool = True,
        high_watermark: int | None = None,
        excluded_ids: set[int] | None = None,
    ) -> list[IntroEffectOutbox]:
        stmt = (
            select(IntroEffectOutbox)
            .where(IntroEffectOutbox.status == "pending")
            .order_by(IntroEffectOutbox.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        if not include_sheet:
            stmt = stmt.where(IntroEffectOutbox.effect_kind != "sheet_projection")
        if high_watermark is not None:
            stmt = stmt.where(IntroEffectOutbox.id <= high_watermark)
        if excluded_ids:
            stmt = stmt.where(IntroEffectOutbox.id.not_in(excluded_ids))
        effects = list((await session.execute(stmt)).scalars())
        now = datetime.now(timezone.utc)
        for effect in effects:
            effect.status = "processing"
            effect.attempt_count += 1
            effect.attempt_started_at = now
            effect.last_error = None
        await session.flush()
        return effects

    @staticmethod
    async def get_claimed_for_update(
        session: AsyncSession, *, effect_id: int, attempt_count: int
    ) -> IntroEffectOutbox | None:
        effect = await session.scalar(
            select(IntroEffectOutbox)
            .where(
                IntroEffectOutbox.id == effect_id,
                IntroEffectOutbox.status == "processing",
                IntroEffectOutbox.attempt_count == attempt_count,
            )
            .with_for_update()
        )
        return effect

    @staticmethod
    async def mark_stale_processing_unknown(session: AsyncSession, *, older_than: datetime) -> int:
        telegram = await session.execute(
            update(IntroEffectOutbox)
            .where(
                IntroEffectOutbox.status == "processing",
                IntroEffectOutbox.attempt_started_at < older_than,
                IntroEffectOutbox.effect_kind != "sheet_projection",
            )
            .values(status="unknown", last_error="processing claim expired")
        )
        sheet = await session.execute(
            update(IntroEffectOutbox)
            .where(
                IntroEffectOutbox.status == "processing",
                IntroEffectOutbox.attempt_started_at < older_than,
                IntroEffectOutbox.effect_kind == "sheet_projection",
            )
            .values(status="pending", last_error="processing claim expired")
        )
        await session.flush()
        return (telegram.rowcount or 0) + (sheet.rowcount or 0)

    @staticmethod
    async def pending_high_watermark(session: AsyncSession) -> int | None:
        return await session.scalar(
            select(IntroEffectOutbox.id).order_by(IntroEffectOutbox.id.desc()).limit(1)
        )
