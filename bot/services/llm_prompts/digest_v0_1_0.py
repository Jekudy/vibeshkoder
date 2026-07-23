"""Issue #406 digest instructions, inputs, and strict response schemas."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from bot.services.digest_contract import VERIFIER_VERDICT_PAIRS

PROMPT_VERSION = "digest-v0.5.0"

DRAFT_INSTRUCTIONS = """Ты редактор приватного русскоязычного Telegram-чата.
Составь короткое кликабельное оглавление того, что происходило в чате. Используй только
предоставленные сообщения.

Один item — пара: text — очень короткий конкретный ярлык темы или события, не мини-пересказ треда;
details — более полное, но всё ещё редакторское описание в одну строку, 1–3 коротких предложения.
Сохраняй имена, названия, модели, версии, цифры, ссылки и полезные детали. Если в сообщении
есть author_username, называй участника как @username; иначе используй точный author_display.
У каждого item должна быть ровно одна самая полезная citation, общая для text и details. В text и
details пиши только обычный текст без citation tokens и Telegram URLs; оба поля должны опираться
только на выбранную citation.

При publish=true всегда верни layout=flat и ровно одну секцию с пустым heading.
Жёсткого лимита пунктов нет, но не добавляй filler. Weekly должен читаться за 30–60 секунд:
объединяй только очевидные повторы, сохраняй конкретику и не делай тематические секции.

Добавь короткую grounded closing-реплику или шутку с одной citation. Не выдумывай факты и не шути
над участником. Не включай команды, телефоны, credentials или raw payloads. Не создавай заголовок
поста, #дайджест или Telegram URLs источников — их добавит приложение. Если значимых тем нет,
верни publish=false, layout=none, пустые sections и пустую closing. Следуй JSON schema; никакого
текста вне JSON.
"""

VERIFIER_INSTRUCTIONS = """Проверь каждый item и closing только по приложенному cited_evidence.
Проверяй факты, Telegram author_username и display names, числа, версии, модальность и
соответствие citations. Если cited_evidence содержит author_username, а item называет этого
участника display name, без @username или с другим @username, обязательно верни fix_name.
Не оценивай стиль, порядок, рубрики, полноту или сходство с gold.
Верни каждый item_key ровно один раз; порядок не важен. Для каждого выбери один verdict:
keep_ok означает точное подтверждение; fix_* — текст можно исправить, не меняя citations;
block_* — citations не позволяют безопасно исправить утверждение.
Следуй JSON schema; никакого текста вне JSON.
"""

EDITOR_INSTRUCTIONS = """Исправь только переданные factual findings по cited_evidence.
Не меняй структуру, item_key или citation provenance. В text верни только обычный текст без
citation tokens и Telegram URLs; приложение сохранит citations само. Если cited_evidence
содержит author_username, используй @username; иначе используй author_display. Не добавляй
новые факты, источники или внешние знания. Верни каждый item_key ровно один раз и в исходном
порядке. Следуй JSON schema; никакого текста вне JSON.
"""

FINALIZER_INSTRUCTIONS = """Ты финальный редактор уже проверенного недельного дайджеста.
Верни полный adaptive digest с publish=true. Исправь findings с action=fix, а claims с
action=keep не искажай. Можно объединять, переставлять и удалять менее важные пункты, чтобы
весь пост уложился в visible_character_target; жёсткого лимита пунктов нет.

