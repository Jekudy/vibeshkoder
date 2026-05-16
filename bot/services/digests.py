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
from typing import Literal

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
    """Phase 7 / Phase 8 runtime config — separate cost buckets + scheduling
    tunables. Loaded by ``load_digest_config()`` from env vars.

    Phase 7 fields drive the daily digest. Phase 8 ``weekly_*`` fields drive
    the weekly editorial digest (PHASE8_PLAN.md §5.B). The two are
    INDEPENDENT — the weekly cost ceiling is NOT required to be less than
    the daily ceiling (Q7 / §6 — C5 reformulation).
    """

    # Phase 7 (unchanged)
    daily_cost_ceiling_usd: Decimal = Decimal("1.00")
    monthly_cost_ceiling_usd: Decimal = Decimal("10.00")
    source_chat_id: int = 0
    destination_chat_id: int | None = None
    hour_msk: int = 9
    min_cards_threshold: int = 3
    raw_message_top_n: int = 15
    token_budget_input: int = 8000
    # Phase 8 additions — weekly digest tunables. Independent of daily; see
    # PHASE8_PLAN.md §5.B and §6 Q7.
    weekly_cost_ceiling_usd: Decimal = Decimal("5.00")
    weekly_monthly_cost_ceiling_usd: Decimal = Decimal("20.00")
    # L5: weekly min-cards-threshold bumped to 8 (vs daily 3) — weekly window
    # is 7× larger but admin-approved cards over the full week are a
    # higher-quality cohort, so 8 is the empirical middle between daily-3 and
    # linearly-scaled 21.
    weekly_min_cards_threshold: int = 8
    weekly_raw_message_top_n: int = 60
    weekly_token_budget_input: int = 24000

    def to_context_config(self) -> _DigestCtxConfig:
        # FHR HIGH-4 fix: forward weekly tunables so operator env-var overrides
        # (``DIGEST_WEEKLY_TOKEN_BUDGET`` / ``DIGEST_WEEKLY_MIN_CARDS_THRESHOLD``
        # / ``DIGEST_WEEKLY_RAW_MESSAGE_TOP_N``) actually reach
        # ``_weekly_overrides`` in ``digest_context.py``. Before the fix only
        # daily fields were forwarded, so weekly overrides were silently
        # ignored and the dataclass defaults always won.
        return _DigestCtxConfig(
            min_cards_threshold=self.min_cards_threshold,
            raw_message_top_n=self.raw_message_top_n,
            token_budget_input=self.token_budget_input,
            weekly_min_cards_threshold=self.weekly_min_cards_threshold,
            weekly_raw_message_top_n=self.weekly_raw_message_top_n,
            weekly_token_budget_input=self.weekly_token_budget_input,
        )


class ConfigurationError(Exception):
    """Digest configuration invariant violated."""


def load_digest_config() -> DigestConfig:
    src = int(os.environ.get("DIGEST_SOURCE_CHAT_ID", "0"))
    dest_env = os.environ.get("DIGEST_DESTINATION_CHAT_ID")
    dst = int(dest_env) if dest_env else None
    if src and dst is not None and src == dst:
        raise ConfigurationError(
            f"DIGEST_SOURCE_CHAT_ID ({src}) must not equal "
            "DIGEST_DESTINATION_CHAT_ID — digest would post into the same "
            "chat it summarizes (echo loop)."
        )
    return DigestConfig(
        daily_cost_ceiling_usd=Decimal(os.environ.get("DIGEST_DAILY_USD_CEILING", "1.00")),
        monthly_cost_ceiling_usd=Decimal(
            os.environ.get("DIGEST_MONTHLY_USD_CEILING", "10.00")
        ),
        source_chat_id=src,
        destination_chat_id=dst,
        hour_msk=int(os.environ.get("DIGEST_HOUR_MSK", "9")),
        min_cards_threshold=int(os.environ.get("DIGEST_MIN_CARDS_THRESHOLD", "3")),
        raw_message_top_n=int(os.environ.get("DIGEST_RAW_MESSAGE_TOP_N", "15")),
        token_budget_input=int(os.environ.get("DIGEST_TOKEN_BUDGET_INPUT", "8000")),
        # Phase 8 weekly knobs — independent of daily.
        weekly_cost_ceiling_usd=Decimal(
            os.environ.get("DIGEST_WEEKLY_USD_CEILING", "5.00")
        ),
        weekly_monthly_cost_ceiling_usd=Decimal(
            os.environ.get("DIGEST_WEEKLY_MONTHLY_USD_CEILING", "20.00")
        ),
        weekly_token_budget_input=int(
            os.environ.get("DIGEST_WEEKLY_TOKEN_BUDGET", "24000")
        ),
        weekly_min_cards_threshold=int(
            os.environ.get("DIGEST_WEEKLY_MIN_CARDS_THRESHOLD", "8")
        ),
        weekly_raw_message_top_n=int(
            os.environ.get("DIGEST_WEEKLY_RAW_MESSAGE_TOP_N", "60")
        ),
    )


