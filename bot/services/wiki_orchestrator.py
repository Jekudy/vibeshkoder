"""Automatic topic compilation and static projection for the community wiki.

This layer contains no provider calls and no public HTTP server.  It groups
approved cards by their stable ``topic_slug``, asks the audited wiki compiler
to revise only changed topics, and exposes a fail-closed loader for the pure
static exporter.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.wiki_compiler import (
    WikiCompilationResult,
    WikiCompilerGateway,
    compile_topic_page,
)
from bot.services.wiki_governance import validate_sources
from bot.services.wiki_static_export import (
    StaticExportResult,
    StaticWikiPage,
    export_static_site,
)

MAX_CARDS_PER_TOPIC = 64
_PIPELINE_LOCK_ID = int.from_bytes(
    hashlib.sha256(b"memory:wiki-orchestrator").digest()[:8],
    "big",
    signed=True,
)


class WikiOrchestrationError(RuntimeError):
    """Base error for an automatic wiki pipeline refusal."""


class WikiTopicSourceLimitError(WikiOrchestrationError):
    """A topic cannot be compiled without truncating its source history."""


class WikiStaticSourceInvalidError(WikiOrchestrationError):
    """A page selected for static publication has stale or invalid sources."""


@dataclass(frozen=True)
class WikiOrchestrationResult:
    topics_seen: int
    compiled_topics: int
    unchanged_topics: int
    revisions: tuple[WikiCompilationResult, ...]
    remaining_changed_topics: int = 0
    stale_topics: int = 0
    blocked_topics: int = 0


@dataclass(frozen=True)
class _Topic:
    slug: str
    title_hint: str
    card_ids: tuple[uuid.UUID, ...]
    source_message_version_ids: tuple[int, ...]


@dataclass(frozen=True)
class _TopicInventory:
    eligible: tuple[_Topic, ...]
    ineligible_slugs: frozenset[str]
    topics_seen: int


_TOPICS_SQL = """
SELECT
    kc.topic_slug,
    kc.id AS card_id,
    kc.title,
    kc.approved_at,
    kc.created_at,
    count(cs.message_version_id) > 0
        AND bool_and(
            cm.chat_id = :source_chat_id
            AND cm.current_version_id = mv.id
            AND cm.memory_policy = 'normal'
            AND cm.is_redacted = false
            AND mv.is_redacted = false
            AND NOT EXISTS (
                SELECT 1
                FROM forget_events fe
                WHERE fe.status IN ('pending', 'processing', 'completed')
                  AND fe.tombstone_key IN (
                      'message:' || cm.chat_id::text || ':' || cm.message_id::text,
                      'message_hash:' || mv.content_hash,
                      'user:' || cm.user_id::text
                  )
            )
        ) AS eligible,
    array_agg(cs.message_version_id ORDER BY cs.position, cs.message_version_id)
        FILTER (WHERE cs.message_version_id IS NOT NULL) AS source_mvids
FROM knowledge_cards kc
LEFT JOIN card_sources cs ON cs.card_id = kc.id
LEFT JOIN message_versions mv ON mv.id = cs.message_version_id
LEFT JOIN chat_messages cm ON cm.id = mv.chat_message_id
WHERE kc.card_status = 'approved'
  AND kc.topic_slug IS NOT NULL
  AND kc.topic_slug <> ''
GROUP BY kc.id, kc.topic_slug, kc.title, kc.approved_at, kc.created_at
ORDER BY kc.topic_slug, kc.approved_at, kc.created_at, kc.id
"""

_PAGE_STATE_SQL = """
SELECT
    wp.id,
    wp.page_status,
    wp.validation_status,
    wp.visibility,
    wp.public_enabled,
    wp.robots_policy,
    latest.edit_reason,
    latest.source_card_ids_snapshot AS input_card_ids,
    latest.source_message_version_ids_snapshot AS input_mvids
FROM wiki_pages wp
LEFT JOIN LATERAL (
    SELECT
        wr.edit_reason,
        wr.source_card_ids_snapshot,
        wr.source_message_version_ids_snapshot
    FROM wiki_revisions wr
    WHERE wr.wiki_page_id = wp.id
    ORDER BY wr.revision_seq DESC
    LIMIT 1
) latest ON true
WHERE wp.slug = :slug
"""

_STATIC_PAGES_SQL = """
SELECT
    wp.id,
    wp.slug,
    wp.title,
    wp.body_markdown,
    latest.revision_seq
