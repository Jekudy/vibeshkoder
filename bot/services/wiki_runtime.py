"""Strict environment configuration for the automatic static wiki pipeline."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WikiRuntimeConfig:
    publish_dir: Path
    site_title: str
    forbidden_origins: tuple[str, ...]


def load_wiki_runtime_config(
    environ: Mapping[str, str] | None = None,
    *,
    require_forbidden_origins: bool,
) -> WikiRuntimeConfig:
    """Load the filesystem export contract without inventing missing values."""

    values = os.environ if environ is None else environ
    raw_publish_dir = values.get("WIKI_STATIC_PUBLISH_DIR", "")
    if not raw_publish_dir or raw_publish_dir != raw_publish_dir.strip():
        raise ValueError("WIKI_STATIC_PUBLISH_DIR must be a non-empty absolute path")
    publish_dir = Path(raw_publish_dir)
    if not publish_dir.is_absolute() or publish_dir == Path("/"):
        raise ValueError("WIKI_STATIC_PUBLISH_DIR must be a safe absolute path")

    site_title = values.get("WIKI_SITE_TITLE", "Shkoder Wiki").strip()
    if not site_title or len(site_title) > 120:
        raise ValueError("WIKI_SITE_TITLE must contain 1..120 characters")

    raw_forbidden = values.get("WIKI_FORBIDDEN_ORIGINS_JSON", "[]")
    try:
        parsed_forbidden = json.loads(raw_forbidden)
    except json.JSONDecodeError as exc:
        raise ValueError("WIKI_FORBIDDEN_ORIGINS_JSON must be a JSON array") from exc
    if not isinstance(parsed_forbidden, list) or any(
        not isinstance(item, str) or not item.strip() or item != item.strip()
        for item in parsed_forbidden
    ):
        raise ValueError("WIKI_FORBIDDEN_ORIGINS_JSON must contain trimmed strings")
    forbidden_origins = tuple(dict.fromkeys(parsed_forbidden))
    if require_forbidden_origins and not forbidden_origins:
        raise ValueError(
            "WIKI_FORBIDDEN_ORIGINS_JSON must name the VPS origins before public deployment"
        )

    return WikiRuntimeConfig(
        publish_dir=publish_dir,
        site_title=site_title,
        forbidden_origins=forbidden_origins,
    )


__all__ = ["WikiRuntimeConfig", "load_wiki_runtime_config"]
