"""Validate and merge the flat structured digest contract."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from typing import Any

_CITATION_RE = re.compile(r"\[\[mv:([1-9]\d*)\]\]")
_TELEGRAM_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/\S+", re.IGNORECASE
)
_RUSSIAN_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+7|8)[\s().-]*\d{3}[\s().-]*\d{3}[\s.-]*\d{2}[\s.-]*\d{2}(?!\d)"
)


class DigestContractError(ValueError):
    pass


def _json_object(answer_text: str, *, stage: str) -> dict[str, Any]:
    try:
        payload = json.loads(answer_text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise DigestContractError(f"digest {stage} response is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise DigestContractError(f"digest {stage} response must be an object")
    return payload


def _validate_text(text: Any, citations: Any, *, allowed_tokens: frozenset[str]) -> str:
    from bot.services.qa_guardrails import contains_secret_like_data

    if (
        not isinstance(text, str)
        or not text
        or text != text.strip()
        or "\n" in text
        or "\r" in text
    ):
        raise DigestContractError("digest content text is invalid")
    if contains_secret_like_data(text) or _RUSSIAN_PHONE_RE.search(text) is not None:
        raise DigestContractError("digest content contains forbidden sensitive data")
    if "#дайджест" in text.casefold():
        raise DigestContractError("model-authored digest content cannot contain the hashtag")
    if "[[mv:" in text:
        raise DigestContractError("model-authored digest text cannot contain citation tokens")
    if _TELEGRAM_URL_RE.search(text) is not None:
        raise DigestContractError("model-authored digest text cannot contain Telegram URLs")
    if not isinstance(citations, list) or not citations:
        raise DigestContractError("digest content requires citations")
    if any(not isinstance(token, str) or token not in allowed_tokens for token in citations):
        raise DigestContractError("digest content has an unknown citation")
    if len(set(citations)) != len(citations):
        raise DigestContractError("digest content has duplicate citations")
    return text


def parse_draft(answer_text: str, *, citation_tokens: Sequence[str]) -> dict[str, Any]:
    payload = _json_object(answer_text, stage="draft")
    if set(payload) != {"publish", "layout", "sections", "closing"}:
        raise DigestContractError("digest draft has invalid top-level keys")
    publish = payload["publish"]
    layout = payload["layout"]
    sections = payload["sections"]
    closing = payload["closing"]
    if not isinstance(publish, bool) or layout not in {"none", "flat"}:
        raise DigestContractError("digest draft has invalid publication decision")
    if not isinstance(sections, list) or not isinstance(closing, dict):
        raise DigestContractError("digest draft has invalid structure")
    if not publish:
        if layout != "none" or sections or closing != {"text": "", "citations": []}:
            raise DigestContractError("publish=false draft must be empty")
        return {"publish": False, "layout": "none", "sections": [], "closing": closing}
    if layout != "flat" or len(sections) != 1:
        raise DigestContractError("publish=true draft requires one flat section")

    allowed = frozenset(citation_tokens)
    parsed_sections: list[dict[str, Any]] = []
    item_number = 0
    for section in sections:
        if not isinstance(section, dict) or set(section) != {"heading", "items"}:
            raise DigestContractError("digest section is malformed")
        heading, items = section["heading"], section["items"]
        if (
            not isinstance(heading, str)
            or heading != heading.strip()
            or "\n" in heading
            or _CITATION_RE.search(heading) is not None
            or "#дайджест" in heading.casefold()
            or not isinstance(items, list)
            or not items
        ):
            raise DigestContractError("digest section is invalid")
        parsed_items: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict) or set(item) != {"text", "details", "citations"}:
                raise DigestContractError("digest item is malformed")
            if not isinstance(item["citations"], list) or len(item["citations"]) != 1:
                raise DigestContractError("digest content requires exactly one citation")
            item_number += 1
            parsed_items.append(
                {
                    "item_key": f"item_{item_number}",
                    "text": _validate_text(
                        item["text"], item["citations"], allowed_tokens=allowed
                    ),
                    "details": _validate_text(
                        item["details"], item["citations"], allowed_tokens=allowed
                    ),
                    "citations": list(item["citations"]),
                }
            )
        parsed_sections.append({"heading": heading, "items": parsed_items})

    if parsed_sections[0]["heading"] != "":
        raise DigestContractError("flat digest requires one unheaded section")

    if set(closing) != {"text", "citations"}:
        raise DigestContractError("digest closing is malformed")
    if not isinstance(closing["citations"], list) or len(closing["citations"]) != 1:
        raise DigestContractError("digest content requires exactly one citation")
    parsed_closing = {
        "item_key": "closing",
        "text": _validate_text(closing["text"], closing["citations"], allowed_tokens=allowed),
        "citations": list(closing["citations"]),
    }
    return {
        "publish": True,
        "layout": layout,
        "sections": parsed_sections,
        "closing": parsed_closing,
    }


def factual_units(draft: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not draft["publish"]:
        return []
    units: list[dict[str, Any]] = []
    for section_index, section in enumerate(draft["sections"]):
        if section["heading"]:
            units.append(
                {
                    "item_key": f"heading_{section_index}",
                    "text": section["heading"],
                    "citations": list(
                        dict.fromkeys(
                            token for item in section["items"] for token in item["citations"]
                        )
                    ),
                }
            )
        for item in section["items"]:
            units.extend(
                [
                    item,
                    {
                        "item_key": f"{item['item_key']}_details",
                        "text": item["details"],
                        "citations": list(item["citations"]),
                    },
                ]
            )
    return [*units, draft["closing"]]


VERIFIER_VERDICT_PAIRS = {
    "keep_ok": ("keep", "ok"),
    "fix_fact": ("fix", "fact"),
    "fix_name": ("fix", "name"),
    "fix_number": ("fix", "number"),
    "fix_modality": ("fix", "modality"),
    "block_fact": ("block", "fact"),
    "block_name": ("block", "name"),
    "block_number": ("block", "number"),
    "block_modality": ("block", "modality"),
    "block_citations": ("block", "citations"),
}


def parse_verifier(
    answer_text: str, *, units: Sequence[Mapping[str, Any]]
) -> list[dict[str, str]]:
    payload = _json_object(answer_text, stage="verifier")
    if set(payload) != {"items"} or not isinstance(payload["items"], list):
        raise DigestContractError("digest verifier has invalid structure")
    expected_keys = [unit["item_key"] for unit in units]
    if len(payload["items"]) != len(expected_keys):
        raise DigestContractError("digest verifier coverage mismatch")
    expected_key_set = set(expected_keys)
    parsed_by_key: dict[str, dict[str, str]] = {}
    for item in payload["items"]:
        if not isinstance(item, dict) or set(item) != {"item_key", "verdict"}:
            raise DigestContractError("digest verifier item is malformed")
        item_key = item["item_key"]
        verdict = item["verdict"]
        if (
            not isinstance(item_key, str)
            or item_key not in expected_key_set
            or item_key in parsed_by_key
            or not isinstance(verdict, str)
            or verdict not in VERIFIER_VERDICT_PAIRS
        ):
            raise DigestContractError("digest verifier item is invalid")
        action, reason = VERIFIER_VERDICT_PAIRS[verdict]
        parsed_by_key[item_key] = {
            "item_key": item_key,
            "action": action,
            "reason": reason,
        }
    if set(parsed_by_key) != expected_key_set:
        raise DigestContractError("digest verifier coverage mismatch")
    return [parsed_by_key[item_key] for item_key in expected_keys]


def parse_editor(
    answer_text: str,
    *,
    fixes: Sequence[Mapping[str, Any]],
    allowed_tokens: frozenset[str],
) -> list[dict[str, Any]]:
    payload = _json_object(answer_text, stage="editor")
    if set(payload) != {"items"} or not isinstance(payload["items"], list):
        raise DigestContractError("digest editor has invalid structure")
    if len(payload["items"]) != len(fixes):
        raise DigestContractError("digest editor coverage mismatch")
    parsed: list[dict[str, Any]] = []
    for original, item in zip(fixes, payload["items"], strict=True):
        if not isinstance(item, dict) or set(item) != {"item_key", "text"}:
            raise DigestContractError("digest editor item is malformed")
        if item["item_key"] != original["item_key"]:
            raise DigestContractError("digest editor changed item identity")
        parsed.append(
            {
                "item_key": original["item_key"],
                "text": _validate_text(
                    item["text"], original["citations"], allowed_tokens=allowed_tokens
                ),
                "citations": list(original["citations"]),
            }
        )
    return parsed


def merge_digest(
    *,
    draft: Mapping[str, Any],
    decisions: Sequence[Mapping[str, str]],
    edited_items: Sequence[Mapping[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    merged = _merged_digest(draft=draft, decisions=decisions, edited_items=edited_items)
    return _render_digest(merged)


def compact_weekly_digest(
    *,
    draft: Mapping[str, Any],
    decisions: Sequence[Mapping[str, str]],
    edited_items: Sequence[Mapping[str, Any]],
    visible_length: Callable[[str], int],
    visible_target: int,
) -> tuple[str, list[dict[str, Any]]]:
    """Drop trailing whole items until a verified weekly digest fits."""
    merged = _merged_digest(draft=draft, decisions=decisions, edited_items=edited_items)
    heading_tokens = {
        id(section): frozenset(
            token for item in section["items"] for token in item["citations"]
        )
        for section in merged["sections"]
    }
    while True:
        body, citations = _render_digest(merged)
        if visible_length(body) <= visible_target:
            return body, citations
        for section in reversed(merged["sections"]):
            if section["items"]:
                section["items"].pop()
                break
        else:
            raise DigestContractError("weekly digest cannot fit visible target")
        merged["sections"] = [section for section in merged["sections"] if section["items"]]
        if not merged["sections"]:
            raise DigestContractError("weekly digest cannot fit visible target")
        for section in merged["sections"]:
            surviving_tokens = {
                token for item in section["items"] for token in item["citations"]
            }
            if not heading_tokens[id(section)].issubset(surviving_tokens):
                section["heading"] = ""


def _merged_digest(
    *,
    draft: Mapping[str, Any],
    decisions: Sequence[Mapping[str, str]],
    edited_items: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    units = factual_units(draft)
    if len(units) != len(decisions):
        raise DigestContractError("digest merge coverage mismatch")
    if any(decision["action"] == "block" for decision in decisions):
        raise DigestContractError("digest verifier blocked publication")
    expected_fix_keys = {
        decision["item_key"] for decision in decisions if decision["action"] == "fix"
    }
    edited_by_key = {item["item_key"]: item for item in edited_items}
    if set(edited_by_key) != expected_fix_keys:
        raise DigestContractError("digest editor coverage mismatch")

    merged: dict[str, Any] = deepcopy(draft)
    merged_units = factual_units(merged)
    for unit, decision in zip(merged_units, decisions, strict=True):
        if unit["item_key"] != decision["item_key"]:
            raise DigestContractError("digest merge identity mismatch")
        if decision["action"] == "fix":
            text = edited_by_key[unit["item_key"]]["text"]
            if unit["item_key"].startswith("heading_"):
                merged["sections"][int(unit["item_key"].removeprefix("heading_"))]["heading"] = text
            elif unit["item_key"].endswith("_details"):
                item_number = int(
                    unit["item_key"].removeprefix("item_").removesuffix("_details")
                )
                merged["sections"][0]["items"][item_number - 1]["details"] = text
            else:
                unit["text"] = text
    return merged


def _render_digest(merged: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:

    lines: list[str] = []
    citations: list[dict[str, Any]] = []
    position = 0
    for section_index, section in enumerate(merged["sections"]):
        if section_index:
            lines.append("")
        if section["heading"]:
            lines.append(f"## {section['heading']}")
        for item in section["items"]:
            lines.append(f"- {item['text']} {' '.join(item['citations'])}")
            lines.append(f"  {item['details']}")
            citations.extend(_citation_rows(item["citations"], position=position))
            position += 1
    lines.extend(
        [
            "",
            f"— {merged['closing']['text']} {' '.join(merged['closing']['citations'])}",
        ]
    )
    citations.extend(_citation_rows(merged["closing"]["citations"], position=position))
    body = "\n".join(lines)
    if not body.strip() or not citations:
        raise DigestContractError("digest final result is empty")
    return body, citations


def _citation_rows(tokens: Sequence[str], *, position: int) -> list[dict[str, Any]]:
    return [
        {"kind": "message_version", "id": int(token[5:-2]), "position": position}
        for token in tokens
    ]


__all__ = [
    "DigestContractError",
    "VERIFIER_VERDICT_PAIRS",
    "compact_weekly_digest",
    "factual_units",
    "merge_digest",
    "parse_draft",
    "parse_editor",
    "parse_verifier",
]
