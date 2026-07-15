from __future__ import annotations

import html
import logging
from datetime import datetime
from typing import Any

from aiogram import Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import TelegramUpdate
from bot.db.repos.feature_flag import FeatureFlagRepo
from bot.db.repos.llm_synthesis_cache import SynthesisCacheRepo
from bot.db.repos.llm_usage_ledger import LedgerRepo
from bot.db.repos.qa_trace import QaTraceRepo
from bot.db.repos.user import UserRepo
from bot.services.evidence import EvidenceBundle, EvidenceItem
from bot.services.governance import detect_policy
from bot.services.llm_gateway import (
    AnswerWithCitations,
    LLMGatewayConfig,
    _safe_qa_provider_error_subtype,
    load_gateway_config,
    resolve_provider,
    synthesize_answer,
)
from bot.services.llm_providers import LLMProvider
from bot.services.message_persistence import persist_message_with_policy
from bot.services.qa import run_qa
from bot.services.qa_guardrails import (
    MAX_AI_ANSWER_CHARS,
    SENSITIVE_QA_REFUSAL,
    SENSITIVE_QA_TRACE_MARKER,
    acquire_daily_llm_question_slot,
    build_guarded_llm_query,
    contains_secret_like_data,
    limit_answer_text,
)
from bot.services.qa_trigger import ShkoderQuestionFilter, TriggeredQuestion

logger = logging.getLogger(__name__)

router = Router(name="qa")

QA_FEATURE_FLAG = "memory.qa.enabled"
LLM_SYNTHESIS_FEATURE_FLAG = "memory.qa.llm_synthesis.enabled"
QA_EVIDENCE_LIMIT = 3

# The evidence-bearing prompt shipped after the original v1.0.0 cache format.
# Keep this pin explicit so old ungrounded cache rows can never be reused.
DEFAULT_PROMPT_TEMPLATE_VERSION = "v1.1.0"


def _short_chat_id(chat_id: int) -> str:
    chat_id_str = str(chat_id)
    return chat_id_str.removeprefix("-100") if chat_id_str.startswith("-100") else chat_id_str


def _format_date(value: datetime) -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M")


def _safe_headline(snippet: str) -> str:
    escaped = html.escape(snippet, quote=False)
    return escaped.replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>")


def _author_name(user: object | None) -> str:
    if user is None:
        return "—"

    first_name = getattr(user, "first_name", None)
    last_name = getattr(user, "last_name", None)
    username = getattr(user, "username", None)

    if first_name:
        name = str(first_name)
        if last_name:
            name = f"{name} {last_name}"
        return html.escape(name)
    if username:
        return html.escape(f"@{username}")
    return "—"


def _format_message_item(
    item: EvidenceItem,
    short_chat_id: str,
    users_by_id: dict[int, object],
) -> str:
    """T6-07: message-hit renderer used in the mixed-bundle branch only.

    The pure-message bundle keeps its rendering INLINED in ``_format_response``
    for byte-for-byte Phase 4 preservation (see comment at the Phase 4 fast
    path below; tests/handlers/test_qa_recall_phase4_preserved.py is the
    regression guard).
    """
    author_name = _author_name(users_by_id.get(item.user_id) if item.user_id else None)
    date_text = _format_date(item.message_date)
    snippet = _safe_headline(item.snippet)
    link = f"https://t.me/c/{short_chat_id}/{item.message_id}"
    return (
        f"<blockquote>{snippet}</blockquote>\n"
        f"<i>— {author_name}, {date_text}</i> · "
        f'<a href="{html.escape(link, quote=True)}">сообщение</a> · '
        f"<code>message_version_id:{item.message_version_id}</code>"
    )


