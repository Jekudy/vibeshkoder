"""Butler service state machine — T12-04 (Wave 2 Stream State).

``ButlerService`` is the single source of truth for all butler action
lifecycle transitions:

  plan_action → confirm_action → execute_action
                                  ↑
  cancel_action   expire_action ──┘

Design rationale
----------------
* **Orchestrator only**: ButlerService calls repos + llm_gateway. It does NOT
  issue raw SQL, call LLM providers directly, or import anthropic/openai.
* **FOR UPDATE on butler_actions row**: every mutating transition (confirm, cancel,
  expire) acquires a SELECT ... FOR UPDATE on the action row before any side effect.
  This coordinates with ``_cascade_butler_actions`` which holds the same row lock
  inside the cascade transaction — if cascade is mid-flight, ``get_for_update``
  returns None (NOWAIT semantics in fake; real DB raises lock_not_available which
  the repo wraps) and we raise ``CascadeInFlightError`` (fail-closed). Pattern from
  Phase 9 ``bot/handlers/wiki_publish.py /wiki_publish`` advisory lock.
* **Audit row always written**: every call to plan_action writes a
  ``butler_actions`` row regardless of success/failure. Failed plans write
  status='rejected'. This satisfies the binding invariant that every /butler
  invocation is auditable.
* **Constraint #9 (NULL ledger)**: NULL ``llm_usage_ledger_id`` is allowed only for
  status IN ('rejected','expired','cancelled'). If the LLM call failed before a
  ledger row was written (pre-plan errors like membership_revoked that happen before
  the provider call), the rejected row has NULL ledger — correct per CHECK.
* **Cross-user consent**: affected_user_ids from each ButlerActionStep each get a
  ``butler_action_confirmations`` row. All must reach status='confirmed' before the
  action transitions to 'confirmed' (DB enum value).
* **State enum alignment** (C5): service uses DB-level enum values.
  - All-confirmed state: 'confirmed' (not 'pending_execution')
  - Tool failure state: 'execution_failed' (not 'failed')
  These align with the ck_butler_actions_status CHECK constraint in migration 070.
* **confirmation_token** (C1): each confirmation row gets a fresh
  ``secrets.token_urlsafe(32)`` token stored in the DB. confirm_action verifies
  the presented token against the stored value before accepting the confirmation.
* **query + visibility_scope + plan_payload** (C2/C3): stored on the butler_actions
  row at plan time. confirm_action reads them from the row (not caller-supplied
  defaults) for evidence re-revalidation. execute_action reads plan_payload to
  build the multi-step execution plan.
* **execute_action re-checks confirmations** (C6): after acquiring FOR UPDATE on the
  action row, execute_action re-reads all affected_user confirmation rows to verify
  none were revoked by a concurrent cascade between confirm_action and execute_action.
* **Membership pre-check** (H1): plan_action checks membership via user_repo BEFORE
  evidence build or LLM call, failing fast with status='rejected'.
* **cancel_action uses FOR UPDATE** (H2): coordinates with cascade just like
  confirm_action.
* **Rate bucket completeness** (H3): plan_action checks and increments per-tool-hour
  and chat_actions_day buckets in addition to user_plans_day. execute_action
  increments user_execs_day.
* **Admin verification via user_repo** (M2): cancel_action verifies admin status
  by looking up the cancelling user via user_repo rather than accepting is_admin
  flag from caller.

Hard Constraints (charter §"Hard Constraints")
-----------------------------------------------
#1  NO LLM calls outside llm_gateway.py — no anthropic/openai imports here
#2  No raw DB access outside repos (exception: SELECT FOR UPDATE on butler_actions
    uses get_for_update on the repo which wraps the lock)
#5  Cross-user consent unbypassable
#6  No money/calendar/email/browser/shell
#9  NULL llm_usage_ledger_id only for status IN ('rejected','expired','cancelled')
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from sqlalchemy.exc import OperationalError as _SAOperationalError

# butler_tools does NOT import from butler — no circular import risk.
# Must be module-level (not lazy-imported inside except blocks) so that
# isinstance(exc, ButlerPlanError) uses the same class object as the exc
# was instantiated with — lazy re-import after _clear_modules() in tests
# would create a different class object and isinstance would return False.
from bot.services.butler_tools import ButlerPlanError  # noqa: E402

logger = logging.getLogger(__name__)

# Plan TTL per charter §"Cost / rate / TTL envelopes"
_PLAN_TTL_SECONDS = 900  # 15 min default (low-risk)
_CONFIRMATION_TTL_SECONDS = 300  # 5 min (inline-keyboard freshness)

# Rate bucket defaults (§14.2 of PHASE12_PLAN_REFRESH.md)
_USER_PLANS_DAY_CEILING = 10
_USER_EXECS_DAY_CEILING = 5
_CHAT_ACTIONS_DAY_CEILING = 50
_TOOL_HOUR_CEILING = 20  # per-tool per-hour default

# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class ButlerActionError(Exception):
    """Base exception for Butler action state machine errors.

    Every subclass carries ``error_kind`` (string tag used in audit + tests)
    and optionally ``action_id`` (set after the audit row is written).
    """

    def __init__(
        self,
        message: str,
        *,
        error_kind: str | None = None,
        action_id: int | None = None,
    ) -> None:
        super().__init__(message)
        self.error_kind = error_kind
        self.action_id = action_id


class ButlerActionExpiredError(ButlerActionError):
    """Raised when a confirm/execute attempt is made on an expired action."""


class ButlerActionRejectedError(ButlerActionError):
    """Raised when an action is rejected (e.g. forbidden cancel)."""


class EvidenceStaleError(ButlerActionError):
    """Raised when pre-execute evidence revalidation detects a stale snapshot."""


class CascadeInFlightError(ButlerActionError):
    """Raised when the cascade holds the FOR UPDATE lock on the action row."""


class MembershipRevokedError(ButlerActionError):
    """Raised when the requester is no longer a member at plan time."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _args_hash(args: dict) -> str:
    """Deterministic SHA-256 hash of an args dict."""
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _payload_hash(payload: dict | None) -> str | None:
    if payload is None:
        return None
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _idempotency_key(action_id: int, tool_name: str, seq: int) -> str:
    return f"butler:{action_id}:{tool_name}:{seq}"


