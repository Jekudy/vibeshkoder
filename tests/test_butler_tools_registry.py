"""Unit tests for butler tools registry — T12-03.

TDD red phase: tests written BEFORE implementation.

Covers:
  - ALLOWED_BUTLER_TOOLS contains exactly 5 names
  - TOOL_ARGS_SCHEMA has an entry for every name in ALLOWED_BUTLER_TOOLS (no extras)
  - validate_butler_plan rejects unknown tool_name with ToolNotAllowedError
  - validate_butler_plan rejects schema-invalid args with InvalidToolArgsError (one case per tool)
  - validate_butler_plan accepts well-formed plan for each of the 5 tools
  - ButlerPlan is a pydantic v2 model with expected fields
  - ButlerActionStep has the spec field set
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# T12-03 registry imports
# ---------------------------------------------------------------------------

from bot.services.butler_tools import (
    ALLOWED_BUTLER_TOOLS,
    TOOL_ARGS_SCHEMA,
    ButlerActionStep,
    ButlerPlan,
    ButlerPlanError,
    InvalidToolArgsError,
    RecallEvidenceArgs,
    ScheduleMeetingArgs,
    SendIntroArgs,
    SuggestCardCreationArgs,
    ToolNotAllowedError,
    UpdateIntroArgs,
    validate_butler_plan,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ALL_TOOLS = sorted(ALLOWED_BUTLER_TOOLS)


def _make_step(
    tool_name: str,
    args: dict,
    *,
    requires_confirmation: bool = True,
    affected_user_ids: list[int] | None = None,
    risk_level: str = "low",
    rollback_kind: str = "not_reversible",
    inverse_op_payload: dict | None = None,
) -> ButlerActionStep:
    return ButlerActionStep(
        tool_name=tool_name,
        args=args,
        requires_confirmation=requires_confirmation,
        affected_user_ids=affected_user_ids or [],
        risk_level=risk_level,
        rollback_kind=rollback_kind,
        inverse_op_payload=inverse_op_payload,
    )


def _make_plan(
    tool_name: str,
    args: dict,
    *,
    evidence_ids: list[int] | None = None,
    evidence_context_hash: str = "abc123",
    requester_user_id: int = 42,
    chat_id: int | None = None,
    visibility_scope: str = "member",
    governance_filter_version: str = "v1",
    rationale: str | None = None,
) -> ButlerPlan:
    step = _make_step(tool_name, args)
    return ButlerPlan(
        plan_summary="Test plan",
        evidence_ids=evidence_ids or [1, 2, 3],
        actions=[step],
        evidence_context_hash=evidence_context_hash,
        requester_user_id=requester_user_id,
        chat_id=chat_id,
        visibility_scope=visibility_scope,
        governance_filter_version=governance_filter_version,
        rationale=rationale,
    )


# ---------------------------------------------------------------------------
# ALLOWED_BUTLER_TOOLS
# ---------------------------------------------------------------------------


def test_allowed_butler_tools_is_frozenset() -> None:
    assert isinstance(ALLOWED_BUTLER_TOOLS, frozenset)


def test_allowed_butler_tools_has_exactly_5_names() -> None:
    assert len(ALLOWED_BUTLER_TOOLS) == 5


def test_allowed_butler_tools_contains_expected_names() -> None:
    expected = frozenset(
        {
            "recall_evidence",
            "schedule_meeting",
            "send_intro",
            "update_intro",
            "suggest_card_creation",
        }
    )
    assert ALLOWED_BUTLER_TOOLS == expected


# ---------------------------------------------------------------------------
# TOOL_ARGS_SCHEMA
# ---------------------------------------------------------------------------


def test_tool_args_schema_has_entry_for_every_allowed_tool() -> None:
    assert set(TOOL_ARGS_SCHEMA.keys()) == set(ALLOWED_BUTLER_TOOLS)


def test_tool_args_schema_has_no_extra_entries() -> None:
    for key in TOOL_ARGS_SCHEMA:
        assert key in ALLOWED_BUTLER_TOOLS, f"Extra key in TOOL_ARGS_SCHEMA: {key!r}"


# ---------------------------------------------------------------------------
# validate_butler_plan — unknown tool_name → ToolNotAllowedError
# ---------------------------------------------------------------------------


def test_validate_butler_plan_rejects_unknown_tool_name() -> None:
    # Bypass pydantic Literal validation to simulate an externally-constructed
    # plan dict that arrived with an unknown tool_name (e.g. from a malformed
    # LLM response deserialized via model_construct).  validate_butler_plan is
    # the external-input guard that catches this case.
    bad_step = ButlerActionStep.model_construct(
        tool_name="delete_all_messages",
        args={"confirm": True},
        requires_confirmation=True,
        affected_user_ids=[],
        risk_level="low",
        rollback_kind="not_reversible",
        inverse_op_payload=None,
    )
    plan = ButlerPlan.model_construct(
        plan_summary="Bad plan",
        evidence_ids=[1],
        actions=[bad_step],
        evidence_context_hash="abc123",
        requester_user_id=42,
        chat_id=None,
        visibility_scope="member",
        governance_filter_version="v1",
        rationale=None,
    )
    with pytest.raises(ToolNotAllowedError):
        validate_butler_plan(plan)


def test_tool_not_allowed_error_is_subclass_of_butler_plan_error() -> None:
    assert issubclass(ToolNotAllowedError, ButlerPlanError)


def test_invalid_tool_args_error_is_subclass_of_butler_plan_error() -> None:
    assert issubclass(InvalidToolArgsError, ButlerPlanError)


# ---------------------------------------------------------------------------
# validate_butler_plan — schema-invalid args → InvalidToolArgsError (one per tool)
# ---------------------------------------------------------------------------


def test_validate_butler_plan_rejects_invalid_args_recall_evidence() -> None:
    # RecallEvidenceArgs requires 'query' (str) — pass wrong type
    plan = _make_plan("recall_evidence", {"query": 12345})
    with pytest.raises(InvalidToolArgsError):
        validate_butler_plan(plan)


def test_validate_butler_plan_rejects_invalid_args_schedule_meeting() -> None:
    # ScheduleMeetingArgs requires 'topic' (str) — omit it entirely
    plan = _make_plan("schedule_meeting", {"proposed_time_text": "tomorrow 3pm"})
    with pytest.raises(InvalidToolArgsError):
        validate_butler_plan(plan)


def test_validate_butler_plan_rejects_invalid_args_send_intro() -> None:
    # SendIntroArgs requires 'target_user_id' (int) — pass wrong type
    plan = _make_plan("send_intro", {"target_user_id": "not_an_int", "intro_text": "hi"})
    with pytest.raises(InvalidToolArgsError):
        validate_butler_plan(plan)


def test_validate_butler_plan_rejects_invalid_args_update_intro() -> None:
    # UpdateIntroArgs requires 'message_id' (int) — omit it entirely
    plan = _make_plan("update_intro", {"new_intro_text": "hi"})
    with pytest.raises(InvalidToolArgsError):
        validate_butler_plan(plan)


def test_validate_butler_plan_rejects_invalid_args_suggest_card_creation() -> None:
    # SuggestCardCreationArgs requires 'title' (str) — omit it entirely
    plan = _make_plan("suggest_card_creation", {"summary": "some summary"})
    with pytest.raises(InvalidToolArgsError):
        validate_butler_plan(plan)


# ---------------------------------------------------------------------------
# validate_butler_plan — happy-path for each of the 5 tools
# ---------------------------------------------------------------------------


def test_validate_butler_plan_accepts_recall_evidence() -> None:
    plan = _make_plan("recall_evidence", {"query": "who knows Python?"})
    result = validate_butler_plan(plan)
    # Returns a plan with the same core fields (args may be canonical model_dump)
    assert isinstance(result, ButlerPlan)
    assert result.plan_summary == plan.plan_summary
    assert result.evidence_ids == plan.evidence_ids
    assert len(result.actions) == 1
    assert result.actions[0].tool_name == "recall_evidence"


def test_validate_butler_plan_accepts_schedule_meeting() -> None:
    plan = _make_plan(
        "schedule_meeting",
        {"topic": "intro sync", "proposed_time_text": "Friday 2pm"},
    )
    result = validate_butler_plan(plan)
    assert isinstance(result, ButlerPlan)
    assert result.actions[0].tool_name == "schedule_meeting"


def test_validate_butler_plan_accepts_send_intro() -> None:
    plan = _make_plan(
        "send_intro",
        {"target_user_id": 999, "intro_text": "Meet Alice, she knows Rust!"},
    )
    result = validate_butler_plan(plan)
    assert isinstance(result, ButlerPlan)
    assert result.actions[0].tool_name == "send_intro"


def test_validate_butler_plan_accepts_update_intro() -> None:
    plan = _make_plan(
        "update_intro",
        {"message_id": 555, "new_intro_text": "Updated intro"},
    )
    result = validate_butler_plan(plan)
    assert isinstance(result, ButlerPlan)
    assert result.actions[0].tool_name == "update_intro"


def test_validate_butler_plan_accepts_suggest_card_creation() -> None:
    plan = _make_plan(
        "suggest_card_creation",
        {
            "title": "Rust async patterns",
            "summary": "Key patterns for async Rust",
            "tags": ["rust", "async"],
        },
    )
    result = validate_butler_plan(plan)
    assert isinstance(result, ButlerPlan)
    assert result.actions[0].tool_name == "suggest_card_creation"


# ---------------------------------------------------------------------------
# ButlerPlan model — field presence and pydantic v2 structure
# ---------------------------------------------------------------------------


def test_butler_plan_is_pydantic_v2_model() -> None:
    from pydantic import BaseModel

    assert issubclass(ButlerPlan, BaseModel)


def test_butler_plan_has_required_fields() -> None:
    fields = ButlerPlan.model_fields
    required_fields = {
        "plan_summary",
        "evidence_ids",
        "actions",
        "evidence_context_hash",
        "requester_user_id",
        "visibility_scope",
        "governance_filter_version",
    }
    for f in required_fields:
        assert f in fields, f"Missing required field: {f!r}"


def test_butler_plan_has_optional_fields() -> None:
    fields = ButlerPlan.model_fields
    # chat_id and rationale are optional (can be None)
    assert "chat_id" in fields
    assert "rationale" in fields


def test_butler_action_step_is_pydantic_v2_model() -> None:
    from pydantic import BaseModel

    assert issubclass(ButlerActionStep, BaseModel)


def test_butler_action_step_has_spec_fields() -> None:
    fields = ButlerActionStep.model_fields
    spec_fields = {
        "tool_name",
        "args",
        "requires_confirmation",
        "affected_user_ids",
        "risk_level",
        "rollback_kind",
        "inverse_op_payload",
    }
    for f in spec_fields:
        assert f in fields, f"Missing spec field on ButlerActionStep: {f!r}"


def test_butler_action_step_tool_name_restricted_to_allowed() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ButlerActionStep(
            tool_name="unknown_tool",
            args={},
            requires_confirmation=True,
            affected_user_ids=[],
            risk_level="low",
            rollback_kind="not_reversible",
        )


def test_butler_action_step_risk_level_restricted() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ButlerActionStep(
            tool_name="recall_evidence",
            args={},
            requires_confirmation=True,
            affected_user_ids=[],
            risk_level="extreme",
            rollback_kind="not_reversible",
        )


def test_butler_action_step_rollback_kind_restricted() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ButlerActionStep(
            tool_name="recall_evidence",
            args={},
            requires_confirmation=True,
            affected_user_ids=[],
            risk_level="low",
            rollback_kind="invalid_kind",
        )


# ---------------------------------------------------------------------------
# Per-tool args model sanity checks
# ---------------------------------------------------------------------------


def test_recall_evidence_args_requires_query() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RecallEvidenceArgs()  # type: ignore[call-arg]


def test_schedule_meeting_args_requires_topic() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ScheduleMeetingArgs(proposed_time_text="tomorrow")  # type: ignore[call-arg]


def test_send_intro_args_requires_target_user_id_and_intro_text() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SendIntroArgs(intro_text="hi")  # type: ignore[call-arg]


def test_update_intro_args_requires_message_id() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        UpdateIntroArgs(new_intro_text="hi")  # type: ignore[call-arg]


def test_suggest_card_creation_args_requires_title() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SuggestCardCreationArgs(summary="summarising")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# ButlerTool Protocol shape — H-1 (spec §5.C)
# ---------------------------------------------------------------------------


def _butler_tool_members() -> frozenset[str]:
    """Return the set of Protocol member names from ButlerTool.__protocol_attrs__."""
    from bot.services.butler_tools import ButlerTool

    # __protocol_attrs__ is set by the typing module on Protocol subclasses and
    # contains all abstract member names (attributes + methods).
    return frozenset(getattr(ButlerTool, "__protocol_attrs__", set()))


def test_butler_tool_protocol_has_name_attribute() -> None:
    assert "name" in _butler_tool_members()


def test_butler_tool_protocol_has_schema_version_attribute() -> None:
    assert "schema_version" in _butler_tool_members()


def test_butler_tool_protocol_has_args_model_attribute() -> None:
    assert "args_model" in _butler_tool_members()


def test_butler_tool_protocol_has_validate_policy_method() -> None:
    from bot.services.butler_tools import ButlerTool

    assert hasattr(ButlerTool, "validate_policy")


def test_butler_tool_protocol_has_build_inverse_method() -> None:
    from bot.services.butler_tools import ButlerTool

    assert hasattr(ButlerTool, "build_inverse")


def test_butler_tool_protocol_has_execute_method() -> None:
    from bot.services.butler_tools import ButlerTool

    assert hasattr(ButlerTool, "execute")


# ---------------------------------------------------------------------------
# BUTLER_TOOL_MANIFEST_VERSION constant — H-3
# ---------------------------------------------------------------------------


def test_butler_tool_manifest_version_is_str() -> None:
    from bot.services.butler_tools import BUTLER_TOOL_MANIFEST_VERSION

    assert isinstance(BUTLER_TOOL_MANIFEST_VERSION, str)
    assert BUTLER_TOOL_MANIFEST_VERSION  # non-empty


# ---------------------------------------------------------------------------
# ButlerPlanError carries ledger_id and error_kind — C-3
# ---------------------------------------------------------------------------


def test_butler_plan_error_carries_llm_usage_ledger_id() -> None:
    err = ButlerPlanError("some error", llm_usage_ledger_id=42, error_kind="some_kind")
    assert err.llm_usage_ledger_id == 42
    assert err.error_kind == "some_kind"


def test_butler_plan_error_defaults_ledger_id_to_none() -> None:
    err = ButlerPlanError("plain error")
    assert err.llm_usage_ledger_id is None
    assert err.error_kind is None


# ---------------------------------------------------------------------------
# H-4: args canonicalisation in validate_butler_plan
# ---------------------------------------------------------------------------


def test_validate_butler_plan_args_are_canonical_after_validation() -> None:
    """After validate_butler_plan, step.args is the canonical model_dump form."""
    # ScheduleMeetingArgs has participant_user_ids: list[int] = []
    # If we pass the raw dict without it, the canonical form should include the default.
    plan = _make_plan(
        "schedule_meeting",
        {"topic": "team sync"},  # omit optional proposed_time_text + participant_user_ids
    )
    result = validate_butler_plan(plan)
    canonical_args = result.actions[0].args
    # The canonical form from ScheduleMeetingArgs.model_dump() should include defaults
    assert "topic" in canonical_args
    assert canonical_args["topic"] == "team sync"
    # Default fields should be present in canonical dump
    assert "participant_user_ids" in canonical_args
    assert "proposed_time_text" in canonical_args


# ---------------------------------------------------------------------------
# M-3: ButlerPlan uses tuple types
# ---------------------------------------------------------------------------


def test_butler_plan_evidence_ids_is_tuple() -> None:
    plan = _make_plan("recall_evidence", {"query": "test"}, evidence_ids=[1, 2, 3])
    assert isinstance(plan.evidence_ids, tuple)


def test_butler_plan_actions_is_tuple() -> None:
    plan = _make_plan("recall_evidence", {"query": "test"})
    assert isinstance(plan.actions, tuple)
