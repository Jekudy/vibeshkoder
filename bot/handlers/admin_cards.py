"""Admin Telegram handlers — knowledge cards review + browse (T6-04 + T6-05).

PHASE6_PLAN.md §5.C: ``/candidates``, ``/approve``, ``/reject``, ``/cards``,
``/card <id>``.

Five admin-only commands wired against the Phase 6 schema (T6-01) and the
Phase 5 transaction patterns. Each handler:

* checks ``message.from_user.id in settings.ADMIN_IDS`` and silently no-ops
  for non-admins (matches ``/stats`` and ``/admin_extract`` precedent);
* runs in private chat only via ``PrivateChatFilter`` on the router;
* operates inside the aiogram-injected ``AsyncSession``; the middleware
  commits on success and rolls back on exception.

The 8-step ``/approve`` protocol is the most load-bearing transaction
(PHASE6_PLAN §5.C verbatim). It acquires a per-mvid advisory xact lock,
re-runs deterministic governance, INSERTs the card + sources + decision,
and flips the candidate status — all in one transaction. The advisory
locks serialize with the forget-cascade orchestrator's lock acquisition
(§5.A.5 step 1), closing the H-Cdx-2 race.
"""

from __future__ import annotations

import html
import logging
import uuid as _uuid_module
from datetime import datetime

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.repos.card_source import CardSourceJoinedRow, CardSourceRepo
from bot.db.repos.extraction_candidate import ExtractionCandidateRepo
from bot.db.repos.extraction_decision import ExtractionDecisionRepo
from bot.db.repos.knowledge_card import KnowledgeCardRepo
from bot.db.repos.user import UserRepo
from bot.filters.chat_type import PrivateChatFilter
from bot.services.forget_cascade import _p6_mvid_advisory_lock_id
from bot.services.governance_revalidation import revalidate_sources

logger = logging.getLogger(__name__)

router = Router(name="admin_cards")


# Pagination — keep small for Telegram readability (matches design §4 / §2).
_PAGE_SIZE = 10


# ─── helpers ────────────────────────────────────────────────────────────────


def _is_admin(message: Message) -> bool:
    if message.from_user is None:
        return False
    return message.from_user.id in settings.ADMIN_IDS


def _decided_by_username(message: Message) -> str:
    """Resolve the audit username shadow for ``extraction_decisions``.

    Fallback to ``tg<id>`` when ``users.username`` is NULL (audit shadow is
    NOT NULL per T6-01 schema). T6-04 design §10 Q2.
    """
    uname = getattr(message.from_user, "username", None) if message.from_user else None
    if uname:
        return str(uname)
    return f"tg{message.from_user.id}"


def _short_uuid(uid: _uuid_module.UUID) -> str:
    return str(uid)[:8]


def _short_chat_id(chat_id: int) -> str:
    chat_id_str = str(chat_id)
    return chat_id_str.removeprefix("-100") if chat_id_str.startswith("-100") else chat_id_str


def _parse_page(arg: str | None) -> int:
    if not arg:
        return 1
    stripped = arg.strip()
    if not stripped:
        return 1
    try:
        page = int(stripped)
    except ValueError:
        return 1
    return page if page >= 1 else 1


def _format_dt(value: datetime) -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M UTC")


def _resolve_candidate_id(raw: str) -> _uuid_module.UUID | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return _uuid_module.UUID(raw)
    except (ValueError, AttributeError):
        return None


# ─── /candidates ────────────────────────────────────────────────────────────


@router.message(Command("candidates"), PrivateChatFilter())
async def cmd_candidates(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
) -> None:
    """Admin-only paginated list of pending ``extraction_candidates``."""
    if not _is_admin(message):
        return

    page = _parse_page(command.args)
    offset = (page - 1) * _PAGE_SIZE
    rows = await ExtractionCandidateRepo.list_pending(
        session, limit=_PAGE_SIZE, offset=offset
    )

    if not rows:
        await message.answer(
            f"📋 На странице {page} нет ожидающих кандидатов.",
            parse_mode="HTML",
        )
        return

    lines = [f"📋 <b>Pending candidates</b> (page {page})"]
    for idx, cand in enumerate(rows, start=1):
        title = ""
        if isinstance(cand.candidate_json, dict):
            raw_title = cand.candidate_json.get("title")
            if raw_title:
                title = html.escape(str(raw_title)[:80])
        n_sources = len(cand.source_message_version_ids or [])
        created = _format_dt(cand.created_at) if cand.created_at else "—"
        lines.append(
            f"#{idx}  <code>{_short_uuid(cand.id)}</code>\n"
            f"    title: {title or '—'}\n"
            f"    sources: {n_sources} · created: {created}"
        )
    lines.append(
        "Use <code>/approve &lt;id&gt;</code> or "
        "<code>/reject &lt;id&gt; [reason]</code>. "
        f"Next page: <code>/candidates {page + 1}</code>"
    )
    await message.answer("\n\n".join(lines), parse_mode="HTML")


