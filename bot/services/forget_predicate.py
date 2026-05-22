"""Shared SQL predicate for excluding forgotten content from any context.

Privacy-critical: this module is the SINGLE source of truth across:
- forget_cascade.py  (cascade worker — _cascade_message_versions)
- digest_context.py  (digest source queries — cards + raw fallback)
- llm_gateway.py     (pre-provider revalidation — _DIGEST_REVALIDATE_*_SQL)

If you need to change the predicate semantics (e.g., add a new target_type),
change this module — do NOT inline new logic in caller sites.  Any change here
must also update the golden-snapshot in tests/evals/test_forget_predicate_parity.py
and all three call sites in the same commit.

Issue #291 tracks the consolidation of three textual copies into this module.
"""
from __future__ import annotations


def forget_excludes_sql_fragment() -> str:
    """Return the SQL NOT EXISTS clause that excludes rows with active forget events.

    The returned string is a SQL fragment that can be embedded verbatim into a
    WHERE clause.  It references table aliases ``cm`` (chat_messages) and ``mv``
    (message_versions) which must be in scope in the outer query.

    Semantics: a message_version row is excluded if there is any forget_event in
    status 'pending', 'processing', or 'completed' that targets it via:
      - target_type='message'       AND target_id = cm.id  (single chat message)
      - target_type='user'          AND target_id = cm.user_id  (all user messages)
      - target_type='message_hash'  AND target_id = mv.content_hash  (hash-based)

    'completed' is included because completed events are durable: digests must
    respect them even after the cascade has finished flipping is_redacted to TRUE.
    The is_redacted=FALSE filter alone is insufficient for 'pending'/'processing'
    states where the cascade has not yet run.

    Returns:
        A non-empty SQL string starting with ``NOT EXISTS (``.
    """
    return (
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
