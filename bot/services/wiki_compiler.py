"""Revision-based compiler for durable, source-linked wiki topic pages.

The compiler owns orchestration and persistence, but never calls an LLM
provider directly.  A concrete adapter in ``llm_gateway`` must implement the
``WikiCompilerGateway`` seam, including the final pre-provider governance
revalidation and usage-ledger write.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    Uuid,
    bindparam,
    cast,
    column,
    func,
    literal,
    select,
    table,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, aggregate_order_by
from sqlalchemy.ext.asyncio import AsyncSession

_SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_CITATION_RE = re.compile(
    r"\[\^(?:mv:([1-9]\d*)|card:([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}))\]"
)
_MAX_TITLE_LENGTH = 240
_MAX_BODY_LENGTH = 100_000
_PROMPT_VERSION = "wiki-revision-v0.1.0"

_MESSAGE_SOURCES_SQL = """
SELECT
    mv.id AS message_version_id,
    COALESCE(mv.normalized_text, mv.text, mv.caption, '') AS content
FROM message_versions mv
JOIN chat_messages cm ON cm.id = mv.chat_message_id
WHERE mv.id = ANY(CAST(:ids AS bigint[]))
  AND cm.chat_id = :source_chat_id
  AND cm.current_version_id = mv.id
  AND cm.memory_policy = 'normal'
  AND cm.is_redacted = false
  AND mv.is_redacted = false
  AND NOT EXISTS (
      SELECT 1 FROM forget_events fe
      WHERE fe.status IN ('pending', 'processing', 'completed')
        AND fe.tombstone_key IN (
            'message:' || cm.chat_id::text || ':' || cm.message_id::text,
            'message_hash:' || mv.content_hash,
            'user:' || cm.user_id::text
        )
  )
ORDER BY mv.id
"""

_CARD_SOURCES_SQL = """
SELECT
    kc.id AS card_id,
    kc.title,
    kc.body_markdown,
    array_agg(cs.message_version_id ORDER BY cs.position, cs.message_version_id) AS source_mvids,
    bool_and(
        cm.chat_id = :source_chat_id
        AND cm.current_version_id = mv.id
        AND cm.memory_policy = 'normal'
        AND cm.is_redacted = false
        AND mv.is_redacted = false
        AND NOT EXISTS (
            SELECT 1 FROM forget_events fe
            WHERE fe.status IN ('pending', 'processing', 'completed')
              AND fe.tombstone_key IN (
                  'message:' || cm.chat_id::text || ':' || cm.message_id::text,
                  'message_hash:' || mv.content_hash,
                  'user:' || cm.user_id::text
              )
        )
    ) AS governed
FROM knowledge_cards kc
JOIN card_sources cs ON cs.card_id = kc.id
JOIN message_versions mv ON mv.id = cs.message_version_id
JOIN chat_messages cm ON cm.id = mv.chat_message_id
WHERE kc.id = ANY(CAST(:ids AS uuid[]))
  AND kc.card_status = 'approved'