# ─── /approve ───────────────────────────────────────────────────────────────


async def _acquire_mvid_locks(session: AsyncSession, mvids: list[int]) -> None:
    """Acquire ``pg_advisory_xact_lock`` for every source mvid (§5.C step 2).

    Sorted iteration ensures deterministic acquisition order across
    concurrent transactions — paired with the cascade orchestrator's sorted
    acquisition (forget_cascade._process_one_event), no two callers can
    deadlock.

    SQLite test path: ``pg_advisory_xact_lock`` is Postgres-specific. The
    handler only runs in production against Postgres; unit tests use a
    Postgres test DB (conftest.py), so the lock SQL executes normally there.
    """
    if not mvids:
        return
    if session.bind is None or session.bind.dialect.name != "postgresql":
        # Dialect guard for the test suite (SQLite, etc.) — production is
        # Postgres 16 and always takes the lock.
        return
    for lock_id in sorted(_p6_mvid_advisory_lock_id(m) for m in mvids):
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": lock_id},
        )


@router.message(Command("approve"), PrivateChatFilter())
async def cmd_approve(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
) -> None:
    """Admin-only atomic candidate → approved card promotion.

    Implements PHASE6_PLAN.md §5.C step ordering. Codex round 2 CRITICAL #1
    rebound the original step 1+2 order — advisory locks MUST land BEFORE
    the FOR UPDATE on the candidate, otherwise the FOR UPDATE read happens
    outside the serialization point with the forget cascade and re-opens
    the H-Cdx-2 race window.

    Revised step order (closes Codex round 2 CRITICAL #1):

    1a. Plain SELECT on the candidate (no FOR UPDATE) to read
        ``source_message_version_ids``. Read-only; cannot race the cascade.
    1b. Acquire ``pg_advisory_xact_lock`` for every source mvid (sorted).
        First serialization point with the forget-cascade orchestrator.
    1c. SELECT FOR UPDATE on the candidate to lock the row for the rest of
        the transaction. Now safe because the mvid-advisory locks are
        already held: any concurrent forget cascade for the same mvids has
        either run to completion (and the R3 check in step 3 will see the
        tombstone) or is blocked on our advisory lock until /approve
        commits or rolls back.
    1d. Re-read the mvid set from the FOR UPDATE row. If
        ``source_message_version_ids`` differs from the pre-lock read,
        acquire advisory locks for the new mvids before continuing
        (deterministic sorted order keeps the deadlock-avoidance
        invariant with the cascade orchestrator).
    3.  Re-run deterministic governance check (tombstones + memory_policy +
        is_redacted). On any failure → R3-block: NO extraction_decisions
        row, structured log only, transaction rolls back.
    5.  INSERT knowledge_cards row (card_status='approved').
    6.  INSERT card_sources rows (one per mvid).
    7.  UPDATE extraction_candidates status='approved'.
    8.  INSERT extraction_decisions (action='approved' + audit shadow).
    """
    if not _is_admin(message):
        return

    raw_args = (command.args or "").strip()
    if not raw_args:
        await message.answer(
            "Использование: <code>/approve &lt;candidate_id&gt;</code>",
            parse_mode="HTML",
        )
        return

    candidate_id = _resolve_candidate_id(raw_args.split()[0])
    if candidate_id is None:
        await message.answer(
            "Ошибка: неверный candidate_id (требуется UUID).", parse_mode="HTML"
        )
        return

    # Step 1a: plain (NO FOR UPDATE) read of the candidate's source mvids.
    # This is a minimal read used only to populate the advisory-lock key
    # set; the actual row lock is taken in step 1c. The read can race a
    # concurrent /reject — but that's harmless because step 1c re-reads
    # under FOR UPDATE and checks ``status == 'pending'`` before mutating.
    initial_cand = await ExtractionCandidateRepo.get_by_id(
        session, candidate_id
    )
    if initial_cand is None:
        await message.answer(
            f"❌ Candidate <code>{html.escape(str(candidate_id))}</code> not found.",
            parse_mode="HTML",
        )
        return

    initial_mvids = [
        int(m) for m in (initial_cand.source_message_version_ids or [])
    ]
    if not initial_mvids:
        await message.answer(
            "❌ Approval blocked: candidate has empty source set.",
            parse_mode="HTML",
        )
        logger.info(
            "approve_blocked",
            extra={
                "event": "approve_blocked",
                "candidate_id": str(candidate_id),
                "admin_user_id": message.from_user.id,
                "failure_reason": "empty_source_set",
            },
        )
        return

    # Step 1b: acquire per-mvid advisory locks BEFORE the FOR UPDATE on the
    # candidate. This is the serialization point with the forget cascade
    # (§5.A.5 step 1). The lock auto-releases on COMMIT/ROLLBACK. Sorted
    # acquisition order matches the cascade's sort, guaranteeing no
    # deadlock between the two protocols.
    await _acquire_mvid_locks(session, initial_mvids)

    # Step 1c: NOW safe to take FOR UPDATE on the candidate row. The mvid
    # locks above prevent any concurrent forget cascade from mutating the
    # same mvid set until this transaction commits.
    cand = await ExtractionCandidateRepo.get_by_id_for_update(
        session, candidate_id
    )
    if cand is None:
        # Vanishingly rare: candidate deleted between 1a and 1c.
        await message.answer(
            f"❌ Candidate <code>{html.escape(str(candidate_id))}</code> not found.",
            parse_mode="HTML",
        )
        return

    if cand.status != "pending":
        await message.answer(
            f"⚠️ Candidate <code>{_short_uuid(cand.id)}</code> "
            f"already decided (status=<code>{cand.status}</code>).",
            parse_mode="HTML",
        )
        return

    mvids = [int(m) for m in (cand.source_message_version_ids or [])]
    if not mvids:
        # Defensive: should never happen given 1a returned a non-empty set
        # and the candidate row's source_message_version_ids is not mutated
        # post-creation. If it does, treat as the empty-set blocking case.
        await message.answer(
            "❌ Approval blocked: candidate has empty source set.",
            parse_mode="HTML",
        )
        logger.info(
            "approve_blocked",
            extra={
                "event": "approve_blocked",
                "candidate_id": str(candidate_id),
                "admin_user_id": message.from_user.id,
                "failure_reason": "empty_source_set",
            },
        )
        return

    # Step 1d: if the canonical mvid set picked up any rows that step 1a
    # missed, acquire advisory locks for ALL mvids in the full union (sorted).
    # Passing only the new delta would produce a different global ordering
    # from two concurrent /approve callers whose mvid sets overlap — that is
    # the lock-order inversion that causes deadlock. Passing the full union to
    # _acquire_mvid_locks (which sorts internally) keeps the acquisition order
    # identical to the cascade orchestrator's protocol for any subset of mvids.
    # pg_advisory_xact_lock re-entry is safe: PostgreSQL ignores duplicate
    # xact-lock requests for a key already held within the same transaction.
    full_union_mvids = list(set(initial_mvids) | set(mvids))
    if set(full_union_mvids) != set(initial_mvids):
        # Only re-acquire when the union differs from what was locked in 1b.
        await _acquire_mvid_locks(session, full_union_mvids)

    # Step 3+4: deterministic governance re-validation (NO LLM re-prompt).
    status, payload = await revalidate_sources(session, mvids)
    if status == "blocked":
        logger.warning(
            "approve_blocked",
            extra={
                "event": "approve_blocked",
                "candidate_id": str(candidate_id),
                "admin_user_id": message.from_user.id,
                **payload,
            },
        )
        await message.answer(
            "❌ Approval blocked: source message no longer eligible.\n"
            f"Candidate: <code>{_short_uuid(cand.id)}</code>\n"
            f"Reason: <code>{html.escape(str(payload.get('failure_reason')))}</code>\n"
            f"Source mvid: <code>{payload.get('mvid')}</code>",
            parse_mode="HTML",
        )
        # IMPORTANT: NO extraction_decisions row written; candidate stays
        # pending. Caller's transaction will roll back the SELECT FOR UPDATE
        # but no mutating writes have happened.
        return

    # Step 5: INSERT knowledge_cards.
    candidate_json = cand.candidate_json if isinstance(cand.candidate_json, dict) else {}
    title = str(candidate_json.get("title") or "").strip() or "—"
    body_markdown = str(candidate_json.get("body_markdown") or "").strip() or "—"
    card = await KnowledgeCardRepo.create(
        session,
        title=title,
        body_markdown=body_markdown,
        approved_by_user_id=message.from_user.id,
    )

    # Step 6: INSERT card_sources (FK enforced).
    await CardSourceRepo.bulk_create(
        session, card_id=card.id, message_version_ids=mvids
    )

    # Step 7: UPDATE extraction_candidates status.
    await ExtractionCandidateRepo.mark_status(
        session,
        candidate_id=candidate_id,
        status="approved",
        reviewed_by=message.from_user.id,
    )

    # Step 8: INSERT extraction_decisions.
    await ExtractionDecisionRepo.create(
        session,
        candidate_id=candidate_id,
        action="approved",
        decided_by=message.from_user.id,
        decided_by_username=_decided_by_username(message),
        reason=None,
    )

    await message.answer(
        f"✅ Approved <code>{_short_uuid(cand.id)}</code> → card "
        f"<code>{_short_uuid(card.id)}</code>",
        parse_mode="HTML",
    )


