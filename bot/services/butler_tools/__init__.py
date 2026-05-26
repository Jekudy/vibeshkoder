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

from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from bot.services.butler_evidence import ButlerEvidenceContext


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class ButlerPlanError(Exception):
    """Base exception for Butler plan validation errors."""


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

_ToolNameLiteral = Literal[
    "recall_evidence",
    "schedule_meeting",
    "send_intro",
    "update_intro",
    "suggest_card_creation",
]

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
    - tool_name: Literal[...5 names...]
    - args: dict — raw args from LLM (validated per-tool by validate_butler_plan)
    - requires_confirmation: bool
    - affected_user_ids: list[int]
    - risk_level: Literal["low","medium","high"]
    - rollback_kind: Literal["delete_message","edit_message","followup_correction","cancel_pending","not_reversible"]
    - inverse_op_payload: dict | None
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_name: _ToolNameLiteral
    args: dict[str, Any]
    requires_confirmation: bool = True
    affected_user_ids: list[int] = []
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
    evidence_ids: list[int]
    actions: list[ButlerActionStep]

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


class ButlerTool(Protocol):
    """Protocol that every concrete Butler tool implementation must satisfy.

    T12-06 ships implementations: RecallEvidenceTool, ScheduleMeetingTool,
    SendIntroTool, UpdateIntroTool, SuggestCardCreationTool.

    Hard Constraint #1: no LLM calls inside tool implementations.
    Hard Constraint #2: Butler must not read raw DB outside ButlerEvidenceContext.
    """

    @property
    def tool_name(self) -> str:
        """Tool name — must be in ALLOWED_BUTLER_TOOLS."""
        ...

    async def execute(
        self,
        plan: ButlerPlan,
        ctx: "ButlerEvidenceContext",
        *,
        session: "AsyncSession",
    ) -> ToolResult:
        """Execute the tool action.

        Called ONLY after user confirmation gate passes (T12-04 / T12-05).
        Must write a ``butler_tool_invocations`` row (T12-06 responsibility).
        Must fail-closed on any governance violation (Hard Constraint #3).
        """
        ...


# ---------------------------------------------------------------------------
# validate_butler_plan — enforces whitelist + per-tool args schema
# ---------------------------------------------------------------------------


def validate_butler_plan(plan: ButlerPlan) -> ButlerPlan:
    """Validate a ButlerPlan against the tool whitelist and per-tool args schemas.

    Enforces Hard Constraint #8 (unknown tool → REJECT before confirmation)
    and R8.f/R8.g binding acceptance criteria.

    Steps
    -----
    1. For each action step: reject tool_name not in ALLOWED_BUTLER_TOOLS.
       Raises ``ToolNotAllowedError`` immediately on first violation.
    2. For each action step: route step.args through TOOL_ARGS_SCHEMA[tool_name]
       .model_validate(...). Raises ``InvalidToolArgsError`` on ValidationError.
    3. Returns the plan unchanged if all steps pass.

    Note: ButlerPlan.actions is a list so a multi-step plan is validated in
    full. Fail-fast on first violation (no collecting of all errors).
    """
    for step in plan.actions:
        # Step 1 — tool whitelist check (this should already be enforced by
        # ButlerActionStep's Literal annotation, but validate_butler_plan is
        # the external-input guard that catches dict-constructed plans).
        if step.tool_name not in ALLOWED_BUTLER_TOOLS:
            raise ToolNotAllowedError(
                f"tool_name {step.tool_name!r} is not in ALLOWED_BUTLER_TOOLS"
            )

        # Step 2 — per-tool args schema validation.
        args_model_cls = TOOL_ARGS_SCHEMA[step.tool_name]
        try:
            args_model_cls.model_validate(step.args)
        except ValidationError as exc:
            raise InvalidToolArgsError(
                f"args for tool {step.tool_name!r} failed schema validation: {exc}"
            ) from exc

    return plan
