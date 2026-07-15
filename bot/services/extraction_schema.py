"""Strict data contract shared by extraction and candidate promotion."""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

MAX_EXTRACTION_INPUT_BYTES = 200_000
EXTRACTION_CANDIDATE_SCHEMA_VERSION = "karpathy-wiki-v1"
EXTRACTION_PROMPT_TEMPLATE_VERSION = "v0.1.1"
MAX_TOPIC_SLUG_CHARS = 100
MAX_CARD_TITLE_CHARS = 200
MAX_CARD_BODY_CHARS = 20_000
MAX_TAG_COUNT = 20
MAX_TAG_CHARS = 64

_TOPIC_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_ENVELOPE_KEYS = frozenset({"candidate_json", "source_message_version_ids"})
_CANDIDATE_KEYS = frozenset({"topic_slug", "title", "body_markdown", "tags"})


class CandidateValidationError(ValueError):
    """Provider candidate does not satisfy the canonical card contract."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ValidatedExtractionCandidate:
    topic_slug: str
    title: str
    body_markdown: str
    tags: tuple[str, ...]
    source_message_version_ids: tuple[int, ...]

    @property
    def candidate_json(self) -> dict[str, Any]:
        return {
            "topic_slug": self.topic_slug,
            "title": self.title,
            "body_markdown": self.body_markdown,
            "tags": list(self.tags),
        }


def _required_text(
    value: object,
    *,
    field: str,
    max_chars: int,
    allow_newlines: bool,
) -> str:
    if not isinstance(value, str):
        raise CandidateValidationError(f"{field}_not_string")
    normalized = value.strip()
    if not normalized or len(normalized) > max_chars:
        raise CandidateValidationError(f"{field}_invalid_length")
    if "\x00" in normalized:
        raise CandidateValidationError(f"{field}_contains_nul")
    if not allow_newlines and ("\n" in normalized or "\r" in normalized):
        raise CandidateValidationError(f"{field}_contains_newline")
    return normalized


def validate_candidate_envelope(
    value: object,
    *,
    allowed_source_message_version_ids: Collection[int],
) -> ValidatedExtractionCandidate:
    """Validate one provider envelope without coercing provider-controlled types."""
    if not isinstance(value, Mapping) or set(value) != _ENVELOPE_KEYS:
        raise CandidateValidationError("envelope_keys_invalid")

    raw_candidate = value.get("candidate_json")
    if not isinstance(raw_candidate, Mapping) or set(raw_candidate) != _CANDIDATE_KEYS:
        raise CandidateValidationError("candidate_json_keys_invalid")

    topic_slug = _required_text(
        raw_candidate.get("topic_slug"),
        field="topic_slug",
        max_chars=MAX_TOPIC_SLUG_CHARS,
        allow_newlines=False,
    )
    if not _TOPIC_SLUG_RE.fullmatch(topic_slug):
        raise CandidateValidationError("topic_slug_not_lowercase_kebab")
    title = _required_text(
        raw_candidate.get("title"),
        field="title",
        max_chars=MAX_CARD_TITLE_CHARS,
        allow_newlines=False,
    )
    body_markdown = _required_text(
        raw_candidate.get("body_markdown"),
        field="body_markdown",
        max_chars=MAX_CARD_BODY_CHARS,
        allow_newlines=True,
    )

    raw_tags = raw_candidate.get("tags")
    if not isinstance(raw_tags, list) or len(raw_tags) > MAX_TAG_COUNT:
        raise CandidateValidationError("tags_invalid")
    tags: list[str] = []
    seen_tags: set[str] = set()
    for raw_tag in raw_tags:
        tag = _required_text(
            raw_tag,
            field="tag",
            max_chars=MAX_TAG_CHARS,
            allow_newlines=False,
        )
        if tag not in seen_tags:
            seen_tags.add(tag)
            tags.append(tag)

    raw_source_ids = value.get("source_message_version_ids")
    if not isinstance(raw_source_ids, list) or not raw_source_ids:
        raise CandidateValidationError("source_ids_invalid")
    allowed = set(allowed_source_message_version_ids)
    source_ids: list[int] = []
    seen_source_ids: set[int] = set()
    for raw_source_id in raw_source_ids:
        # ``bool`` is an ``int`` subclass; provider booleans are not ids.
        if type(raw_source_id) is not int or raw_source_id <= 0:
            raise CandidateValidationError("source_id_not_positive_integer")
        if raw_source_id not in allowed:
            raise CandidateValidationError("source_id_not_in_input")
        if raw_source_id not in seen_source_ids:
            seen_source_ids.add(raw_source_id)
            source_ids.append(raw_source_id)

    return ValidatedExtractionCandidate(
        topic_slug=topic_slug,
        title=title,
        body_markdown=body_markdown,
        tags=tuple(tags),
        source_message_version_ids=tuple(source_ids),
    )


def serialize_untrusted_source_versions(
    source_versions: Sequence[Mapping[str, Any]],
) -> str:
    """Render provider input as one canonical JSON object per physical line."""
    lines: list[str] = []
    for source in source_versions:
        raw_mvid = source.get("message_version_id")
        if type(raw_mvid) is not int or raw_mvid <= 0:
            raise ValueError("source message_version_id must be a positive integer")
        for field in ("text", "caption", "normalized_text"):
            field_value = source.get(field)
            if field_value is not None and not isinstance(field_value, str):
                raise ValueError(f"source {field} must be a string or null")
        content = source.get("normalized_text") or source.get("text") or source.get("caption") or ""
        record: dict[str, Any] = {
            "content": content,
            "message_version_id": raw_mvid,
        }
        line = json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        # Prevent source text from spelling the prompt's structural boundary
        # literally. These are standard JSON escapes and preserve content.
        lines.append(line.replace("<", r"\u003c").replace(">", r"\u003e"))
    return "\n".join(lines)


def extraction_input_size_bytes(
    source_versions: Sequence[Mapping[str, Any]],
) -> int:
    return len(serialize_untrusted_source_versions(source_versions).encode("utf-8"))


__all__ = [
    "CandidateValidationError",
    "EXTRACTION_CANDIDATE_SCHEMA_VERSION",
    "EXTRACTION_PROMPT_TEMPLATE_VERSION",
    "MAX_CARD_BODY_CHARS",
    "MAX_CARD_TITLE_CHARS",
    "MAX_EXTRACTION_INPUT_BYTES",
    "MAX_TAG_CHARS",
    "MAX_TAG_COUNT",
    "MAX_TOPIC_SLUG_CHARS",
    "ValidatedExtractionCandidate",
    "extraction_input_size_bytes",
    "serialize_untrusted_source_versions",
    "validate_candidate_envelope",
]
