from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from bot.services.digest_contract import (
    DigestContractError,
    VERIFIER_VERDICT_PAIRS,
    factual_units,
    merge_digest,
    parse_draft,
    parse_editor,
    parse_verifier,
)
from bot.services.digest_renderer import render_digest_html
from bot.services.llm_prompts.digest_v0_1_0 import verifier_response_schema


def _draft_payload(*, item_count: int = 1) -> str:
    return json.dumps(
        {
            "publish": True,
            "layout": "flat",
            "sections": [
                {
                    "heading": "",
                    "items": [
                        {
                            "text": f"Женя поднял тему {index}",
                            "citations": [f"[[mv:{index}]]"],
                        }
                        for index in range(1, item_count + 1)
                    ],
                }
            ],
            "closing": {
                "text": "К вечеру у чата появился план",
                "citations": ["[[mv:1]]"],
            },
        },
        ensure_ascii=False,
    )


def test_contract_has_no_hard_item_limit() -> None:
    tokens = [f"[[mv:{index}]]" for index in range(1, 66)]
    draft = parse_draft(_draft_payload(item_count=65), citation_tokens=tokens)
    assert len(factual_units(draft)) == 66  # 65 items plus grounded closing


def test_sectioned_digest_appends_multiple_sources_and_single_final_hashtag() -> None:
    draft = parse_draft(
        json.dumps(
            {
                "publish": True,
                "layout": "sectioned",
                "sections": [
                    {
                        "heading": "Про проектный менеджмент агентов",
                        "items": [
                            {
                                "text": "Женя предложил новый ритм, а Маша принесла сравнение",
                                "citations": ["[[mv:10]]", "[[mv:11]]"],
                            }
                        ],
                    }
                ],
                "closing": {
                    "text": "Теперь даже агенты знают, когда у них планёрка",
                    "citations": ["[[mv:10]]"],
                },
            },
            ensure_ascii=False,
        ),
        citation_tokens=["[[mv:10]]", "[[mv:11]]"],
    )
    units = factual_units(draft)
    decisions = parse_verifier(
        json.dumps(
            {
                "items": [
                    {"item_key": unit["item_key"], "verdict": "keep_ok"}
                    for unit in units
                ]
            }
        ),
        units=units,
    )
    body, citations = merge_digest(draft=draft, decisions=decisions, edited_items=[])
    assert body == (
        "## Про проектный менеджмент агентов\n"
        "- Женя предложил новый ритм, а Маша принесла сравнение [[mv:10]] [[mv:11]]\n\n"
        "— Теперь даже агенты знают, когда у них планёрка [[mv:10]]"
    )
    assert [citation["position"] for citation in citations] == [0, 0, 1]

    rendered = render_digest_html(
        body,
        window_start_utc=datetime(2026, 7, 20, 2, tzinfo=timezone.utc),
        source_links_by_citation={
            "[[mv:10]]": "https://t.me/c/123456789/10",
            "[[mv:11]]": "https://t.me/c/123456789/11",
        },
    )
    assert "<b>Про проектный менеджмент агентов</b>" in rendered
    assert rendered.count("↗ источник") == 3
    assert rendered.count("#дайджест") == 1
    assert rendered.splitlines()[-1] == "#дайджест"


def test_verifier_accepts_reversed_complete_order_and_canonicalizes() -> None:
    units = factual_units(
        parse_draft(_draft_payload(), citation_tokens=["[[mv:1]]"])
    )
    decisions = parse_verifier(
        json.dumps(
            {
                "items": [
                    {"item_key": "closing", "verdict": "fix_number"},
                    {"item_key": "item_1", "verdict": "keep_ok"},
                ]
            }
        ),
        units=units,
    )
    assert decisions == [
        {"item_key": "item_1", "action": "keep", "reason": "ok"},
        {"item_key": "closing", "action": "fix", "reason": "number"},
    ]


@pytest.mark.parametrize(
    "items",
    [
        [
            {"item_key": "item_1", "verdict": "keep_ok"},
            {"item_key": "item_1", "verdict": "keep_ok"},
        ],
        [{"item_key": "item_1", "verdict": "keep_ok"}],
        [
            {"item_key": "unknown", "verdict": "keep_ok"},
            {"item_key": "closing", "verdict": "keep_ok"},
        ],
        [
            {"item_key": "item_1", "verdict": "unknown"},
            {"item_key": "closing", "verdict": "keep_ok"},
        ],
    ],
    ids=["duplicate", "missing", "unknown-key", "unknown-verdict"],
)
def test_verifier_rejects_inexact_key_coverage(items: list[dict[str, str]]) -> None:
    units = factual_units(
        parse_draft(_draft_payload(), citation_tokens=["[[mv:1]]"])
    )
    with pytest.raises(DigestContractError):
        parse_verifier(json.dumps({"items": items}), units=units)