def _format_card_item(item: EvidenceItem, short_chat_id: str) -> str:
    """T6-07 card-hit rendering with back-citation trace.

    Per PHASE6_PLAN.md §1 invariant #4 every card citation MUST surface the
    source ``message_version_id`` set so the rendered output traces back to
    the underlying messages. The anchor source's Telegram message is also
    linked so admins can jump to the primary source.
    """
    date_text = _format_date(item.message_date)  # T6-06 substitutes approved_at here
    snippet = _safe_headline(item.snippet)
    anchor_link = f"https://t.me/c/{short_chat_id}/{item.message_id}"
    # R-07-07: truncate source mvid list at 5; append "+N more" for the remainder.
    all_mvids = item.card_source_message_version_ids
    _MVID_DISPLAY_LIMIT = 5
    if len(all_mvids) > _MVID_DISPLAY_LIMIT:
        shown = ", ".join(str(m) for m in all_mvids[:_MVID_DISPLAY_LIMIT])
        mvid_list = f"{shown}, +{len(all_mvids) - _MVID_DISPLAY_LIMIT} more"
    else:
        mvid_list = ", ".join(str(m) for m in all_mvids)
    return (
        f"<blockquote>{snippet}</blockquote>\n"
        f"<i>\U0001f4cb Карточка, {date_text}</i> · "
        f'<a href="{html.escape(anchor_link, quote=True)}">первоисточник</a> · '
        f"<code>card_id:{item.card_id}</code> · "
        f"<code>sources:[{mvid_list}]</code>"
    )


def _format_response(bundle: EvidenceBundle, users_by_id: dict[int, object]) -> str:
    if bundle.abstained:
        return "Не нашёл подходящих свидетельств в истории чата."

    # T6-07: detect mixed-bundle (any card hit) vs pure-message bundle. The
    # pure-message path stays INLINED below for byte-for-byte Phase 4 preservation
    # (tests/handlers/test_qa_recall_phase4_preserved.py is the regression guard).
    has_card = any(item.source_type == "card" for item in bundle.items)
    if not has_card:
        # Phase 4 path — preserved byte-for-byte from the original implementation.
        parts = ["<b>Найденные свидетельства:</b>"]
        short_chat_id = _short_chat_id(bundle.chat_id)
        for item in bundle.items:
            author_name = _author_name(users_by_id.get(item.user_id) if item.user_id else None)
            date_text = _format_date(item.message_date)
            snippet = _safe_headline(item.snippet)
            link = f"https://t.me/c/{short_chat_id}/{item.message_id}"
            parts.append(
                f"<blockquote>{snippet}</blockquote>\n"
                f"<i>— {author_name}, {date_text}</i> · "
                f'<a href="{html.escape(link, quote=True)}">сообщение</a> · '
                f"<code>message_version_id:{item.message_version_id}</code>"
            )
        return "\n\n".join(parts)

    # T6-07 mixed/card path — uses the helper renderers above.
    parts = ["<b>Найденные свидетельства:</b>"]
    short_chat_id = _short_chat_id(bundle.chat_id)
    for item in bundle.items:
        if item.source_type == "card":
            parts.append(_format_card_item(item, short_chat_id))
        else:
            parts.append(_format_message_item(item, short_chat_id, users_by_id))
    return "\n\n".join(parts)


def _load_gateway_config() -> LLMGatewayConfig:
    """Load the shared gateway config with the QA prompt-version pin."""

    return load_gateway_config(
        prompt_template_version=DEFAULT_PROMPT_TEMPLATE_VERSION,
    )


def _resolve_provider(provider_name: str) -> LLMProvider:
    """Instantiate a configured provider through the shared resolver."""

    return resolve_provider(provider_name)


