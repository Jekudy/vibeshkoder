"""Butler tool registry — T12-03 (Wave 1 Stream C).

Defines:
- ``ButlerTool`` Protocol — interface that every concrete tool implementation
  must satisfy (T12-06 ships the 5 implementations).
- ``ALLOWED_BUTLER_TOOLS`` — closed frozenset of 5 whitelisted tool names.
  Unknown tool_name → REJECT before user confirmation (Hard Constraint #8).
- Per-tool pydantic v2 args models (``RecallEvidenceArgs``, etc.).
- ``TOOL_ARGS_SCHEMA`` — mapping tool_name → pydantic args model class.
- ``ButlerActionStep`` pydantic model — a single action inside a ButlerPlan.
- ``ButlerPlan`` pydantic model — the structured output of the LLM planning
  call, validated against the tool whitelist + args schemas.
- ``validate_butler_plan`` — validation function that enforces both the tool
  whitelist and the per-tool args schema.
- Custom exceptions: ``ButlerPlanError`` (base), ``ToolNotAllowedError``,
  ``InvalidToolArgsError``.

Design rationale
----------------
* NO LLM calls in this module (Hard Constraint #1). The registry is
  pure-Python — it defines schemas and validates. The actual provider call
  lives in ``bot/services/llm_gateway.py::plan_butler_action``.
* NO imports of ``anthropic``, ``openai``, or ``bot.services.graph_query``
  (enforced by G3.a AST scan in T12-09).
* Pydantic v2 — all models use ``model_config = ConfigDict(...)`` style.
  ``ButlerPlan`` is model_config(frozen=True, extra="forbid") so deserialised
  LLM output cannot add unknown fields.

Spec references
---------------
* Charter §"Hard Constraints" #8 — tool whitelist is closed; unknown tool → REJECT
* PHASE12_PLAN_REFRESH.md §10 T12-03 — authoritative field lists for ButlerPlan
  and ButlerActionStep
* PHASE12_PLAN_REFRESH.md §12.4 R8.f/R8.g — binding acceptance criteria
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, ValidationError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from bot.services.butler_evidence import ButlerEvidenceContext


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class ButlerPlanError(Exception):
    """Base exception for Butler plan validation errors.

    Carries ``llm_usage_ledger_id`` so T12-04 can populate
    ``butler_actions.llm_usage_ledger_id`` even for failed plans
    (status='rejected'), and ``error_kind`` for downstream routing.
    """

    def __init__(
        self,
        message: str,
        *,
        llm_usage_ledger_id: int | None = None,
        error_kind: str | None = None,
    ) -> None:
        super().__init__(message)
        self.llm_usage_ledger_id = llm_usage_ledger_id
        self.error_kind = error_kind


class ToolNotAllowedError(ButlerPlanError):
    """Raised when a ButlerPlan references a tool_name not in ALLOWED_BUTLER_TOOLS.

    Hard Constraint #8: unknown tool → REJECT before user confirmation.
    """


class InvalidToolArgsError(ButlerPlanError):
    """Raised when a ButlerPlan's args fail the per-tool pydantic args schema."""


# ---------------------------------------------------------------------------
# Closed tool whitelist — 5 names, immutable
# ---------------------------------------------------------------------------

ALLOWED_BUTLER_TOOLS: frozenset[str] = frozenset(
    {
        "recall_evidence",
        "schedule_meeting",
        "send_intro",
        "update_intro",
        "suggest_card_creation",
    }
)

# Tool manifest version — semver string that is included in the planning prompt
# body so it becomes part of prompt_hash for G3.b replay verification.
# Bump this when tool schemas change in a backward-incompatible way (T12-06+).
BUTLER_TOOL_MANIFEST_VERSION: str = "v1.0.0"


# ---------------------------------------------------------------------------
# Per-tool args models (pydantic v2)
# ---------------------------------------------------------------------------