# ─── /reject ────────────────────────────────────────────────────────────────


@router.message(Command("reject"), PrivateChatFilter())
async def cmd_reject(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
) -> None:
    """Admin-only candidate rejection.

    Simpler than ``/approve``: no governance re-check, no advisory locks
    (does not mutate eligible sources). Writes ``extraction_decisions`` row
    with ``action='rejected'`` and flips candidate status.
    """
    if not _is_admin(message):
        return

    raw_args = (command.args or "").strip()
    if not raw_args:
        await message.answer(
            "Использование: <code>/reject &lt;candidate_id&gt; [reason]</code>",
            parse_mode="HTML",
        )
        return

    tokens = raw_args.split(maxsplit=1)
    candidate_id = _resolve_candidate_id(tokens[0])
    if candidate_id is None:
        await message.answer(
            "Ошибка: неверный candidate_id (требуется UUID).", parse_mode="HTML"
        )
        return
    reason = tokens[1].strip() if len(tokens) > 1 else None
    if reason:
        # Defensive truncation; admins type by hand.
        reason = reason[:1024]

    cand = await ExtractionCandidateRepo.get_by_id_for_update(
        session, candidate_id
    )
    if cand is None:
        await message.answer(
            f"❌ Candidate <code>{html.escape(str(candidate_id))}</code> not found.",
            parse_mode="HTML",
        )
        return

    if cand.status != "pending":
        await message.answer(
            f"⚠️ Candidate <code>{_short_uuid(cand.id)}</code> "
            f"already decided (status=<code>{cand.status}</code>).",
            parse_mode="HTML",
        )
        return

    await ExtractionCandidateRepo.mark_status(
        session,
        candidate_id=candidate_id,
        status="rejected",
        reviewed_by=message.from_user.id,
    )
    await ExtractionDecisionRepo.create(
        session,
        candidate_id=candidate_id,
        action="rejected",
        decided_by=message.from_user.id,
        decided_by_username=_decided_by_username(message),
        reason=reason,
    )
    await message.answer(
        f"✅ Rejected <code>{_short_uuid(cand.id)}</code>" +
        (f" reason=<code>{html.escape(reason)}</code>" if reason else ""),
        parse_mode="HTML",
    )