GROUP BY kc.id, kc.title, kc.body_markdown
ORDER BY kc.id
"""

_WIKI_PAGES = table(
    "wiki_pages",
    column("id", Uuid()),
    column("slug", String(255)),
    column("title", Text()),
    column("body_markdown", Text()),
    column("updated_at", DateTime(timezone=True)),
    column("page_status", String(32)),
    column("visibility", String(32)),
    column("public_enabled", Boolean()),
    column("robots_policy", String(16)),
    column("validation_status", String(32)),
)
_WIKI_REVISIONS = table(
    "wiki_revisions",
    column("wiki_page_id", Uuid()),
    column("revision_seq", Integer()),
    column("edit_reason", Text()),
    column("source_card_ids_snapshot", JSONB()),
    column("source_message_version_ids_snapshot", JSONB()),
)
_WIKI_PAGE_CARD_SOURCES = table(
    "wiki_page_card_sources",
    column("wiki_page_id", Uuid()),
    column("card_id", Uuid()),
    column("position", Integer()),
)
_WIKI_PAGE_MESSAGE_SOURCES = table(
    "wiki_page_message_sources",
    column("wiki_page_id", Uuid()),
    column("message_version_id", BigInteger()),
    column("position", Integer()),
)

_WP = _WIKI_PAGES.alias("wp")
_WR = _WIKI_REVISIONS.alias("wr")
_WPCS = _WIKI_PAGE_CARD_SOURCES.alias("wpcs")
_WPMS = _WIKI_PAGE_MESSAGE_SOURCES.alias("wpms")


def _build_page_select():
    latest_revision = (
        select(_WR.c.revision_seq)
        .where(_WR.c.wiki_page_id == _WP.c.id)
        .order_by(_WR.c.revision_seq.desc())
        .limit(1)
    )
    latest_edit_reason = (
        select(_WR.c.edit_reason)
        .where(_WR.c.wiki_page_id == _WP.c.id)
        .order_by(_WR.c.revision_seq.desc())
        .limit(1)
        .scalar_subquery()
    )
    latest_card_snapshot = (
        select(_WR.c.source_card_ids_snapshot)
        .where(_WR.c.wiki_page_id == _WP.c.id)
        .order_by(_WR.c.revision_seq.desc())
        .limit(1)
        .scalar_subquery()
    )
    latest_message_snapshot = (
        select(_WR.c.source_message_version_ids_snapshot)
        .where(_WR.c.wiki_page_id == _WP.c.id)
        .order_by(_WR.c.revision_seq.desc())
        .limit(1)
        .scalar_subquery()
    )
    live_card_ids = (
        select(
            func.array_agg(
                aggregate_order_by(
                    cast(_WPCS.c.card_id, Text),
                    _WPCS.c.position,
                    _WPCS.c.card_id,
                )
            )
        )
        .where(_WPCS.c.wiki_page_id == _WP.c.id)
        .scalar_subquery()
    )
    live_message_ids = (
        select(
            func.array_agg(
                aggregate_order_by(
                    _WPMS.c.message_version_id,
                    _WPMS.c.position,
                    _WPMS.c.message_version_id,
                )
            )
        )
        .where(_WPMS.c.wiki_page_id == _WP.c.id)
        .scalar_subquery()
    )
    return (
        select(
            _WP.c.id,
            _WP.c.title,
            _WP.c.body_markdown,
            _WP.c.updated_at,
            _WP.c.page_status,
            _WP.c.visibility,
            _WP.c.public_enabled,
            _WP.c.robots_policy,
            _WP.c.validation_status,
            func.coalesce(latest_revision.scalar_subquery(), 0).label("revision_seq"),
            latest_edit_reason.label("latest_edit_reason"),
            func.coalesce(
                latest_card_snapshot,
                literal([], type_=JSONB()),
            ).label("input_card_ids"),
            func.coalesce(
                latest_message_snapshot,
                literal([], type_=JSONB()),
            ).label("input_mvids"),
            func.coalesce(
                live_card_ids,
                literal([], type_=ARRAY(Text())),
            ).label("card_ids"),
            func.coalesce(
                live_message_ids,
                literal([], type_=ARRAY(BigInteger())),
            ).label("mvids"),
        )
        .select_from(_WP)
        .where(_WP.c.slug == bindparam("slug"))
    )


_PAGE_SELECT = _build_page_select()
_PAGE_SELECT_FOR_UPDATE = _PAGE_SELECT.with_for_update(of=_WP)

_PAGE_SOURCE_CHAT_IDS_SQL = """
SELECT array_agg(DISTINCT cm.chat_id ORDER BY cm.chat_id) AS chat_ids
FROM (
    SELECT wpms.message_version_id
    FROM wiki_page_message_sources wpms
    WHERE wpms.wiki_page_id = :page_id
    UNION
    SELECT cs.message_version_id
    FROM wiki_page_card_sources wpcs
    JOIN card_sources cs ON cs.card_id = wpcs.card_id
    WHERE wpcs.wiki_page_id = :page_id
) page_sources
JOIN message_versions mv ON mv.id = page_sources.message_version_id
JOIN chat_messages cm ON cm.id = mv.chat_message_id
"""


class WikiCompilerError(RuntimeError):
    """Base error for a refused compilation."""


class WikiSourceRejectedError(WikiCompilerError):
    """A requested source is absent, unapproved, redacted, or forgotten."""


class WikiCompilerContractError(WikiCompilerError):
    """The gateway returned malformed or unsupported page content."""


class WikiConcurrentUpdateError(WikiCompilerError):
    """The topic changed while the gateway was producing its revision."""


@runtime_checkable
class WikiCompilerGateway(Protocol):
    """Required ``llm_gateway`` adapter surface.

    The implementation must revalidate every message id in ``source_cards``
    and ``source_messages`` immediately before provider dispatch, then record
    the call with ``call_type='wiki_compilation'`` in ``llm_usage_ledger``.
    The response must contain that positive ledger id and a full revised page,
    not a detached one-shot summary.
    """

    async def revise_wiki_topic(
        self, session: AsyncSession, **kwargs: Any
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class WikiCompilationResult:
    page_id: uuid.UUID
    slug: str
    revision_seq: int
    changed: bool
    llm_usage_ledger_id: int


@dataclass(frozen=True)
class _PageState:
    page_id: uuid.UUID
    title: str
    body: str
    updated_at: datetime
    page_status: str
    visibility: str
    public_enabled: bool
    robots_policy: str
    validation_status: str
    revision_seq: int
    latest_edit_reason: str | None
    input_card_ids: tuple[uuid.UUID, ...]
    input_mvids: tuple[int, ...]
    card_ids: tuple[uuid.UUID, ...]
    mvids: tuple[int, ...]


async def compile_topic_page(
    session: AsyncSession,
    *,
    slug: str,
    title_hint: str,
    source_card_ids: Sequence[uuid.UUID],
    source_message_version_ids: Sequence[int],
    actor_user_id: int,
    gateway: WikiCompilerGateway,
    publication_authorized: bool,
    source_chat_id: int,
) -> WikiCompilationResult:
    """Create or revise one durable topic page in the caller's transaction."""
    if publication_authorized is not True:
        raise WikiCompilerContractError("explicit publication authorization is required")
    _validate_request(
        slug,
        title_hint,
        source_card_ids,
        source_message_version_ids,
        source_chat_id=source_chat_id,
    )
    card_ids = tuple(sorted(set(source_card_ids), key=str))
    direct_mvids = tuple(sorted(set(int(value) for value in source_message_version_ids)))
    base = await _load_page(session, slug=slug)
    if base is not None:
        if base.latest_edit_reason != "automatic_compiler":
            raise WikiSourceRejectedError(
                "existing wiki page is not owned by the automatic compiler"
            )
        await _assert_page_source_chat_scope(
            session,
            page_id=base.page_id,
            source_chat_id=source_chat_id,
        )
    cards, messages, allowed_mvids = await _load_governed_sources(
        session,
        card_ids=card_ids,
        direct_mvids=direct_mvids,
        source_chat_id=source_chat_id,
    )
    source_snapshot_hash = _source_snapshot_hash(cards=cards, messages=messages)

    draft = await gateway.revise_wiki_topic(
        session,
        slug=slug,
        title_hint=title_hint,
        prior_title=base.title if base else None,
        prior_body_markdown=base.body if base else None,
        prior_revision_seq=base.revision_seq if base else 0,
        source_cards=cards,
        source_messages=messages,
        prompt_template_version=_PROMPT_VERSION,
        source_chat_id=source_chat_id,
    )
    title, body, ledger_id, cited_cards, cited_mvids = _validate_draft(
        draft, allowed_cards=set(card_ids), allowed_mvids=allowed_mvids
    )
    input_mvid_snapshot = tuple(
        sorted(
            set(direct_mvids).union(*(set(card["source_message_version_ids"]) for card in cards))
        )
    )

    await session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_id)"),
        {"lock_id": _slug_lock_id(slug)},
    )
    current = await _load_page(session, slug=slug, for_update=True)
    if current != base:
        raise WikiConcurrentUpdateError(f"wiki topic {slug!r} changed during compilation")

    # Recheck after the network call.  The gateway contract performs the
    # privacy-critical pre-dispatch recheck; this second pass prevents a stale
    # result from being persisted if governance changed while it was running.
    try:
        cards_after, messages_after, _allowed_after = await _load_governed_sources(
            session,
            card_ids=card_ids,
            direct_mvids=direct_mvids,
            source_chat_id=source_chat_id,
        )
    except WikiSourceRejectedError as exc:
        raise WikiConcurrentUpdateError(
            "wiki source snapshot became ineligible during compilation"
        ) from exc
    if _source_snapshot_hash(cards=cards_after, messages=messages_after) != source_snapshot_hash:
        raise WikiConcurrentUpdateError("wiki source snapshot changed during compilation")
    await _assert_wiki_ledger(session, ledger_id=ledger_id)

    cited_card_tuple = tuple(cited_cards)
    cited_mvid_tuple = tuple(cited_mvids)
    content_unchanged = bool(
        current
        and current.title == title
        and current.body == body
        and current.card_ids == cited_card_tuple
        and current.mvids == cited_mvid_tuple
    )
    metadata_current = bool(
        current
        and current.page_status == "reviewed"
        and current.visibility == "public_candidate"
        and current.public_enabled is False
        and current.robots_policy == "noindex"
        and current.validation_status == "valid"
    )
    input_snapshot_current = bool(
        current
        and current.latest_edit_reason == "automatic_compiler"
        and set(current.input_card_ids) == set(card_ids)
        and set(current.input_mvids) == set(input_mvid_snapshot)
    )
    if current and content_unchanged and metadata_current and input_snapshot_current:
        return WikiCompilationResult(current.page_id, slug, current.revision_seq, False, ledger_id)
    if current and content_unchanged and input_snapshot_current:
        await session.execute(
            text(
                "UPDATE wiki_pages SET page_status='reviewed', "
                "visibility='public_candidate', validation_status='valid', "
                "public_enabled=false, robots_policy='noindex', "
                "last_validated_at=now(), invalidated_at=NULL, "
                "invalidated_by_forget_event_id=NULL, reviewed_by_user_id=:actor, "
                "reviewed_at=now(), updated_at=now() WHERE id=:id"
            ),
            {"id": str(current.page_id), "actor": actor_user_id},
        )
        await session.flush()
        return WikiCompilationResult(current.page_id, slug, current.revision_seq, True, ledger_id)

    page_id = current.page_id if current else uuid.uuid4()
    revision_seq = (current.revision_seq if current else 0) + 1
    if current is None:
        await session.execute(
            text(
                "INSERT INTO wiki_pages "
                "(id, slug, title, body_markdown, page_status, visibility, public_enabled, "
                "robots_policy, validation_status, last_validated_at, created_by_user_id, "
                "reviewed_by_user_id, reviewed_at, created_at, updated_at) VALUES "
                "(:id, :slug, :title, :body, 'reviewed', 'public_candidate', false, 'noindex', "
                "'valid', now(), :actor, :actor, now(), now(), now())"
            ),
            {
                "id": str(page_id),
                "slug": slug,
                "title": title,
                "body": body,
                "actor": actor_user_id,
            },
        )
    else:
        await session.execute(
            text(
                "UPDATE wiki_pages SET title=:title, body_markdown=:body, "
                "page_status='reviewed', visibility='public_candidate', "
                "validation_status='valid', public_enabled=false, "
                "robots_policy='noindex', last_validated_at=now(), invalidated_at=NULL, "
                "invalidated_by_forget_event_id=NULL, reviewed_by_user_id=:actor, "
                "reviewed_at=now(), updated_at=now() WHERE id=:id"
            ),
            {"id": str(page_id), "title": title, "body": body, "actor": actor_user_id},
        )

    await _replace_live_sources(
        session, page_id=page_id, card_ids=cited_card_tuple, mvids=cited_mvid_tuple
    )
    await session.execute(
        text(
            "INSERT INTO wiki_revisions "
            "(wiki_page_id, revision_seq, body_markdown, revision_status, "
            "source_card_ids_snapshot, source_message_version_ids_snapshot, "
            "revision_sources_resolved_at, edited_by_user_id, edited_at, edit_reason, created_at) "
            "VALUES (:page_id, :seq, :body, 'active', CAST(:cards AS jsonb), "
            "CAST(:mvids AS jsonb), now(), :actor, now(), 'automatic_compiler', now())"
        ),
        {
            "page_id": str(page_id),
            "seq": revision_seq,
            "body": body,
            "cards": json.dumps([str(value) for value in card_ids]),
            "mvids": json.dumps(list(input_mvid_snapshot)),
            "actor": actor_user_id,
        },
    )
    await session.flush()
    return WikiCompilationResult(page_id, slug, revision_seq, True, ledger_id)