@pytest.mark.parametrize(
    ("verdict", "expected_pair"), list(VERIFIER_VERDICT_PAIRS.items())
)
def test_every_verifier_verdict_maps_to_exact_internal_pair(
    verdict: str, expected_pair: tuple[str, str]
) -> None:
    units = [{"item_key": "item_1"}]
    decision = parse_verifier(
        json.dumps({"items": [{"item_key": "item_1", "verdict": verdict}]}),
        units=units,
    )[0]
    assert (decision["action"], decision["reason"]) == expected_pair


def test_verifier_schema_has_one_strict_verdict_enum() -> None:
    item_schema = verifier_response_schema(["item_1"])["properties"]["items"][
        "items"
    ]
    assert item_schema["required"] == ["item_key", "verdict"]
    assert set(item_schema["properties"]) == {"item_key", "verdict"}
    assert item_schema["properties"]["verdict"]["enum"] == list(
        VERIFIER_VERDICT_PAIRS
    )


def test_publish_false_is_one_empty_structured_decision() -> None:
    draft = parse_draft(
        '{"publish":false,"layout":"none","sections":[],"closing":{"text":"","citations":[]}}',
        citation_tokens=["[[mv:1]]"],
    )
    assert draft["publish"] is False
    assert factual_units(draft) == []


def test_plain_model_text_with_separate_citations_is_accepted() -> None:
    draft = parse_draft(_draft_payload(), citation_tokens=["[[mv:1]]"])
    assert factual_units(draft)[0]["text"] == "Женя поднял тему 1"


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("Женя поднял тему [[mv:1]]", "citation tokens"),
        ("Женя оставил ссылку https://t.me/c/123/1", "Telegram URLs"),
    ],
)
def test_model_text_rejects_source_markup(text: str, error: str) -> None:
    payload = json.loads(_draft_payload())
    payload["sections"][0]["items"][0]["text"] = text
    with pytest.raises(DigestContractError, match=error):
        parse_draft(json.dumps(payload, ensure_ascii=False), citation_tokens=["[[mv:1]]"])


def test_exact_user_style_output_has_server_authored_source_and_footer() -> None:
    payload = json.loads(_draft_payload())
    payload["sections"][0]["items"][0]["text"] = (
        "Обсудили новую встречу — решили провести её в пятницу."
    )
    payload["closing"]["text"] = "Календарь всё-таки договорился с чатом."
    draft = parse_draft(
        json.dumps(payload, ensure_ascii=False), citation_tokens=["[[mv:1]]"]
    )
    units = factual_units(draft)
    body, _ = merge_digest(
        draft=draft,
        decisions=[
            {"item_key": unit["item_key"], "action": "keep", "reason": "ok"}
            for unit in units
        ],
        edited_items=[],
    )
    rendered = render_digest_html(
        body,
        window_start_utc=datetime(2026, 7, 20, 2, tzinfo=timezone.utc),
        source_links_by_citation={"[[mv:1]]": "https://t.me/c/123456789/1"},
    )
    assert rendered == (
        "Что было в чате — 20 июля\n\n"
        "- Обсудили новую встречу — решили провести её в пятницу. "
        '[<a href="https://t.me/c/123456789/1">↗ источник</a>]\n\n'
        '<i>— Календарь всё-таки договорился с чатом. '
        '[<a href="https://t.me/c/123456789/1">↗ источник</a>]</i>\n\n'
        "#дайджест"
    )


def test_model_authored_hashtag_is_rejected_to_prevent_duplicate_footer() -> None:
    payload = json.loads(_draft_payload())
    payload["closing"]["text"] = "Финал #ДАЙДЖЕСТ"
    with pytest.raises(DigestContractError, match="hashtag"):
        parse_draft(json.dumps(payload, ensure_ascii=False), citation_tokens=["[[mv:1]]"])


def test_ordinary_urls_emails_versions_and_numbers_are_allowed() -> None:
    payload = json.loads(_draft_payload())
    payload["sections"][0]["items"][0]["text"] = (
        "Женя сравнил v5.6 с v5.7 для аккаунта @team на example.com и написал "
        "team@example.com"
    )
    draft = parse_draft(
        json.dumps(payload, ensure_ascii=False), citation_tokens=["[[mv:1]]"]
    )
    assert "team@example.com" in draft["sections"][0]["items"][0]["text"]


def test_editor_may_fix_text_but_cannot_change_citation_provenance() -> None:
    draft = parse_draft(_draft_payload(), citation_tokens=["[[mv:1]]"])
    item = factual_units(draft)[0]
    edited = parse_editor(
        json.dumps(
            {"items": [{"item_key": item["item_key"], "text": "Женя уточнил тему"}]},
            ensure_ascii=False,
        ),
        fixes=[item],
        allowed_tokens=frozenset({"[[mv:1]]"}),
    )
    assert edited[0]["citations"] == ["[[mv:1]]"]