def _format_synthesized_response(
    answer: AnswerWithCitations,
    bundle: EvidenceBundle,
    users_by_id: dict[int, object],
) -> str:
    """HTML reply for a synthesized answer with citation footer.

    Layout::

        <synthesized answer — HTML-escaped>

        <b>Источники:</b>
        [1] {date} — {author}: {snippet}
        [2] ...

    The footer enumerates ``bundle.items`` in bundle order so citation
    markers ``[N]`` in the synthesized text deterministically resolve to
    the same evidence row. ``answer.citation_ids`` is NOT used to drive
    the footer because the v1.0.0 prompt template does not yet emit
    structured citation markers — F5 (gateway) keeps citation_ids tied to
    surviving evidence so the cascade can invalidate them, but the
    user-facing layout reuses the Phase 4 evidence list verbatim.
    """
    # T6-07: detect mixed-bundle. Pure-message bundles keep the Phase 5 footer
    # byte-for-byte (tests/handlers/test_qa_llm_synthesis.py is the guard).
    has_card = any(item.source_type == "card" for item in bundle.items)
    answer_text = html.escape(answer.answer_text, quote=False)
    parts = [answer_text, "", "<b>Источники:</b>"]
    if not has_card:
        # Phase 5 path — preserved byte-for-byte.
        for idx, item in enumerate(bundle.items, start=1):
            author_name = _author_name(users_by_id.get(item.user_id) if item.user_id else None)
            date_text = _format_date(item.message_date)
            snippet = _safe_headline(item.snippet)
            parts.append(f"[{idx}] {date_text} — {author_name}: {snippet}")
        return "\n".join(parts)

    # T6-07 mixed/card path.
    for idx, item in enumerate(bundle.items, start=1):
        if item.source_type == "card":
            parts.append(_format_synth_card_footer(idx, item))
        else:
            author_name = _author_name(users_by_id.get(item.user_id) if item.user_id else None)
            date_text = _format_date(item.message_date)
            snippet = _safe_headline(item.snippet)
            parts.append(f"[{idx}] {date_text} — {author_name}: {snippet}")
    return "\n".join(parts)


def _escaped_text_with_budget(value: str, max_chars: int) -> str:
    """Return normalized, escaped text without splitting an HTML entity."""

    if max_chars <= 0:
        return ""
    normalized = limit_answer_text(value)
    escaped = html.escape(normalized, quote=False)
    if len(escaped) <= max_chars:
        return escaped

    candidate = normalized[: max(0, max_chars - 1)].rstrip()
    while candidate:
        escaped = html.escape(f"{candidate}…", quote=False)
        if len(escaped) <= max_chars:
            return escaped
        candidate = candidate[:-1].rstrip()
    return "…" if max_chars >= 1 else ""


def _compact_author_value(user: object | None) -> str:
    if user is None:
        return "—"
    first_name = getattr(user, "first_name", None)
    last_name = getattr(user, "last_name", None)
    username = getattr(user, "username", None)
    if first_name:
        raw = str(first_name)
        if last_name:
            raw = f"{raw} {last_name}"
        return raw
    elif username:
        return f"@{username}"
    return "—"


def _compact_author(user: object | None) -> str:
    return _escaped_text_with_budget(_compact_author_value(user), 40)


def _format_bounded_mention_response(
    answer_text: str,
    bundle: EvidenceBundle,
    users_by_id: dict[int, object],
    *,
    sources_heading: str = "Источники:",
) -> str:
    """Render the entire mention answer, including sources, within 1200 chars."""

    if contains_secret_like_data(answer_text) or any(
        contains_secret_like_data(item.snippet)
        or contains_secret_like_data(
            _compact_author_value(
                users_by_id.get(item.user_id) if item.user_id is not None else None
            )
        )
        for item in bundle.items[:QA_EVIDENCE_LIMIT]
    ):
        return SENSITIVE_QA_REFUSAL

    source_lines: list[str] = []
    for idx, item in enumerate(bundle.items[:QA_EVIDENCE_LIMIT], start=1):
        date_text = item.message_date.astimezone().strftime("%Y-%m-%d")
        author = _compact_author(
            users_by_id.get(item.user_id) if item.user_id is not None else None
        )
        snippet = _escaped_text_with_budget(item.snippet, 120)
        source_lines.append(f"[{idx}] {date_text} — {author}: {snippet}")

    # ponytail: a fixed three-source footer avoids an HTML-aware truncator;
    # add one only if Telegram formatting grows beyond this known-safe shape.
    footer = f"<b>{html.escape(sources_heading)}</b>\n" + "\n".join(source_lines)
    separator = "\n\n" if answer_text else ""
    answer_budget = MAX_AI_ANSWER_CHARS - len(separator) - len(footer)
    if answer_budget < 1:
        # Defensive fallback for future footer changes.  Evidence identifiers
        # remain visible while untrusted snippets are omitted.
        source_lines = [
            f"[{idx}] message_version_id:{item.message_version_id}"
            for idx, item in enumerate(
                bundle.items[:QA_EVIDENCE_LIMIT],
                start=1,
            )
        ]
        footer = f"<b>{html.escape(sources_heading)}</b>\n" + "\n".join(source_lines)
        answer_budget = MAX_AI_ANSWER_CHARS - len(separator) - len(footer)

    if answer_budget < 0:
        raise ValueError("mention source footer exceeds configured limit")
    bounded_answer = _escaped_text_with_budget(answer_text, answer_budget)
    rendered = f"{bounded_answer}{separator}{footer}"
    if len(rendered) > MAX_AI_ANSWER_CHARS:
        raise ValueError("bounded mention response exceeds configured limit")
    return rendered


