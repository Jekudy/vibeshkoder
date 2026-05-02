"""Citation invariant tests for the Phase 4 recall / evidence pipeline (T11-W2-02).

Four sub-cases verified against a live seeded database:

  C1 every-item-has-id    — each EvidenceItem.message_version_id is a positive int
                            that resolves to an existing message_versions row.
  C2 cited-row-visible    — the parent chat_messages row for each cited version has
                            memory_policy='normal' AND is_redacted=FALSE.
  C3 cited-not-tombstoned — no forget_events tombstone covers any cited message.
  C4 audit-trace-matches  — evidence_ids written to qa_traces match the bundle.
"""
from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select

from bot.db.models import ChatMessage, ForgetEvent, MessageVersion, QaTrace
from bot.db.repos.qa_trace import QaTraceRepo
from bot.services.eval_runner import run_eval_recall
from tests.evals.conftest import SEED_CHAT_ID, Seed

# ---------------------------------------------------------------------------
# Module-level seed query used for all citation checks.
# q_01 is a single-hit answerable query — deterministic enough for invariants.
# ---------------------------------------------------------------------------
_CITATION_QUERY = "Когда будет воркшоп по Postgres FTS?"
_CITATION_USER_TG_ID = 999_000_001  # synthetic user id, never in users table


@pytest.mark.usefixtures("eval_app_env")
class TestCitationInvariants:
    """Citation invariant checks — C1 through C4.

    Uses class-scoped DB fixtures (eval_db_session + golden_recall_seed) so that
    the seed is loaded once and all four cases share the same transaction.
    """

    @pytest_asyncio.fixture(scope="class")
    async def mv_ids(
        self,
        eval_db_session: Any,
        golden_recall_seed: Seed,
    ) -> list[int]:
        """Run recall once and expose the retrieved message_version_ids."""
        bundle, _trace = await run_eval_recall(
            eval_db_session,
            query=_CITATION_QUERY,
            chat_id=SEED_CHAT_ID,
        )
        assert not bundle.abstained, (
            "Bundle abstained — seed data may not have been inserted correctly"
        )
        return bundle.evidence_ids

    # ------------------------------------------------------------------
    # C1 — every cited message_version_id resolves in message_versions
    # ------------------------------------------------------------------
    async def test_c1_every_item_has_id(
        self,
        eval_db_session: Any,
        mv_ids: list[int],
    ) -> None:
        assert mv_ids, "Pre-condition: bundle must have at least one evidence item"

        for mv_id in mv_ids:
            assert isinstance(mv_id, int) and mv_id > 0, (
                f"message_version_id must be a positive int, got {mv_id!r}"
            )
            row = await eval_db_session.scalar(
                select(MessageVersion).where(MessageVersion.id == mv_id)
            )
            assert row is not None, (
                f"message_version_id={mv_id} not found in message_versions table"
            )

    # ------------------------------------------------------------------
    # C2 — parent chat_messages row is visible (normal policy, not redacted)
    # ------------------------------------------------------------------
    async def test_c2_cited_row_visible(
        self,
        eval_db_session: Any,
        mv_ids: list[int],
    ) -> None:
        assert mv_ids, "Pre-condition: bundle must have at least one evidence item"

        for mv_id in mv_ids:
            mv = await eval_db_session.scalar(
                select(MessageVersion).where(MessageVersion.id == mv_id)
            )
            assert mv is not None, f"message_version_id={mv_id} missing (C2 pre-check)"

            cm = await eval_db_session.scalar(
                select(ChatMessage).where(ChatMessage.id == mv.chat_message_id)
            )
            assert cm is not None, (
                f"chat_messages row missing for chat_message_id={mv.chat_message_id} "
                f"(mv_id={mv_id})"
            )
            assert cm.memory_policy == "normal", (
                f"chat_messages.id={cm.id} has memory_policy={cm.memory_policy!r}, "
                f"expected 'normal' (mv_id={mv_id})"
            )
            assert cm.is_redacted is False, (
                f"chat_messages.id={cm.id} is_redacted=True (mv_id={mv_id})"
            )

    # ------------------------------------------------------------------
    # C3 — no forget_events tombstone covers any cited message
    # ------------------------------------------------------------------
    async def test_c3_cited_row_not_tombstoned(
        self,
        eval_db_session: Any,
        mv_ids: list[int],
    ) -> None:
        assert mv_ids, "Pre-condition: bundle must have at least one evidence item"

        for mv_id in mv_ids:
            mv = await eval_db_session.scalar(
                select(MessageVersion).where(MessageVersion.id == mv_id)
            )
            assert mv is not None, f"message_version_id={mv_id} missing (C3 pre-check)"

            cm = await eval_db_session.scalar(
                select(ChatMessage).where(ChatMessage.id == mv.chat_message_id)
            )
            assert cm is not None, (
                f"chat_messages row missing for chat_message_id={mv.chat_message_id} "
                f"(mv_id={mv_id})"
            )

            # Tombstone key format: message:<chat_id>:<message_id>
            tombstone_key = f"message:{cm.chat_id}:{cm.message_id}"
            forget_row = await eval_db_session.scalar(
                select(ForgetEvent).where(
                    ForgetEvent.tombstone_key == tombstone_key
                )
            )
            assert forget_row is None, (
                f"forget_events tombstone {tombstone_key!r} exists for "
                f"mv_id={mv_id} — cited message is tombstoned"
            )

    # ------------------------------------------------------------------
    # C4 — qa_traces.evidence_ids matches the bundle after audit write
    # ------------------------------------------------------------------
    async def test_c4_audit_trace_matches(
        self,
        eval_db_session: Any,
        mv_ids: list[int],
    ) -> None:
        assert mv_ids, "Pre-condition: bundle must have at least one evidence item"

        # Write a qa_trace the same way the production handler does.
        trace = await QaTraceRepo.create(
            eval_db_session,
            user_tg_id=_CITATION_USER_TG_ID,
            chat_id=SEED_CHAT_ID,
            query=_CITATION_QUERY,
            evidence_ids=list(mv_ids),
            abstained=False,
            redact_query=False,
        )
        await eval_db_session.flush()

        # Reload from DB to verify persistence (not just ORM cache).
        persisted = await eval_db_session.scalar(
            select(QaTrace).where(QaTrace.id == trace.id)
        )
        assert persisted is not None, "QaTrace row was not persisted"
        assert sorted(persisted.evidence_ids) == sorted(mv_ids), (
            f"qa_traces.evidence_ids={persisted.evidence_ids!r} does not match "
            f"bundle.evidence_ids={mv_ids!r}"
        )