def _msk_day_bucket_key(dt: datetime) -> str:
    """Convert UTC datetime to MSK-calendar day bucket key (YYYY-MM-DD in MSK)."""
    # MSK = UTC+3
    msk_dt = dt + timedelta(hours=3)
    return f"day:{msk_dt.strftime('%Y-%m-%d')}"


def _msk_day_window(dt: datetime) -> tuple[datetime, datetime]:
    """Return (window_start, window_end) for the MSK calendar day containing dt."""
    msk_dt = dt + timedelta(hours=3)
    msk_start = msk_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    msk_end = msk_start + timedelta(days=1)
    # Convert back to UTC
    utc_start = msk_start - timedelta(hours=3)
    utc_end = msk_end - timedelta(hours=3)
    return utc_start, utc_end


def _msk_hour_bucket_key(dt: datetime) -> str:
    """Convert UTC datetime to MSK-calendar hour bucket key (YYYY-MM-DD-HH in MSK)."""
    msk_dt = dt + timedelta(hours=3)
    return f"hour:{msk_dt.strftime('%Y-%m-%d-%H')}"


def _msk_hour_window(dt: datetime) -> tuple[datetime, datetime]:
    """Return (window_start, window_end) for the MSK calendar hour containing dt."""
    msk_dt = dt + timedelta(hours=3)
    msk_start = msk_dt.replace(minute=0, second=0, microsecond=0)
    msk_end = msk_start + timedelta(hours=1)
    utc_start = msk_start - timedelta(hours=3)
    utc_end = msk_end - timedelta(hours=3)
    return utc_start, utc_end


# ---------------------------------------------------------------------------
# ButlerService
# ---------------------------------------------------------------------------


