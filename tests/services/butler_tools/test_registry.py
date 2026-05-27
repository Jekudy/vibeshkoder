"""Registry-level tests for butler_tools (T12-06).

Tests that:
- All 5 tool names map to callable tool objects in TOOL_DISPATCH
- Unknown tool name raises KeyError / is not in registry
- Each tool satisfies the ButlerTool Protocol check
- TOOL_DISPATCH keys exactly match ALLOWED_BUTLER_TOOLS
"""

from __future__ import annotations

import pytest

from bot.services.butler_tools import ALLOWED_BUTLER_TOOLS, ButlerTool


def test_tool_dispatch_exists():
    """TOOL_DISPATCH dict must exist in the butler_tools module."""
    from bot.services.butler_tools import TOOL_DISPATCH
    assert isinstance(TOOL_DISPATCH, dict)


def test_tool_dispatch_keys_match_allowed_butler_tools():
    """TOOL_DISPATCH must have exactly the same keys as ALLOWED_BUTLER_TOOLS."""
    from bot.services.butler_tools import TOOL_DISPATCH
    assert set(TOOL_DISPATCH.keys()) == set(ALLOWED_BUTLER_TOOLS)


def test_all_five_tools_registered():
    """All 5 whitelisted tool names must be present in TOOL_DISPATCH."""
    from bot.services.butler_tools import TOOL_DISPATCH

    expected = {
        "recall_evidence",
        "schedule_meeting",
        "send_intro",
        "update_intro",
        "suggest_card_creation",
    }
    assert set(TOOL_DISPATCH.keys()) == expected


def test_unknown_tool_name_not_in_dispatch():
    """An unknown tool name must NOT be in TOOL_DISPATCH."""
    from bot.services.butler_tools import TOOL_DISPATCH
    assert "delete_all_messages" not in TOOL_DISPATCH
    assert "unknown_tool" not in TOOL_DISPATCH


def test_each_tool_satisfies_protocol():
    """Each registered tool must satisfy the ButlerTool Protocol (runtime_checkable)."""
    from bot.services.butler_tools import TOOL_DISPATCH

    for tool_name, tool in TOOL_DISPATCH.items():
        assert isinstance(tool, ButlerTool), (
            f"Tool {tool_name!r} does not satisfy ButlerTool Protocol"
        )


def test_each_tool_has_correct_name():
    """Each tool's .name attribute must match its key in TOOL_DISPATCH."""
    from bot.services.butler_tools import TOOL_DISPATCH

    for key, tool in TOOL_DISPATCH.items():
        assert tool.name == key, f"tool.name={tool.name!r} != key={key!r}"


def test_each_tool_has_schema_version():
    """Each tool must have a non-empty schema_version string."""
    from bot.services.butler_tools import TOOL_DISPATCH

    for tool_name, tool in TOOL_DISPATCH.items():
        assert tool.schema_version, f"Tool {tool_name!r} missing schema_version"


def test_each_tool_has_args_model():
    """Each tool must have an args_model pointing to the correct pydantic model."""
    from bot.services.butler_tools import TOOL_ARGS_SCHEMA, TOOL_DISPATCH

    for tool_name, tool in TOOL_DISPATCH.items():
        expected_model = TOOL_ARGS_SCHEMA[tool_name]
        assert tool.args_model is expected_model, (
            f"Tool {tool_name!r} args_model mismatch: "
            f"got {tool.args_model!r}, expected {expected_model!r}"
        )


def test_dispatch_values_are_callable_tools():
    """Each value in TOOL_DISPATCH must be a tool instance with async execute method."""
    from bot.services.butler_tools import TOOL_DISPATCH
    import inspect

    for tool_name, tool in TOOL_DISPATCH.items():
        assert hasattr(tool, "execute"), f"Tool {tool_name!r} missing execute()"
        assert hasattr(tool, "validate_policy"), f"Tool {tool_name!r} missing validate_policy()"
        assert hasattr(tool, "build_inverse"), f"Tool {tool_name!r} missing build_inverse()"
        assert inspect.iscoroutinefunction(tool.execute), f"Tool {tool_name!r}.execute is not async"