# ─── /cards ─────────────────────────────────────────────────────────────────


@router.message(Command("cards"), PrivateChatFilter())
async def cmd_cards(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
) -> None:
    """Admin-only paginated list of approved cards."""
    if not _is_admin(message):
        return

    page = _parse_page(command.args)
    offset = (page - 1) * _PAGE_SIZE
    rows = await KnowledgeCardRepo.list_approved(
        session, limit=_PAGE_SIZE, offset=offset
    )

    if not rows:
        await message.answer(
            f"📚 На странице {page} нет одобренных карточек.",
            parse_mode="HTML",
        )
        return

    lines = [f"📚 <b>Approved cards</b> (page {page})"]
    for idx, card in enumerate(rows, start=1):
        title = html.escape(str(card.title or "—")[:80])
        preview = html.escape(str(card.body_markdown or "")[:80])
        approved_at = (
            _format_dt(card.approved_at) if card.approved_at else "—"
        )
        approver = "—"
        if card.approved_by_user_id is not None:
            user = await UserRepo.get(session, card.approved_by_user_id)
            if user is not None and user.username:
                approver = html.escape(f"@{user.username}")
            else:
                approver = f"tg{card.approved_by_user_id}"
        lines.append(
            f"#{idx}  <code>{_short_uuid(card.id)}</code>\n"
            f"    {title}\n"
            f"    preview: {preview}\n"
            f"    approved: {approved_at} by {approver}"
        )
    lines.append(
        f"Use <code>/card &lt;id&gt;</code> for detail. "
        f"Next page: <code>/cards {page + 1}</code>"
    )
    await message.answer("\n\n".join(lines), parse_mode="HTML")


