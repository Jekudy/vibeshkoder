"""Phase 9 — admin Telegram handlers for wiki publication (T9-06).

PHASE9_PLAN.md §5.E (publish flow) + §5.F (unpublish flow).

Commands (admin-only, private chat):
- ``/wiki_publish <slug>``  — publish a reviewed page.
- ``/wiki_unpublish <slug>`` — withdraw a page from public view.
- ``/wiki_robots <slug> {index|noindex}`` — set robots_policy.

Non-admin calls: reply with refusal message (R6.a).
Feature-flag gate: ``memory.wiki.enabled`` — if disabled reply with
  "Wiki временно недоступна." and return.

All three handlers wrap UPDATE + INSERT into ``wiki_publication_log`` in a
single transaction so that ``public_enabled=true`` can never be set without
an audit row (AC#6 atomicity).

Advisory locking during ``/wiki_publish`` re-uses
``acquire_advisory_lock`` from ``bot.services.import_chunking`` to close the
TOCTOU window between the initial ``validate_sources`` call and the DB write
(PHASE9_PLAN.md §5.E step 7).
"""

from __future__ import annotations

import json
import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.repos.feature_flag import FeatureFlagRepo
from bot.filters.chat_type import PrivateChatFilter
from bot.html_escape import html_escape
from bot.services.wiki_governance import (
    WikiSourcesMissingError,
    assert_publishable,
    validate_sources,
)

logger = logging.getLogger(__name__)

router = Router(name="wiki_admin")

_FEATURE_FLAG = "memory.wiki.enabled"


# ── helpers ───────────────────────────────────────────────────────────────────


def _is_admin(message: Message) -> bool:
    if message.from_user is None:
        return False
    return message.from_user.id in settings.ADMIN_IDS


def _actor_id(message: Message) -> int | None:
    return message.from_user.id if message.from_user else None


def _format_source_check_summary(result) -> str:
    """Build a short human-readable summary of a failed SourceCheckResult."""
    lines = ["Источники не прошли проверку:"]
    for card_id in result.invalid_card_ids[:3]:
        reason = result.reasons.get(f"card:{card_id}", "unknown")
        lines.append(f"  card:{str(card_id)[:8]}… — {reason}")
    for mv_id in result.invalid_mvids[:3]:
        reason = result.reasons.get(f"mvid:{mv_id}", "unknown")
        lines.append(f"  mvid:{mv_id} — {reason}")
    total = len(result.invalid_card_ids) + len(result.invalid_mvids)
    if total > 3:
        lines.append(f"  … и ещё {total - 3} нарушений.")
    return "\n".join(lines)


# ── /wiki_publish ─────────────────────────────────────────────────────────────


