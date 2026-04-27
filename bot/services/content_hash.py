"""Canonical content hash for ``message_versions`` (T1-08).

The hash pins one content state of a message so q&a citations remain stable across
edits (Phase 4). Two versions with identical content produce identical hashes —
``MessageVersionRepo.insert_version`` uses this for idempotency.

## Canonical recipe (T1-08, format version "chv1")

The hash inputs are exactly the four fields below, in this fixed order:

1. **Format version tag** — currently ``"chv1"``. Bumped if the recipe changes; the
   tag is included in the hashed payload so a recipe change cleanly produces new
   hashes for the same content.
2. **text** — the message text body, or ``""`` if absent.
3. **caption** — the media caption, or ``""`` if absent.
4. **message_kind** — the classification (``text``, ``photo``, ``video``, ...), or
   ``"text"`` if absent.
5. **entities** — list of Telegram message entities normalized into a stable order
   (sorted by offset, then length, then type). The list is JSON-encoded with
   ``sort_keys=True`` so each entity dict is also stable. Empty list (or None) is
   treated identically: ``[]``.

The payload is serialized with ``json.dumps(..., sort_keys=True, separators=(',',':'))``
to remove whitespace and dict-key ambiguity, then SHA-256 hashed (UTF-8) and returned
hex-encoded.

## What this hash MUST NOT include

- Volatile raw_json fields (date, id, from_user, message_id, chat) — those are
  metadata, not content. They are intentionally NOT accepted by this function.
- Reactions, edit_date, view counts — those are state, not content.
- Anything that would make the same logical message hash differently across
  ingestion attempts.

The function signature enforces this: only the four canonical inputs are accepted;
no kwargs catch-all.

## Backward-compat note for T1-07 backfilled rows

T1-07's first-cut recipe predated the version tag and entity normalization. v1 rows
created by the T1-07 backfill migration persist with their legacy hashes; live
ingestion (T1-04 + T1-14) and any post-T1-08 inserts produce ``chv1`` hashes.

This divergence does NOT break ``MessageVersionRepo.insert_version`` idempotency:
the repo keys on ``(chat_message_id, content_hash)``, so a given chat_message_id
sees its legacy v1 hash AND any new chv1 hashes as distinct rows — which is the
correct semantic outcome (a recipe change IS a semantic difference).

If we ever need to migrate legacy hashes forward, that's a separate ticket: walk
v1 rows where ``content_hash`` doesn't match the current chv1 of the same
``(text, caption, message_kind)``, recompute, and store. T1-08 deliberately does NOT
do this — backfill stability is preserved.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# Bump ONLY when the recipe produces different output for the same logical content
# (new field added, default changed, normalization changed). Pure code refactors that
# preserve output (variable rename, comment tweaks, equivalent serialization) MUST NOT
# bump the tag — bumping invalidates existing hashes and forces all live-ingested
# versions to re-hash on next edit.
#
# Old hashes persist in the DB forever; new hashes use the new tag. Repo idempotency
# keys on (chat_message_id, content_hash) so divergence creates new version rows
# rather than colliding.
HASH_FORMAT_VERSION = "chv1"


def _normalize_entities(entities: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Return entities sorted by ``(offset, length, type)`` so list-ordering does not
    affect the hash.

    Telegram delivers entities in the order they appear in the message, but downstream
    code might re-order them (e.g. when merging entity lists after an edit). The hash
    is intended to capture content equivalence — re-ordered entities that describe the
    same span set are equivalent and must hash the same.
    """
    if not entities:
        return []
    return sorted(
        entities,
        key=lambda e: (
            int(e.get("offset", 0)),
            int(e.get("length", 0)),
            str(e.get("type", "")),
        ),
    )


def compute_content_hash(
    text: str | None,
    caption: str | None,
    message_kind: str | None,
    entities: list[dict[str, Any]] | None = None,
) -> str:
    """Return the canonical hex SHA-256 ``content_hash`` for a message version.

    See module docstring for the formal recipe and the backward-compat note. The
    function takes ONLY the four canonical inputs — no kwargs, no raw_json.
    """
    payload = json.dumps(
        [
            HASH_FORMAT_VERSION,
            text or "",
            caption or "",
            message_kind or "text",
            _normalize_entities(entities),
        ],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