def _validate_request(
    slug: str,
    title_hint: str,
    card_ids: Sequence[uuid.UUID],
    mvids: Sequence[int],
    *,
    source_chat_id: int,
) -> None:
    if (
        isinstance(source_chat_id, bool)
        or not isinstance(source_chat_id, int)
        or source_chat_id == 0
    ):
        raise ValueError("source_chat_id must be a non-zero integer")
    if not _SLUG_RE.fullmatch(slug) or len(slug) > 120:
        raise ValueError("slug must be lowercase kebab-case and at most 120 characters")
    if not title_hint.strip() or len(title_hint.strip()) > _MAX_TITLE_LENGTH:
        raise ValueError("title_hint must be non-empty and at most 240 characters")
    if not card_ids and not mvids:
        raise ValueError("at least one source is required")
    if any(not isinstance(value, uuid.UUID) for value in card_ids):
        raise ValueError("source_card_ids must contain UUID values")
    if any(not isinstance(value, int) or value <= 0 for value in mvids):
        raise ValueError("source_message_version_ids must contain positive integers")


async def _load_governed_sources(
    session: AsyncSession,
    *,
    card_ids: tuple[uuid.UUID, ...],
    direct_mvids: tuple[int, ...],
    source_chat_id: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[int]]:
    card_rows = []
    if card_ids:
        card_rows = (
            await session.execute(
                text(_CARD_SOURCES_SQL),
                {"ids": [str(v) for v in card_ids], "source_chat_id": source_chat_id},
            )
        ).all()
    returned_cards = {uuid.UUID(str(row.card_id)) for row in card_rows if bool(row.governed)}
    missing_cards = set(card_ids) - returned_cards
    if missing_cards:
        raise WikiSourceRejectedError("one or more knowledge card sources are not governed")

    message_rows = []
    if direct_mvids:
        message_rows = (
            await session.execute(
                text(_MESSAGE_SOURCES_SQL),
                {"ids": list(direct_mvids), "source_chat_id": source_chat_id},
            )
        ).all()
    returned_mvids = {int(row.message_version_id) for row in message_rows}
    if set(direct_mvids) - returned_mvids:
        raise WikiSourceRejectedError("one or more message_version sources are not governed")

    cards = [
        {
            "card_id": str(row.card_id),
            "title": row.title,
            "body_markdown": row.body_markdown,
            "source_message_version_ids": [int(value) for value in row.source_mvids],
        }
        for row in card_rows
        if bool(row.governed)
    ]
    messages = [
        {"message_version_id": int(row.message_version_id), "content": row.content}
        for row in message_rows
    ]
    allowed_mvids = returned_mvids | {
        int(value) for card in cards for value in card["source_message_version_ids"]
    }
    return cards, messages, allowed_mvids