# ─── /card <id> ─────────────────────────────────────────────────────────────


def _format_source_lines(
    sources: list[CardSourceJoinedRow],
) -> list[str]:
    """Render back-citations.

    Per T6-05 design §3: filter out sources whose memory_policy != 'normal'
    or whose is_redacted flags are True — show a placeholder instead. This
    guards the partial-cascade case where ``card_sources`` rows still exist
    but the underlying message has been redacted.
    """
    lines: list[str] = []
    for idx, src in enumerate(sources, start=1):
        if (
            src.memory_policy != "normal"
            or src.is_redacted
            or src.mv_is_redacted
        ):
            lines.append(
                f"[{idx}] &lt;source redacted&gt;  "
                f"(card_source_id <code>{_short_uuid(src.card_source_id)}</code>)"
            )
            continue
        short_chat = _short_chat_id(src.chat_id)
        link = f"https://t.me/c/{short_chat}/{src.message_id}"
        lines.append(
            f"[{idx}] <a href=\"{html.escape(link, quote=True)}\">message</a>  "
            f"(mvid <code>{src.message_version_id}</code>)"
        )
    return lines


@router.message(Command("card"), PrivateChatFilter())
async def cmd_card(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
) -> None:
    """Admin-only card detail view.

    Renders title + body + approval metadata + source back-citations — ONLY
    for ``card_status='approved'`` rows. Draft and archived cards are
    excluded from the lookup entirely (treated as not-found) so neither
    title/status nor archived_reason leak via this endpoint (Codex round 2
    MED #1 / T6-05 design §3).
    """
    if not _is_admin(message):
        return

    raw_args = (command.args or "").strip()
    if not raw_args:
        await message.answer(
            "Использование: <code>/card &lt;card_id_or_prefix&gt;</code>",
            parse_mode="HTML",
        )
        return

    arg = raw_args.split()[0]

    card = None
    parsed = _resolve_candidate_id(arg)  # same UUID resolver
    if parsed is not None:
        card = await KnowledgeCardRepo.get_by_id(session, parsed)
    else:
        # Short-prefix lookup; arg is not a full UUID.
        prefix_rows = await KnowledgeCardRepo.get_by_id_prefix(session, arg)
        if len(prefix_rows) > 1:
            await message.answer(
                f"⚠️ Multiple cards match prefix <code>{html.escape(arg)}</code>. "
                "Specify more.",
                parse_mode="HTML",
            )
            return
        if len(prefix_rows) == 1:
            card = prefix_rows[0]

    # Privacy filter: non-approved cards are treated as not-found so neither
    # title, status, nor archived_reason leak. The previous implementation
    # surfaced these fields for draft/archived rows — Codex round 2 MED #1
    # flagged that as a leak vector for any admin who happens to know a
    # full card UUID.
    if card is None or card.card_status != "approved":
        await message.answer("❌ Card not found.", parse_mode="HTML")
        return

    # Approver name.
    approver = "—"
    if card.approved_by_user_id is not None:
        user = await UserRepo.get(session, card.approved_by_user_id)
        if user is not None and user.username:
            approver = html.escape(f"@{user.username}")
        else:
            approver = f"tg{card.approved_by_user_id}"
    approved_at = (
        _format_dt(card.approved_at) if card.approved_at else "—"
    )

    # L-3: 'Status:' line removed — cmd_card is gated on card_status='approved'
    # (line ~615) so the status is always 'approved' at this point; the line
    # was redundant and slightly noisy for the admin reader.
    header_lines = [
        f"📄 <b>Card detail</b>  <code>{card.id}</code>",
        f"Approved: {approved_at} by {approver}",
        f"Title: {html.escape(str(card.title or '—'))}",
    ]
    # Body — render as HTML <pre> to avoid MarkdownV2 parse-crash risk
    # (T6-05 design §3 conservative choice). Body content is admin-
    # authored output from the extractor.
    body_text = str(card.body_markdown or "")
    if len(body_text) > 3500:
        body_text = body_text[:3500] + "\n… (truncated)"
    header_lines.append(
        f"\n<b>Body:</b>\n<pre>{html.escape(body_text)}</pre>"
    )

    sources = await CardSourceRepo.list_for_card(session, card.id)
    if sources:
        header_lines.append(f"\n<b>Sources ({len(sources)}):</b>")
        header_lines.extend(_format_source_lines(sources))

    await message.answer(
        "\n".join(header_lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