Используй только факты из cited_evidence и только citation tokens, уже использованные в
исходном draft. Не добавляй новые факты, источники или tokens. У каждого factual item должна
остаться пара text/details с одной общей citation; у короткой grounded closing — отдельная citation.
text и details должны быть без citation tokens и Telegram URLs. Приложение само добавит заголовок,
source labels и #дайджест; их длина
уже включена в target. Следуй JSON schema; никакого текста вне JSON.
"""


def load_private_gold_examples() -> list[dict[str, Any]]:
    """Load exactly two runtime-injected human gold examples without logging them."""
    raw_path = os.environ.get("DIGEST_GOLD_EXAMPLES_PATH")
    if raw_path is None or not raw_path.strip():
        raise ValueError("DIGEST_GOLD_EXAMPLES_PATH is required")
    path = Path(raw_path).expanduser()
    if not path.is_file() or path.is_symlink():
        raise ValueError("DIGEST_GOLD_EXAMPLES_PATH must be a regular file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("DIGEST_GOLD_EXAMPLES_PATH is unreadable or invalid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"examples"}:
        raise ValueError("digest gold file must contain only examples")
    examples = payload["examples"]
    if not isinstance(examples, list) or len(examples) != 2:
        raise ValueError("digest gold file must contain exactly two examples")
    expected_activities = {"short", "busy"}
    if any(
        not isinstance(example, dict)
        or set(example) != {"activity", "input", "output"}
        or example["activity"] not in expected_activities
        or not isinstance(example["input"], dict)
        or not isinstance(example["output"], dict)
        for example in examples
    ):
        raise ValueError("digest gold examples have an invalid schema")
    if {example["activity"] for example in examples} != expected_activities:
        raise ValueError("digest gold file requires one short and one busy example")
    return examples


def message_payload(message: Any) -> dict[str, Any]:
    return {
        "citation": f"[[mv:{message.message_version_id}]]",
        "telegram_message_id": getattr(message, "telegram_message_id", None),
        "author_display": message.author_display,
        "author_username": getattr(message, "author_username", None),
        "timestamp": message.ts.isoformat(),
        "text": message.text,
        "caption": getattr(message, "caption", None),
        "message_kind": getattr(message, "message_kind", None),
        "reply_to_message_id": getattr(message, "reply_to_message_id", None),
        "message_thread_id": getattr(message, "message_thread_id", None),
        "media_kind": getattr(message, "media_kind", None),
        "media_description": getattr(message, "media_description", None),
        "forward_origin": {
            "type": getattr(message, "forward_origin_type", None),
            "display": getattr(message, "forward_origin_display", None),
            "date": getattr(message, "forward_origin_date", None),
        },
    }


def build_draft_input(
    *,
    digest_type: str,
    window_start_msk: str,
    window_end_msk: str,
    messages: Sequence[Any],
    gold_examples: Sequence[Mapping[str, Any]],
) -> str:
    return json.dumps(
        {
            "digest_type": digest_type,
            "window": {"start": window_start_msk, "end": window_end_msk},
            "human_gold_examples": list(gold_examples),
            "messages": [message_payload(message) for message in messages],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _citation_schema(citation_tokens: Sequence[str]) -> dict[str, Any]:
    return {"type": "string"}


def draft_response_schema(citation_tokens: Sequence[str]) -> dict[str, Any]:
    citation = _citation_schema(citation_tokens)
    citations = {"type": "array", "items": citation, "maxItems": 1}
    content = {
        "type": "object",
        "additionalProperties": False,
        "required": ["text", "citations"],
        "properties": {
            "text": {"type": "string"},
            "citations": citations,
        },
    }
    item_content = {
        **content,
        "required": ["text", "details", "citations"],
        "properties": {**content["properties"], "details": {"type": "string"}},
    }
    section = {
        "type": "object",
        "additionalProperties": False,
        "required": ["heading", "items"],
        "properties": {
            "heading": {"type": "string"},
            "items": {"type": "array", "items": item_content},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["publish", "layout", "sections", "closing"],
        "properties": {
            "publish": {"type": "boolean"},
            "layout": {"type": "string", "enum": ["none", "flat"]},
            "sections": {"type": "array", "items": section, "maxItems": 1},
            "closing": content,
        },
    }


def verifier_response_schema(item_keys: Sequence[str]) -> dict[str, Any]:
    keys = list(item_keys)
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["items"],
        "properties": {
            "items": {
                "type": "array",
                "minItems": len(keys),
                "maxItems": len(keys),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["item_key", "verdict"],
                    "properties": {
                        "item_key": {"type": "string", "enum": keys},
                        "verdict": {"type": "string", "enum": list(VERIFIER_VERDICT_PAIRS)},
                    },
                },
            }
        },
    }


def editor_response_schema(item_keys: Sequence[str]) -> dict[str, Any]:
    keys = list(item_keys)
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["items"],
        "properties": {
            "items": {
                "type": "array",
                "minItems": len(keys),
                "maxItems": len(keys),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["item_key", "text"],
                    "properties": {
                        "item_key": {"type": "string", "enum": keys},
                        "text": {"type": "string"},
                    },
                },
            }
        },
    }


def build_verifier_input(
    *, items: Sequence[Mapping[str, Any]], citation_evidence: Mapping[str, Mapping[str, Any]]
) -> str:
    scoped = []
    for item in items:
        scoped.append(
            {
                "item_key": item["item_key"],
                "text": item["text"],
                "citations": item["citations"],
                "cited_evidence": [citation_evidence[token] for token in item["citations"]],
            }
        )
    return json.dumps({"items": scoped}, ensure_ascii=False, separators=(",", ":"))


def build_editor_input(
    *, fixes: Sequence[Mapping[str, Any]], citation_evidence: Mapping[str, Mapping[str, Any]]
) -> str:
    scoped = []
    for item in fixes:
        scoped.append(
            {
                "item_key": item["item_key"],
                "text": item["text"],
                "citations": item["citations"],
                "reason": item["reason"],
                "cited_evidence": [citation_evidence[token] for token in item["citations"]],
            }
        )
    return json.dumps({"items": scoped}, ensure_ascii=False, separators=(",", ":"))


def build_finalizer_input(
    *,
    draft: Mapping[str, Any],
    decisions: Sequence[Mapping[str, str]],
    citation_evidence: Mapping[str, Mapping[str, Any]],
    visible_target: int,
) -> str:
    used_tokens = [
        token
        for section in draft["sections"]
        for item in section["items"]
        for token in item["citations"]
    ]
    used_tokens.extend(draft["closing"]["citations"])
    scoped_evidence = {
        token: citation_evidence[token] for token in dict.fromkeys(used_tokens)
    }
    return json.dumps(
        {
            "visible_character_target": visible_target,
            "draft": draft,
            "verifier_decisions": list(decisions),
            "cited_evidence": scoped_evidence,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


__all__ = [
    "DRAFT_INSTRUCTIONS",
    "EDITOR_INSTRUCTIONS",
    "FINALIZER_INSTRUCTIONS",
    "PROMPT_VERSION",
    "VERIFIER_INSTRUCTIONS",
    "build_draft_input",
    "build_editor_input",
    "build_finalizer_input",
    "build_verifier_input",
    "draft_response_schema",
    "editor_response_schema",
    "load_private_gold_examples",
    "message_payload",
    "verifier_response_schema",
]
