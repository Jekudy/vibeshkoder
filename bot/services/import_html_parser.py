"""Telegram Desktop HTML export adapter.

Telegram Desktop can export a chat as paginated ``messages*.html`` files. The
existing importer consumes the JSON export shape, so this module converts HTML
messages into the same canonical dictionaries instead of creating a second
persistence pipeline.

Only HTML pages are opened. Linked media, including voice files, is never opened
or transcribed. Errors identify page/message structure but never message content.
HTML has no Telegram chat id, so conversion/apply requires one explicitly.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Iterator
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from bot.services.governance import detect_policy
from bot.services.import_author_exclusion import (
    normalize_import_author_name,
    normalize_import_excluded_author_names,
)
from bot.services.import_parser import (
    MEDIA_KINDS,
    ImportDryRunReport,
    _check_duplicates,
    _classify_td_kind,
    _extract_text_content,
    _to_datetime,
)


_PAGE_RE = re.compile(r"^messages(?P<number>(?:[2-9]|[1-9][0-9]+))?\.html$")
_MESSAGE_ID_RE = re.compile(r"^message(?P<id>-?[0-9]+)$")
_REPLY_ID_RE = re.compile(r"(?:^|#)go_to_message(?P<id>[0-9]+)$")

# Telegram ids fit inside 52 bits. HTML does not expose them, so name-derived
# ghost ids use a disjoint high positive range accepted by ``user<N>`` mapping.
_SYNTHETIC_USER_ID_BASE = 1 << 61
_SYNTHETIC_USER_ID_MASK = (1 << 61) - 1

_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)

_MEDIA_ROOTS = frozenset(
    {
        "animations",
        "audio_files",
        "files",
        "photos",
        "round_video_messages",
        "stickers",
        "video_files",
        "voice_messages",
    }
)

_HTML_MEDIA_CLASS_TO_CANONICAL: tuple[tuple[str, str], ...] = (
    ("media_voice_message", "voice_message"),
    ("media_audio_file", "audio_file"),
    ("media_video_message", "video_message"),
    ("media_animation", "animation"),
    ("media_sticker", "sticker"),
    ("media_photo", "photo"),
    ("media_video", "video_file"),
    ("media_file", "document"),
    ("media_poll", "poll"),
    ("video_file_wrap", "video_file"),
    ("video_file", "video_file"),
    ("photo", "photo"),
    ("sticker", "sticker"),
)


class HtmlExportValidationError(ValueError):
    """Expected, operator-actionable HTML export validation failure."""


@dataclass
class _HtmlNode:
    tag: str
    attrs: dict[str, str]
    parent: _HtmlNode | None = None
    children: list[_HtmlNode | str] = field(default_factory=list)

    @property
    def classes(self) -> frozenset[str]:
        return frozenset(self.attrs.get("class", "").split())

    def descendants(self, *, include_self: bool = False) -> Iterator[_HtmlNode]:
        if include_self:
            yield self
        for child in self.children:
            if isinstance(child, _HtmlNode):
                yield child
                yield from child.descendants()


class _TelegramHtmlTreeParser(HTMLParser):
    """Small tolerant DOM builder using only the Python standard library."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _HtmlNode(tag="document", attrs={})
        self._stack: list[_HtmlNode] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _HtmlNode(
            tag=tag.lower(),
            attrs={key.lower(): value or "" for key, value in attrs},
            parent=self._stack[-1],
        )
        self._stack[-1].children.append(node)
        if node.tag not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self._stack[-1].children.append(data)


