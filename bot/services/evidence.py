"""Frozen evidence bundle contract for Phase 4 retrieval consumers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol


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