FROM wiki_pages wp
JOIN LATERAL (
    SELECT wr.revision_seq, wr.edit_reason
    FROM wiki_revisions wr
    WHERE wr.wiki_page_id = wp.id
    ORDER BY wr.revision_seq DESC
    LIMIT 1
) latest ON true
WHERE wp.page_status = 'reviewed'
  AND wp.validation_status = 'valid'
  AND wp.visibility = 'public_candidate'
  AND wp.public_enabled = false
  AND wp.robots_policy = 'noindex'
  AND latest.edit_reason = 'automatic_compiler'
ORDER BY wp.slug
"""

_AUTOMATIC_PAGES_SQL = """
SELECT wp.id, wp.slug
FROM wiki_pages wp
JOIN LATERAL (
    SELECT wr.edit_reason
    FROM wiki_revisions wr
    WHERE wr.wiki_page_id = wp.id
    ORDER BY wr.revision_seq DESC
    LIMIT 1
) latest ON true
WHERE latest.edit_reason = 'automatic_compiler'
ORDER BY wp.slug
"""


async def compile_changed_topics(
    session: AsyncSession,
    *,
    actor_user_id: int,
    gateway: WikiCompilerGateway,
    publication_authorized: bool,
    source_chat_id: int,
    max_topics: int | None = None,
) -> WikiOrchestrationResult:
    """Compile every changed topic exactly once in the caller transaction."""
    if publication_authorized is not True:
        raise WikiOrchestrationError("explicit publication authorization is required")
    _require_source_chat_id(source_chat_id)
    if max_topics is not None and (type(max_topics) is not int or not 1 <= max_topics <= 256):
        raise ValueError("max_topics must be an integer from 1 to 256")
    await _require_actor(session, actor_user_id)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": _PIPELINE_LOCK_ID},
        )
    inventory = await _load_topics(session, source_chat_id=source_chat_id)
    eligible_topics: list[_Topic] = []
    ineligible_slugs = set(inventory.ineligible_slugs)
    for topic in inventory.eligible:
        if len(topic.card_ids) > MAX_CARDS_PER_TOPIC:
            ineligible_slugs.add(topic.slug)
        else:
            eligible_topics.append(topic)

    stale_slugs = set(ineligible_slugs)
    blocked_slugs: set[str] = set()
    changed: list[_Topic] = []
    for topic in eligible_topics:
        state = await _load_topic_page_state(session, slug=topic.slug)
        if state is not None and state["edit_reason"] != "automatic_compiler":
            blocked_slugs.add(topic.slug)
            continue
        if state is not None:
            source_check = await validate_sources(
                session,
                page_id=uuid.UUID(str(state["id"])),
                source_chat_id=source_chat_id,
            )
            if not source_check.valid:
                await _mark_automatic_page_stale(
                    session,
                    page_id=uuid.UUID(str(state["id"])),
                )
                if "wrong_chat" in source_check.reasons.values():
                    stale_slugs.add(topic.slug)
                    continue
        if not _topic_is_current(topic, state):
            changed.append(topic)

    stale_slugs.update(
        await _reconcile_ineligible_automatic_pages(
            session,
            eligible_slugs={topic.slug for topic in eligible_topics} - stale_slugs,
        )
    )

    selected_topics = changed if max_topics is None else changed[:max_topics]
    revisions: list[WikiCompilationResult] = []
    for topic in selected_topics:
        revisions.append(
            await compile_topic_page(
                session,
                slug=topic.slug,
                title_hint=topic.title_hint,
                source_card_ids=topic.card_ids,
                source_message_version_ids=(),
                actor_user_id=actor_user_id,
                gateway=gateway,
                publication_authorized=True,
                source_chat_id=source_chat_id,
            )
        )

    return WikiOrchestrationResult(
        topics_seen=inventory.topics_seen,
        compiled_topics=len(revisions),
        unchanged_topics=len(eligible_topics)
        - len(changed)
        - len(blocked_slugs)
        - len(stale_slugs & {topic.slug for topic in eligible_topics}),
        revisions=tuple(revisions),
        remaining_changed_topics=len(changed) - len(revisions),
        stale_topics=len(stale_slugs),
        blocked_topics=len(blocked_slugs),
    )


async def load_static_wiki_pages(
    session: AsyncSession,
    *,
    source_chat_id: int,
) -> list[StaticWikiPage]:
    """Load only automatic, reviewed pages and fail closed on source drift."""
    _require_source_chat_id(source_chat_id)
    rows = (await session.execute(text(_STATIC_PAGES_SQL))).mappings().all()
    pages: list[StaticWikiPage] = []
    for row in rows:
        source_check = await validate_sources(
            session,
            page_id=uuid.UUID(str(row["id"])),
            source_chat_id=source_chat_id,
        )
        if not source_check.valid:
            raise WikiStaticSourceInvalidError(
                f"wiki page {row['slug']!r} has invalid publication sources"
            )
        revision_seq = int(row["revision_seq"])
        if revision_seq <= 0:
            raise WikiStaticSourceInvalidError(f"wiki page {row['slug']!r} has no active revision")
        pages.append(
            StaticWikiPage(
                slug=str(row["slug"]),
                title=str(row["title"]),
                body_markdown=str(row["body_markdown"]),
                revision_seq=revision_seq,
            )
        )
    return pages


async def export_static_wiki(
    session: AsyncSession,
    *,
    publish_dir: Path,
    site_title: str,
    forbidden_origins: tuple[str, ...] = (),
    publication_authorized: bool,
    source_chat_id: int,
) -> StaticExportResult:
    """Build a deterministic site from the governed automatic page projection."""
    _require_source_chat_id(source_chat_id)
    await _lock_static_publication_snapshot(session)
    pages = await load_static_wiki_pages(session, source_chat_id=source_chat_id)
    return export_static_site(
        pages,
        publish_dir=publish_dir,
        site_title=site_title,
        publication_authorized=publication_authorized,
        forbidden_origins=forbidden_origins,
    )


async def _lock_static_publication_snapshot(session: AsyncSession) -> None:
    """Hold compiler/source locks until the caller finishes its upload.

    The scheduler intentionally keeps this transaction open through the
    one-way Cloudflare upload. Every automatic compiler uses the same global
    advisory lock, while message invalidation uses the same sorted per-MV lock
    namespace. Row SHARE locks close ordinary page/card/source update races.
    """

    if session.bind is None or session.bind.dialect.name != "postgresql":
        return
    from bot.services.forget_cascade import _p6_mvid_advisory_lock_id

    await session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_id)"),
        {"lock_id": _PIPELINE_LOCK_ID},
    )
    page_ids = list(
        (
            await session.scalars(
                text(
                    "SELECT wp.id FROM wiki_pages wp "
                    "JOIN LATERAL (SELECT wr.edit_reason FROM wiki_revisions wr "
                    "WHERE wr.wiki_page_id=wp.id ORDER BY wr.revision_seq DESC LIMIT 1) "
                    "latest ON true "
                    "WHERE wp.page_status='reviewed' AND wp.validation_status='valid' "
                    "AND wp.visibility='public_candidate' AND wp.public_enabled=false "
                    "AND wp.robots_policy='noindex' "
                    "AND latest.edit_reason='automatic_compiler' ORDER BY wp.id"
                )
            )
        ).all()
    )
    if not page_ids:
        return
    mvids = list(
        (
            await session.scalars(
                text(
                    "SELECT message_version_id FROM wiki_page_message_sources "
                    "WHERE wiki_page_id = ANY(:page_ids) "
                    "UNION SELECT cs.message_version_id "
                    "FROM wiki_page_card_sources wpcs "
                    "JOIN card_sources cs ON cs.card_id=wpcs.card_id "
                    "WHERE wpcs.wiki_page_id = ANY(:page_ids) "
                    "ORDER BY message_version_id"
                ),
                {"page_ids": page_ids},
            )
        ).all()
    )
    for lock_id in sorted(_p6_mvid_advisory_lock_id(int(mvid)) for mvid in mvids):
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": lock_id},
        )

    await session.execute(
        text("SELECT id FROM wiki_pages WHERE id = ANY(:ids) ORDER BY id FOR SHARE"),
        {"ids": page_ids},
    )
    await session.execute(
        text(
            "SELECT kc.id FROM knowledge_cards kc "
            "JOIN wiki_page_card_sources wpcs ON wpcs.card_id=kc.id "
            "WHERE wpcs.wiki_page_id = ANY(:ids) ORDER BY kc.id FOR SHARE OF kc"
        ),
        {"ids": page_ids},
    )
    if mvids:
        await session.execute(
            text(
                "SELECT mv.id FROM message_versions mv "
                "WHERE mv.id = ANY(:ids) ORDER BY mv.id FOR SHARE"
            ),
            {"ids": mvids},
        )
        await session.execute(
            text(
                "SELECT cm.id FROM chat_messages cm "
                "JOIN message_versions mv ON mv.chat_message_id=cm.id "
                "WHERE mv.id = ANY(:ids) ORDER BY cm.id FOR SHARE OF cm"
            ),
            {"ids": mvids},
        )


async def _require_actor(session: AsyncSession, actor_user_id: int) -> None:
    if isinstance(actor_user_id, bool) or not isinstance(actor_user_id, int) or actor_user_id <= 0:
        raise ValueError("actor_user_id must be a positive integer")
    exists = (
        await session.execute(
            text("SELECT EXISTS(SELECT 1 FROM users WHERE id=:actor)"),
            {"actor": actor_user_id},
        )
    ).scalar_one()
    if not exists:
        raise WikiOrchestrationError("automation actor user does not exist")


def _require_source_chat_id(source_chat_id: int) -> None:
    if (
        isinstance(source_chat_id, bool)
        or not isinstance(source_chat_id, int)
        or source_chat_id == 0
    ):
        raise ValueError("source_chat_id must be a non-zero integer")


async def _load_topics(
    session: AsyncSession,
    *,
    source_chat_id: int,
) -> _TopicInventory:
    rows = (
        (
            await session.execute(
                text(_TOPICS_SQL),
                {"source_chat_id": source_chat_id},
            )
        )
        .mappings()
        .all()
    )
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(str(row["topic_slug"]), []).append(dict(row))

    eligible: list[_Topic] = []
    ineligible_slugs: set[str] = set()
    for slug, card_rows in grouped.items():
        if not all(bool(row["eligible"]) for row in card_rows):
            ineligible_slugs.add(slug)
            continue
        eligible.append(
            _Topic(
                slug=slug,
                title_hint=str(card_rows[0]["title"]),
                card_ids=tuple(uuid.UUID(str(row["card_id"])) for row in card_rows),
                source_message_version_ids=tuple(
                    sorted(
                        {int(value) for row in card_rows for value in (row["source_mvids"] or [])}
                    )
                ),
            )
        )
    return _TopicInventory(
        eligible=tuple(eligible),
        ineligible_slugs=frozenset(ineligible_slugs),
        topics_seen=len(grouped),
    )


async def _load_topic_page_state(
    session: AsyncSession,
    *,
    slug: str,
) -> dict | None:
    row = (await session.execute(text(_PAGE_STATE_SQL), {"slug": slug})).mappings().one_or_none()
    return dict(row) if row is not None else None


def _topic_is_current(topic: _Topic, row: dict | None) -> bool:
    if row is None or row["edit_reason"] != "automatic_compiler":
        return False
    current_card_ids = {uuid.UUID(str(value)) for value in (row["input_card_ids"] or [])}
    current_mvids = {int(value) for value in (row["input_mvids"] or [])}
    return (
        current_card_ids == set(topic.card_ids)
        and current_mvids == set(topic.source_message_version_ids)
        and row["page_status"] == "reviewed"
        and row["validation_status"] == "valid"
        and row["visibility"] == "public_candidate"
        and row["public_enabled"] is False
        and row["robots_policy"] == "noindex"
    )


async def _mark_automatic_page_stale(
    session: AsyncSession,
    *,
    page_id: uuid.UUID,
) -> None:
    await session.execute(
        text(
            "UPDATE wiki_pages wp SET page_status='stale', validation_status='stale', "
            "public_enabled=false, robots_policy='noindex', "
            "invalidated_at=COALESCE(wp.invalidated_at, now()), updated_at=now() "
            "WHERE wp.id=:page_id AND (SELECT wr.edit_reason FROM wiki_revisions wr "
            "WHERE wr.wiki_page_id=wp.id ORDER BY wr.revision_seq DESC LIMIT 1) "
            "='automatic_compiler'"
        ),
        {"page_id": str(page_id)},
    )


async def _reconcile_ineligible_automatic_pages(
    session: AsyncSession,
    *,
    eligible_slugs: set[str],
) -> set[str]:
    rows = (await session.execute(text(_AUTOMATIC_PAGES_SQL))).mappings().all()
    stale_slugs: set[str] = set()
    for row in rows:
        if str(row["slug"]) not in eligible_slugs:
            stale_slugs.add(str(row["slug"]))
            await _mark_automatic_page_stale(
                session,
                page_id=uuid.UUID(str(row["id"])),
            )
    return stale_slugs


__all__ = [
    "MAX_CARDS_PER_TOPIC",
    "WikiOrchestrationError",
    "WikiOrchestrationResult",
    "WikiStaticSourceInvalidError",
    "WikiTopicSourceLimitError",
    "compile_changed_topics",
    "export_static_wiki",
    "load_static_wiki_pages",
]
