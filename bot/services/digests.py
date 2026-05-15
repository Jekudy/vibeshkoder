"""Phase 7 / T7-02 — daily-digest orchestrator.

``run_digest`` is the single entry point for producing a digest row for a
given (type, window_start, window_end) tuple. The scheduler hook (T7-04)
and admin handler ``/digest_now`` (T7-06) both call into here.

ALWAYS returns a `Digest` row — including for cost_exceeded / skipped /
failed states (no None return path). The caller inspects ``digest.status``
to decide whether to publish.

Privacy invariants enforced upstream and re-checked here:
- I-2: no LLM calls outside ``llm_gateway`` — synthesis routes via
  ``llm_gateway.synthesize_digest``, never direct provider imports.
- I-3: governance filter applied by ``digest_context.build_digest_context``
  AND re-validated inside the gateway before provider dispatch.
- I-4: citations are stored as JSONB id-arrays; never raw message text.
- I-5: digest is derived prose, not canonical truth.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Digest, DigestRun
from bot.services.digest_context import DigestConfig as _DigestCtxConfig
from bot.services.digest_context import build_digest_context
from bot.services.llm_gateway import (
    DigestCitationValidationError,
    DigestContextStaleError,
    DigestEmptyWindowError,
    DigestProviderError,
    LLMBudgetExceededError,
    LLMGatewayConfig,
    LedgerRepoProtocol,
    synthesize_digest,
)
from bot.services.llm_providers import LLMProvider

logger = logging.getLogger(__name__)

DIGEST_LOCK_NAMESPACE = "phase7:digest_idempotency"


@dataclass(frozen=True)
class DigestConfig:
    """Phase 7 runtime config — separate cost bucket + scheduling tunables.

    Loaded by ``load_digest_config()`` from env vars per PHASE7_PLAN.md §12.
    """

    daily_cost_ceiling_usd: Decimal = Decimal("1.00")
    monthly_cost_ceiling_usd: Decimal = Decimal("10.00")
    source_chat_id: int = 0
    destination_chat_id: int | None = None
    hour_msk: int = 9
    min_cards_threshold: int = 3
    raw_message_top_n: int = 15
    token_budget_input: int = 8000

    def to_context_config(self) -> _DigestCtxConfig:
        return _DigestCtxConfig(
            min_cards_threshold=self.min_cards_threshold,
            raw_message_top_n=self.raw_message_top_n,
            token_budget_input=self.token_budget_input,
        )


def load_digest_config() -> DigestConfig:
    dest_env = os.environ.get("DIGEST_DESTINATION_CHAT_ID")
    return DigestConfig(
        daily_cost_ceiling_usd=Decimal(os.environ.get("DIGEST_DAILY_USD_CEILING", "1.00")),
        monthly_cost_ceiling_usd=Decimal(
            os.environ.get("DIGEST_MONTHLY_USD_CEILING", "10.00")
        ),
        source_chat_id=int(os.environ.get("DIGEST_SOURCE_CHAT_ID", "0")),
        destination_chat_id=int(dest_env) if dest_env else None,
        hour_msk=int(os.environ.get("DIGEST_HOUR_MSK", "9")),
        min_cards_threshold=int(os.environ.get("DIGEST_MIN_CARDS_THRESHOLD", "3")),
        raw_message_top_n=int(os.environ.get("DIGEST_RAW_MESSAGE_TOP_N", "15")),
        token_budget_input=int(os.environ.get("DIGEST_TOKEN_BUDGET_INPUT", "8000")),
    )


async def _cost_ceiling_breached(
    session: AsyncSession, *, digest_config: DigestConfig
) -> bool:
    """Phase 7 separate cost bucket: SUM(cost_usd) from llm_usage_ledger
    JOIN digests WHERE digests.created_at >= today_00_utc.

    Returns True if daily or monthly ceiling reached.
    """
    sql_daily = text(
        """
        SELECT COALESCE(SUM(l.cost_usd), 0)
        FROM llm_usage_ledger l
        JOIN digests d ON d.llm_usage_ledger_id = l.id
        WHERE d.created_at >= date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
        """
    )
    daily = (await session.execute(sql_daily)).scalar_one_or_none() or Decimal("0")
    if Decimal(str(daily)) >= digest_config.daily_cost_ceiling_usd:
        return True
    sql_monthly = text(
        """
        SELECT COALESCE(SUM(l.cost_usd), 0)
        FROM llm_usage_ledger l
        JOIN digests d ON d.llm_usage_ledger_id = l.id
        WHERE d.created_at >= date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
        """
    )
    monthly = (await session.execute(sql_monthly)).scalar_one_or_none() or Decimal("0")
    if Decimal(str(monthly)) >= digest_config.monthly_cost_ceiling_usd:
        return True
    return False


async def _acquire_idempotency_lock(
    session: AsyncSession,
    *,
    type: str,
    window_start: datetime,
    window_end: datetime,
) -> None:
    """Acquire pg_advisory_xact_lock for (type, ws, we). Released at COMMIT.

    Lock key uses UTC ISO 8601 canonicalization to avoid timezone-sensitive
    string divergence between concurrent callers.
    """
    sql = text(
        """
        SELECT pg_advisory_xact_lock(hashtextextended(
            :type
            || '|'
            || to_char(:ws AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
            || '|'
            || to_char(:we AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
            0
        ))
        """
    )
    await session.execute(sql, {"type": type, "ws": window_start, "we": window_end})


async def run_digest(
    session: AsyncSession,
    *,
    type: Literal["daily"],
    window_start: datetime,
    window_end: datetime,
    ledger_repo: LedgerRepoProtocol,
    provider: LLMProvider,
    config: LLMGatewayConfig,
    digest_config: DigestConfig,
) -> Digest:
    """Orchestrate a digest run. Always returns a Digest row."""
    if type != "daily":
        raise ValueError(f"Phase 7 only supports type='daily', got {type!r}")

    # Step 1 — race-safe idempotency.
    await _acquire_idempotency_lock(
        session, type=type, window_start=window_start, window_end=window_end
    )
    existing_sql = text(
        """
        SELECT id FROM digests
        WHERE type = :type AND window_start = :ws AND window_end = :we
        FOR UPDATE
        """
    )
    existing = (
        await session.execute(
            existing_sql,
            {"type": type, "ws": window_start, "we": window_end},
        )
    ).scalar_one_or_none()
    if existing is not None:
        result = await session.execute(
            text("SELECT * FROM digests WHERE id = :id"), {"id": existing}
        )
        row = result.mappings().one()
        return _row_to_digest(row)

    # Step 2 — Phase 7 separate-bucket cost ceiling pre-check.
    if await _cost_ceiling_breached(session, digest_config=digest_config):
        digest = Digest(
            type=type,
            window_start=window_start,
            window_end=window_end,
            body_markdown=None,
            citations=[],
            status="cost_exceeded",
            error_text="daily digest budget exceeded",
        )
        session.add(digest)
        await session.flush()
        run = DigestRun(
            digest_id=digest.id,
            status="cost_exceeded",
            error_text="daily digest budget exceeded",
            finished_at=datetime.now(timezone.utc),
        )
        session.add(run)
        await session.flush()
        return digest

    # Step 3-4 — open digest_runs + digests in 'running'.
    digest = Digest(
        type=type,
        window_start=window_start,
        window_end=window_end,
        body_markdown=None,
        citations=[],
        status="running",
    )
    session.add(digest)
    await session.flush()
    run = DigestRun(digest_id=digest.id, status="running")
    session.add(run)
    await session.flush()

    # Step 5 — build context.
    try:
        ctx = await build_digest_context(
            session,
            type=type,
            window_start=window_start,
            window_end=window_end,
            source_chat_id=digest_config.source_chat_id,
            digest_config=digest_config.to_context_config(),
        )
    except Exception as exc:
        digest.status = "failed"
        digest.error_text = f"context_build_failed:{type(exc).__name__}"
        run.status = "failed"
        run.error_text = str(exc)[:2000]
        run.finished_at = datetime.now(timezone.utc)
        await session.flush()
        return digest

    # Step 6 — empty window short-circuit.
    if not ctx.cards and not ctx.messages:
        digest.status = "skipped"
        run.status = "skipped"
        run.finished_at = datetime.now(timezone.utc)
        await session.flush()
        return digest

    # Step 7 — synthesize.
    try:
        result = await synthesize_digest(
            session,
            context=ctx,
            config=config,
            ledger_repo=ledger_repo,
            provider=provider,
        )
    except DigestEmptyWindowError:
        digest.status = "skipped"
        run.status = "skipped"
        run.finished_at = datetime.now(timezone.utc)
        await session.flush()
        return digest
    except DigestContextStaleError as exc:
        digest.status = "failed"
        digest.error_text = "context_stale_post_forget_race"
        run.status = "failed"
        run.error_text = str(exc)[:2000]
        run.finished_at = datetime.now(timezone.utc)
        await session.flush()
        return digest
    except LLMBudgetExceededError as exc:
        digest.status = "cost_exceeded"
        digest.error_text = "gateway_shared_budget_exceeded"
        run.status = "cost_exceeded"
        run.error_text = str(exc)[:2000]
        run.finished_at = datetime.now(timezone.utc)
        await session.flush()
        return digest
    except (DigestProviderError, DigestCitationValidationError) as exc:
        digest.status = "failed"
        digest.error_text = type(exc).__name__
        run.status = "failed"
        run.error_text = str(exc)[:2000]
        run.finished_at = datetime.now(timezone.utc)
        await session.flush()
        return digest
    except Exception as exc:
        digest.status = "failed"
        digest.error_text = f"unexpected:{type(exc).__name__}"
        run.status = "failed"
        run.error_text = str(exc)[:2000]
        run.finished_at = datetime.now(timezone.utc)
        await session.flush()
        return digest

    # Step 8 — success.
    digest.body_markdown = result.body_markdown
    digest.citations = result.citations
    digest.llm_usage_ledger_id = result.llm_usage_ledger_id
    digest.status = "draft"
    run.status = "finished"
    run.finished_at = datetime.now(timezone.utc)
    await session.flush()
    return digest


def _row_to_digest(row: Any) -> Digest:
    """Hydrate an in-flight Digest ORM object from a mappings row."""
    return Digest(
        id=row["id"],
        type=row["type"],
        window_start=row["window_start"],
        window_end=row["window_end"],
        body_markdown=row["body_markdown"],
        citations=row["citations"],
        status=row["status"],
        llm_usage_ledger_id=row.get("llm_usage_ledger_id"),
        posted_chat_id=row.get("posted_chat_id"),
        posted_message_id=row.get("posted_message_id"),
        posted_at=row.get("posted_at"),
        posting_started_at=row.get("posting_started_at"),
        error_text=row.get("error_text"),
    )
