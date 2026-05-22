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
    2. Embeds _FORGET_EXCLUDES at least twice (cards query + raw fallback query)
    """
    import inspect

    from bot.services import digest_context

    source = inspect.getsource(digest_context)

    assert "forget_predicate" in source, (
        "digest_context.py must import from bot.services.forget_predicate (#291). "
        "Replace inline NOT EXISTS clauses with the shared helper."
    )
    # Must reference _FORGET_EXCLUDES at least twice in SQL f-strings
    count = source.count("{_FORGET_EXCLUDES}")
    assert count >= 2, (
        f"digest_context.py must reference {{_FORGET_EXCLUDES}} in SQL at least twice "
        f"(cards query + raw fallback), found {count}. "
        "Ensure both inline NOT EXISTS clauses are replaced."
    )


def test_llm_gateway_uses_shared_predicate() -> None:
    """llm_gateway.py must import and use forget_predicate._FORGET_EXCLUDES.

    Checks that the source file:
    1. Imports from bot.services.forget_predicate
    2. Embeds _FORGET_EXCLUDES at least twice (_DIGEST_REVALIDATE_MV_SQL + _CS_SQL)
    """
    import inspect

    from bot.services import llm_gateway

    source = inspect.getsource(llm_gateway)

    assert "forget_predicate" in source, (
        "llm_gateway.py must import from bot.services.forget_predicate (#291). "
        "Replace inline NOT EXISTS clauses with the shared helper."
    )
    # Must reference _FORGET_EXCLUDES at least twice in SQL f-strings
    count = source.count("{_FORGET_EXCLUDES}")
    assert count >= 2, (
        f"llm_gateway.py must reference {{_FORGET_EXCLUDES}} in SQL at least twice "
        f"(_DIGEST_REVALIDATE_MV_SQL + _DIGEST_REVALIDATE_CS_SQL), found {count}. "
        "Ensure both inline NOT EXISTS clauses are replaced."
    )