class ButlerService:
    """State machine orchestrator for butler actions.

    All five transition methods are the public API. Handlers (T12-05) call
    these methods; they must NOT bypass via direct repo access.

    Wiring (constructor injection):
    - session: AsyncSession — DB session owned by the handler
    - ledger_repo: LedgerRepoProtocol — from bot/db/repos/llm_usage_ledger.py
    - butler_action_repo: ButlerActionRepo
    - butler_action_confirmation_repo: ButlerActionConfirmationRepo
    - butler_tool_invocation_repo: ButlerToolInvocationRepo
    - butler_rate_bucket_repo: ButlerRateBucketRepo
    - user_repo: UserRepoProtocol — get(session, user_id) → User | None (H1/M2)
    - llm_gateway: object with plan_butler_action(**kwargs) → (ButlerPlan, int, Decimal)
    - evidence_builder: object with build_butler_evidence(**kwargs) → ButlerEvidenceContext
    - settings: object with butler_plan_ttl_seconds + butler_confirmation_ttl_seconds
    """

    def __init__(
        self,
        *,
        session: Any,
        ledger_repo: Any,
        butler_action_repo: Any,
        butler_action_confirmation_repo: Any,
        butler_tool_invocation_repo: Any,
        butler_rate_bucket_repo: Any,
        user_repo: Any = None,
        llm_gateway: Any,
        evidence_builder: Any,
        settings: Any,
    ) -> None:
        self._session = session
        self._ledger_repo = ledger_repo
        self._action_repo = butler_action_repo
        self._confirmation_repo = butler_action_confirmation_repo
        self._invocation_repo = butler_tool_invocation_repo
        self._rate_bucket_repo = butler_rate_bucket_repo
        # user_repo may be None in tests that don't exercise H1/M2 paths.
        # When None, membership check is skipped (only safe in test doubles).
        self._user_repo = user_repo
        self._gateway = llm_gateway
        self._evidence_builder = evidence_builder
        self._settings = settings

    # -----------------------------------------------------------------------
    # plan_action
    # -----------------------------------------------------------------------

    async def plan_action(
        self,
        *,
        requester_user_id: int,
        chat_id: int | None,
        query: str,
        visibility_scope: Literal["member", "admin", "self"],
    ) -> Any:  # returns ButlerAction ORM row (or fake in tests)
        """Plan a butler action.

        Steps
        -----
        0. Membership pre-check (H1): fail fast before spending budget.
           Writes rejected row + raises MembershipRevokedError on non-member.
        1. Rate-check: user_plans_day bucket + chat_actions_day (pre-LLM, no ledger yet).
           On fail → INSERT rejected row + raise ButlerActionError(error_kind='rate_limit_exceeded').
        2. Build ButlerEvidenceContext via evidence_builder.
        3. Call llm_gateway.plan_butler_action → (plan, ledger_id, cost).
           On ButlerPlanError → INSERT rejected row + re-raise as ButlerActionError.
        4. INSERT butler_actions row (status='pending_confirmation', query/visibility_scope/
           plan_payload stored, llm_usage_ledger_id set).
        5. INSERT butler_action_confirmations for requester + every affected_user_id in the plan.
           Each confirmation row gets a fresh secrets.token_urlsafe(32) token (C1).
        6. Return the butler_actions row.

        All 8 rejection paths write a rejected row so every /butler invocation is auditable.
        """
        now = _now_utc()
        effective_chat_id = chat_id if chat_id is not None else 0

        # Step 0 — membership pre-check (H1).
        # Skip if user_repo not wired (test doubles that don't exercise this path).
        if self._user_repo is not None:
            user = await self._user_repo.get(self._session, requester_user_id)
            is_member = user is not None and (
                getattr(user, "is_member", False) or getattr(user, "is_admin", False)
            )
            if not is_member:
                # Write rejected audit row (NULL ledger — pre-plan failure, per constraint #9)
                action_row = await self._action_repo.create(
                    self._session,
                    requester_tg_id=requester_user_id,
                    chat_id=effective_chat_id,
                    action_type="recall",
                    status="rejected",
                    tool_name="recall_evidence",
                    tool_manifest_version="v1.0.0",
                    governance_filter_version="",
                    evidence_context_hash="",
                    plan_summary="",
                    action_args={},
                    action_args_hash="",
                    rollback_kind="not_reversible",
                    risk_level="low",
                    rejection_reason="membership_revoked",
                    llm_usage_ledger_id=None,
                    query=query,
                    visibility_scope=visibility_scope,
                    plan_payload={},
                )
                raise MembershipRevokedError(
                    f"user {requester_user_id} is not a member",
                    error_kind="membership_revoked",
                    action_id=action_row.id,
                )

        # Step 1 — rate-checks (pre-LLM, no ledger yet).
        # H1: track every successfully-incremented bucket so we can roll back if a
        # later rate-check in this same call fails.  Each entry is a dict of kwargs
        # for ButlerRateBucketRepo.decrement.
        _incremented_buckets: list[dict] = []

        bucket_key = _msk_day_bucket_key(now)
        win_start, win_end = _msk_day_window(now)

        # user_plans_day
        rate_ok = await self._rate_bucket_repo.try_increment(
            self._session,
            bucket_kind="user_plans_day",
            scope_id=requester_user_id,
            bucket_key=bucket_key,
            window_start=win_start,
            window_end=win_end,
            ceiling=getattr(self._settings, "user_plans_day_ceiling", _USER_PLANS_DAY_CEILING),
        )
        if not rate_ok:
            action_row = await self._action_repo.create(
                self._session,
                requester_tg_id=requester_user_id,
                chat_id=effective_chat_id,
                action_type="recall",
                status="rejected",
                tool_name="recall_evidence",
                tool_manifest_version="v1.0.0",
                governance_filter_version="",
                evidence_context_hash="",
                plan_summary="",
                action_args={},
                action_args_hash="",
                rollback_kind="not_reversible",
                risk_level="low",
                rejection_reason="rate_limit_exceeded",
                llm_usage_ledger_id=None,
                query=query,
                visibility_scope=visibility_scope,
                plan_payload={},
            )
            raise ButlerActionError(
                f"rate limit exceeded for user {requester_user_id}",
                error_kind="rate_limit_exceeded",
                action_id=action_row.id,
            )
        _incremented_buckets.append(
            {"bucket_kind": "user_plans_day", "scope_id": requester_user_id, "bucket_key": bucket_key}
        )

        # chat_actions_day (H3)
        chat_rate_ok = await self._rate_bucket_repo.try_increment(
            self._session,
            bucket_kind="chat_actions_day",
            scope_id=effective_chat_id,
            bucket_key=bucket_key,
            window_start=win_start,
            window_end=win_end,
            ceiling=getattr(self._settings, "chat_actions_day_ceiling", _CHAT_ACTIONS_DAY_CEILING),
        )
        if not chat_rate_ok:
            action_row = await self._action_repo.create(
                self._session,
                requester_tg_id=requester_user_id,
                chat_id=effective_chat_id,
                action_type="recall",
                status="rejected",
                tool_name="recall_evidence",
                tool_manifest_version="v1.0.0",
                governance_filter_version="",
                evidence_context_hash="",
                plan_summary="",
                action_args={},
                action_args_hash="",
                rollback_kind="not_reversible",
                risk_level="low",
                rejection_reason="rate_limit_exceeded",
                llm_usage_ledger_id=None,
                query=query,
                visibility_scope=visibility_scope,
                plan_payload={},
            )
            # H1: roll back prior increments (user_plans_day already consumed).
            for _bucket in _incremented_buckets:
                await self._rate_bucket_repo.decrement(self._session, **_bucket)
            raise ButlerActionError(
                f"chat rate limit exceeded for chat {effective_chat_id}",
                error_kind="rate_limit_exceeded",
                action_id=action_row.id,
            )
        _incremented_buckets.append(
            {"bucket_kind": "chat_actions_day", "scope_id": effective_chat_id, "bucket_key": bucket_key}
        )

        # Step 2 — build evidence context
        evidence_context = await self._evidence_builder.build_butler_evidence(
            session=self._session,
            requester_user_id=requester_user_id,
            query=query,
            chat_id=chat_id,
            visibility_scope=visibility_scope,
        )

        # Step 3 — call LLM gateway
        plan = None
        ledger_id = None
        try:
            plan, ledger_id, _cost = await self._gateway.plan_butler_action(
                session=self._session,
                requester_user_id=requester_user_id,
                chat_id=chat_id,
                query=query,
                evidence_context=evidence_context,
                visibility_scope=visibility_scope,
                config=getattr(self._settings, "llm_config", None),
                ledger_repo=self._ledger_repo,
                provider=getattr(self._settings, "llm_provider", None),
            )
        except Exception as exc:
            # Map any ButlerPlanError to a ButlerActionError + write rejected row
            if isinstance(exc, ButlerPlanError):
                error_kind = exc.error_kind or "plan_error"
                plan_ledger_id = exc.llm_usage_ledger_id
            else:
                error_kind = "plan_error"
                plan_ledger_id = None

            action_row = await self._action_repo.create(
                self._session,
                requester_tg_id=requester_user_id,
                chat_id=effective_chat_id,
                action_type="recall",
                status="rejected",
                tool_name="recall_evidence",
                tool_manifest_version="v1.0.0",
                governance_filter_version=evidence_context.governance_filter_version,
                evidence_context_hash=evidence_context.context_hash,
                plan_summary="",
                action_args={},
                action_args_hash="",
                rollback_kind="not_reversible",
                risk_level="low",
                rejection_reason=error_kind,
                llm_usage_ledger_id=plan_ledger_id,
                query=query,
                visibility_scope=visibility_scope,
                plan_payload={},
            )
            raise ButlerActionError(
                str(exc),
                error_kind=error_kind,
                action_id=action_row.id,
            ) from exc

        # Step 4 — derive metadata from the plan
        first_action = plan.actions[0] if plan.actions else None
        action_type = _tool_name_to_action_type(
            first_action.tool_name if first_action else "recall_evidence"
        )
        tool_name = first_action.tool_name if first_action else "recall_evidence"
        rollback_kind = first_action.rollback_kind if first_action else "not_reversible"
        risk_level = first_action.risk_level if first_action else "low"
        action_args = first_action.args if first_action else {}

        ttl_secs = getattr(self._settings, "butler_plan_ttl_seconds", _PLAN_TTL_SECONDS)
        expires_at = now + timedelta(seconds=ttl_secs)

        # Collect all affected_user_ids across all action steps
        affected_user_ids: set[int] = set()
        for step in plan.actions:
            for uid in step.affected_user_ids:
                if uid != requester_user_id:
                    affected_user_ids.add(uid)

        # Per-tool-hour rate check for the primary tool (H3).
        # Only check the first/primary tool; multi-tool plans are deferred to T12-06+.
        tool_hour_key = _msk_hour_bucket_key(now)
        th_win_start, th_win_end = _msk_hour_window(now)
        tool_rate_ok = await self._rate_bucket_repo.try_increment(
            self._session,
            bucket_kind=f"tool_hour:{tool_name}",
            scope_id=requester_user_id,
            bucket_key=tool_hour_key,
            window_start=th_win_start,
            window_end=th_win_end,
            ceiling=getattr(self._settings, "tool_hour_ceiling", _TOOL_HOUR_CEILING),
        )
        if not tool_rate_ok:
            # H1: roll back prior increments so the user's daily counters are not
            # permanently consumed when the per-tool ceiling blocks this request.
            for _bucket in _incremented_buckets:
                await self._rate_bucket_repo.decrement(self._session, **_bucket)

            action_row = await self._action_repo.create(
                self._session,
                requester_tg_id=requester_user_id,
                chat_id=effective_chat_id,
                action_type=action_type,
                status="rejected",
                tool_name=tool_name,
                tool_manifest_version=getattr(plan, "tool_manifest_version", "v1.0.0"),
                governance_filter_version=plan.governance_filter_version,
                evidence_context_hash=evidence_context.context_hash,
                plan_summary="",
                action_args=action_args,
                action_args_hash=_args_hash(action_args),
                rollback_kind=rollback_kind,
                risk_level=risk_level,
                rejection_reason="rate_limit_exceeded",
                llm_usage_ledger_id=ledger_id,
                query=query,
                visibility_scope=visibility_scope,
                plan_payload=plan.model_dump() if hasattr(plan, "model_dump") else {},
            )
            raise ButlerActionError(
                f"tool hour rate limit exceeded for tool {tool_name}",
                error_kind="rate_limit_exceeded",
                action_id=action_row.id,
            )

        # Serialize the full plan for multi-step execute-time replay (C3)
        plan_payload_data: dict
        if hasattr(plan, "model_dump"):
            plan_payload_data = plan.model_dump()
        elif hasattr(plan, "__dict__"):
            plan_payload_data = dict(vars(plan))
        else:
            plan_payload_data = {}

        action_row = await self._action_repo.create(
            self._session,
            requester_tg_id=requester_user_id,
            chat_id=effective_chat_id,
            action_type=action_type,
            status="pending_confirmation",
            tool_name=tool_name,
            tool_manifest_version=getattr(plan, "tool_manifest_version", "v1.0.0"),
            governance_filter_version=plan.governance_filter_version,
            evidence_context_hash=evidence_context.context_hash,
            evidence_ids=list(plan.evidence_ids),
            plan_summary=plan.plan_summary,
            action_args=action_args,
            action_args_hash=_args_hash(action_args),
            rollback_kind=rollback_kind,
            risk_level=risk_level,
            requires_confirmation=True,
            confirmation_policy="per_action",
            expires_at=expires_at,
            llm_usage_ledger_id=ledger_id,
            # C2/C3: store query, visibility_scope, plan_payload on the row
            query=query,
            visibility_scope=visibility_scope,
            plan_payload=plan_payload_data,
        )

        # Step 5 — INSERT confirmation rows (C1: fresh token per row)
        confirmation_ttl = getattr(
            self._settings, "butler_confirmation_ttl_seconds", _CONFIRMATION_TTL_SECONDS
        )
        conf_expires_at = now + timedelta(seconds=confirmation_ttl)
        preview_hash = _payload_hash({"plan_summary": plan.plan_summary}) or ""

        # Requester confirmation — fresh token (C1)
        await self._confirmation_repo.create(
            self._session,
            action_id=action_row.id,
            confirmer_tg_id=requester_user_id,
            confirmation_role="requester",
            status="pending",
            preview_payload_hash=preview_hash,
            expires_at=conf_expires_at,
            confirmation_token=secrets.token_urlsafe(32),
        )

        # Affected user confirmations (cross-user consent — Hard Constraint #5)
        for uid in sorted(affected_user_ids):
            await self._confirmation_repo.create(
                self._session,
                action_id=action_row.id,
                confirmer_tg_id=uid,
                confirmation_role="affected_user",
                status="pending",
                preview_payload_hash=preview_hash,
                expires_at=conf_expires_at,
                confirmation_token=secrets.token_urlsafe(32),
            )

        logger.info(
            "butler: plan_action created action_id=%s status=%s requester=%s",
            action_row.id,
            action_row.status,
            requester_user_id,
        )
        return action_row

    # -----------------------------------------------------------------------
    # confirm_action
    # -----------------------------------------------------------------------

    async def confirm_action(
        self,
        *,
        action_id: int,
        confirming_user_id: int,
        confirmation_token: str,
    ) -> Any:
        """Confirm a pending butler action (inline keyboard callback).

        Transition: pending_confirmation → 'confirmed' (all confirmed)
                    OR pending_confirmation (partial — not all confirmed yet)

        Steps
        -----
        1. get_for_update on butler_actions (coordinates with cascade via FOR UPDATE /
           NOWAIT semantics — cascade holds same lock; NOWAIT raises immediately on
           contention → CascadeInFlightError).
        2. Check action.status == 'pending_confirmation' + not expired.
        3. Find the confirmation row for (action_id, confirming_user_id).
        4. Validate token + not-expired + not-already-confirmed.
        5. Mark confirmation as 'confirmed'.
        6. If ALL confirmations for this action are confirmed:
           a. Revalidate evidence context hash using stored query + visibility_scope (C2).
           b. On mismatch → mark action rejected, raise EvidenceStaleError.
           c. Mark action status='confirmed' (C5 — matches DB CHECK enum).
        7. Return action row.
        """
        # Step 1 — acquire FOR UPDATE (cascade-vs-callback race protection, C4)
        # Real DB: SELECT FOR UPDATE NOWAIT raises OperationalError (LockNotAvailable)
        # if cascade holds the lock. Fake (tests): returns None when locked_ids contains it.
        # Both paths → CascadeInFlightError (fail-closed).
        try:
            action = await self._action_repo.get_for_update(self._session, action_id)
        except _SAOperationalError as exc:
            raise CascadeInFlightError(
                f"butler_actions(id={action_id}) locked by cascade (NOWAIT) — try again",
                error_kind="cascade_in_flight",
                action_id=action_id,
            ) from exc
        if action is None:
            raise CascadeInFlightError(
                f"butler_actions(id={action_id}) locked by cascade — try again",
                error_kind="cascade_in_flight",
                action_id=action_id,
            )

        # Check if cascade already redacted this action (C4 — post-cascade path)
        if action.status in ("expired", "cancelled"):
            if getattr(action, "rejection_reason", None) == "source_forgotten":
                raise ButlerActionError(
                    f"action_id={action_id} was redacted by forget cascade",
                    error_kind="source_forgotten",
                    action_id=action_id,
                )

        # Step 1b — idempotency: check confirmation row BEFORE action status check
        # so re-presenting an already-confirmed token gives already_confirmed_by_user,
        # not wrong_status (the action may have moved to 'confirmed' already).
        early_conf = await self._confirmation_repo.get_for_action_user(
            self._session, action_id, confirming_user_id
        )
        if early_conf is not None and early_conf.status == "confirmed":
            raise ButlerActionError(
                f"user {confirming_user_id} already confirmed action_id={action_id}",
                error_kind="already_confirmed_by_user",
                action_id=action_id,
            )

        # Step 2 — check status and expiry
        if action.status not in ("pending_confirmation",):
            if action.status == "expired":
                raise ButlerActionExpiredError(
                    f"action_id={action_id} has status='expired'",
                    error_kind="expired",
                    action_id=action_id,
                )
            raise ButlerActionError(
                f"action_id={action_id} is not in pending_confirmation (got {action.status!r})",
                error_kind="wrong_status",
                action_id=action_id,
            )

        now = _now_utc()
        if action.expires_at is not None and action.expires_at <= now:
            await self._action_repo.update_status(
                self._session,
                action_id,
                status="expired",
                rejection_reason="ttl_expired",
            )
            raise ButlerActionExpiredError(
                f"action_id={action_id} TTL expired",
                error_kind="expired",
                action_id=action_id,
            )

        # Step 3 — find confirmation row
        confirmation = await self._confirmation_repo.get_for_action_user(
            self._session, action_id, confirming_user_id
        )
        if confirmation is None:
            raise ButlerActionError(
                f"no confirmation row for action_id={action_id} user={confirming_user_id}",
                error_kind="bad_token",
                action_id=action_id,
            )

        # Step 4 — validate token + status
        if confirmation.status == "confirmed":
            raise ButlerActionError(
                f"user {confirming_user_id} already confirmed action_id={action_id}",
                error_kind="already_confirmed_by_user",
                action_id=action_id,
            )

        if confirmation.confirmation_token != confirmation_token:
            raise ButlerActionError(
                "confirmation_token mismatch",
                error_kind="bad_token",
                action_id=action_id,
            )

        if confirmation.expires_at <= now:
            raise ButlerActionError(
                f"confirmation token for action_id={action_id} has expired",
                error_kind="expired",
                action_id=action_id,
            )

        # Step 5 — mark this confirmation confirmed
        await self._confirmation_repo.mark_resolved(
            self._session,
            confirmation.id,
            status="confirmed",
            resolved_at=now,
        )

        # Step 6 — check if ALL confirmations are now confirmed
        all_confirmations = await self._confirmation_repo.list_for_action(
            self._session, action_id
        )
        all_confirmed = all(c.status == "confirmed" for c in all_confirmations)

        if not all_confirmed:
            # Partial — return action unchanged
            return action

        # 6a — revalidate evidence hash using stored query + visibility_scope (C2).
        # After migration 074, both columns are NOT NULL with server_default.
        # A None value means a schema invariant is broken — raise immediately instead
        # of silently masking the bug with a getattr default.
        if action.query is None or action.visibility_scope is None:
            raise ButlerActionError(
                f"action {action.id} missing query/visibility_scope post-migration-074",
                error_kind="invariant_broken",
                action_id=action.id,
            )
        stored_query = action.query
        stored_visibility = action.visibility_scope
        revalidated_context = await self._evidence_builder.build_butler_evidence(
            session=self._session,
            requester_user_id=action.requester_tg_id,
            query=stored_query,
            chat_id=action.chat_id if action.chat_id != 0 else None,
            visibility_scope=stored_visibility,
        )

        new_hash = revalidated_context.context_hash

        if new_hash != action.evidence_context_hash:
            # 6b — evidence stale — mark rejected
            await self._action_repo.update_status(
                self._session,
                action_id,
                status="rejected",
                rejection_reason="evidence_stale",
            )
            raise EvidenceStaleError(
                f"evidence hash mismatch for action_id={action_id} — context changed since plan",
                error_kind="evidence_stale",
                action_id=action_id,
            )

        # 6c — all confirmed + evidence fresh → 'confirmed' (C5: matches DB CHECK enum)
        await self._action_repo.update_status(
            self._session,
            action_id,
            status="confirmed",
            confirmed_at=now,
        )
        # Re-fetch to return updated row
        updated = await self._action_repo.get(self._session, action_id)
        return updated or action

    # -----------------------------------------------------------------------
    # execute_action
    # -----------------------------------------------------------------------

    async def execute_action(
        self,
        *,
        action_id: int,
        tool_registry: dict[str, Any] | None = None,
    ) -> Any:
        """Execute a confirmed butler action.

        Transition: confirmed → succeeded | execution_failed (C5).

        Steps
        -----
        1. get_for_update on butler_actions WHERE status='confirmed'.
        2. Re-check all affected_user confirmations (C6) — any non-confirmed →
           reject with error_kind='affected_user_consent_revoked'.
        3. Increment user_execs_day rate bucket (H3).
        4. For each action step in plan_payload['actions'] (C3):
           a. INSERT invocation row (status='running').
           b. Resolve tool from tool_registry.
           c. await tool.validate_policy(context, args).
           d. await tool.execute(plan, ctx, session=session).
           e. Update invocation status='succeeded'|'failed'.
           f. On failure → mark action status='execution_failed' (C5) + return.
        5. On all success → action status='succeeded'.
        """
        try:
            action = await self._action_repo.get_for_update(self._session, action_id)
        except _SAOperationalError as exc:
            raise CascadeInFlightError(
                f"butler_actions(id={action_id}) locked by cascade (NOWAIT) — try again",
                error_kind="cascade_in_flight",
                action_id=action_id,
            ) from exc
        if action is None:
            raise CascadeInFlightError(
                f"butler_actions(id={action_id}) locked",
                error_kind="cascade_in_flight",
                action_id=action_id,
            )

        # C5: accept 'confirmed' (correct DB enum), not 'pending_execution'
        if action.status != "confirmed":
            raise ButlerActionError(
                f"action_id={action_id} is not confirmed (got {action.status!r})",
                error_kind="wrong_status",
                action_id=action_id,
            )

        # C6 — re-check all affected_user confirmation rows before any side effect.
        # Cascade or a concurrent cancel may have revoked consent between confirm_action
        # completing and execute_action being called.
        all_confirmations = await self._confirmation_repo.list_for_action(
            self._session, action_id
        )
        for conf in all_confirmations:
            if conf.confirmation_role == "affected_user" and conf.status != "confirmed":
                # Consent was revoked — fail closed, reject the action
                await self._action_repo.update_status(
                    self._session,
                    action_id,
                    status="rejected",
                    rejection_reason="affected_user_consent_revoked",
                )
                raise ButlerActionRejectedError(
                    f"affected user consent revoked for action_id={action_id} "
                    f"confirmer_tg_id={conf.confirmer_tg_id}",
                    error_kind="affected_user_consent_revoked",
                    action_id=action_id,
                )

        # H3 — user_execs_day rate bucket at execute time
        now_exec = _now_utc()
        exec_bucket_key = _msk_day_bucket_key(now_exec)
        exec_win_start, exec_win_end = _msk_day_window(now_exec)
        execs_rate_ok = await self._rate_bucket_repo.try_increment(
            self._session,
            bucket_kind="user_execs_day",
            scope_id=action.requester_tg_id,
            bucket_key=exec_bucket_key,
            window_start=exec_win_start,
            window_end=exec_win_end,
            ceiling=getattr(self._settings, "user_execs_day_ceiling", _USER_EXECS_DAY_CEILING),
        )
        if not execs_rate_ok:
            await self._action_repo.update_status(
                self._session,
                action_id,
                status="rejected",
                rejection_reason="rate_limit_exceeded",
            )
            raise ButlerActionError(
                f"user exec rate limit exceeded for requester {action.requester_tg_id}",
                error_kind="rate_limit_exceeded",
                action_id=action_id,
            )

        # Mark executing
        await self._action_repo.update_status(self._session, action_id, status="executing")

        # Build step list from plan_payload (C3 — multi-step replay)
        steps = self._get_plan_steps(action)

        for seq, step in enumerate(steps, start=1):
            tool_name = step.get("tool_name") if isinstance(step, dict) else step.tool_name
            step_args = step.get("args", {}) if isinstance(step, dict) else step.args

            # Generate idempotency key
            idem_key = _idempotency_key(action_id, tool_name, seq)
            req_payload = {"args": step_args}
            req_hash = _args_hash(req_payload) or ""

            invocation = await self._invocation_repo.create(
                self._session,
                action_id=action_id,
                tool_name=tool_name,
                idempotency_key=idem_key,
                request_payload=req_payload,
                request_payload_hash=req_hash,
                status="running",
                invocation_seq=seq,
            )

            # Resolve tool
            tool = None
            if tool_registry:
                tool = tool_registry.get(tool_name)

            result = None
            if tool is not None:
                try:
                    # validate_policy may raise — treat as failure
                    await tool.validate_policy(None, step_args)
                    result = await tool.execute(None, None, session=self._session)
                except Exception as exc:
                    await self._invocation_repo.update_invocation(
                        self._session,
                        invocation.id,
                        status="failed",
                        error_code=str(exc)[:200],
                        finished_at=_now_utc(),
                    )
                    # C5: 'execution_failed' (not 'failed') to match DB CHECK enum
                    await self._action_repo.update_status(
                        self._session,
                        action_id,
                        status="execution_failed",
                        error_code=str(exc)[:200],
                        executed_at=_now_utc(),
                    )
                    updated = await self._action_repo.get(self._session, action_id)
                    return updated or action
            else:
                # No tool registered — treat as no-op success for T12-04 stub
                result = _StubToolResult(success=True, payload={}, error=None)

            # Check result
            if result is not None and not result.success:
                error_str = getattr(result, "error", "tool_execution_failed") or "tool_execution_failed"
                await self._invocation_repo.update_invocation(
                    self._session,
                    invocation.id,
                    status="failed",
                    error_code=error_str,
                    response_payload={"error": error_str},
                    finished_at=_now_utc(),
                )
                # C5: 'execution_failed' (not 'failed') to match DB CHECK enum
                await self._action_repo.update_status(
                    self._session,
                    action_id,
                    status="execution_failed",
                    error_code=error_str,
                    executed_at=_now_utc(),
                )
                updated = await self._action_repo.get(self._session, action_id)
                return updated or action

            # Success for this step
            resp_payload = getattr(result, "payload", {}) or {}
            await self._invocation_repo.update_invocation(
                self._session,
                invocation.id,
                status="succeeded",
                response_payload=resp_payload,
                response_payload_hash=_payload_hash(resp_payload),
                finished_at=_now_utc(),
            )

        # All steps succeeded
        await self._action_repo.update_status(
            self._session,
            action_id,
            status="succeeded",
            executed_at=_now_utc(),
        )
        updated = await self._action_repo.get(self._session, action_id)
        return updated or action

    def _get_plan_steps(self, action: Any) -> list[Any]:
        """Extract plan steps from the action row (C3 — plan_payload replay).

        Reads action.plan_payload['actions'] (the stored ButlerPlan serialization).
        Migration 074 makes plan_payload NOT NULL with default '{}'::jsonb.
        plan_action always writes plan.model_dump() which contains 'actions'.
        Missing 'actions' key means a schema invariant is broken — raise immediately.
        Test fixtures that create butler_actions rows directly MUST include a valid
        plan_payload (use plan.model_dump() shape).
        """
        plan_payload = action.plan_payload
        if not plan_payload or "actions" not in plan_payload:
            raise ButlerActionError(
                f"action {action.id} has invalid plan_payload — missing 'actions' key",
                error_kind="invariant_broken",
                action_id=action.id,
            )
        steps = plan_payload["actions"]
        return steps

    # -----------------------------------------------------------------------
    # cancel_action
    # -----------------------------------------------------------------------

    async def cancel_action(
        self,
        *,
        action_id: int,
        cancelling_user_id: int,
        is_admin: bool = False,
    ) -> Any:
        """Cancel a pending or confirmed action.

        Auth: only requester OR admin. Others → ButlerActionError(forbidden).
        All pending confirmations are marked 'cancelled'.

        H2: uses get_for_update to coordinate with cascade (same lock ordering as
        confirm_action). If cascade is mid-flight, raises CascadeInFlightError.

        M2: if user_repo is wired, admin status is verified via user lookup rather
        than trusting the is_admin caller flag (caller flag still accepted as fallback
        when user_repo is None, e.g. in test doubles).
        """
        # H2: acquire FOR UPDATE (NOWAIT) to coordinate with cascade.
        # OperationalError (NOWAIT lock contention) → CascadeInFlightError.
        try:
            action = await self._action_repo.get_for_update(self._session, action_id)
        except _SAOperationalError as exc:
            raise CascadeInFlightError(
                f"butler_actions(id={action_id}) locked by cascade (NOWAIT) — try again",
                error_kind="cascade_in_flight",
                action_id=action_id,
            ) from exc
        if action is None:
            raise CascadeInFlightError(
                f"butler_actions(id={action_id}) locked by cascade",
                error_kind="cascade_in_flight",
                action_id=action_id,
            )

        # M2: verify admin status via user_repo when available
        effective_is_admin = is_admin  # fallback for test doubles without user_repo
        if self._user_repo is not None and cancelling_user_id != action.requester_tg_id:
            cancelling_user = await self._user_repo.get(self._session, cancelling_user_id)
            effective_is_admin = (
                cancelling_user is not None and getattr(cancelling_user, "is_admin", False)
            )

        # Auth check
        if cancelling_user_id != action.requester_tg_id and not effective_is_admin:
            raise ButlerActionRejectedError(
                f"user {cancelling_user_id} is not authorized to cancel action_id={action_id}",
                error_kind="forbidden",
                action_id=action_id,
            )

        # C5: 'confirmed' (all confirmed) is also cancellable
        if action.status not in ("pending_confirmation", "confirmed"):
            raise ButlerActionError(
                f"action_id={action_id} cannot be cancelled from status={action.status!r}",
                error_kind="wrong_status",
                action_id=action_id,
            )

        # Mark all pending confirmations as cancelled
        await self._confirmation_repo.mark_all_for_action(
            self._session, action_id, status="cancelled"
        )

        await self._action_repo.update_status(
            self._session,
            action_id,
            status="cancelled",
            rejection_reason="cancelled_by_user",
        )

        updated = await self._action_repo.get(self._session, action_id)
        return updated or action

    # -----------------------------------------------------------------------
    # expire_action
    # -----------------------------------------------------------------------

    async def expire_action(self, *, action_id: int) -> Any:
        """Expire a pending_confirmation action if past its TTL.

        Idempotent: already-expired actions are returned unchanged.
        Not-yet-expired actions are also returned unchanged (caller's problem
        to schedule correctly — TTL reaper calls this).
        """
        action = await self._action_repo.get(self._session, action_id)
        if action is None:
            raise ButlerActionError(
                f"action_id={action_id} not found",
                error_kind="not_found",
                action_id=action_id,
            )

        # Already expired/cancelled/etc — idempotent
        if action.status == "expired":
            return action

        # Not a pending_confirmation — nothing to expire
        if action.status != "pending_confirmation":
            return action

        now = _now_utc()
        if action.expires_at is None or action.expires_at > now:
            # Not yet expired
            return action

        await self._action_repo.update_status(
            self._session,
            action_id,
            status="expired",
            rejection_reason="ttl_expired",
        )
        updated = await self._action_repo.get(self._session, action_id)
        return updated or action


# ---------------------------------------------------------------------------
# Internal stub ToolResult for T12-04 (real implementations ship in T12-06)
# ---------------------------------------------------------------------------

class _StubToolResult:
    """Minimal ToolResult stub used when no tool is registered (T12-04)."""

    __slots__ = ("success", "payload", "error")

    def __init__(
        self,
        *,
        success: bool,
        payload: dict | None = None,
        error: str | None = None,
    ) -> None:
        self.success = success
        self.payload = payload
        self.error = error


# ---------------------------------------------------------------------------
# Helper: map tool_name → action_type enum
# ---------------------------------------------------------------------------

_TOOL_TO_ACTION_TYPE: dict[str, str] = {
    "recall_evidence": "recall",
    "schedule_meeting": "meeting",
    "send_intro": "intro",
    "update_intro": "intro_update",
    "suggest_card_creation": "card_suggestion",
}


def _tool_name_to_action_type(tool_name: str) -> str:
    return _TOOL_TO_ACTION_TYPE.get(tool_name, "recall")
