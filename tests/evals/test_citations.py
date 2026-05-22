"""Citation invariant tests for the Phase 4 / Phase 6 recall pipeline.

Phase 4 cases (T11-W2-02, message-only bundles):

  C1 every-item-has-id    — each EvidenceItem.message_version_id is a positive int
                            that resolves to an existing message_versions row.
  C2 cited-row-visible    — the parent chat_messages row for each cited version has
                            memory_policy='normal' AND is_redacted=FALSE.
  C3 cited-not-tombstoned — no forget_events tombstone covers any cited message.
  C4 audit-trace-matches  — evidence_ids written to qa_traces match the bundle.

Phase 6 cases (T6-06 / T6-07, card-discriminator bundles):

  C5 card-citation-trace  — every card EvidenceItem carries card_id != None AND
                            a non-empty card_source_message_version_ids tuple; the
                            anchor mvid (EvidenceItem.message_version_id) appears
                            in the source list; every source mvid resolves to a
                            non-redacted message_versions row whose parent
                            chat_messages row has memory_policy='normal' and
                            is_redacted=FALSE.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select

from bot.db.models import (
    CardSource,
    ChatMessage,
    ForgetEvent,
    KnowledgeCard,
    MessageVersion,
    QaTrace,
)
from bot.services.eval_runner import run_eval_recall
from tests.evals.conftest import SEED_CHAT_ID, Seed

# ---------------------------------------------------------------------------
# Module-level seed query used for all citation checks.
# q_01 is a single-hit answerable query — deterministic enough for invariants.
# ---------------------------------------------------------------------------
_CITATION_QUERY = "Когда будет воркшоп по Postgres FTS?"
_CITATION_USER_TG_ID = 999_000_001  # synthetic user id, never in users table


pytestmark = pytest.mark.asyncio(loop_scope="class")


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

        # Call the SAME `_write_trace` helper the production handler uses
        # (bot/handlers/qa.py::_write_trace) — guarantees C4 catches a future
        # divergence between handler trace-write and bundle.evidence_ids.
        # If a refactor changes the helper signature or the persisted shape,
        # this test fails immediately rather than silently letting the
        # synthetic duplication drift.
        from bot.handlers.qa import _write_trace

        await _write_trace(
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
            select(QaTrace)
            .where(QaTrace.user_tg_id == _CITATION_USER_TG_ID)
            .where(QaTrace.chat_id == SEED_CHAT_ID)
            .order_by(QaTrace.id.desc())
            .limit(1)
        )
        assert persisted is not None, "QaTrace row was not persisted via handler helper"
        assert sorted(persisted.evidence_ids) == sorted(mv_ids), (
            f"qa_traces.evidence_ids={persisted.evidence_ids!r} does not match "
            f"bundle.evidence_ids={mv_ids!r}"
        )
        # Verify the row shape written by the REAL handler helper: abstained=False
        # for a non-empty bundle, and query text preserved verbatim.
        assert persisted.abstained is False, (
            "QaTrace.abstained must be False for a non-empty evidence bundle"
        )
        assert persisted.query_text == _CITATION_QUERY, (
            f"QaTrace.query_text={persisted.query_text!r} does not match query sent to handler"
        )


# ---------------------------------------------------------------------------
# T6-06 / T6-07 card citation invariant — C5.
#
# Phase 11 binding sub-gate: when a card is returned by /recall, the back-
# citation trace must point at non-redacted, governance-clean messages. The
# anchor mvid (EvidenceItem.message_version_id) must be a member of the
# card_source_message_version_ids tuple. Every mvid in that tuple must
# resolve to a non-redacted message_versions row.
# ---------------------------------------------------------------------------

# Function-scoped (so cleanup runs per test) — C5 owns its own card so we
# don't have to coordinate with the class-scope golden_recall_seed fixture.
_C5_QUERY = "карточка-cyrus уникальное диковина15"
_C5_TOPIC = "карточка-cyrus уникальное диковина15 для теста"


@pytest.mark.asyncio(loop_scope="class")
@pytest.mark.usefixtures("eval_app_env")
class TestPhase6CardCitationInvariants:
    """C5: approved-card hits must surface a complete, intact source trace.

    A single approved card + 3 governance-clean source messages is seeded
    once at class-scope via the ``c5_seed`` fixture (mirrors the C1-C4
    class-scoped pattern). All four sub-cases (C5a/b/c/d) share the seed.
    Cleanup happens implicitly when ``eval_db_session`` rolls back its
    outer transaction at class teardown.
    """

    @pytest_asyncio.fixture(scope="class")
    async def c5_seed(
        self,
        eval_db_session: Any,
        golden_recall_seed: Seed,
    ) -> tuple[Any, list[int]]:
        """Seed: 3 governance-clean messages + 1 approved knowledge_card
        wiring them as card_sources in position order.

        The ``golden_recall_seed`` dependency is intentional: it primes the
        DB to the same state the message-only suite expects (so the C5
        cleanup at the start doesn't strip the C1-C4 seed). We rely on
        message_id collision avoidance — C5 uses 12_400+ while seed_v1
        uses lower ranges (see seed_v1/chat_history.jsonl).
        """
        _ = golden_recall_seed  # ensure C1-C4 seed loaded first
        from bot.db.repos.user import UserRepo

        admin_id = 950_000_001
        await UserRepo.upsert(
            eval_db_session,
            telegram_id=admin_id,
            username=f"c5_admin_{admin_id}",
            first_name="C5Admin",
            last_name=None,
        )

        mvids: list[int] = []
        for idx in range(3):
            user_id = 951_000_000 + idx
            await UserRepo.upsert(
                eval_db_session,
                telegram_id=user_id,
                username=f"c5_user_{idx}",
                first_name="C5",
                last_name=None,
            )
            cm = ChatMessage(
                message_id=12_400 + idx,
                chat_id=SEED_CHAT_ID,
                user_id=user_id,
                text=f"источник-{idx} {_C5_TOPIC}",
                caption=None,
                date=datetime.now(timezone.utc),
                memory_policy="normal",
                is_redacted=False,
                content_hash=f"c5-hash-{idx}",
            )
            eval_db_session.add(cm)
            await eval_db_session.flush()
            mv = MessageVersion(
                chat_message_id=cm.id,
                version_seq=1,
                text=cm.text,
                caption=None,
                normalized_text=cm.text,
                content_hash=cm.content_hash,
                is_redacted=False,
            )
            eval_db_session.add(mv)
            await eval_db_session.flush()
            cm.current_version_id = mv.id
            await eval_db_session.flush()
            mvids.append(mv.id)

        card = KnowledgeCard(
            title="C5 Card",
            body_markdown=_C5_TOPIC,
            card_status="approved",
            approved_by_user_id=admin_id,
            approved_at=datetime.now(timezone.utc),
        )
        eval_db_session.add(card)
        await eval_db_session.flush()
        for position, mvid in enumerate(mvids):
            eval_db_session.add(
                CardSource(card_id=card.id, message_version_id=mvid, position=position)
            )
        await eval_db_session.flush()

        return card.id, mvids

    @pytest_asyncio.fixture(scope="class")
    async def c5_bundle(
        self,
        eval_db_session: Any,
        c5_seed: tuple[Any, list[int]],
    ) -> Any:
        """Run /recall once at class-scope and expose the bundle."""
        _ = c5_seed
        bundle, _trace = await run_eval_recall(
            eval_db_session, query=_C5_QUERY, chat_id=SEED_CHAT_ID
        )
        assert not bundle.abstained, "C5 seed must yield at least one card hit"
        return bundle

    async def test_c5a_card_item_has_card_id(
        self,
        c5_bundle: Any,
        c5_seed: tuple[Any, list[int]],
    ) -> None:
        card_id, _ = c5_seed
        card_items = [item for item in c5_bundle.items if item.source_type == "card"]
        assert card_items, "Expected at least one card hit in bundle"
        match = next((c for c in card_items if c.card_id == card_id), None)
        assert match is not None, f"Inserted card {card_id} did not surface"
        assert match.card_id is not None

    async def test_c5b_card_source_list_non_empty_and_ordered(
        self,
        c5_bundle: Any,
        c5_seed: tuple[Any, list[int]],
    ) -> None:
        card_id, expected_mvids = c5_seed
        card_items = [
            c for c in c5_bundle.items
            if c.source_type == "card" and c.card_id == card_id
        ]
        assert card_items, "Expected our seeded card to surface"
        match = card_items[0]
        assert match.card_source_message_version_ids, (
            "C5b: card_source_message_version_ids must be non-empty"
        )
        assert tuple(match.card_source_message_version_ids) == tuple(expected_mvids), (
            "Source list must preserve insertion order (position ASC)"
        )
        # Anchor mvid is the lowest-position source.
        assert match.message_version_id == expected_mvids[0], (
            "C5: anchor mvid must be the first card_sources entry"
        )

    async def test_c5c_card_sources_resolve_to_visible_message_versions(
        self,
        eval_db_session: Any,
        c5_bundle: Any,
        c5_seed: tuple[Any, list[int]],
    ) -> None:
        """Every source mvid resolves to non-redacted, normal-policy rows; no
        forget_event tombstones cover them."""
        card_id, _ = c5_seed
        card_items = [
            c for c in c5_bundle.items
            if c.source_type == "card" and c.card_id == card_id
        ]
        assert card_items
        match = card_items[0]
        for mvid in match.card_source_message_version_ids:
            mv = await eval_db_session.scalar(
                select(MessageVersion).where(MessageVersion.id == mvid)
            )
            assert mv is not None, f"Source mvid {mvid} missing"
            assert mv.is_redacted is False, (
                f"Source mvid {mvid} is is_redacted=True — C5c violation"
            )
            cm = await eval_db_session.scalar(
                select(ChatMessage).where(ChatMessage.id == mv.chat_message_id)
            )
            assert cm is not None
            assert cm.memory_policy == "normal", (
                f"Source chat_message has memory_policy={cm.memory_policy!r}"
            )
            assert cm.is_redacted is False
            fe_tomb = await eval_db_session.scalar(
                select(ForgetEvent).where(
                    ForgetEvent.tombstone_key
                    == f"message:{cm.chat_id}:{cm.message_id}"
                )
            )
            assert fe_tomb is None, f"Forget event tombstones source mvid {mvid}"

    async def test_c5d_evidence_ids_preserves_mvid_only_contract(
        self,
        c5_bundle: Any,
    ) -> None:
        """``EvidenceBundle.evidence_ids`` returns mvids only — Phase 5
        gateway contract preserved across the discriminator boundary."""
        for mvid in c5_bundle.evidence_ids:
            assert isinstance(mvid, int), (
                f"C5d: evidence_ids must be int (mvid), got {type(mvid).__name__}"
            )
            assert mvid > 0