@router.message(Command("wiki_publish"), PrivateChatFilter())
async def cmd_wiki_publish(
    message: Message,
    session: AsyncSession,
    command: CommandObject,
) -> None:
    """Publish a reviewed wiki page (PHASE9_PLAN.md §5.E).

    Steps (all inside ONE transaction):
    1. Admin gate.
    2. Feature-flag gate.
    3. Parse slug.
    4. SELECT wiki_pages FOR UPDATE — lock the row.
    5. Require page_status='reviewed'.
    6. assert_publishable — require at least one source.
    7. validate_sources — require all sources to be clean.
    8. Capture prior_pe, prior_rp from the locked row (plan §5.E step 6a).
    9. UPDATE public_enabled=true.
    10. INSERT wiki_publication_log(action='publish', …).
    11. Reply success.
    """
    # 1. Admin gate (R6.a)
    if not _is_admin(message):
        await message.answer("Команда доступна только администратору.")
        return

    # 2. Feature-flag gate
    wiki_enabled = await FeatureFlagRepo.get(session, _FEATURE_FLAG)
    if not wiki_enabled:
        await message.answer("Wiki временно недоступна.")
        return

    # 3. Parse slug
    slug = (command.args or "").strip()
    if not slug:
        await message.answer(
            "Использование: <code>/wiki_publish &lt;slug&gt;</code>",
            parse_mode="HTML",
        )
        return

    actor_id = _actor_id(message)

    try:
        async with session.begin_nested():
            # 4. SELECT … FOR UPDATE
            row = (
                await session.execute(
                    text(
                        "SELECT id, page_status, public_enabled, robots_policy "
                        "FROM wiki_pages WHERE slug = :slug FOR UPDATE"
                    ),
                    {"slug": slug},
                )
            ).mappings().one_or_none()

            if row is None:
                await message.answer(f"Страница не найдена: <code>{html_escape(slug)}</code>.", parse_mode="HTML")
                return

            page_id = str(row["id"])

            # 5. Require page_status='reviewed' (R6.b)
            if row["page_status"] != "reviewed":
                await message.answer("Страница не прошла ревью.")
                return

            # 6. assert_publishable — at least one source (R6.c precondition)
            import uuid as _uuid_module
            page_uuid = _uuid_module.UUID(page_id)
            try:
                await assert_publishable(session, page_id=page_uuid)
            except WikiSourcesMissingError:
                await message.answer("Нет источников.")
                return

            # 7. validate_sources — all sources must be clean (R6.c)
            result = await validate_sources(session, page_id=page_uuid)
            if not result.valid:
                await message.answer(_format_source_check_summary(result))
                return

            # 8. Capture prior values from the FOR UPDATE-locked row (plan §5.E step 6a)
            prior_pe: bool = bool(row["public_enabled"])
            prior_rp: str = str(row["robots_policy"])

            # 9. UPDATE public_enabled=true (robots_policy unchanged by /wiki_publish)
            await session.execute(
                text(
                    "UPDATE wiki_pages SET public_enabled = true, updated_at = now() "
                    "WHERE id = :page_id"
                ),
                {"page_id": page_id},
            )

            # 10. INSERT audit log
            source_check_json = json.dumps(result.to_dict())
            await session.execute(
                text(
                    "INSERT INTO wiki_publication_log "
                    "(wiki_page_id, action, actor_user_id, prior_public_enabled, "
                    " new_public_enabled, prior_robots_policy, new_robots_policy, "
                    " source_check_result) "
                    "VALUES "
                    "(:pid, 'publish', :actor, :prior_pe, true, :prior_rp, :new_rp, "
                    " CAST(:src AS jsonb))"
                ),
                {
                    "pid": page_id,
                    "actor": actor_id,
                    "prior_pe": prior_pe,
                    "prior_rp": prior_rp,
                    "new_rp": prior_rp,  # robots_policy unchanged by publish
                    "src": source_check_json,
                },
            )

    except Exception as exc:
        logger.exception("cmd_wiki_publish: transaction failed for slug=%s", slug)
        await message.answer(
            f"❌ Ошибка публикации: <code>{html_escape(str(exc)[:300])}</code>",
            parse_mode="HTML",
        )
        return

    # 11. Reply success
    await message.answer(
        f"Опубликовано: /wiki/public/{html_escape(slug)}",
        parse_mode="HTML",
    )


# ── /wiki_unpublish ───────────────────────────────────────────────────────────


@router.message(Command("wiki_unpublish"), PrivateChatFilter())
async def cmd_wiki_unpublish(
    message: Message,
    session: AsyncSession,
    command: CommandObject,
) -> None:
    """Withdraw a wiki page from public view (PHASE9_PLAN.md §5.F).

    Steps (all inside ONE transaction):
    1. Admin gate.
    2. Feature-flag gate.
    3. Parse slug.
    4. SELECT wiki_pages FOR UPDATE.
    5. Capture prior_pe, prior_rp.
    6. UPDATE public_enabled=false, robots_policy='noindex'.
    7. INSERT wiki_publication_log(action='unpublish', …).
    8. Reply.
    """
    if not _is_admin(message):
        await message.answer("Команда доступна только администратору.")
        return

    wiki_enabled = await FeatureFlagRepo.get(session, _FEATURE_FLAG)
    if not wiki_enabled:
        await message.answer("Wiki временно недоступна.")
        return

    slug = (command.args or "").strip()
    if not slug:
        await message.answer(
            "Использование: <code>/wiki_unpublish &lt;slug&gt;</code>",
            parse_mode="HTML",
        )
        return

    actor_id = _actor_id(message)

    try:
        async with session.begin_nested():
            row = (
                await session.execute(
                    text(
                        "SELECT id, public_enabled, robots_policy "
                        "FROM wiki_pages WHERE slug = :slug FOR UPDATE"
                    ),
                    {"slug": slug},
                )
            ).mappings().one_or_none()

            if row is None:
                await message.answer(f"Страница не найдена: <code>{html_escape(slug)}</code>.", parse_mode="HTML")
                return

            page_id = str(row["id"])
            prior_pe: bool = bool(row["public_enabled"])
            prior_rp: str = str(row["robots_policy"])

            await session.execute(
                text(
                    "UPDATE wiki_pages "
                    "SET public_enabled = false, robots_policy = 'noindex', updated_at = now() "
                    "WHERE id = :page_id"
                ),
                {"page_id": page_id},
            )

            await session.execute(
                text(
                    "INSERT INTO wiki_publication_log "
                    "(wiki_page_id, action, actor_user_id, prior_public_enabled, "
                    " new_public_enabled, prior_robots_policy, new_robots_policy, "
                    " source_check_result) "
                    "VALUES "
                    "(:pid, 'unpublish', :actor, :prior_pe, false, :prior_rp, 'noindex', "
                    " CAST(:src AS jsonb))"
                ),
                {
                    "pid": page_id,
                    "actor": actor_id,
                    "prior_pe": prior_pe,
                    "prior_rp": prior_rp,
                    "src": "{}",
                },
            )

    except Exception as exc:
        logger.exception("cmd_wiki_unpublish: transaction failed for slug=%s", slug)
        await message.answer(
            f"❌ Ошибка снятия с публикации: <code>{html_escape(str(exc)[:300])}</code>",
            parse_mode="HTML",
        )
        return

    await message.answer(f"Снято с публикации: {html_escape(slug)}")


