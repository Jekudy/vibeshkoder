"""Handler for ``edited_message`` Telegram updates (T1-14).

When a Telegram user edits a message the bot has previously ingested, this handler:
1. Looks up the existing ``chat_messages`` row by ``(chat_id, message_id)``.
2. Computes the new content hash via ``compute_content_hash`` (chv1).
3. If the hash changed — appends a new ``message_versions`` row (v(n+1)) and updates
   ``chat_messages.current_version_id``.
4. If the hash is unchanged — no-op (same content, no new version row).
5. Re-runs ``detect_policy`` on the EDITED content before any DB mutation to enforce the
   #offrecord ordering rule (AUTHORIZED_SCOPE.md §`#offrecord` ordering rule).
6. Handles policy flips:
   - ``normal → offrecord``: retroactively nulls ``text``, ``caption``, ``raw_json``;
     sets ``is_redacted=True``; updates ``memory_policy='offrecord'``; creates
     ``offrecord_marks`` row — all in the same transaction.
   - ``offrecord → normal``: updates ``memory_policy='normal'`` but does NOT restore
     content (irreversibility doctrine — content already lost on first ingest; see
     HANDOFF.md §10).
   - ``normal → nomem`` or ``nomem → normal`` and similar: updates ``memory_policy``.
7. Unknown prior message (no row for ``(chat_id, message_id)``): logs a warning and
   returns. No placeholder row created (locked scope item 4).

Transaction model: all mutations happen inside the ``DbSessionMiddleware`` session.
``session.flush()`` is called by repos internally. The middleware commits at handler exit.
No partial flushes with side-effects before the full logic is complete.

Irreversibility note (BLOCKER #2 / HANDOFF.md §10):
When a message that was first ingested as ``#offrecord`` (content redacted, NULL
text/caption) is later edited to remove the ``#offrecord`` token, the new policy
becomes ``normal``. However, the original content was already lost — the DB row has
``text=NULL, caption=NULL`` with no recovery path (the raw content was never stored).
The handler sets ``memory_policy='normal'`` but leaves content fields NULL. Future
edits that carry actual content will create a new ``message_versions`` row with
chv1 hash of that content; citations will point at that version. The original content
remains unrecoverable and will not be restored.
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.types import Message
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import ChatMessage
from bot.db.repos.message_version import MessageVersionRepo
from bot.db.repos.offrecord_mark import OffrecordMarkRepo
from bot.filters.chat_type import GroupChatFilter
from bot.services.content_hash import compute_content_hash
from bot.services.governance import detect_policy
from bot.services.normalization import classify_message_kind, extract_caption

logger = logging.getLogger(__name__)

router = Router(name="edited_message")

# Legacy hash strategy (HIGH #3): we do NOT distinguish legacy vs chv1 by inspecting the
# hash string itself — both are 64-char SHA-256 hex with no structural difference. Instead
# the handler always recomputes chv1 from the existing row's text/caption/kind and compares
# to the new edit chv1 hash; ``MessageVersionRepo.insert_version`` is idempotent on
# (chat_message_id, content_hash). See ``handle_edited_message`` Step 6 for the inline
# logic.


def _build_entities_json(message: Message) -> list[dict] | None:
    """Extract entities from edited message for version storage."""
    entities = getattr(message, "entities", None)
    if entities is None:
        return None
    try:
        return [e.model_dump(mode="json", exclude_none=True) for e in entities]
    except Exception:
        return None


def _extract_entities_list(message: Message) -> list[dict] | None:
    """Extract normalized entities for content hash computation."""
    entities = getattr(message, "entities", None)
    if entities is None:
        # Also try caption_entities for media messages
        entities = getattr(message, "caption_entities", None)
    if entities is None:
        return None
    try:
        return [e.model_dump(mode="json", exclude_none=True) for e in entities]
    except Exception:
        return None


async def _find_chat_message(
    session: AsyncSession,
    chat_id: int,
    message_id: int,
) -> ChatMessage | None:
    """Return the ``chat_messages`` row for ``(chat_id, message_id)``, or None.

    Uses ``SELECT ... FOR UPDATE`` to acquire a row-level lock for the duration of the
    transaction. This prevents a TOCTOU race where two concurrent edit handlers (asyncio
    tasks within the same single-instance bot, or two delivery attempts) could both read
    ``memory_policy='normal'`` and one of them then writes ``text/caption`` *after* the
    other has flipped to ``offrecord``. Without the lock, the privacy invariant relies on
    a single-transaction guarantee that does not hold across two transactions.
    """
    result = await session.execute(
        select(ChatMessage)
        .where(
            ChatMessage.chat_id == chat_id,
            ChatMessage.message_id == message_id,
        )
        .with_for_update()
    )
    return result.scalar_one_or_none()


async def _apply_offrecord_flip(
    session: AsyncSession,
    row: ChatMessage,
    mark_payload: dict | None,
    set_by_user_id: int | None,
    thread_id: int | None,
) -> None:
    """Retroactively redact content and create offrecord_marks row in the same transaction.

    Called when the edit flips the policy to 'offrecord'. Nulls content fields,
    sets is_redacted=True, memory_policy='offrecord' on the chat_messages row.
    Creates offrecord_marks audit row. All in the same session (no commit).
    """
    await session.execute(
        update(ChatMessage)
        .where(ChatMessage.id == row.id)
        .values(
            text=None,
            caption=None,
            raw_json=None,
            is_redacted=True,
            memory_policy="offrecord",
        )
    )
    await session.flush()

    if mark_payload is not None:
        await OffrecordMarkRepo.create_for_message(
            session,
            chat_message_id=row.id,
            mark_type="offrecord",
            detected_by=mark_payload["detected_by"],
            set_by_user_id=set_by_user_id,
            thread_id=thread_id,
        )


async def _update_memory_policy(
    session: AsyncSession,
    row: ChatMessage,
    new_policy: str,
    mark_payload: dict | None,
    set_by_user_id: int | None,
    thread_id: int | None,
) -> None:
    """Update memory_policy for non-offrecord policy changes.

    Handles: normal→nomem, nomem→normal, offrecord→normal (irreversibility applies).
    Does NOT restore content for offrecord→normal flips per irreversibility doctrine.
    """
    await session.execute(
        update(ChatMessage).where(ChatMessage.id == row.id).values(memory_policy=new_policy)
    )
    await session.flush()

    if new_policy != "normal" and mark_payload is not None:
        await OffrecordMarkRepo.create_for_message(
            session,
            chat_message_id=row.id,
            mark_type=new_policy,
            detected_by=mark_payload["detected_by"],
            set_by_user_id=set_by_user_id,
            thread_id=thread_id,
        )


@router.edited_message(GroupChatFilter())
async def handle_edited_message(
    message: Message,
    session: AsyncSession,
) -> None:
    """Handle Telegram ``edited_message`` updates for community group chat.

    Follows the transactional pattern from ``chat_messages.py``:
    detect_policy BEFORE any content mutation, all DB work in one session.
    """
    if message.chat.id != settings.COMMUNITY_CHAT_ID:
        return

    chat_id = message.chat.id
    message_id = message.message_id
    user_id = getattr(message.from_user, "id", None) if message.from_user else None

    # Step 1: find existing chat_messages row.
    existing = await _find_chat_message(session, chat_id, message_id)
    if existing is None:
        logger.warning(
            "edited_message: no prior row for chat_id=%s message_id=%s — skipping",
            chat_id,
            message_id,
        )
        return

    # Step 2: extract normalized content from the edited message.
    text = getattr(message, "text", None)
    caption = extract_caption(message)
    message_kind = classify_message_kind(message)
    entities = _extract_entities_list(message)
    edit_date = getattr(message, "edit_date", None)

    # Step 3: detect policy on EDITED content BEFORE any DB mutation.
    # #offrecord ordering rule: detect_policy runs first, content mutations come after.
    new_policy, mark_payload = detect_policy(text, caption)

    # Step 4: compute new content hash (chv1).
    new_hash = compute_content_hash(
        text=text,
        caption=caption,
        message_kind=message_kind,
        entities=entities,
    )

    # Step 5: handle policy flips — BEFORE version insertion so policy state is correct.
    old_policy = existing.memory_policy

    if new_policy == "offrecord" and old_policy != "offrecord":
        # normal/nomem → offrecord: retroactively zero out content (BLOCKER #1).
        thread_id = getattr(message, "message_thread_id", None)
        await _apply_offrecord_flip(session, existing, mark_payload, user_id, thread_id)
        # After flip, content is nulled — version text/caption must also be null.
        # Recompute hash with null content to reflect actual stored state.
        # Use the new_hash as-is; the version row will have is_redacted=True.
    elif new_policy != old_policy:
        # Any other policy transition (offrecord→normal, normal→nomem, nomem→normal).
        thread_id = getattr(message, "message_thread_id", None)
        await _update_memory_policy(session, existing, new_policy, mark_payload, user_id, thread_id)

    # Step 6: check if content actually changed by looking up the new hash.
    # HIGH #3 legacy hash: MessageVersionRepo.insert_version already handles idempotency
    # on (chat_message_id, content_hash). If the new chv1 hash matches an existing version
    # row's hash, get_by_hash returns it and insert_version returns it without a new row.
    # For legacy v1 rows with a pre-chv1 hash: the new chv1 hash will NOT match the stored
    # legacy hash (different recipe → different hash for the same content), so we would
    # incorrectly create v2 for unchanged content.
    #
    # Runtime fix (no migration): if current_version_id is None OR the stored
    # content_hash on chat_messages doesn't match the new chv1 hash we just computed for
    # the SAME content (i.e., text/caption/kind are the same as the edit), we need to
    # check whether the content is truly identical by recomputing chv1 from the existing
    # row's stored text/caption/kind and comparing to the new hash.
    #
    # Concretely: recompute chv1 from existing row's text/caption/message_kind.
    # If that matches new_hash → content unchanged → no-op.
    # If it doesn't → content changed → insert v(n+1).

    # Reload the existing row to get current text/caption after potential offrecord flip.
    await session.refresh(existing)

    existing_chv1 = compute_content_hash(
        text=existing.text,
        caption=existing.caption,
        message_kind=existing.message_kind,
        entities=None,  # existing row doesn't store normalized entities in chat_messages
    )

    # PRIVACY (Codex review HIGH): for offrecord versions we MUST NOT store the chv1 of
    # the un-redacted edit content as ``content_hash`` in ``message_versions``. Doing so
    # would let anyone with read access to the audit table verify "was content X said?"
    # by computing chv1(X) and grep'ing the column. Phase 1 invariant requires that
    # offrecord content fingerprints are NOT durably stored.
    #
    # Fix: when ``new_policy == "offrecord"``, both the lookup hash and the stored
    # ``content_hash`` are computed from the redacted state (None text, None caption,
    # message_kind, None entities) instead of the raw edit content. This means:
    # - Re-confirmation edits of an already-offrecord row produce the same redacted-state
    #   hash → ``get_by_hash`` returns existing version → no duplicate row inserted.
    # - normal→offrecord flip: existing row's text was nulled in ``_apply_offrecord_flip``
    #   above, so ``existing_chv1`` (computed from the now-null parent row) ALSO equals
    #   the redacted-state hash → idempotency holds and no duplicate version row is
    #   created. Note this means we do NOT insert a fresh v(n+1) on the flip itself —
    #   the parent row's policy/is_redacted/memory_policy fields carry the state change.
    #   ``offrecord_marks`` is the audit timeline for this transition.
    is_redacted_version = new_policy == "offrecord"
    if is_redacted_version:
        hash_to_check = compute_content_hash(
            text=None, caption=None, message_kind=message_kind, entities=None
        )
    else:
        hash_to_check = new_hash

    existing_version = await MessageVersionRepo.get_by_hash(session, existing.id, hash_to_check)
    if existing_version is not None:
        return

    if hash_to_check == existing_chv1:
        return

    # Content changed — insert new version v(n+1).
    version_text = None if is_redacted_version else text
    version_caption = None if is_redacted_version else caption

    entities_json = None
    if not is_redacted_version:
        entities_json = _build_entities_json(message)

    new_version = await MessageVersionRepo.insert_version(
        session,
        chat_message_id=existing.id,
        content_hash=hash_to_check,
        text=version_text,
        caption=version_caption,
        normalized_text=version_text,
        entities_json=entities_json,
        edit_date=edit_date,
        is_redacted=is_redacted_version,
    )

    # Step 7: update chat_messages to point at the new version and reflect edited content.
    # IRREVERSIBILITY DOCTRINE (HANDOFF.md §10): if the parent row was previously offrecord
    # (i.e., text/caption/raw_json were already nulled), leave them NULL even when the edit
    # flips policy back to normal. The original content window is destroyed permanently;
    # the new edited content is recorded only inside message_versions, not surfaced back to
    # chat_messages. This closes the cross-team-review BLOCKER on offrecord→normal flips.
    update_values: dict = {"current_version_id": new_version.id}
    parent_was_redacted = old_policy == "offrecord"
    if new_policy != "offrecord" and not parent_was_redacted:
        update_values["text"] = text
        update_values["caption"] = caption
    await session.execute(
        update(ChatMessage).where(ChatMessage.id == existing.id).values(**update_values)
    )
    await session.flush()