async def _reply_to_mention(message: Message, text: str, **kwargs: Any) -> None:
    """Apply the final secret/size fence shared by every mention reply."""

    reply_text = text
    if contains_secret_like_data(text) or contains_secret_like_data(html.unescape(text)):
        reply_text = SENSITIVE_QA_REFUSAL
    if len(reply_text) > MAX_AI_ANSWER_CHARS:
        raise ValueError("mention reply exceeds configured limit")
    await message.reply(reply_text, **kwargs)


def _format_synth_card_footer(idx: int, item: EvidenceItem) -> str:
    """T6-07 Phase 5 synthesis-mode card footer entry."""
    date_text = _format_date(item.message_date)  # T6-06 substitutes approved_at
    snippet = _safe_headline(item.snippet)
    source_count = len(item.card_source_message_version_ids)
    return (
        f"[{idx}] {date_text} — \U0001f4cb Card "
        f"<code>{item.card_id}</code> "
        f"(sources: {source_count}): {snippet}"
    )


async def _write_trace(
    session: AsyncSession,
    *,
    user_tg_id: int,
    chat_id: int,
    query: str,
    evidence_ids: list[int],
    abstained: bool,
    redact_query: bool,
    source_chat_message_id: int | None = None,
) -> None:
    await QaTraceRepo.create(
        session,
        user_tg_id=user_tg_id,
        chat_id=chat_id,
        query=query,
        evidence_ids=evidence_ids,
        abstained=abstained,
        redact_query=redact_query,
        source_chat_message_id=source_chat_message_id,
    )


async def _write_sensitive_trace(
    session: AsyncSession,
    *,
    user_tg_id: int,
    chat_id: int,
    source_chat_message_id: int,
) -> None:
    await _write_trace(
        session,
        user_tg_id=user_tg_id,
        chat_id=chat_id,
        query=SENSITIVE_QA_TRACE_MARKER,
        evidence_ids=[],
        abstained=True,
        redact_query=True,
        source_chat_message_id=source_chat_message_id,
    )


