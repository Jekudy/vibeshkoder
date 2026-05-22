"""G3 binding test — graph_provenance XOR constraint (migration 067).

Spec: 10.5-9 — ck_graph_provenance_has_source tightened from OR to XOR.

G3a: Insert row with only source_message_version_id → succeeds.
G3b: Insert row with only source_card_id → succeeds.
G3c: Insert row with BOTH non-NULL → fails with IntegrityError (XOR violated).
G3d: Insert row with NEITHER → fails (existing OR constraint or NOT NULL logic).

DB-backed test — skipped if postgres unreachable.
"""

from __future__ import annotations

import itertools
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.usefixtures("app_env")

_counter = itertools.count(start=67_000_000)


def _next_id() -> int:
    return next(_counter)


async def _make_user(db_session) -> int:
    from bot.db.repos.user import UserRepo

    uid = _next_id()
    await UserRepo.upsert(
        db_session,
        telegram_id=uid,
        username=f"u{uid}",
        first_name="Test",
        last_name=None,
    )
    return uid


async def _make_message_version(db_session) -> tuple[int, int]:
    """Create a ChatMessage + MessageVersion. Returns (cm_id, mv_id)."""
    from bot.db.models import ChatMessage, MessageVersion

    uid = await _make_user(db_session)
    chat_id = -1_000_000_000_000 - _next_id()
    msg_id = _next_id()
    when = datetime.now(timezone.utc)

    msg = ChatMessage(
        message_id=msg_id,
        chat_id=chat_id,
        user_id=uid,
        text="xor test",
        date=when,
        raw_json={"text": "xor test"},
        memory_policy="normal",
        is_redacted=False,
    )
    db_session.add(msg)
    await db_session.flush()

    ver = MessageVersion(
        chat_message_id=msg.id,
        version_seq=1,
        text="xor test",
        normalized_text="xor test",
        entities_json={"entities": []},
        content_hash=f"h-g3-{_next_id()}",
        is_redacted=False,
    )
    db_session.add(ver)
    await db_session.flush()

    msg.current_version_id = ver.id
    await db_session.flush()
    return msg.id, ver.id


async def _make_knowledge_card(db_session) -> uuid.UUID:
    """Create a minimal knowledge_card row (draft status). Returns card_id."""
    from bot.db.models import KnowledgeCard

    card_id = uuid.uuid4()
    card = KnowledgeCard(
        id=card_id,
        title="XOR test card",
        body_markdown="body",
        card_status="draft",
    )
    db_session.add(card)
    await db_session.flush()
    return card_id


async def _make_projection_run(db_session) -> int:
    from bot.db.repos.graph_projection_run import create_run

    run = await create_run(db_session, mode="incremental", started_by="test")
    await db_session.flush()
    return run.id


class TestGraphProvenanceXOR:
    """G3: XOR constraint on graph_provenance — only one source FK may be non-NULL."""

    async def test_g3a_message_version_only_succeeds(self, db_session) -> None:
        """G3a: Insert with only source_message_version_id set → succeeds."""
        from bot.db.models import GraphProvenance

        _cm_id, mv_id = await _make_message_version(db_session)
        run_id = await _make_projection_run(db_session)

        prov = GraphProvenance(
            projection_run_id=run_id,
            source_table="message_versions",
            source_pk=str(mv_id),
            source_message_version_id=mv_id,
            source_card_id=None,
            graph_node_key=f"msg:{mv_id}",
            triple_hash=f"th-g3a-{_next_id()}",
            governance_policy="normal",
        )
        db_session.add(prov)
        await db_session.flush()  # must not raise
        assert prov.id is not None, "G3a: provenance row was not persisted"

    async def test_g3b_card_only_succeeds(self, db_session) -> None:
        """G3b: Insert with only source_card_id set → succeeds."""
        from bot.db.models import GraphProvenance

        card_id = await _make_knowledge_card(db_session)
        run_id = await _make_projection_run(db_session)

        prov = GraphProvenance(
            projection_run_id=run_id,
            source_table="knowledge_cards",
            source_pk=str(card_id),
            source_message_version_id=None,
            source_card_id=card_id,
            graph_node_key=f"card:{card_id}",
            triple_hash=f"th-g3b-{_next_id()}",
            governance_policy="normal",
        )
        db_session.add(prov)
        await db_session.flush()  # must not raise
        assert prov.id is not None, "G3b: provenance row was not persisted"

    async def test_g3c_both_sources_violates_xor(self, db_session) -> None:
        """G3c: Insert with BOTH source_message_version_id AND source_card_id → IntegrityError."""
        from bot.db.models import GraphProvenance

        _cm_id, mv_id = await _make_message_version(db_session)
        card_id = await _make_knowledge_card(db_session)
        run_id = await _make_projection_run(db_session)

        prov = GraphProvenance(
            projection_run_id=run_id,
            source_table="message_versions",
            source_pk=str(mv_id),
            source_message_version_id=mv_id,
            source_card_id=card_id,
            graph_node_key=f"msg:{mv_id}",
            triple_hash=f"th-g3c-{_next_id()}",
            governance_policy="normal",
        )
        db_session.add(prov)
        with pytest.raises(IntegrityError, match="ck_graph_provenance_has_source"):
            await db_session.flush()

    async def test_g3d_neither_source_violates_constraint(self, db_session) -> None:
        """G3d: Insert with NEITHER source set → IntegrityError (old OR also catches this)."""
        from bot.db.models import GraphProvenance

        run_id = await _make_projection_run(db_session)

        prov = GraphProvenance(
            projection_run_id=run_id,
            source_table="message_versions",
            source_pk="0",
            source_message_version_id=None,
            source_card_id=None,
            graph_node_key="msg:0",
            triple_hash=f"th-g3d-{_next_id()}",
            governance_policy="normal",
        )
        db_session.add(prov)
        with pytest.raises(IntegrityError):
            await db_session.flush()