def discover_html_pages(path: str | Path) -> list[Path]:
    """Return Telegram HTML pages in deterministic natural order.

    A directory export must contain a contiguous sequence beginning with
    ``messages.html``. Missing middle pages fail fast because silently accepting
    them would violate the import completeness contract.
    """
    source = Path(path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"HTML export path not found: {source}")

    if source.is_file():
        if _page_number(source.name) is None:
            raise HtmlExportValidationError(
                f"Expected a Telegram messages*.html page, got: {source.name}"
            )
        source = source.parent
    elif not source.is_dir():
        raise HtmlExportValidationError(
            f"HTML export path is neither a file nor directory: {source}"
        )

    numbered: list[tuple[int, Path]] = []
    for candidate in source.iterdir():
        if not candidate.is_file():
            continue
        number = _page_number(candidate.name)
        if number is not None:
            numbered.append((number, candidate.resolve()))

    if not numbered:
        raise HtmlExportValidationError(f"No messages*.html pages found in HTML export: {source}")

    numbered.sort(key=lambda item: item[0])
    actual = [number for number, _ in numbered]
    expected = list(range(1, actual[-1] + 1))
    if actual != expected:
        raise HtmlExportValidationError(
            "Telegram HTML pages must be a unique contiguous sequence beginning "
            f"with messages.html; found page numbers {actual}"
        )
    return [page for _, page in numbered]


def iter_html_messages(path: str | Path) -> Iterator[dict]:
    """Yield canonical JSON-import-shaped messages in page and DOM order."""
    seen_ids: set[int] = set()
    last_author: str | None = None

    for page in discover_html_pages(path):
        root = _parse_page(page)
        message_nodes = [
            node for node in root.descendants() if node.tag == "div" and "message" in node.classes
        ]
        for node in message_nodes:
            message_id = _parse_message_id(node, page)
            if message_id in seen_ids:
                raise HtmlExportValidationError(
                    f"duplicate message id {message_id} in Telegram HTML export"
                )
            seen_ids.add(message_id)

            canonical, author = _canonicalize_message(
                node,
                page_name=page.name,
                prior_author=last_author,
            )
            if author is not None:
                last_author = author
            yield canonical


def build_canonical_envelope(
    path: str | Path,
    *,
    chat_id: int | None,
    chat_type: str = "private_supergroup",
) -> dict:
    """Build the existing JSON import envelope from an HTML export."""
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        raise HtmlExportValidationError("chat_id is required for Telegram HTML conversion/apply")
    return {
        "id": chat_id,
        "name": "Telegram HTML export",
        "type": chat_type,
        "messages": list(iter_html_messages(path)),
    }


def count_html_author_matches(
    path: str | Path,
    excluded_author_names: Iterable[str],
) -> dict[str, int]:
    """Count exact normalized author matches without exposing message content."""
    names = normalize_import_excluded_author_names(excluded_author_names)
    return _count_author_matches(iter_html_messages(path), names)