class RecallEvidenceArgs(BaseModel):
    """Args for the recall_evidence tool.

    Triggers a governed evidence recall from the community memory.
    The gateway does NOT re-query the DB; it describes the recall intent
    that ``ButlerService.request_plan`` already executed upstream.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str


class ScheduleMeetingArgs(BaseModel):
    """Args for the schedule_meeting tool.

    Proposes a meeting with one or more community members.
    No external calendar API — Telegram-native proposal only (Hard Constraint #6).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str
    proposed_time_text: str | None = None
    participant_user_ids: list[int] = []


class SendIntroArgs(BaseModel):
    """Args for the send_intro tool.

    Sends a Telegram introduction message on behalf of the requester.
    Requires cross-user consent from the affected user (Hard Constraint #5).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_user_id: int
    intro_text: str


class UpdateIntroArgs(BaseModel):
    """Args for the update_intro tool.

    Edits a previously sent Butler-owned introduction message OR posts a
    followup correction if the original message is no longer editable.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    message_id: int
    new_intro_text: str


class SuggestCardCreationArgs(BaseModel):
    """Args for the suggest_card_creation tool.

    Writes a pending ``extraction_candidates`` row + a ``butler_card_suggestions``
    mapping row. Phase 6 admin review takes over from there (T12-06).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str
    summary: str | None = None
    tags: list[str] = []


# ---------------------------------------------------------------------------
# TOOL_ARGS_SCHEMA — mapping tool_name → pydantic args model
# ---------------------------------------------------------------------------

TOOL_ARGS_SCHEMA: dict[str, type[BaseModel]] = {
    "recall_evidence": RecallEvidenceArgs,
    "schedule_meeting": ScheduleMeetingArgs,
    "send_intro": SendIntroArgs,
    "update_intro": UpdateIntroArgs,
    "suggest_card_creation": SuggestCardCreationArgs,
}

# Invariant: TOOL_ARGS_SCHEMA must cover exactly ALLOWED_BUTLER_TOOLS.
assert set(TOOL_ARGS_SCHEMA.keys()) == set(ALLOWED_BUTLER_TOOLS), (
    "TOOL_ARGS_SCHEMA keys must exactly match ALLOWED_BUTLER_TOOLS"
)


# ---------------------------------------------------------------------------
# ButlerActionStep — a single action inside a ButlerPlan
# ---------------------------------------------------------------------------

_RiskLevel = Literal["low", "medium", "high"]

_RollbackKind = Literal[
    "delete_message",
    "edit_message",
    "followup_correction",
    "cancel_pending",
    "not_reversible",
]


class ButlerActionStep(BaseModel):
    """A single action step inside a ``ButlerPlan``.

    Spec §10 T12-03 authoritative field list:
    - tool_name: str — plain str so pydantic accepts any string from LLM output.
      Allowlist enforcement is done exclusively by ``validate_butler_plan``
      (single source of truth for tool name validation per Option A of the
      Codex HIGH fix). Unknown tool_name → ToolNotAllowedError(error_kind='tool_not_allowed').
    - args: dict — raw args from LLM (validated per-tool by validate_butler_plan)
    - requires_confirmation: bool
    - affected_user_ids: list[int]
    - risk_level: Literal["low","medium","high"]
    - rollback_kind: Literal["delete_message","edit_message","followup_correction","cancel_pending","not_reversible"]
    - inverse_op_payload: dict | None
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Plain str — NOT Literal[...]. Allowlist check lives entirely in
    # validate_butler_plan so unknown tool names raise ToolNotAllowedError
    # (error_kind='tool_not_allowed') rather than a pydantic ValidationError
    # that would be caught as error_kind='invalid_plan_schema'.
    tool_name: str
    args: dict[str, Any]
    requires_confirmation: bool = True
    # tuple not list: immutable inside frozen model (M-3)
    affected_user_ids: tuple[int, ...] = ()
    risk_level: _RiskLevel = "low"
    rollback_kind: _RollbackKind = "not_reversible"
    inverse_op_payload: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# ButlerPlan — full structured output of the LLM planning call
# ---------------------------------------------------------------------------


class ButlerPlan(BaseModel):
    """Structured Butler plan returned by ``plan_butler_action``.

    Spec §10 T12-03 authoritative field list (PHASE12_PLAN_REFRESH.md):

    Core plan fields:
    - plan_summary: str — LLM's human-readable summary of the plan
    - evidence_ids: list[int] — message_version_ids the plan was based on
    - actions: list[ButlerActionStep] — ordered list of action steps

    Gateway-metadata fields (bound at planning time, echoed in LLM output):
    - evidence_context_hash: str — butler_context_hash(...) for replay verification
    - requester_user_id: int — Telegram user_id of the /butler invoker
    - chat_id: int | None — community chat scope (None = DM-scoped)
    - visibility_scope: Literal["member","admin","self"]
    - governance_filter_version: str — detect_policy + cascade-layer hash
    - rationale: str | None — LLM's explanation (optional)

    ``extra="forbid"`` ensures the LLM cannot inject unknown fields.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Core plan fields from spec §10 T12-03
    plan_summary: str
    # tuple not list: immutable inside frozen model (M-3)
    evidence_ids: tuple[int, ...]
    actions: tuple[ButlerActionStep, ...]

    # Gateway-metadata fields — bound at planning time
    evidence_context_hash: str
    requester_user_id: int
    chat_id: int | None = None
    visibility_scope: Literal["member", "admin", "self"]
    governance_filter_version: str
    rationale: str | None = None


# ---------------------------------------------------------------------------
# ButlerTool Protocol — interface for T12-06 concrete tool implementations
# ---------------------------------------------------------------------------


class ToolResult:
    """Minimal result container returned by ButlerTool.execute.

    T12-06 ships the full typed ToolResult; T12-03 defines the minimal
    Protocol shape so the registry module is self-contained.
    """

    __slots__ = ("success", "payload", "error")

    def __init__(
        self,
        *,
        success: bool,
        payload: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        self.success = success
        self.payload = payload
        self.error = error


@runtime_checkable
class ButlerTool(Protocol):
    """Protocol that every concrete Butler tool implementation must satisfy.

    Spec §5.C verbatim shape (PHASE12_PLAN_REFRESH.md lines 657-665):

    .. code-block:: python

        class ButlerTool(Protocol):
            name: str                      # not tool_name
            schema_version: str
            args_model: type[BaseModel]
            async def validate_policy(self, context, args) -> None: ...
            async def execute(self, plan: ButlerPlan, ctx: ButlerEvidenceContext,
                              *, session: AsyncSession) -> ToolResult: ...
            async def build_inverse(self, result: ToolResult) -> dict[str, object]: ...

    T12-06 ships implementations: RecallEvidenceTool, ScheduleMeetingTool,
    SendIntroTool, UpdateIntroTool, SuggestCardCreationTool.

    Hard Constraint #1: no LLM calls inside tool implementations.
    Hard Constraint #2: Butler must not read raw DB outside ButlerEvidenceContext.
    """

    # Tool name — must be in ALLOWED_BUTLER_TOOLS (renamed from tool_name per spec §5.C)
    name: str
    # Semver string identifying the args schema version (e.g. "v1.0.0")
    schema_version: str
    # Pydantic model class for validating the tool's args dict
    args_model: type[BaseModel]

    async def validate_policy(
        self,
        context: "ButlerEvidenceContext",
        args: BaseModel,
    ) -> None:
        """Validate governance policy BEFORE execution.

        Must raise ButlerPlanError (or a subclass) if the action violates
        any Hard Constraint (cross-user consent, off-record boundary, etc.).
        Called BEFORE execute by T12-04 confirm_action.
        """
        ...

    async def execute(
        self,
        ctx: "ButlerEvidenceContext",
        args: BaseModel,
        *,
        session: "AsyncSession",
        bot: Any = None,
        action_repo: Any = None,
        invocation_repo: Any = None,
        action_id: int,
    ) -> ToolResult:
        """Execute the tool action.

        Called ONLY after user confirmation gate passes (T12-04 / T12-05).
        The ``butler_tool_invocations`` row is written by the service caller
        (ButlerService.execute_action) — NOT by the tool implementation.
        Must fail-closed on any governance violation (Hard Constraint #3).

        Parameters
        ----------
        ctx:
            Sealed ButlerEvidenceContext built by build_butler_evidence.
        args:
            Validated pydantic args model (e.g. RecallEvidenceArgs). Caller
            MUST pass a typed pydantic instance — never a raw dict.
        session:
            Async SQLAlchemy session. Caller owns commit/rollback.
        bot:
            Telegram Bot instance (threaded from handler, per Phase 7 FHR
            pattern). None for tools that produce no Telegram output.
        action_repo:
            ButlerActionRepo instance. None for tools that do not need it.
        invocation_repo:
            ButlerToolInvocationRepo instance. Passed to tools that need
            invocation lookup by posted_message_id (update_intro). None for
            tools that do not need it.
        action_id:
            The butler_actions.id for this execution. NO default — caller
            MUST pass the real PK to prevent FK violation on DB writes.
        """
        ...

    async def build_inverse(self, result: ToolResult) -> dict[str, object]:
        """Build a rollback/inverse operation payload for the given ToolResult.

        Used to populate ``butler_action_steps.inverse_op_payload`` after
        a successful execution so the action can be undone if needed.
        Returns a plain dict that is JSON-serialisable.
        """
        ...


# ---------------------------------------------------------------------------
# validate_butler_plan — enforces whitelist + per-tool args schema
# ---------------------------------------------------------------------------


def validate_butler_plan(
    plan: ButlerPlan,
    *,
    allowed_tools: frozenset[str] | None = None,
) -> ButlerPlan:
    """Validate a ButlerPlan against the tool whitelist and per-tool args schemas.

    Enforces Hard Constraint #8 (unknown tool → REJECT before confirmation)
    and R8.f/R8.g binding acceptance criteria.

    Parameters
    ----------
    plan:
        The ButlerPlan to validate.
    allowed_tools:
        Override the default ALLOWED_BUTLER_TOOLS set for this call.
        Useful for incident-response tooling where a per-call subset is needed.
        Defaults to the module-level ``ALLOWED_BUTLER_TOOLS``.

    Steps
    -----
    1. For each action step: reject tool_name not in allowed_tools (defaults to
       ALLOWED_BUTLER_TOOLS). Raises ``ToolNotAllowedError`` immediately on first
       violation.
    2. For each action step: route step.args through TOOL_ARGS_SCHEMA[tool_name]
       .model_validate(...). Raises ``InvalidToolArgsError`` on ValidationError.
    3. Replaces each step's args with the canonical model_dump() output (type-
       coerced, defaults filled) — H-4 args canonicalisation.
    4. Returns the validated plan with canonicalised args.

    Note: ButlerPlan.actions is a tuple so a multi-step plan is validated in
    full. Fail-fast on first violation (no collecting of all errors).
    """
    _allowed = allowed_tools if allowed_tools is not None else ALLOWED_BUTLER_TOOLS
    validated_actions: list[ButlerActionStep] = []

    for step in plan.actions:
        # Step 1 — tool whitelist check (Option A: ButlerActionStep.tool_name is
        # plain str, so this is the SINGLE source of truth for allowlist enforcement).
        if step.tool_name not in _allowed:
            raise ToolNotAllowedError(
                f"tool_name={step.tool_name!r} not in ALLOWED_BUTLER_TOOLS",
                error_kind="tool_not_allowed",
            )

        # Step 2 — per-tool args schema validation.
        args_model_cls = TOOL_ARGS_SCHEMA[step.tool_name]
        try:
            validated_args = args_model_cls.model_validate(step.args)
        except ValidationError as exc:
            raise InvalidToolArgsError(
                f"args validation failed for tool={step.tool_name}: {exc}",
                error_kind="invalid_args",
            ) from exc

        # Step 3 — args canonicalisation (H-4): replace raw args with the
        # validated model's model_dump() output so downstream gets type-coerced,
        # defaults-filled canonical form instead of the raw LLM dict.
        step_dict = step.model_dump()
        step_dict["args"] = validated_args.model_dump()
        canonical_step = ButlerActionStep.model_validate(step_dict)
        validated_actions.append(canonical_step)

    return plan.model_copy(update={"actions": tuple(validated_actions)})


# ---------------------------------------------------------------------------
# TOOL_DISPATCH — maps tool_name → ButlerTool instance (T12-06)
# ---------------------------------------------------------------------------
# Lazy imports to avoid circular imports between butler.py and tool modules.
# Import the concrete implementations at module load time (they are safe to import
# without DB connection — all state is in the Protocol instance, not module-level).

def _build_tool_dispatch() -> "dict[str, ButlerTool]":
    """Build the TOOL_DISPATCH registry from the 5 tool implementations."""
    from bot.services.butler_tools.recall_evidence import RecallEvidenceTool
    from bot.services.butler_tools.schedule_meeting import ScheduleMeetingTool
    from bot.services.butler_tools.send_intro import SendIntroTool
    from bot.services.butler_tools.suggest_card_creation import SuggestCardCreationTool
    from bot.services.butler_tools.update_intro import UpdateIntroTool

    tools: list[ButlerTool] = [
        RecallEvidenceTool(),
        ScheduleMeetingTool(),
        SendIntroTool(),
        UpdateIntroTool(),
        SuggestCardCreationTool(),
    ]
    dispatch = {t.name: t for t in tools}
    # Invariant: TOOL_DISPATCH keys must exactly match ALLOWED_BUTLER_TOOLS.
    assert set(dispatch.keys()) == set(ALLOWED_BUTLER_TOOLS), (
        f"TOOL_DISPATCH keys {set(dispatch.keys())} != ALLOWED_BUTLER_TOOLS {set(ALLOWED_BUTLER_TOOLS)}"
    )
    return dispatch


TOOL_DISPATCH: "dict[str, ButlerTool]" = _build_tool_dispatch()
