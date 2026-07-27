from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Application
from bot.services.intro_contract import IntroContractError, get_intro_catalog


class ApplicationRepo:
    @staticmethod
    async def create(
        session: AsyncSession,
        *,
        user_id: int,
        flow_kind: str,
        base_application_id: int | None,
        catalog_version: str,
    ) -> Application:
        if flow_kind not in {"admission", "refresh"}:
            raise ValueError("Application flow kind must be admission or refresh")
        if flow_kind == "admission" and base_application_id is not None:
            raise ValueError("Admission application cannot have a base application")
        try:
            get_intro_catalog(catalog_version)
        except IntroContractError as error:
            raise ValueError("Unknown application catalog version") from error

        app = Application(
            user_id=user_id,
            status="filling",
            flow_kind=flow_kind,
            base_application_id=base_application_id,
            catalog_version=catalog_version,
        )
        session.add(app)
        await session.flush()
        return app

    @staticmethod
    async def get(session: AsyncSession, app_id: int) -> Application | None:
        result = await session.execute(
            select(Application)
            .where(Application.id == app_id)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_active(
        session: AsyncSession, user_id: int, *, include_added: bool = False
    ) -> Application | None:
        """Compatibility lookup for the admission lifecycle only."""
        statuses = ["filling", "confirmed", "pending", "privacy_block", "vouched"]
        lifecycle = and_(
            Application.flow_kind == "admission",
            Application.status.in_(statuses + (["added"] if include_added else [])),
        )
        if include_added:
            lifecycle = or_(
                lifecycle,
                and_(Application.flow_kind.is_(None), Application.status == "added"),
            )
        result = await session.execute(
            select(Application)
            .where(
                Application.user_id == user_id,
                lifecycle,
            )
            .order_by(Application.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_active_refresh(session: AsyncSession, user_id: int) -> Application | None:
        result = await session.execute(
            select(Application)
            .where(
                Application.user_id == user_id,
                Application.flow_kind == "refresh",
                Application.status.in_(("filling", "confirmed")),
            )
            .order_by(Application.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_last_rejected(session: AsyncSession, user_id: int) -> Application | None:
        result = await session.execute(
            select(Application)
            .where(
                Application.user_id == user_id,
                Application.status == "rejected",
            )
            .order_by(Application.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def update_status_if(
        session: AsyncSession,
        app_id: int,
        expected_from: str,
        new_status: str,
        **extra_fields,
    ) -> bool:
        """CAS update — returns True if matched and updated, False if no match."""
        values: dict = {"status": new_status, **extra_fields}
        result = await session.execute(
            update(Application)
            .where(Application.id == app_id, Application.status == expected_from)
            .values(**values)
        )
        await session.flush()
        return result.rowcount > 0

    @staticmethod
    async def get_pending_older_than(session: AsyncSession, hours: int) -> list[Application]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        result = await session.execute(
            select(Application).where(
                Application.status == "pending",
                func.coalesce(Application.submitted_at, Application.created_at) < cutoff,
            )
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_pending_created_older_than(
        session: AsyncSession, hours: int
    ) -> list[Application]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        result = await session.execute(
            select(Application).where(
                Application.status == "pending",
                Application.created_at < cutoff,
            )
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_funnel_stats(session: AsyncSession) -> dict:
        result = await session.execute(
            select(Application.status, func.count()).group_by(Application.status)
        )
        return dict(result.all())