def parse_html_export(
    path: str | Path,
    *,
    excluded_author_names: Iterable[str] = (),
) -> ImportDryRunReport:
    """Return an offline, content-free structural report for an HTML export."""
    source = Path(path).expanduser().resolve()
    messages = list(iter_html_messages(source))
    normalized_excluded_names = normalize_import_excluded_author_names(excluded_author_names)
    excluded_counts = _count_author_matches(messages, normalized_excluded_names)
    warnings: list[str] = []

    service_messages = 0
    media_count = 0
    forward_count = 0
    edited_count = 0
    reply_count = 0
    anonymous_channel_count = 0
    all_ids: list[int] = []
    from_ids: list[str] = []
    datetimes: list[datetime] = []
    reply_targets: list[int] = []
    kind_counter: Counter[str] = Counter()
    policy_counter: Counter[str] = Counter(normal=0, nomem=0, offrecord=0)

    for message in messages:
        message_id = message.get("id")
        if isinstance(message_id, int):
            all_ids.append(message_id)

        # Service date separators have no timestamp; avoid content-bearing warnings.
        if message.get("type") != "service":
            dt = _to_datetime(message.get("date_unixtime"), message.get("date"), warnings)
            if dt is not None:
                datetimes.append(dt)

        kind = _classify_td_kind(message, warnings)
        kind_counter[kind] += 1

        if message.get("type") == "service":
            service_messages += 1
            continue

        from_id = message.get("from_id")
        if isinstance(from_id, str):
            from_ids.append(from_id)
            if from_id.startswith("channel"):
                anonymous_channel_count += 1

        if "edited" in message:
            edited_count += 1
        if kind in MEDIA_KINDS:
            media_count += 1
        if kind == "forward":
            forward_count += 1

        reply_to = message.get("reply_to_message_id")
        if isinstance(reply_to, int):
            reply_count += 1
            reply_targets.append(reply_to)

        text, caption = _extract_text_content(message, kind)
        policy, _ = detect_policy(text or None, caption or None)
        policy_counter[policy] += 1

    known_ids = set(all_ids)
    distinct_ids = sorted(set(from_ids))
    return ImportDryRunReport(
        source_file=str(source),
        chat_id=None,
        chat_name=None,
        chat_type="private_supergroup",
        total_messages=len(messages),
        user_messages=len(messages) - service_messages,
        service_messages=service_messages,
        media_count=media_count,
        distinct_users=len([item for item in distinct_ids if item.startswith("user")]),
        distinct_export_user_ids=distinct_ids,
        date_range_start=min(datetimes) if datetimes else None,
        date_range_end=max(datetimes) if datetimes else None,
        reply_count=reply_count,
        dangling_reply_count=sum(1 for item in reply_targets if item not in known_ids),
        duplicate_export_msg_ids=_check_duplicates(all_ids),
        edited_message_count=edited_count,
        forward_count=forward_count,
        anonymous_channel_message_count=anonymous_channel_count,
        message_kind_counts=dict(kind_counter),
        policy_marker_counts=dict(policy_counter),
        parse_warnings=warnings,
        excluded_author_message_count=sum(excluded_counts.values()),
        excluded_author_message_counts=excluded_counts,
    )


def _count_author_matches(
    messages: Iterable[dict],
    excluded_author_names: frozenset[str],
) -> dict[str, int]:
    counts = {name: 0 for name in sorted(excluded_author_names)}
    for message in messages:
        author = message.get("from")
        if not isinstance(author, str):
            continue
        normalized = normalize_import_author_name(author)
        if normalized in counts:
            counts[normalized] += 1
    return counts


def _page_number(name: str) -> int | None:
    match = _PAGE_RE.fullmatch(name)
    if match is None:
        return None
    raw = match.group("number")
    return int(raw) if raw is not None else 1


def _parse_page(page: Path) -> _HtmlNode:
    parser = _TelegramHtmlTreeParser()
    try:
        with page.open(encoding="utf-8") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), ""):
                parser.feed(chunk)
        parser.close()
    except UnicodeDecodeError as exc:
        raise HtmlExportValidationError(f"Telegram HTML page is not UTF-8: {page.name}") from exc
    return parser.root


def _parse_message_id(node: _HtmlNode, page: Path) -> int:
    raw = node.attrs.get("id", "")
    match = _MESSAGE_ID_RE.fullmatch(raw)
    if match is None:
        raise HtmlExportValidationError(
            f"Malformed or missing message id in Telegram HTML page {page.name}"
        )
    return int(match.group("id"))


