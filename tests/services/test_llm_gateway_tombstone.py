"""Regression tests for ``bot.services.llm_gateway`` tombstone gate (FHR P5 follow-up).

Mirrors the T6-02 round 3 regression in ``tests/services/test_extractor.py``:
the live ``MessageRepo.save`` path leaves ``chat_messages.content_hash`` NULL
and only populates ``message_versions.content_hash``. Therefore the
``_TOMBSTONE_GATE_SQL`` ``message_hash:`` predicate MUST match
``mv.content_hash`` (joined), NOT ``c.content_hash`` — otherwise live
content is silently leaked to the LLM under a ``message_hash:`` forget event.

These tests use a real Postgres ``db_session`` so they exercise the actual
SQL produced by ``synthesize_answer`` — the bug is invisible to the existing
``FakeSession``-based tests in ``tests/services/test_llm_gateway.py``.
"""

from __future__ import annotations

import itertools
import uuid as _uuid_module
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from bot.services.evidence import EvidenceBundle
from bot.services.llm_gateway import (
    Abstention,
    LLMGatewayConfig,
    synthesize_answer,
)
from bot.services.llm_providers import ProviderResult
from bot.services.search import SearchHit

pytestmark = pytest.mark.usefixtures("app_env")


_user_counter = itertools.count(start=9_700_000_000)
_msg_counter = itertools.count(start=970_000)
_chat_counter = itertools.count(start=1)
_key_counter = itertools.count(start=1)


def _next_user() -> int:
    return next(_user_counter)


def _next_msg_id() -> int:
    return next(_msg_counter)


def _next_chat_id() -> int:
    return -1_000_000_000_001 - next(_chat_counter)


def _next_tombstone_key(prefix: str) -> str:
    return f"{prefix}:p5fu:test:{next(_key_counter)}"


async def _make_user(db_session) -> int:
    from bot.db.repos.user import UserRepo

    uid = _next_user()
    await UserRepo.upsert(
        db_session,
        telegram_id=uid,
        username=f"u{uid}",
        first_name="Test",
        last_name=None,
    )
    return uid


async def _make_live_chat_message(
    db_session,
    *,
    chat_id: int | None = None,
    user_id: int | None = None,
    when: datetime | None = None,
    text: str = "live message body",
    mv_content_hash: str | None = None,
) -> tuple[int, int, int, int]:
    """Insert a chat_messages + message_versions row.

    Mirrors the live persistence path (``bot/db/repos/message.py::MessageRepo.save``)
    — ``chat_messages.content_hash`` is left NULL and only
    ``message_versions.content_hash`` is set. Tests asserting
    ``message_hash:`` tombstone behaviour against a live row must pass
    ``mv_content_hash`` explicitly.
    """
    from sqlalchemy import update as sa_update

    from bot.db.models import ChatMessage, MessageVersion

    if user_id is None:
        user_id = await _make_user(db_session)
    if chat_id is None:
        chat_id = _next_chat_id()
    if when is None:
        when = datetime.now(timezone.utc)
    message_id = _next_msg_id()

    msg = ChatMessage(
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text=text,
        date=when,
        created_at=when,
        memory_policy="normal",
        is_redacted=False,
    )
    db_session.add(msg)
    await db_session.flush()

    v = MessageVersion(
        chat_message_id=msg.id,
        version_seq=1,
        text=text,
        normalized_text=text,
        entities_json={},
        content_hash=(
            mv_content_hash
            if mv_content_hash is not None
            else f"h{_uuid_module.uuid4().hex[:16]}"
        ),
        is_redacted=False,
    )
    db_session.add(v)
    await db_session.flush()
    await db_session.execute(
        sa_update(ChatMessage)
        .where(ChatMessage.id == msg.id)
        .values(current_version_id=v.id)
    )
    await db_session.flush()
    return msg.id, v.id, chat_id, message_id


