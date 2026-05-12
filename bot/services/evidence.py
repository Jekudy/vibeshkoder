"""Frozen evidence bundle contract for Phase 4 retrieval consumers.

Phase 6 (T6-07) extension: ``EvidenceItem`` carries a ``source_type`` field
discriminating message-version hits from approved-knowledge-card hits. Card
hits additionally expose ``card_id`` and ``card_source_message_version_ids``
so the consumer (``bot/handlers/qa.py`` rendering, the LLM gateway citation
filter) can render a back-citation trace per ``PHASE6_PLAN.md §1`` invariant #4.

The new fields default to message-shape values so every Phase 4 caller (search
hits, tests, fakes) constructs an ``EvidenceItem`` unchanged.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol


class SearchHitLike(Protocol):
    message_version_id: int
    chat_message_id: int
    chat_id: int
    message_id: int
    user_id: int | None
    snippet: str
    ts_rank: float
    captured_at: datetime
    message_date: datetime


@dataclass(frozen=True, slots=True)  # type: ignore[call-overload]
class EvidenceItem:
    message_version_id: int
    chat_message_id: int
    chat_id: int
    message_id: int
    user_id: int | None
    snippet: str
    ts_rank: float
    captured_at: datetime
    message_date: datetime
    # T6-07: source-type discriminator. Defaults trip for Phase 4 callers so
    # existing construction sites (search hits, test fakes) are unchanged.
    # For card hits, ``message_version_id`` is the anchor source's mvid
    # (lowest-position ``card_sources`` row); the FULL source-mvid set is
    # surfaced via ``card_source_message_version_ids`` for the renderer.
    source_type: Literal["message", "card"] = "message"
    card_id: uuid.UUID | None = None
    card_source_message_version_ids: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "message_version_id": self.message_version_id,
            "chat_message_id": self.chat_message_id,
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "user_id": self.user_id,
            "snippet": self.snippet,
            "ts_rank": self.ts_rank,
            "captured_at": self.captured_at.isoformat(),
            "message_date": self.message_date.isoformat(),
            "source_type": self.source_type,
            "card_id": str(self.card_id) if self.card_id is not None else None,
            "card_source_message_version_ids": list(
                self.card_source_message_version_ids
            ),
        }


@dataclass(frozen=True, slots=True)  # type: ignore[call-overload]
class EvidenceBundle:
    query: str
    chat_id: int
    items: tuple[EvidenceItem, ...]
    abstained: bool
    created_at: datetime

    @classmethod
    def from_hits(
        cls,
        query: str,
        chat_id: int,
        hits: Sequence[SearchHitLike],
    ) -> EvidenceBundle:
        items = tuple(
            EvidenceItem(
                message_version_id=hit.message_version_id,
                chat_message_id=hit.chat_message_id,
                chat_id=hit.chat_id,
                message_id=hit.message_id,
                user_id=hit.user_id,
                snippet=hit.snippet,
                ts_rank=hit.ts_rank,
                captured_at=hit.captured_at,
                message_date=hit.message_date,
                # T6-07 cross-stream contract: T6-06 ``SearchHit`` exposes
                # ``source_type`` / ``card_id`` / ``card_source_message_version_ids``;
                # pre-T6-06 fakes don't have them — ``getattr`` defaults trip
                # to message-shape values.
                source_type=getattr(hit, "source_type", "message"),
                card_id=getattr(hit, "card_id", None),
                card_source_message_version_ids=tuple(
                    getattr(hit, "card_source_message_version_ids", ())
                ),
            )
            for hit in hits
        )
        return cls(
            query=query,
            chat_id=chat_id,
            items=items,
            abstained=len(items) == 0,
            created_at=datetime.now(timezone.utc),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "query": self.query,
            "chat_id": self.chat_id,
            "items": [item.to_dict() for item in self.items],
            "abstained": self.abstained,
            "created_at": self.created_at.isoformat(),
        }

    @property
    def evidence_ids(self) -> list[int]:
        return [item.message_version_id for item in self.items]