def _canonicalize_message(
    node: _HtmlNode,
    *,
    page_name: str,
    prior_author: str | None,
) -> tuple[dict, str | None]:
    id_match = _MESSAGE_ID_RE.fullmatch(node.attrs["id"])
    if id_match is None:  # guarded by _parse_message_id; retained for type safety
        raise HtmlExportValidationError(f"Malformed message id in Telegram HTML page {page_name}")
    message_id = int(id_match.group("id"))
    source_path = f"{page_name}#message{message_id}"
    if "service" in node.classes:
        return ({"id": message_id, "type": "service", "source_path": source_path}, None)

    body = _first_direct_child_with_class(node, "body")
    if body is None:
        raise HtmlExportValidationError(
            f"message {message_id} has no body in Telegram HTML page {page_name}"
        )

    author_node = _first_direct_child_with_class(body, "from_name")
    author = _flatten_text(author_node) if author_node is not None else prior_author
    if not author:
        raise HtmlExportValidationError(
            f"message {message_id} has no resolvable author in Telegram HTML page {page_name}"
        )

    date_node = _first_direct_child_with_class(body, "date")
    date_title = date_node.attrs.get("title", "") if date_node is not None else ""
    date = _parse_date_title(date_title, page_name=page_name, message_id=message_id)

    canonical: dict = {
        "id": message_id,
        "type": "message",
        "date": date.isoformat(),
        "date_unixtime": str(int(date.timestamp())),
        "from": author,
        "from_id": _synthetic_from_id(author),
        "source_path": source_path,
        "text": _extract_message_text(body),
    }

    reply_id = _extract_reply_id(body)
    if reply_id is not None:
        canonical["reply_to_message_id"] = reply_id

    forwarded_from = _extract_forwarded_from(body)
    if forwarded_from:
        canonical["forwarded_from"] = forwarded_from

    html_media_kind, media_node = _detect_media(node)
    linked_refs = _extract_media_refs(node)
    media_refs = _extract_media_refs(media_node) if media_node is not None else linked_refs
    if html_media_kind is not None:
        media_caption = canonical.get("text")
        if isinstance(media_caption, str) and media_caption:
            canonical["caption"] = media_caption
        if html_media_kind == "document":
            canonical["mime_type"] = "application/octet-stream"
        elif html_media_kind == "poll":
            question_node = _first_descendant_with_class(media_node, "question")
            canonical["poll"] = {"question": _flatten_text(question_node)}
        else:
            canonical["media_type"] = html_media_kind

        canonical["media_refs"] = media_refs
        metadata = _extract_media_metadata(media_node)
        if metadata:
            canonical["media_metadata"] = metadata

        if html_media_kind == "photo" and media_refs:
            canonical["photo"] = media_refs[0]
        elif html_media_kind not in ("photo", "poll") and media_refs:
            canonical["file"] = media_refs[0]
        non_primary_refs = [item for item in linked_refs if item not in set(media_refs)]
        if non_primary_refs:
            canonical["linked_media_refs"] = non_primary_refs
    elif linked_refs:
        canonical["media_refs"] = linked_refs

    return canonical, author


def _parse_date_title(title: str, *, page_name: str, message_id: int) -> datetime:
    try:
        return datetime.strptime(title, "%d.%m.%Y %H:%M:%S UTC%z")
    except ValueError as exc:
        raise HtmlExportValidationError(
            f"message {message_id} has an invalid date in Telegram HTML page {page_name}"
        ) from exc


def _synthetic_from_id(author: str) -> str:
    normalized = " ".join(unicodedata.normalize("NFKC", author).split()).casefold()
    digest = hashlib.blake2b(normalized.encode("utf-8"), digest_size=8).digest()
    numeric = _SYNTHETIC_USER_ID_BASE + (
        int.from_bytes(digest, byteorder="big", signed=False) & _SYNTHETIC_USER_ID_MASK
    )
    return f"user{numeric}"


def _first_direct_child_with_class(node: _HtmlNode, class_name: str) -> _HtmlNode | None:
    for child in node.children:
        if isinstance(child, _HtmlNode) and class_name in child.classes:
            return child
    return None


def _first_descendant_with_class(
    node: _HtmlNode | None,
    class_name: str,
) -> _HtmlNode | None:
    if node is None:
        return None
    for descendant in node.descendants(include_self=True):
        if class_name in descendant.classes:
            return descendant
    return None


def _flatten_text(
    node: _HtmlNode | None,
    *,
    excluded_classes: frozenset[str] = frozenset(),
) -> str:
    if node is None:
        return ""
    parts: list[str] = []

    def visit(current: _HtmlNode) -> None:
        if current is not node and current.classes & excluded_classes:
            return
        for child in current.children:
            if isinstance(child, str):
                parts.append(child)
            elif child.tag == "br":
                parts.append("\n")
            else:
                visit(child)

    visit(node)
    lines = [" ".join(line.replace("\xa0", " ").split()) for line in "".join(parts).splitlines()]
    return "\n".join(line for line in lines if line)


