from __future__ import annotations

import html
import logging
import os
from datetime import datetime
from decimal import Decimal
from typing import Any

from aiogram import Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command, CommandObject
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
    synthesize_answer,
)
from bot.services.llm_providers import LLMProvider
from bot.services.llm_providers.anthropic import (
    DEFAULT_ANTHROPIC_MODEL,
    AnthropicProvider,
)
from bot.services.llm_providers.openai import DEFAULT_OPENAI_MODEL, OpenAIProvider
from bot.services.message_persistence import persist_message_with_policy
from bot.services.qa import run_qa

logger = logging.getLogger(__name__)

router = Router(name="qa")

QA_FEATURE_FLAG = "memory.qa.enabled"
LLM_SYNTHESIS_FEATURE_FLAG = "memory.qa.llm_synthesis.enabled"

# Default prompt template version per contracts.md §12.5 ratification.
# Stays in sync with whatever ``_build_prompt`` ships in the gateway today.
DEFAULT_PROMPT_TEMPLATE_VERSION = "v1.0.0"


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
        f"<a href=\"{html.escape(link, quote=True)}\">сообщение</a> · "
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
        f"<a href=\"{html.escape(anchor_link, quote=True)}\">первоисточник</a> · "
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
                f"<a href=\"{html.escape(link, quote=True)}\">сообщение</a> · "
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
    """Resolve LLM gateway config from env vars with sane defaults.

    Reads:
      * ``LLM_PROVIDER`` (default ``"anthropic"``) — gateway provider tag.
      * ``LLM_MODEL`` (provider-specific default) — model id passed to
        ``MODEL_PRICING`` and the provider SDK.
      * ``LLM_DAILY_USD_CEILING`` (default ``Decimal("5.00")``).
      * ``LLM_MONTHLY_USD_CEILING`` (default ``Decimal("50.00")``).

    ``prompt_template_version`` is the ``v1.0.0`` baseline introduced
    with T5-04b — see ``bot/services/llm_pricing.py`` + contracts.md §12.5.
    """
    provider = os.environ.get("LLM_PROVIDER", "anthropic")
    if provider not in ("anthropic", "openai"):
        # Reject typos early; the gateway expects a Literal["anthropic","openai"].
        raise ValueError(f"unknown provider: {provider}")

    default_model = (
        DEFAULT_OPENAI_MODEL if provider == "openai" else DEFAULT_ANTHROPIC_MODEL
    )
    model = os.environ.get("LLM_MODEL", default_model)
    daily = Decimal(os.environ.get("LLM_DAILY_USD_CEILING", "5.00"))
    monthly = Decimal(os.environ.get("LLM_MONTHLY_USD_CEILING", "50.00"))
    return LLMGatewayConfig(
        provider=provider,  # type: ignore[arg-type]  # validated above
        model=model,
        daily_ceiling_usd=daily,
        monthly_ceiling_usd=monthly,
        prompt_template_version=DEFAULT_PROMPT_TEMPLATE_VERSION,
    )


def _resolve_provider(provider_name: str) -> LLMProvider:
    """Instantiate Anthropic or OpenAI provider per config.

    Raises ``ValueError`` on unknown ``provider_name``. The handler catches
    every exception from the LLM-synthesis branch and falls back to the
    Phase 4 rendering path, so a misconfigured provider never crashes the
    bot — but it does abstain from synthesis.
    """
    if provider_name == "anthropic":
        return AnthropicProvider()
    if provider_name == "openai":
        return OpenAIProvider()
    raise ValueError(f"unknown provider: {provider_name}")


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
            author_name = _author_name(
                users_by_id.get(item.user_id) if item.user_id else None
            )
            date_text = _format_date(item.message_date)
            snippet = _safe_headline(item.snippet)
            parts.append(f"[{idx}] {date_text} — {author_name}: {snippet}")
        return "\n".join(parts)

    # T6-07 mixed/card path.
    for idx, item in enumerate(bundle.items, start=1):
        if item.source_type == "card":
            parts.append(_format_synth_card_footer(idx, item))
        else:
            author_name = _author_name(
                users_by_id.get(item.user_id) if item.user_id else None
            )
            date_text = _format_date(item.message_date)
            snippet = _safe_headline(item.snippet)
            parts.append(f"[{idx}] {date_text} — {author_name}: {snippet}")
    return "\n".join(parts)


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
) -> None:
    await QaTraceRepo.create(
        session,
        user_tg_id=user_tg_id,
        chat_id=chat_id,
        query=query,
        evidence_ids=evidence_ids,
        abstained=abstained,
        redact_query=redact_query,
    )


@router.message(Command("recall"))
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
        except Exception:
            logger.exception(
                "llm_synthesis_dispatch_failed; falling back to Phase 4 path",
                extra={"qa_trace_id": trace.id, "chat_id": message.chat.id},
            )
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
            reply_text = _format_synthesized_response(
                synth_result, result.bundle, users_by_id
            )
        else:
            reply_text = _format_response(result.bundle, users_by_id)
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
