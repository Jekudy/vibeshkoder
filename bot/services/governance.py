"""Governance stub (T1-04 placeholder).

This module's contract is established by T1-04 so the ``#offrecord`` ordering rule from
``docs/memory-system/AUTHORIZED_SCOPE.md`` is satisfied from day one:

    Detection runs in the same DB transaction as the raw insert, BEFORE commit.

T1-04 ships a stub that always returns ``('normal', None)``. T1-12 replaces the stub
with the real ``#nomem`` / ``#offrecord`` deterministic detector over text and caption,
plus the ``offrecord_marks`` row creation (T1-13). When that swap happens, the
ingestion pipeline (``bot/services/ingestion.py``) does NOT need to change — the
detector signature is fixed and the redaction wiring is already in place.

Do not add LLM calls here. Detection is and remains deterministic.
"""

from __future__ import annotations

from typing import Literal

PolicyOutcome = Literal["normal", "nomem", "offrecord"]


def detect_policy(
    text: str | None, caption: str | None
) -> tuple[PolicyOutcome, dict | None]:
    """Return ``('normal', None)`` for any input.

    Replaced by T1-12 with real detection. The second return value carries an
    ``offrecord_marks`` payload dict when the policy is ``'offrecord'`` so the caller
    can persist the audit row in the same transaction.
    """
    # Stub — see module docstring. Args intentionally unused.
    del text, caption
    return ("normal", None)


def redact_raw_for_offrecord(raw_json: dict | None) -> dict | None:
    """Return a sanitized copy of ``raw_json`` with content fields removed.

    Used by ``bot/services/ingestion.py`` when ``detect_policy`` returns ``'offrecord'``.
    The T1-04 stub detector never returns ``'offrecord'``, so this function is currently
    a defensive helper waiting for T1-12. Implementation strategy when T1-12 lands:

    - Drop ``message.text``, ``message.caption``, ``message.entities``,
      ``edited_message.text`` / ``.caption`` / ``.entities``, ``callback_query.data`` —
      anything that carries user content
    - Keep ids, timestamps, message_kind, raw_hash, redaction marker

    The function takes and returns a dict (not a SQLAlchemy row) so it can be unit-tested
    without a DB.
    """
    if raw_json is None:
        return None
    # T1-04 stub: pass through. T1-12 will implement the redaction.
    # Until then, this branch is unreachable from production code because the stub
    # detector never returns 'offrecord'.
    return raw_json