def _extract_reply_id(body: _HtmlNode) -> int | None:
    reply = _first_descendant_with_class(body, "reply_to")
    if reply is None:
        return None
    for node in reply.descendants(include_self=True):
        href = node.attrs.get("href", "")
        fragment = urlsplit(href).fragment
        candidate = f"#{fragment}" if fragment else href
        match = _REPLY_ID_RE.search(candidate)
        if match is not None:
            return int(match.group("id"))
    return None


def _extract_message_text(body: _HtmlNode) -> str:
    chunks: list[str] = []
    for node in body.descendants():
        if node.tag != "div" or "text" not in node.classes:
            continue
        if _has_ancestor_class(node, "reply_to", stop=body):
            continue
        value = _flatten_text(node, excluded_classes=frozenset({"reactions"}))
        if value:
            chunks.append(value)
    return "\n".join(chunks)


def _extract_forwarded_from(body: _HtmlNode) -> str | None:
    for node in body.descendants():
        if "forwarded" not in node.classes or "body" not in node.classes:
            continue
        source = _first_direct_child_with_class(node, "from_name")
        value = _flatten_text(source, excluded_classes=frozenset({"date"}))
        return value or None
    return None


def _detect_media(node: _HtmlNode) -> tuple[str | None, _HtmlNode | None]:
    for descendant in node.descendants():
        for html_class, canonical_kind in _HTML_MEDIA_CLASS_TO_CANONICAL:
            if html_class in descendant.classes:
                return canonical_kind, descendant
    return None, None


def _extract_media_refs(node: _HtmlNode) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    for descendant in node.descendants(include_self=True):
        if _has_ancestor_class(descendant, "reactions", stop=node):
            continue
        for attr in ("href", "src"):
            safe = _normalize_media_ref(descendant.attrs.get(attr))
            if safe is not None and safe not in seen:
                seen.add(safe)
                refs.append(safe)
    return refs


def _normalize_media_ref(value: str | None) -> str | None:
    if not value:
        return None
    # Reject controls before urlsplit can strip them, Windows separators, and
    # encoded separators/controls. Encoded separators change path boundaries and
    # must never be normalized into an apparently safe path.
    if "\\" in value or _contains_control_character(value):
        return None
    if re.search(r"%(?:2f|5c|0[0-9a-f]|1[0-9a-f]|7f)", value, flags=re.IGNORECASE):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or not parsed.path:
        return None
    try:
        decoded_path = unquote(parsed.path, encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    # A remaining '%' is ambiguous/invalid or a second encoding layer. Controls,
    # backslashes, dot segments, and normalization changes are rejected outright.
    if "%" in decoded_path or "\\" in decoded_path or _contains_control_character(decoded_path):
        return None
    raw_parts = decoded_path.split("/")
    if any(part in ("", ".", "..") for part in raw_parts):
        return None
    path = PurePosixPath(decoded_path)
    if path.is_absolute() or not path.parts or path.as_posix() != decoded_path:
        return None
    if path.parts[0] not in _MEDIA_ROOTS:
        return None
    return path.as_posix()


def _contains_control_character(value: str) -> bool:
    return any(unicodedata.category(char) == "Cc" for char in value)


def _extract_media_metadata(node: _HtmlNode | None) -> dict[str, str]:
    metadata: dict[str, str] = {}
    if node is None:
        return metadata
    for key in ("title", "description", "status"):
        child = _first_descendant_with_class(node, key)
        value = _flatten_text(child)
        if value:
            metadata[key] = value
    return metadata


def _has_ancestor_class(
    node: _HtmlNode,
    class_name: str,
    *,
    stop: _HtmlNode,
) -> bool:
    current = node.parent
    while current is not None and current is not stop:
        if class_name in current.classes:
            return True
        current = current.parent
    return False