def _validate_draft(
    draft: Mapping[str, Any],
    *,
    allowed_cards: set[uuid.UUID],
    allowed_mvids: set[int],
) -> tuple[str, str, int, list[uuid.UUID], list[int]]:
    if not isinstance(draft, Mapping):
        raise WikiCompilerContractError("gateway result must be a mapping")
    title = draft.get("title")
    body = draft.get("body_markdown")
    ledger_id = draft.get("llm_usage_ledger_id")
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > _MAX_TITLE_LENGTH:
        raise WikiCompilerContractError("gateway title is invalid")
    if not isinstance(body, str) or not body.strip() or len(body) > _MAX_BODY_LENGTH:
        raise WikiCompilerContractError("gateway body_markdown is invalid")
    if isinstance(ledger_id, bool) or not isinstance(ledger_id, int) or ledger_id <= 0:
        raise WikiCompilerContractError("positive llm_usage_ledger_id is required")

    cited_cards: list[uuid.UUID] = []
    cited_mvids: list[int] = []
    for match in _CITATION_RE.finditer(body):
        if match.group(1):
            value = int(match.group(1))
            if value not in cited_mvids:
                cited_mvids.append(value)
        else:
            value_uuid = uuid.UUID(match.group(2))
            if value_uuid not in cited_cards:
                cited_cards.append(value_uuid)
    if not cited_cards and not cited_mvids:
        raise WikiCompilerContractError("gateway page must contain at least one citation")
    if set(cited_cards) - allowed_cards or set(cited_mvids) - allowed_mvids:
        raise WikiCompilerContractError("gateway returned an unsupported citation")
    return title.strip(), body.strip(), ledger_id, cited_cards, cited_mvids


