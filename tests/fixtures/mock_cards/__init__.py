"""Mock factory for knowledge_cards rows.

Returns plain dicts that match the ratified Phase 6 knowledge_cards schema exactly
(PHASE6_PLAN_DRAFT.md lines 178-197). No ORM objects — the knowledge_cards table
does not exist yet (Phase 6 not closed). Real ORM integration happens at Sprint 0c
promotion.

Schema reference (from 032_add_knowledge_cards migration):
  - id uuid primary key
  - title text not null
  - body_markdown text not null
  - body_tsv  (generated, not in dict — DB-side computed column)
  - source_message_version_ids jsonb not null default '[]'
  - card_status text not null  CHECK IN ('draft','approved','archived','deprecated')
  - approved_by_user_id bigint references users(id) on delete set null
  - approved_at timestamptz  (null for non-approved cards)
  - created_at timestamptz not null default now()
  - updated_at timestamptz not null default now()

Constraints (enforced by make_* factories):
  - source_message_version_ids must be a non-empty JSON array before card_status='approved'
  - card_status='approved' implies approved_by_user_id is not null and approved_at is not null
  - card_status!='approved' rows are not citation-eligible
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4


def make_approved_card(
    body_markdown: str,
    source_message_version_ids: list[int],
    title: str = "Test Card",
    approved_by_user_id: int | None = 1,
) -> dict:
    """Return a dict matching the Phase 6 knowledge_cards schema for an approved card.

    Invariant: approved cards must have non-empty source_message_version_ids and
    a non-null approved_by_user_id.

    Raises:
        ValueError: if source_message_version_ids is empty (violates schema constraint).
    """
    if not source_message_version_ids:
        raise ValueError(
            "source_message_version_ids must be non-empty for an approved card "
            "(constraint: card_status='approved' requires non-empty sources)"
        )
    if approved_by_user_id is None:
        raise ValueError(
            "approved_by_user_id must be non-null for card_status='approved' "
            "(constraint from PHASE6_PLAN_DRAFT.md line 196)"
        )

    now = datetime.now(UTC)
    return {
        "id": uuid4(),
        "title": title,
        "body_markdown": body_markdown,
        "card_status": "approved",
        "source_message_version_ids": source_message_version_ids,
        "approved_by_user_id": approved_by_user_id,
        "approved_at": now,
        "created_at": now,
        "updated_at": now,
    }


def make_draft_card(
    body_markdown: str,
    source_message_version_ids: list[int],
    title: str = "Draft Card",
) -> dict:
    """Return a dict matching the Phase 6 knowledge_cards schema for a draft card.

    Draft cards are not citation-eligible. approved_by_user_id and approved_at are null.
    source_message_version_ids may be empty for drafts (no constraint).
    """
    now = datetime.now(UTC)
    return {
        "id": uuid4(),
        "title": title,
        "body_markdown": body_markdown,
        "card_status": "draft",
        "source_message_version_ids": source_message_version_ids,
        "approved_by_user_id": None,
        "approved_at": None,
        "created_at": now,
        "updated_at": now,
    }


def make_archived_card(
    body_markdown: str,
    source_message_version_ids: list[int],
    title: str = "Archived Card",
) -> dict:
    """Return a dict matching the Phase 6 knowledge_cards schema for an archived card.

    Archived cards were once approved but are no longer active.
    approved_by_user_id and approved_at are null for simplicity in test fixtures
    (an archived card may have been approved and then archived).
    """
    now = datetime.now(UTC)
    return {
        "id": uuid4(),
        "title": title,
        "body_markdown": body_markdown,
        "card_status": "archived",
        "source_message_version_ids": source_message_version_ids,
        "approved_by_user_id": None,
        "approved_at": None,
        "created_at": now,
        "updated_at": now,
    }