# ── /wiki_robots ──────────────────────────────────────────────────────────────


@router.message(Command("wiki_robots"), PrivateChatFilter())
async def cmd_wiki_robots(
    message: Message,
    session: AsyncSession,
    command: CommandObject,
) -> None:
    """Set robots_policy for a wiki page.

    Usage: ``/wiki_robots <slug> {index|noindex}``

    For ``index``: requires public_enabled=true (R6.d).
    For ``noindex``: always succeeds.

    Steps (all inside ONE transaction):
    1. Admin gate.
    2. Feature-flag gate.
    3. Parse slug + policy.
    4. SELECT wiki_pages FOR UPDATE.
    5. For index: refuse if not public.
    6. Capture prior_rp.
    7. UPDATE robots_policy.
    8. INSERT wiki_publication_log.
    9. Reply.
    """
    if not _is_admin(message):
        await message.answer("Команда доступна только администратору.")
        return

    wiki_enabled = await FeatureFlagRepo.get(session, _FEATURE_FLAG)
    if not wiki_enabled:
        await message.answer("Wiki временно недоступна.")
        return

    args = (command.args or "").strip().split()
    if len(args) < 2 or args[1].lower() not in ("index", "noindex"):
        await message.answer(
            "Использование: <code>/wiki_robots &lt;slug&gt; {index|noindex}</code>",
            parse_mode="HTML",
        )
        return

    slug = args[0]
    new_rp = args[1].lower()
    action = "robots_index" if new_rp == "index" else "robots_noindex"

    actor_id = _actor_id(message)

    try:
        async with session.begin_nested():
            row = (
                await session.execute(
                    text(
                        "SELECT id, public_enabled, robots_policy "
                        "FROM wiki_pages WHERE slug = :slug FOR UPDATE"
                    ),
                    {"slug": slug},
                )
            ).mappings().one_or_none()

            if row is None:
                await message.answer(f"Страница не найдена: <code>{html_escape(slug)}</code>.", parse_mode="HTML")
                return

            page_id = str(row["id"])
            public_enabled: bool = bool(row["public_enabled"])
            prior_rp: str = str(row["robots_policy"])

            # R6.d — cannot index a non-public page
            if new_rp == "index" and not public_enabled:
                await message.answer("Нельзя индексировать непубличную страницу.")
                return

            await session.execute(
                text(
                    "UPDATE wiki_pages SET robots_policy = :rp, updated_at = now() "
                    "WHERE id = :page_id"
                ),
                {"rp": new_rp, "page_id": page_id},
            )

            await session.execute(
                text(
                    "INSERT INTO wiki_publication_log "
                    "(wiki_page_id, action, actor_user_id, prior_public_enabled, "
                    " new_public_enabled, prior_robots_policy, new_robots_policy, "
                    " source_check_result) "
                    "VALUES "
                    "(:pid, :action, :actor, :pe, :pe, :prior_rp, :new_rp, "
                    " CAST(:src AS jsonb))"
                ),
                {
                    "pid": page_id,
                    "action": action,
                    "actor": actor_id,
                    "pe": public_enabled,
                    "prior_rp": prior_rp,
                    "new_rp": new_rp,
                    "src": "{}",
                },
            )

    except Exception as exc:
        logger.exception("cmd_wiki_robots: transaction failed for slug=%s policy=%s", slug, new_rp)
        await message.answer(
            f"❌ Ошибка: <code>{html_escape(str(exc)[:300])}</code>",
            parse_mode="HTML",
        )
        return

    await message.answer(
        f"Robots policy для <code>{html_escape(slug)}</code>: <code>{new_rp}</code>",
        parse_mode="HTML",
    )


__all__ = ["router"]