async def _cost_ceiling_breached(
    session: AsyncSession,
    *,
    digest_config: DigestConfig,
    type: Literal["daily", "weekly"] = "daily",
) -> bool:
    """Type-aware separate cost bucket per PHASE8_PLAN.md §5.B (H6).

    SUM(cost_usd) from llm_usage_ledger JOIN digests, filtered by
    ``WHERE d.type = :type`` so daily and weekly costs accumulate to
    INDEPENDENT monthly buckets. Phase 7 callsite uses the default
    ``type='daily'`` (back-compat); Phase 8 weekly callsite passes
    ``type='weekly'``.

    Reads the bucket-specific ceiling out of ``digest_config`` based on
    the ``type`` arg:
      - daily  → ``daily_cost_ceiling_usd`` / ``monthly_cost_ceiling_usd``
      - weekly → ``weekly_cost_ceiling_usd`` / ``weekly_monthly_cost_ceiling_usd``

    Returns True if the daily-bucket OR monthly-bucket ceiling for the
    requested type has been reached.
    """
    if type == "weekly":
        daily_ceiling = digest_config.weekly_cost_ceiling_usd
        monthly_ceiling = digest_config.weekly_monthly_cost_ceiling_usd
    else:
        daily_ceiling = digest_config.daily_cost_ceiling_usd
        monthly_ceiling = digest_config.monthly_cost_ceiling_usd
    sql_daily = text(
        """
        SELECT COALESCE(SUM(l.cost_usd), 0)
        FROM llm_usage_ledger l
        JOIN digests d ON d.llm_usage_ledger_id = l.id
        WHERE d.type = :type
          AND d.created_at >= date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
        """
    )
    daily = (
        await session.execute(sql_daily, {"type": type})
    ).scalar_one_or_none() or Decimal("0")
    if Decimal(str(daily)) >= daily_ceiling:
        return True
    sql_monthly = text(
        """
        SELECT COALESCE(SUM(l.cost_usd), 0)
        FROM llm_usage_ledger l
        JOIN digests d ON d.llm_usage_ledger_id = l.id
        WHERE d.type = :type
          AND d.created_at >= date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
        """
    )
    monthly = (
        await session.execute(sql_monthly, {"type": type})
    ).scalar_one_or_none() or Decimal("0")
    if Decimal(str(monthly)) >= monthly_ceiling:
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
    type: Literal["daily", "weekly"],
    window_start: datetime,
    window_end: datetime,
    ledger_repo: LedgerRepoProtocol,
    provider: LLMProvider,
    config: LLMGatewayConfig,
    digest_config: DigestConfig,
) -> Digest:
    """Orchestrate a digest run. Always returns a Digest row.

    Type widening (Phase 8 / T8-02 — PHASE8_PLAN.md §5.B): now accepts
    ``type='weekly'``. The auto-pipeline terminal state for weekly is
    ``status='draft'``; the transition to ``awaiting_review`` happens in
    ``digest_weekly_job`` after this function returns (T8-04 / T8-05
    territory). This keeps ``run_digest`` type-agnostic in its terminal
    contract.

    Cost ceiling routes via ``_cost_ceiling_breached(type=type)`` (H6
    type filter) so daily and weekly buckets are INDEPENDENT.
    """
    if type not in ("daily", "weekly"):
        raise ValueError(f"unsupported digest type {type!r}; expected 'daily' or 'weekly'")

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
        # Use session.get() so the returned object is session-tracked (identity map).
        # _row_to_digest() returned a detached Digest() whose state mutations would
        # NOT propagate to the DB — publisher state transitions (draft→posting→posted)
        # would silently fail.
        digest = await session.get(Digest, existing)
        if digest is None:
            raise RuntimeError(f"digest {existing} disappeared after FOR UPDATE")
        return digest

    # Step 2 — separate-bucket cost ceiling pre-check. Daily and weekly
    # buckets are INDEPENDENT (PHASE8_PLAN.md §6 Q7 / AC6).
    if await _cost_ceiling_breached(session, digest_config=digest_config, type=type):
        budget_err = f"{type} digest budget exceeded"
        digest = Digest(
            type=type,
            window_start=window_start,
            window_end=window_end,
            body_markdown=None,
            citations=[],
            status="cost_exceeded",
            error_text=budget_err,
        )
        session.add(digest)
        await session.flush()
        run = DigestRun(
            digest_id=digest.id,
            status="cost_exceeded",
            error_text=budget_err,
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

    # Step 5 — build context. Weekly path support lands in T8-03 (parallel
    # sprint); the call passes `type=type` per spec §5.B step 5.
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
        # Use ``exc.__class__.__name__`` rather than ``type(exc).__name__``
        # — ``type`` is a kwarg in this scope and shadows the builtin.
        digest.error_text = f"context_build_failed:{exc.__class__.__name__}"
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

    # Step 7 — synthesize. Type-aware routing into the correct prompt template
    # module happens inside ``synthesize_digest`` (§5.F).
    try:
        result = await synthesize_digest(
            session,
            context=ctx,
            config=config,
            ledger_repo=ledger_repo,
            provider=provider,
            type=type,
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
        # Same ``type`` shadowing — use ``exc.__class__.__name__``.
        digest.error_text = exc.__class__.__name__
        run.status = "failed"
        run.error_text = str(exc)[:2000]
        run.finished_at = datetime.now(timezone.utc)
        await session.flush()
        return digest
    except Exception as exc:
        digest.status = "failed"
        digest.error_text = f"unexpected:{exc.__class__.__name__}"
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


