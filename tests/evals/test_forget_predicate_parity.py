"""Phase 11 binding test — forget_predicate parity snapshot (#291).

Golden-snapshot test: `forget_excludes_sql_fragment()` must return a
byte-identical string to the frozen reference below.  If any caller
tries to change the predicate semantics they MUST update this snapshot
— which forces a deliberate, reviewed change rather than silent drift.

Privacy-critical: this predicate is the SINGLE source of truth across:
- forget_cascade.py (cascade worker)
- digest_context.py (digest source queries)
- llm_gateway.py   (pre-provider revalidation)

If the snapshot drifts, one of the call sites diverged — privacy leak risk.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Frozen golden snapshot — the EXACT string that `forget_excludes_sql_fragment()`
# must return.  Any change to the SQL must be intentional and land here first.
# ---------------------------------------------------------------------------
_GOLDEN_SNAPSHOT = (
    "NOT EXISTS (\n"
    "    SELECT 1 FROM forget_events fe\n"
    "    WHERE fe.status IN ('pending', 'processing', 'completed')\n"
    "      AND (\n"
    "          (fe.target_type = 'message' AND fe.target_id = cm.id::text)\n"
    "          OR\n"
    "          (fe.target_type = 'user' AND fe.target_id = cm.user_id::text)\n"
    "          OR\n"
    "          (fe.target_type = 'message_hash' AND fe.target_id = mv.content_hash)\n"
    "      )\n"
    ")"
)


def test_forget_predicate_module_exists() -> None:
    """The module bot.services.forget_predicate must exist and be importable."""
    from bot.services import forget_predicate  # noqa: F401


def test_forget_excludes_sql_fragment_returns_string() -> None:
    """forget_excludes_sql_fragment() must return a non-empty string."""
    from bot.services.forget_predicate import forget_excludes_sql_fragment

    result = forget_excludes_sql_fragment()
    assert isinstance(result, str)
    assert result.strip()


def test_forget_predicate_golden_snapshot() -> None:
    """forget_excludes_sql_fragment() must be byte-identical to the frozen reference.

    This test is a drift guard: if any of the three call sites (forget_cascade,
    digest_context, llm_gateway) diverge from this string, the shared helper is
    no longer used everywhere and the privacy invariant can silently erode.

    If the predicate semantics must change (e.g., new target_type added), update
    BOTH this snapshot AND the three call sites in the same PR.
    """
    from bot.services.forget_predicate import forget_excludes_sql_fragment

    result = forget_excludes_sql_fragment()
    assert result == _GOLDEN_SNAPSHOT, (
        "forget_excludes_sql_fragment() drifted from golden snapshot.\n"
        "If you intentionally changed the predicate, update _GOLDEN_SNAPSHOT "
        "AND all three call sites (forget_cascade.py, digest_context.py, "
        "llm_gateway.py) in the same commit.\n\n"
        f"Expected:\n{_GOLDEN_SNAPSHOT}\n\nGot:\n{result}"
    )


def test_digest_context_uses_shared_predicate() -> None:
    """digest_context.py must import and use forget_predicate._FORGET_EXCLUDES.

    Checks that the source file:
    1. Imports from bot.services.forget_predicate
    2. Embeds _FORGET_EXCLUDES in the complete raw-message query
    """
    import inspect

    from bot.services import digest_context

    source = inspect.getsource(digest_context)

    assert "forget_predicate" in source, (
        "digest_context.py must import from bot.services.forget_predicate (#291). "
        "Replace inline NOT EXISTS clauses with the shared helper."
    )
    # Issue #406 removed the cards query: the full eligible message window is
    # now the only authoritative digest input.
    count = source.count("{_FORGET_EXCLUDES}")
    assert count >= 1, (
        "digest_context.py must reference {_FORGET_EXCLUDES} in its raw-message SQL, "
        f"found {count}."
    )


def test_llm_gateway_uses_shared_predicate() -> None:
    """llm_gateway.py must use both shared SQL and Core predicates.

    Checks that the source file:
    1. Imports from bot.services.forget_predicate
    2. Embeds _FORGET_EXCLUDES in digest message revalidation
    3. Calls forget_excludes_expression() in wiki Core revalidation queries
    """
    import inspect

    from bot.services import llm_gateway

    source = inspect.getsource(llm_gateway)

    assert "forget_predicate" in source, (
        "llm_gateway.py must import from bot.services.forget_predicate (#291). "
        "Replace inline NOT EXISTS clauses with the shared helper."
    )
    digest_revalidation = source[
        source.index("_DIGEST_REVALIDATE_MV_SQL") : source.index(
            "async def _digest_context_is_clean"
        )
    ]
    assert "{_FORGET_EXCLUDES}" in digest_revalidation, (
        "llm_gateway.py digest message revalidation must use {_FORGET_EXCLUDES}."
    )
    assert source.count("forget_excludes_expression()") >= 2, (
        "llm_gateway.py must use forget_excludes_expression() for both wiki "
        "message and card-source revalidation queries."
    )