def _source_snapshot_hash(*, cards: list[dict[str, Any]], messages: list[dict[str, Any]]) -> str:
    payload = json.dumps(
        {"cards": cards, "messages": messages},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


async def _assert_wiki_ledger(session: AsyncSession, *, ledger_id: int) -> None:
    call_type = (
        await session.execute(
            text("SELECT call_type FROM llm_usage_ledger WHERE id=:id"), {"id": ledger_id}
        )
    ).scalar_one_or_none()
    if call_type != "wiki_compilation":
        raise WikiCompilerContractError("llm_usage_ledger_id must reference a wiki_compilation row")


async def _load_page(
    session: AsyncSession, *, slug: str, for_update: bool = False
) -> _PageState | None:
    statement = _PAGE_SELECT_FOR_UPDATE if for_update else _PAGE_SELECT
    row = (await session.execute(statement, {"slug": slug})).one_or_none()
    if row is None:
        return None
    return _PageState(
        page_id=uuid.UUID(str(row.id)),
        title=str(row.title),
        body=str(row.body_markdown),
        updated_at=row.updated_at,
        page_status=str(row.page_status),
        visibility=str(row.visibility),
        public_enabled=bool(row.public_enabled),
        robots_policy=str(row.robots_policy),
        validation_status=str(row.validation_status),
        revision_seq=int(row.revision_seq),
        latest_edit_reason=(
            str(row.latest_edit_reason) if row.latest_edit_reason is not None else None
        ),
        input_card_ids=tuple(uuid.UUID(str(value)) for value in row.input_card_ids),
        input_mvids=tuple(int(value) for value in row.input_mvids),
        card_ids=tuple(uuid.UUID(str(value)) for value in row.card_ids),
        mvids=tuple(int(value) for value in row.mvids),
    )


async def _assert_page_source_chat_scope(
    session: AsyncSession,
    *,
    page_id: uuid.UUID,
    source_chat_id: int,
) -> None:
    chat_ids = (
        await session.execute(
            text(_PAGE_SOURCE_CHAT_IDS_SQL),
            {"page_id": str(page_id)},
        )
    ).scalar_one()
    if chat_ids and set(int(value) for value in chat_ids) != {source_chat_id}:
        raise WikiSourceRejectedError("existing wiki page contains sources outside source_chat_id")


async def _replace_live_sources(
    session: AsyncSession,
    *,
    page_id: uuid.UUID,
    card_ids: tuple[uuid.UUID, ...],
    mvids: tuple[int, ...],
) -> None:
    await session.execute(
        text("DELETE FROM wiki_page_card_sources WHERE wiki_page_id=:pid"), {"pid": str(page_id)}
    )
    await session.execute(
        text("DELETE FROM wiki_page_message_sources WHERE wiki_page_id=:pid"),
        {"pid": str(page_id)},
    )
    if card_ids:
        await session.execute(
            text(
                "INSERT INTO wiki_page_card_sources (wiki_page_id, card_id, position) "
                "VALUES (:pid, :source_id, :position)"
            ),
            [
                {"pid": str(page_id), "source_id": str(value), "position": index}
                for index, value in enumerate(card_ids)
            ],
        )
    if mvids:
        await session.execute(
            text(
                "INSERT INTO wiki_page_message_sources "
                "(wiki_page_id, message_version_id, position) "
                "VALUES (:pid, :source_id, :position)"
            ),
            [
                {"pid": str(page_id), "source_id": value, "position": index}
                for index, value in enumerate(mvids)
            ],
        )


def _slug_lock_id(slug: str) -> int:
    return int.from_bytes(
        hashlib.sha256(f"wiki-topic:{slug}".encode()).digest()[:8], "big", signed=True
    )


__all__ = [
    "WikiCompilationResult",
    "WikiCompilerContractError",
    "WikiCompilerError",
    "WikiCompilerGateway",
    "WikiConcurrentUpdateError",
    "WikiSourceRejectedError",
    "compile_topic_page",
]
