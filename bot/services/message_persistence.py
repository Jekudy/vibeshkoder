"""persist_message_with_policy — unified message persistence with governance (Sprint #89).

Public API:

    result = await persist_message_with_policy(session, message)

Mirrors the body of ``bot/handlers/chat_messages.py::save_chat_message`` (lines 38-109)
so that byte-identical DB state results whether the call site is the live handler or the
importer.

Sprint #80 invariants preserved:
- Advisory lock acquired BEFORE any DB operation.
- Sticky offrecord CASE in MessageRepo.save (server-side monotonic ratchet).

Hotfix #164 (CRITICAL 1 fix):
- MessageVersionRepo.insert_version IS NOW called here. This helper is the SOLE writer
  for (chat_messages, message_versions, current_version_id) triples. Previously deferred
  per Sprint #81 comment; relocation closes CRITICAL 1, CRITICAL 2, CRITICAL 3, Risk H2.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.locks import advisory_lock_chat_message
from bot.db.models import ChatMessage
from bot.db.repos.message import MessageRepo
from bot.db.repos.message_version import MessageVersionRepo
from bot.db.repos.offrecord_mark import OffrecordMarkRepo
from bot.services.content_hash import compute_content_hash
from bot.services.governance import detect_policy
from bot.services.normalization import extract_entities_unified, extract_normalized_fields


@dataclass(frozen=True)
class PersistResult:
    chat_message: ChatMessage
    policy: str  # "normal" | "nomem" | "offrecord"
    is_offrecord_mark_created: bool


async def persist_message_with_policy(
    session: AsyncSession,
    message: Any,  # aiogram Message OR structurally-compatible duck (importer)
    *,
    raw_update_id: int | None = None,
    source: Literal["live", "import"] = "live",
    captured_at: datetime | None = None,
) -> PersistResult:
    """Save a message with policy detection, mark creation, and v1 MessageVersion.

    Internal flow (hotfix #164 extended from chat_messages.py:38-109):
    1. Acquire advisory lock for (chat_id, message_id) — Sprint #80 invariant.
    2. Extract normalized fields (reply/thread/caption/kind).
    3. Detect policy (offrecord > nomem > normal).
    4. Build persist-content fields per policy:
       - offrecord → text=None, caption=None, raw_json=None, is_redacted=True
       - else       → preserve message fields; raw_json only when message.text truthy
    5. MessageRepo.save with sticky CASE preserved, optional captured_at override.
    6. If policy != "normal" and mark_payload is not None → OffrecordMarkRepo.create_for_message.
    7. Build v1 content fields per redaction policy.
    8. Compute content_hash (chv1 recipe).
    9. Insert v1 row (idempotent on (chat_message_id, content_hash)).
    10. Close FK loop: UPDATE chat_messages SET current_version_id = v1.id.
    11. Return PersistResult.
    """
    # 1. Advisory lock before any DB op (Sprint #80).
    await advisory_lock_chat_message(session, message.chat.id, message.message_id)

    # 2. Normalized fields.
    normalized = extract_normalized_fields(message)

    # 3. Policy detection — extract extra fields for broadened scan (Sprint #89 Commit 2).
    # poll_question: survey question text
    poll_question = getattr(getattr(message, "poll", None), "question", None)
    # contact_name: first + last name joined (phone number excluded — smaller attack surface)
    contact = getattr(message, "contact", None)
    contact_name: str | None = None
    if contact is not None:
        parts = [getattr(contact, "first_name", None), getattr(contact, "last_name", None)]
        contact_name = " ".join(p for p in parts if p) or None
    # forward content is already captured in text/caption columns today; forward_text /
    # forward_caption remain None here as a placeholder for future expansion.
    forward_text: str | None = None
    forward_caption: str | None = None

    policy, mark_payload = detect_policy(
        getattr(message, "text", None),
        getattr(message, "caption", None),
        poll_question=poll_question,
        contact_name=contact_name,
        forward_text=forward_text,
        forward_caption=forward_caption,
    )

    # 4. Build persisted-content fields per policy.
    if policy == "offrecord":
        persist_text: str | None = None
        persist_caption: str | None = None
        persist_raw_json: dict | None = None
        is_redacted_flag = True
    else:
        persist_text = getattr(message, "text", None)
        persist_caption = normalized["caption"]
        # raw_json tracks text presence (gatekeeper-era behaviour).
        # Uses model_dump() when available (aiogram Message); falls back to None for
        # importer ducks that lack model_dump.
        _model_dump = getattr(message, "model_dump", None)
        persist_raw_json = (
            _model_dump(mode="json", exclude_none=True)
            if message.text and _model_dump is not None
            else None
        )
        is_redacted_flag = False

    user_id = message.from_user.id if message.from_user is not None else None

    # 5. Save — sticky CASE in MessageRepo.save handles offrecord monotonic ratchet.
    # When captured_at is provided (e.g. import preserving export timestamp), override date.
    save_date = captured_at if captured_at is not None else message.date
    saved = await MessageRepo.save(
        session,
        message_id=message.message_id,
        chat_id=message.chat.id,
        user_id=user_id,
        text=persist_text,
        date=save_date,
        raw_json=persist_raw_json,
        reply_to_message_id=normalized["reply_to_message_id"],
        message_thread_id=normalized["message_thread_id"],
        caption=persist_caption,
        message_kind=normalized["message_kind"],
        raw_update_id=raw_update_id,
        memory_policy=policy,
        is_redacted=is_redacted_flag,
    )

    # 6. Persist audit mark for any non-normal policy.
    mark_created = False
    if policy != "normal" and mark_payload is not None:
        await OffrecordMarkRepo.create_for_message(
            session,
            chat_message_id=saved.id,
            mark_type=policy,
            detected_by=mark_payload["detected_by"],
            set_by_user_id=user_id,
            thread_id=normalized["message_thread_id"],
        )
        mark_created = True

    # 7. Build v1 content fields per redaction policy (hotfix #164 CRITICAL 1 fix).
    if is_redacted_flag:
        v1_text = None
        v1_caption = None
        v1_normalized_text = None
        v1_entities_list = None
        v1_entities_json = None
    else:
        v1_text = persist_text
        v1_caption = persist_caption
        v1_normalized_text = persist_text  # raw text; canonicalization deferred to #153
        v1_entities_list = extract_entities_unified(message)
        v1_entities_json = json.dumps(v1_entities_list) if v1_entities_list else None

    # 8. Compute content_hash (chv1 recipe: text, caption, message_kind, entities).
    v1_content_hash = compute_content_hash(
        text=v1_text,
        caption=v1_caption,
        message_kind=normalized["message_kind"],
        entities=v1_entities_list,
    )

    # 9. Insert v1 (idempotent on (chat_message_id, content_hash)).
    v1 = await MessageVersionRepo.insert_version(
        session,
        chat_message_id=saved.id,
        content_hash=v1_content_hash,
        text=v1_text,
        caption=v1_caption,
        normalized_text=v1_normalized_text,
        entities_json=v1_entities_json,
        edit_date=None,  # v1 is NOT an edit
        raw_update_id=raw_update_id,
        is_redacted=is_redacted_flag,
        imported_final=(source == "import"),
        captured_at=captured_at,  # None → server_default now(); override for imports/eval
    )

    # 10. Close FK loop on chat_messages (idempotent guard for retry-races).
    if saved.current_version_id != v1.id:
        await session.execute(
            update(ChatMessage)
            .where(ChatMessage.id == saved.id)
            .values(current_version_id=v1.id)
        )
        await session.flush()
        saved.current_version_id = v1.id

    # 11. Return result.
    return PersistResult(saved, policy, mark_created)