async def recall_handler(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    raw_update: TelegramUpdate | None = None,  # surfaced by RawUpdatePersistenceMiddleware
    **data: Any,
) -> None:
    # Persist the /recall message itself FIRST, regardless of feature-flag state.
    # This closes the silent-drop hole when memory.qa.enabled=False.
    # Only persists for community-chat messages with a known sender.
    if message.chat.id == settings.COMMUNITY_CHAT_ID and message.from_user is not None:
        await UserRepo.upsert(
            session,
            telegram_id=message.from_user.id,
            username=getattr(message.from_user, "username", None),
            first_name=getattr(message.from_user, "first_name", None),
            last_name=getattr(message.from_user, "last_name", None),
        )
        await persist_message_with_policy(
            session,
            message,
            raw_update_id=raw_update.id if raw_update is not None else None,
            source="live",
        )

    if not await FeatureFlagRepo.get(session, QA_FEATURE_FLAG):
        return

    if message.from_user is None:
        return

    query = (command.args or "").strip()
    policy, _payload = detect_policy(text=query, caption=None)
    redact_query = policy != "normal"

    async def audit_empty(abstained: bool = True) -> None:
        await _write_trace(
            session,
            user_tg_id=message.from_user.id,
            chat_id=message.chat.id,
            query=query,
            evidence_ids=[],
            abstained=abstained,
            redact_query=redact_query,
        )

    if message.chat.id != settings.COMMUNITY_CHAT_ID:
        try:
            await message.reply("Команда /recall работает только в community чате.")
        except TelegramForbiddenError:
            # Bot lacks can_send_messages in this chat (e.g. kicked, restricted).
            # Audit-only path: still record the abstain trace, do not raise.
            logger.info(
                "recall refused: bot lacks send permission",
                extra={
                    "chat_id": message.chat.id,
                    "user_id": getattr(message.from_user, "id", None),
                },
            )
        await audit_empty()
        return

    user = await UserRepo.get(session, message.from_user.id)
    if user is None or not (user.is_member or user.is_admin):
        await message.reply("Доступ только участникам сообщества.")
        await audit_empty()
        return

    if not query:
        await message.reply(
            "Использование: <code>/recall &lt;вопрос&gt;</code>",
            parse_mode="HTML",
        )
        await audit_empty()
        return

    result = await run_qa(
        session,
        query=query,
        chat_id=message.chat.id,
        redact_query_in_audit=redact_query,
    )

    users_by_id: dict[int, object] = {}
    for item in result.bundle.items:
        if item.user_id is None or item.user_id in users_by_id:
            continue
        author = await UserRepo.get(session, item.user_id)
        if author is not None:
            users_by_id[item.user_id] = author

    # Phase 5 LLM synthesis branch — only when flag ON AND bundle non-empty.
    # When the flag is OFF (or bundle empty), execution falls through to the
    # byte-for-byte Phase 4 path below (contracts.md §6.2).
    if (
        await FeatureFlagRepo.get(session, LLM_SYNTHESIS_FEATURE_FLAG)
        and not result.bundle.abstained
        and len(result.bundle.evidence_ids) > 0
    ):
        quota = await acquire_daily_llm_question_slot(
            session,
            user_tg_id=message.from_user.id,
        )
        if not quota.allowed:
            deterministic_answer = _format_response(
                result.bundle,
                users_by_id,
            )
            await message.reply(
                f"Лимит — {quota.limit} AI-вопроса в день. "
                f"Показываю обычный поиск без AI.\n\n{deterministic_answer}",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            await _write_trace(
                session,
                user_tg_id=message.from_user.id,
                chat_id=message.chat.id,
                query=query,
                evidence_ids=result.bundle.evidence_ids,
                abstained=False,
                redact_query=result.query_redacted,
            )
            return

        # BINDING 4-step ORDER per contracts.md §6.1. Tested in
        # tests/handlers/test_qa_llm_synthesis.py.
        #
        # Step 1: Create QaTrace FIRST so the gateway can populate
        # llm_usage_ledger.qa_trace_id from the start of the call. This
        # makes the §8 cascade layers join correctly via either FK
        # direction.
        trace = await QaTraceRepo.create(
            session,
            user_tg_id=message.from_user.id,
            chat_id=message.chat.id,
            query=query,
            evidence_ids=result.bundle.evidence_ids,
            abstained=False,
            redact_query=result.query_redacted,
        )

        # Step 2: dispatch synthesize_answer with required qa_trace_id +
        # DI deps. Any exception (provider misconfig, transport bug,
        # gateway invariant breach) is caught here so the handler NEVER
        # crashes the bot — the fallback path renders the Phase 4 reply.
        # The qa_traces row remains intact with LLM columns NULL so the
        # audit log shows "tried synthesis, dispatch failed". No
        # update_llm_fields call is issued (no ledger row to point at).
        try:
            cfg = _load_gateway_config()
            provider = _resolve_provider(cfg.provider)
            synth_result = await synthesize_answer(
                session,
                bundle=result.bundle,
                query=query,
                config=cfg,
                qa_trace_id=trace.id,
                ledger_repo=LedgerRepo(),
                cache_repo=SynthesisCacheRepo(),
                provider=provider,
            )
        except Exception as exc:
            logger.error(
                "recall_llm_synthesis_failed",
                extra={
                    "qa_trace_id": trace.id,
                    "chat_id": message.chat.id,
                    "error_class": type(exc).__name__,
                    "error_subtype": _safe_qa_provider_error_subtype(exc),
                },
            )
            # The trace (and any gateway ledger mutation completed before the
            # exception) must survive an outbound Telegram failure.
            await session.commit()
            await message.reply(
                _format_response(result.bundle, users_by_id),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return

        # Step 3: UPDATE QaTrace with LLM fields. Touches ONLY the 4
        # Phase 5 columns (query_text / evidence_ids / abstained /
        # query_redacted stay untouched per contracts.md §6.1 + §12.3).
        await QaTraceRepo.update_llm_fields(
            session,
            qa_trace_id=trace.id,
            llm_call_id=synth_result.llm_call_id,
            llm_response_summary=getattr(synth_result, "answer_text", None),
            llm_response_redacted=False,
            cost_usd=synth_result.cost_usd,
        )

        # Step 4: render the AnswerWithCitations template OR fall back to
        # the Phase 4 evidence list on Abstention.
        if isinstance(synth_result, AnswerWithCitations):
            reply_text = _format_synthesized_response(synth_result, result.bundle, users_by_id)
        else:
            reply_text = _format_response(result.bundle, users_by_id)
        # Keep paid usage/quota and the trace durable before the external send.
        # DbSessionMiddleware rolls back when Telegram raises.
        await session.commit()
        await message.reply(
            reply_text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    # Flag OFF (or bundle empty) → Phase 4 byte-for-byte path (UNCHANGED).
    # Do NOT refactor or reformat this block — tested by
    # tests/handlers/test_qa_recall_phase4_preserved.py.
    await message.reply(
        _format_response(result.bundle, users_by_id),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await _write_trace(
        session,
        user_tg_id=message.from_user.id,
        chat_id=message.chat.id,
        query=query,
        evidence_ids=result.bundle.evidence_ids,
        abstained=result.bundle.abstained,
        redact_query=result.query_redacted,
    )


@router.message(ShkoderQuestionFilter(settings.COMMUNITY_CHAT_ID))
async def mention_question_handler(
    message: Message,
    qa_question: TriggeredQuestion,
    session: AsyncSession,
    raw_update: TelegramUpdate | None = None,
) -> None:
    """Answer a member's mention/reply question through the evidence-only LLM path.

    Trigger eligibility is enforced by :class:`ShkoderQuestionFilter` before
    this handler runs.  This handler still fails closed on sender/chat state so
    direct calls or future router changes cannot widen the public Q&A surface.
    """

    sender = message.from_user
    if sender is None or sender.is_bot or message.chat.id != settings.COMMUNITY_CHAT_ID:
        return

    # qa.router is above chat_messages.router.  A triggering question is
    # consumed here, so persist it before any flag/access/LLM early return.
    await UserRepo.upsert(
        session,
        telegram_id=sender.id,
        username=getattr(sender, "username", None),
        first_name=sender.first_name,
        last_name=getattr(sender, "last_name", None),
    )
    persisted = await persist_message_with_policy(
        session,
        message,
        raw_update_id=raw_update.id if raw_update is not None else None,
        source="live",
    )

    if not await FeatureFlagRepo.get(session, QA_FEATURE_FLAG):
        return

    user = await UserRepo.get(session, sender.id)
    if user is None or not (user.is_member or user.is_admin):
        await _reply_to_mention(message, "Доступ только участникам сообщества.")
        return

    existing_trace = await QaTraceRepo.get_by_source_chat_message_id(
        session,
        persisted.chat_message.id,
    )
    query = qa_question.query
    query_redacted = False
    raw_content = message.text if isinstance(message.text, str) else message.caption
    if contains_secret_like_data(query) or (
        isinstance(raw_content, str) and contains_secret_like_data(raw_content)
    ):
        await _reply_to_mention(message, SENSITIVE_QA_REFUSAL)
        if existing_trace is None:
            await _write_sensitive_trace(
                session,
                user_tg_id=sender.id,
                chat_id=message.chat.id,
                source_chat_message_id=persisted.chat_message.id,
            )
        return
    if existing_trace is not None:
        if existing_trace.llm_response_summary:
            if contains_secret_like_data(existing_trace.llm_response_summary):
                await _reply_to_mention(message, SENSITIVE_QA_REFUSAL)
            else:
                prefix = "Уже отвечал на этот вопрос:\n\n"
                previous = _escaped_text_with_budget(
                    existing_trace.llm_response_summary,
                    MAX_AI_ANSWER_CHARS - len(prefix),
                )
                await _reply_to_mention(
                    message,
                    f"{prefix}{previous}",
                    parse_mode="HTML",
                )
        else:
            await _reply_to_mention(message, "Этот вопрос уже обработан.")
        return

    if not query:
        await _reply_to_mention(
            message,
            "Напиши вопрос после упоминания Шкодера или в ответе ему.",
        )
        await _write_trace(
            session,
            user_tg_id=sender.id,
            chat_id=message.chat.id,
            query="",
            evidence_ids=[],
            abstained=True,
            redact_query=query_redacted,
            source_chat_message_id=persisted.chat_message.id,
        )
        return

    # Conversational questions are AI-only.  The deterministic search service
    # remains an internal retrieval layer and is not exposed as a command.
    if not await FeatureFlagRepo.get(session, LLM_SYNTHESIS_FEATURE_FLAG):
        await _reply_to_mention(message, "AI-поиск по памяти сейчас недоступен.")
        await _write_trace(
            session,
            user_tg_id=sender.id,
            chat_id=message.chat.id,
            query=query,
            evidence_ids=[],
            abstained=True,
            redact_query=query_redacted,
            source_chat_message_id=persisted.chat_message.id,
        )
        return

    result = await run_qa(
        session,
        query=query,
        chat_id=message.chat.id,
        redact_query_in_audit=query_redacted,
        limit=QA_EVIDENCE_LIMIT,
    )
    if any(contains_secret_like_data(item.snippet) for item in result.bundle.items):
        await _reply_to_mention(message, SENSITIVE_QA_REFUSAL)
        await _write_sensitive_trace(
            session,
            user_tg_id=sender.id,
            chat_id=message.chat.id,
            source_chat_message_id=persisted.chat_message.id,
        )
        return
    if result.bundle.abstained or not result.bundle.evidence_ids:
        await _reply_to_mention(
            message,
            "Не нашёл достаточно подтверждений в памяти сообщества.",
        )
        await _write_trace(
            session,
            user_tg_id=sender.id,
            chat_id=message.chat.id,
            query=query,
            evidence_ids=[],
            abstained=True,
            redact_query=query_redacted,
            source_chat_message_id=persisted.chat_message.id,
        )
        return

    users_by_id: dict[int, object] = {}
    sensitive_author = False
    for item in result.bundle.items:
        if item.user_id is None or item.user_id in users_by_id:
            continue
        author = await UserRepo.get(session, item.user_id)
        if author is not None:
            if contains_secret_like_data(_compact_author_value(author)):
                sensitive_author = True
                break
            users_by_id[item.user_id] = author

    if sensitive_author:
        await _reply_to_mention(message, SENSITIVE_QA_REFUSAL)
        await _write_sensitive_trace(
            session,
            user_tg_id=sender.id,
            chat_id=message.chat.id,
            source_chat_message_id=persisted.chat_message.id,
        )
        return

    quota = await acquire_daily_llm_question_slot(
        session,
        user_tg_id=sender.id,
    )
    if not quota.allowed:
        deterministic_answer = _format_bounded_mention_response(
            f"Лимит — {quota.limit} AI-вопроса в день. Показываю обычный поиск без AI.",
            result.bundle,
            users_by_id,
            sources_heading="Найденные свидетельства:",
        )
        await _reply_to_mention(
            message,
            deterministic_answer,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        await _write_trace(
            session,
            user_tg_id=sender.id,
            chat_id=message.chat.id,
            query=query,
            evidence_ids=result.bundle.evidence_ids,
            abstained=False,
            redact_query=query_redacted,
            source_chat_message_id=persisted.chat_message.id,
        )
        return

    # Trace-before-gateway preserves the Phase 5 cascade/ledger FK contract.
    trace = await QaTraceRepo.create(
        session,
        user_tg_id=sender.id,
        chat_id=message.chat.id,
        query=query,
        evidence_ids=result.bundle.evidence_ids,
        abstained=False,
        redact_query=query_redacted,
        source_chat_message_id=persisted.chat_message.id,
    )
    try:
        cfg = _load_gateway_config()
        provider = _resolve_provider(cfg.provider)
        synth_result = await synthesize_answer(
            session,
            bundle=result.bundle,
            query=build_guarded_llm_query(query),
            config=cfg,
            qa_trace_id=trace.id,
            ledger_repo=LedgerRepo(),
            cache_repo=SynthesisCacheRepo(),
            provider=provider,
        )
    except Exception as exc:
        # Provider payloads may contain user content or credentials.  Log only
        # the exception class and stable audit identifiers, then preserve the
        # trace before attempting the deterministic Telegram fallback.
        logger.error(
            "mention_llm_synthesis_failed error_class=%s",
            type(exc).__name__,
            extra={"qa_trace_id": trace.id, "chat_id": message.chat.id},
        )
        await session.commit()
        await _reply_to_mention(
            message,
            _format_bounded_mention_response(
                "",
                result.bundle,
                users_by_id,
                sources_heading="Найденные свидетельства:",
            ),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    sensitive_gateway_refusal = not isinstance(
        synth_result, AnswerWithCitations
    ) and synth_result.reason in {"sensitive_input", "sensitive_output"}
    if sensitive_gateway_refusal:
        reply_text = SENSITIVE_QA_REFUSAL
        response_summary = None
        response_redacted = True
    elif isinstance(synth_result, AnswerWithCitations):
        try:
            bounded_answer = limit_answer_text(synth_result.answer_text)
        except ValueError:
            reply_text = SENSITIVE_QA_REFUSAL
            response_summary = None
            response_redacted = True
        else:
            if bounded_answer:
                reply_text = _format_bounded_mention_response(
                    bounded_answer,
                    result.bundle,
                    users_by_id,
                )
                response_summary = bounded_answer
                response_redacted = bounded_answer != synth_result.answer_text
            else:
                reply_text = _format_bounded_mention_response(
                    "",
                    result.bundle,
                    users_by_id,
                    sources_heading="Найденные свидетельства:",
                )
                response_summary = None
                response_redacted = bool(synth_result.answer_text)
    else:
        reply_text = _format_bounded_mention_response(
            "",
            result.bundle,
            users_by_id,
            sources_heading="Найденные свидетельства:",
        )
        response_summary = None
        response_redacted = False

    await QaTraceRepo.update_llm_fields(
        session,
        qa_trace_id=trace.id,
        llm_call_id=synth_result.llm_call_id,
        llm_response_summary=response_summary,
        llm_response_redacted=response_redacted,
        cost_usd=synth_result.cost_usd,
    )
    # Provider usage, cache, trace, and the per-user quota ledger must survive
    # an outbound Telegram failure.  The middleware may roll back after a send
    # exception, so make the paid/audited transaction durable first.
    await session.commit()
    await _reply_to_mention(
        message,
        reply_text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
