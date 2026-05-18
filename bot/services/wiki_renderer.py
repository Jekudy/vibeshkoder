"""Server-side wiki renderer — T9-04 / Phase 9.

Converts wiki page ``body_markdown`` (with ``[^mv:<int>]`` / ``[^card:<uuid>]``
citation tokens) into sanitized HTML for member and admin roles.

Pipeline
--------
1. Extract all citation tokens via regex.
2. Call ``validate_sources`` (wiki_governance) to determine which mvids /
   card_ids are invalid.
3. Decide ``page_archived``:
   - True if any cited card_id is in ``invalid_card_ids`` with reason
     starting with "archived" OR "transitive_forget" (both indicate the card
     is no longer renderable).
   - True if ALL cited sources fail governance (zero valid sources remain).
4. For each token in body_markdown:
   - valid mv  → replace with ``<a class="wiki-citation" href="#mv-{id}">[^{id}]</a>``
   - invalid mv:
     - role='member' → suppress (empty string), add to suppressed_citations
     - role='admin'  → replace with ``[⚠ SOURCE UNAVAILABLE]``, add to
       admin_unavailable_markers
   - invalid card → page_archived=True (flagged in step 3; handled below)
5. If page_archived=True → return WikiRenderResult(html_body='', …).
6. Otherwise: parse body via markdown-it-py CommonMark renderer.
7. Sanitize via bleach with the declared allowlist.
8. Return WikiRenderResult(html_body=sanitized, …).

G1 lint: this file must NEVER import neo4j, bot.services.graph_*, or
bot.services.llm_* modules.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Literal

import bleach
from markdown_it import MarkdownIt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.wiki_governance import validate_sources

# ── Bleach allowlist ──────────────────────────────────────────────────────────

BLEACH_ALLOWED_TAGS: list[str] = [
    "p",
    "br",
    "strong",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "ul",
    "ol",
    "li",
    "blockquote",
    "code",
    "pre",
    "a",
    "hr",
    # Citation anchor added by this module — must be in allowlist.
    # bleach allows all listed tags regardless of class/href attributes
    # being present; the attributes allowlist controls per-tag attribute filtering.
]

BLEACH_ALLOWED_ATTRIBUTES: dict[str, list[str]] = {
    "a": ["href", "title", "class"],
}

BLEACH_ALLOWED_PROTOCOLS: list[str] = ["http", "https"]

# ── Token regexes ─────────────────────────────────────────────────────────────

# mv: positive integer only (no leading zero, no leading sign).
_MV_TOKEN_RE = re.compile(r"\[\^mv:([1-9]\d*)\]")
# card: canonical UUID format (hex + hyphens at positions 8/13/18/23).
# Accept upper- and lower-case for robustness; uuid.UUID() normalises both.
_CARD_TOKEN_RE = re.compile(
    r"\[\^card:([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\]"
)

# ── Linked-source queries ─────────────────────────────────────────────────────
#
# Returns the SET of message_version_ids actually linked to the page —
# directly (via wiki_page_message_sources) OR transitively via
# wiki_page_card_sources → card_sources. Used to distinguish "linked but
# governance-blocked" mv tokens from "unknown / unlinked" tokens (the latter
# must also be suppressed/marked, never rendered as valid citations).
_LINKED_MVIDS_QUERY = """
SELECT message_version_id AS mv_id FROM wiki_page_message_sources
WHERE wiki_page_id = :pid
UNION
SELECT cs.message_version_id AS mv_id
FROM wiki_page_card_sources wpcs
JOIN card_sources cs ON cs.card_id = wpcs.card_id
WHERE wpcs.wiki_page_id = :pid
"""

_LINKED_CARD_IDS_QUERY = """
SELECT card_id FROM wiki_page_card_sources WHERE wiki_page_id = :pid
"""


# ── Result dataclass ──────────────────────────────────────────────────────────


@dataclass
class WikiRenderResult:
    """Result of render_wiki_page().

    Attributes:
        html_body:
            Sanitized HTML body. Empty string when page_archived=True.
        page_archived:
            True if the page cannot be rendered because at least one cited
            card_id is invalid (archived / all-sources-forgotten) or ALL cited
            sources fail governance.
        suppressed_citations:
            List of mv_ids whose tokens were removed from member-role output.
        admin_unavailable_markers:
            List of mv_ids whose tokens were replaced with ``[⚠ SOURCE
            UNAVAILABLE]`` in admin-role output.
    """

    html_body: str
    page_archived: bool
    suppressed_citations: list[int] = field(default_factory=list)
    admin_unavailable_markers: list[int] = field(default_factory=list)


# ── Public API ────────────────────────────────────────────────────────────────


async def render_wiki_page(
    session: AsyncSession,
    *,
    page_id: uuid.UUID,
    role: Literal["admin", "member"],
    body_markdown: str,
) -> WikiRenderResult:
    """Render a wiki page body to sanitized HTML with citation governance.

    Parameters
    ----------
    session:
        Active AsyncSession. Read-only — no DB writes.
    page_id:
        UUID of the wiki page (used to fetch governance result).
    role:
        ``'member'`` or ``'admin'``. Controls how invalid mv citations are
        rendered (suppressed vs. warning marker).
    body_markdown:
        Raw Markdown body from ``wiki_pages.body_markdown`` (or the current
        revision body). May contain ``[^mv:<int>]`` and ``[^card:<uuid>]``
        tokens.

    Returns
    -------
    WikiRenderResult
        See dataclass docstring.

    Notes
    -----
    - No LLM calls are made. The pipeline is purely structural.
    - ``page_archived=True`` is returned (with ``html_body=''``) when any
      cited card_id has reason starting with "archived" or "transitive_forget",
      or when all cited sources fail governance.
    """
    suppressed_citations: list[int] = []
    admin_unavailable_markers: list[int] = []

    # ── Step 1 & 2: extract tokens + governance check ─────────────────────────
    mv_ids_in_body = [int(m) for m in _MV_TOKEN_RE.findall(body_markdown)]
    card_ids_in_body = [uuid.UUID(m) for m in _CARD_TOKEN_RE.findall(body_markdown)]

    # validate_sources raises WikiPageNotFoundError if page_id doesn't exist.
    gov = await validate_sources(session, page_id=page_id)

    invalid_mv_set: set[int] = set(gov.invalid_mvids)
    invalid_card_set: set[uuid.UUID] = set(gov.invalid_card_ids)

    # ── Step 2b: fetch the page's LINKED source IDs ──────────────────────────
    # A body mv token must be linked to the page (directly OR transitively
    # through a cited card) AND not in invalid_mv_set to render as a valid
    # citation. Tokens referring to unlinked mvids are unknown — must NOT
    # be rendered as citations (Codex HIGH fix).
    linked_mvid_rows = (
        await session.execute(text(_LINKED_MVIDS_QUERY), {"pid": str(page_id)})
    ).fetchall()
    linked_mv_set: set[int] = {int(r.mv_id) for r in linked_mvid_rows}

    linked_card_rows = (
        await session.execute(text(_LINKED_CARD_IDS_QUERY), {"pid": str(page_id)})
    ).fetchall()
    linked_card_set: set[uuid.UUID] = {uuid.UUID(str(r.card_id)) for r in linked_card_rows}

    valid_mv_set: set[int] = linked_mv_set - invalid_mv_set
    valid_card_set: set[uuid.UUID] = linked_card_set - invalid_card_set

    # ── Step 3: decide page_archived ─────────────────────────────────────────
    page_archived = False

    # Any cited card explicitly archived / transitive_forget → archived.
    for card_id in card_ids_in_body:
        if card_id in invalid_card_set:
            reason = gov.reasons.get(f"card:{card_id}", "")
            if reason.startswith("archived") or reason == "transitive_forget":
                page_archived = True
                break

    # General all-sources-fail rule: any citation tokens AND none of them
    # resolve to a valid linked source → page is archived. Covers card-only,
    # mv-only, and mixed pages (Codex MED fix — mv-only case was missed).
    if not page_archived:
        has_any_citation = bool(mv_ids_in_body) or bool(card_ids_in_body)
        any_valid = (
            any(m in valid_mv_set for m in mv_ids_in_body)
            or any(c in valid_card_set for c in card_ids_in_body)
        )
        if has_any_citation and not any_valid:
            page_archived = True

    if page_archived:
        return WikiRenderResult(
            html_body="",
            page_archived=True,
            suppressed_citations=suppressed_citations,
            admin_unavailable_markers=admin_unavailable_markers,
        )

    # ── Step 4: replace tokens in body ───────────────────────────────────────
    processed_body = _replace_tokens(
        body_markdown,
        valid_mv_set=valid_mv_set,
        role=role,
        suppressed_citations=suppressed_citations,
        admin_unavailable_markers=admin_unavailable_markers,
    )

    # ── Steps 6 & 7: parse Markdown → sanitize HTML ──────────────────────────
    md = MarkdownIt("commonmark")
    raw_html = md.render(processed_body)
    # Pre-strip dangerous elements and their content before bleach.
    # bleach with strip=True removes tags but preserves inner text; for
    # elements like <script> we must also remove their content.
    raw_html = _strip_dangerous_elements(raw_html)
    sanitized = bleach.clean(
        raw_html,
        tags=BLEACH_ALLOWED_TAGS,
        attributes=BLEACH_ALLOWED_ATTRIBUTES,
        protocols=BLEACH_ALLOWED_PROTOCOLS,
        strip=True,
    )

    return WikiRenderResult(
        html_body=sanitized,
        page_archived=False,
        suppressed_citations=suppressed_citations,
        admin_unavailable_markers=admin_unavailable_markers,
    )


# ── Internal helpers ──────────────────────────────────────────────────────────

# Dangerous tags whose content must also be removed (not just the tag wrapper).
_DANGEROUS_ELEMENT_RE = re.compile(
    r"<(script|style|iframe|form|object|embed|applet|base|link|meta)"
    r"(\s[^>]*)?>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
# Also strip self-closing / void dangerous tags.
_DANGEROUS_VOID_RE = re.compile(
    r"<(script|style|iframe|form|object|embed|applet|base|link|meta)(\s[^>]*)?/?>",
    re.IGNORECASE,
)


def _strip_dangerous_elements(html: str) -> str:
    """Remove dangerous HTML elements and their content entirely.

    bleach with strip=True removes the outer tags but keeps inner text content.
    For elements like ``<script>`` we must also remove the content; this helper
    runs before bleach to handle that case.
    """
    html = _DANGEROUS_ELEMENT_RE.sub("", html)
    html = _DANGEROUS_VOID_RE.sub("", html)
    return html


def _replace_tokens(
    body: str,
    *,
    valid_mv_set: set[int],
    role: Literal["admin", "member"],
    suppressed_citations: list[int],
    admin_unavailable_markers: list[int],
) -> str:
    """Replace [^mv:N] and [^card:UUID] tokens in body.

    - mv in valid_mv_set  → citation anchor HTML.
    - mv NOT in valid_mv_set (invalid OR unlinked/unknown) → empty (member)
      or warning marker (admin).
    - Card tokens → stripped (governance already decided page_archived above;
      cards don't produce inline output — they're source-list declarations
      rendered by T9-05).

    For member role, a post-pass collapses whitespace and trailing punctuation
    that would otherwise reveal that a citation was suppressed
    ("See [^mv:42]." → "See ." → "See.") — Codex MED fix.
    """

    def _replace_mv(match: re.Match) -> str:  # type: ignore[type-arg]
        mv_id = int(match.group(1))
        if mv_id in valid_mv_set:
            # Valid mv → citation anchor. The anchor href + class survive
            # bleach via the BLEACH_ALLOWED_* allowlist.
            return f'<a class="wiki-citation" href="#mv-{mv_id}">[^{mv_id}]</a>'
        # Either explicitly invalid OR unlinked/unknown → suppress (member)
        # or mark (admin).
        if role == "member":
            suppressed_citations.append(mv_id)
            return ""
        admin_unavailable_markers.append(mv_id)
        return "[⚠ SOURCE UNAVAILABLE]"

    def _replace_card(match: re.Match) -> str:  # type: ignore[type-arg]
        # Card tokens are source declarations, not inline citations.
        # Strip them from rendered output — they appear in the source list
        # section (future T9-05 responsibility).
        return ""

    result = _MV_TOKEN_RE.sub(_replace_mv, body)
    result = _CARD_TOKEN_RE.sub(_replace_card, result)

    if role == "member" and suppressed_citations:
        # Collapse "text ." → "text.", "text ;" → "text;" etc. so suppression
        # doesn't leave a citation-shaped gap that reveals the deletion.
        result = re.sub(r"[ \t]+([.,;:!?])", r"\1", result)
        # Collapse multiple internal spaces to a single space.
        result = re.sub(r"[ \t]{2,}", " ", result)

    return result


__all__ = [
    "BLEACH_ALLOWED_ATTRIBUTES",
    "BLEACH_ALLOWED_PROTOCOLS",
    "BLEACH_ALLOWED_TAGS",
    "WikiRenderResult",
    "render_wiki_page",
]
