"""Strict author-name exclusion contract for historical imports.

Telegram HTML exports do not expose ``is_bot`` or numeric sender ids. Operators
must therefore name bot authors explicitly. Matching is exact after NFKC,
whitespace collapsing, and Unicode casefold; substring/fuzzy matching is forbidden.
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Iterable, Mapping


IMPORT_EXCLUDED_AUTHOR_NAMES_ENV = "IMPORT_EXCLUDED_AUTHOR_NAMES_JSON"


def normalize_import_author_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def load_import_excluded_author_names(
    *,
    env: Mapping[str, str],
    cli_names: Iterable[str] | None,
) -> frozenset[str]:
    """Load and validate exact author names from JSON env plus repeatable CLI args."""
    values: list[str] = []
    if IMPORT_EXCLUDED_AUTHOR_NAMES_ENV in env:
        raw = env[IMPORT_EXCLUDED_AUTHOR_NAMES_ENV]
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{IMPORT_EXCLUDED_AUTHOR_NAMES_ENV} must be a JSON array of non-empty strings"
            ) from exc
        if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
            raise ValueError(
                f"{IMPORT_EXCLUDED_AUTHOR_NAMES_ENV} must be a JSON array of non-empty strings"
            )
        values.extend(decoded)

    if cli_names is not None:
        values.extend(cli_names)

    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError(
                f"{IMPORT_EXCLUDED_AUTHOR_NAMES_ENV} must be a JSON array of non-empty strings"
            )
        item = normalize_import_author_name(value)
        if not item:
            raise ValueError(
                f"{IMPORT_EXCLUDED_AUTHOR_NAMES_ENV} must contain only non-empty strings"
            )
        normalized.add(item)
    return frozenset(normalized)


def normalize_import_excluded_author_names(names: Iterable[str]) -> frozenset[str]:
    """Validate and normalize names supplied directly to the apply service."""
    normalized: set[str] = set()
    for value in names:
        if not isinstance(value, str):
            raise ValueError("excluded_author_names must contain only strings")
        item = normalize_import_author_name(value)
        if not item:
            raise ValueError("excluded_author_names must contain only non-empty strings")
        normalized.add(item)
    return frozenset(normalized)


def is_import_author_excluded(author: str | None, names: frozenset[str]) -> bool:
    if not isinstance(author, str):
        return False
    return normalize_import_author_name(author) in names