def _make_bundle(
    *, message_version_id: int, chat_message_id: int, chat_id: int, message_id: int
) -> EvidenceBundle:
    timestamp = datetime.now(timezone.utc)
    hit = SearchHit(
        message_version_id=message_version_id,
        chat_message_id=chat_message_id,
        chat_id=chat_id,
        message_id=message_id,
        user_id=42,
        snippet="<b>match</b>",
        ts_rank=0.5,
        captured_at=timestamp,
        message_date=timestamp,
    )
    return EvidenceBundle.from_hits("query", chat_id, [hit])


def _config() -> LLMGatewayConfig:
    return LLMGatewayConfig(
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        daily_ceiling_usd=Decimal("5.00"),
        monthly_ceiling_usd=Decimal("50.00"),
        prompt_template_version="v1",
    )


@dataclass
class _RecordingProvider:
    """Asserts the provider was NEVER called when the tombstone gate fires."""

    calls: list[dict[str, Any]] = field(default_factory=list)

    async def call(self, *, prompt: str, model: str) -> ProviderResult:  # pragma: no cover
        self.calls.append({"prompt": prompt, "model": model})
        return ProviderResult(
            answer_text="should not happen",
            citation_ids=(),
            tokens_in=0,
            tokens_out=0,
            request_id="never",
            raw_latency_ms=0,
        )


# ─── Regression: tombstone via mv.content_hash for live-path messages ────────


async def test_synthesize_answer_excludes_live_message_via_mv_content_hash_tombstone(
    db_session,
) -> None:
    """Regression for FHR Phase 5 follow-up CRITICAL.

    The live ``MessageRepo.save`` path leaves ``chat_messages.content_hash``
    NULL and only populates ``message_versions.content_hash``. The
    ``_TOMBSTONE_GATE_SQL`` ``message_hash:`` predicate MUST therefore
    match ``mv.content_hash`` (joined), NOT ``c.content_hash`` —
    otherwise a ``message_hash:<X>`` forget event fails to invalidate the
    live row and the gateway leaks the forgotten content to the LLM.

    Acceptance: ``synthesize_answer`` returns
    ``Abstention(reason='forget_invalidated')`` and the provider is NOT
    invoked.
    """
    from bot.db.repos.forget_event import ForgetEventRepo
    from bot.db.repos.llm_synthesis_cache import SynthesisCacheRepo
    from bot.db.repos.llm_usage_ledger import LedgerRepo

    when = datetime.now(timezone.utc)
    mv_hash = "live_msg_sha_for_p5fu_tombstone_test"

    cm_id, ver_live, chat_id, message_id = await _make_live_chat_message(
        db_session,
        when=when,
        text="DO_NOT_LEAK_LIVE_HASH",
        mv_content_hash=mv_hash,
    )

    # Insert forget_event keyed by message_hash matching the MV's content_hash.
    # If the SQL incorrectly uses chat_messages.content_hash (NULL) — the
    # match fails and the gateway leaks ver_live to the LLM. The fix uses
    # mv.content_hash (NOT NULL by schema).
    await ForgetEventRepo.create(
        db_session,
        target_type="message_hash",
        target_id=mv_hash,
        actor_user_id=None,
        authorized_by="admin",
        tombstone_key=f"message_hash:{mv_hash}",
    )

    bundle = _make_bundle(
        message_version_id=ver_live,
        chat_message_id=cm_id,
        chat_id=chat_id,
        message_id=message_id,
    )
    provider = _RecordingProvider()

    result = await synthesize_answer(
        db_session,
        bundle=bundle,
        query="anything",
        config=_config(),
        qa_trace_id=None,
        ledger_repo=LedgerRepo(),
        cache_repo=SynthesisCacheRepo(),
        provider=provider,
    )

    assert isinstance(result, Abstention)
    assert result.reason == "forget_invalidated"
    # Provider MUST NOT have been called — tombstone gate fires before dispatch.
    assert provider.calls == []
