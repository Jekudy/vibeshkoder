from __future__ import annotations

import pytest


PREDKO_ANSWERS = [
    ("name", "Сергей"),
    ("location", "UK"),
    ("referral", "@oxanagesina"),
    ("experience", "Есть реализованная система лидогенерации и авторассылок."),
    ("projects", "Есть реализованная система лидогенерации и авторассылок."),
    (
        "hardest",
        "Вот эту систему и сделал. И вокруг нее поднял всю необходимую инфраструктуру.",
    ),
    (
        "goals",
        "Хочу делать больше и сложнее. Чтобы автономность и надежность были высокими "
        "+ решения были ценными и генерящими выручку.",
    ),
]

PREDKO_BODY = "\n".join(
    [
        "👤 Имя: Сергей",
        "📍 Основная локация: UK",
        "🔗 От кого узнал о чате: @oxanagesina",
        "💡 Опыт с вайб-кодингом: Есть реализованная система лидогенерации и авторассылок.",
        "🚀 Проекты и автоматизации: Есть реализованная система лидогенерации и авторассылок.",
        "🏋️ Самое сложное: Вот эту систему и сделал. И вокруг нее поднял всю необходимую инфраструктуру.",
        "🎯 Цели: Хочу делать больше и сложнее. Чтобы автономность и надежность были высокими "
        "+ решения были ценными и генерящими выручку.",
    ]
)

LEGACY_BODY = "\n".join(
    [
        "👤 Сергей",
        "📍 UK",
        "🔗 Откуда узнал: @oxanagesina",
        "💡 Опыт: Есть реализованная система лидогенерации и авторассылок.",
        "🚀 Проекты: Есть реализованная система лидогенерации и авторассылок.",
        "🏋️ Самое сложное: Вот эту систему и сделал. И вокруг нее поднял всю необходимую инфраструктуру.",
        "🎯 Цели: Хочу делать больше и сложнее. Чтобы автономность и надежность были высокими "
        "+ решения были ценными и генерящими выручку.",
    ]
)


def test_intro_v2_catalog_is_the_exact_seven_field_public_contract() -> None:
    from bot.services.intro_contract import get_intro_catalog

    assert [
        (field.field_id, field.question, field.public_label, field.sheet_header)
        for field in get_intro_catalog("intro-v2")
    ] == [
        ("name", "Как тебя зовут?", "👤 Имя", "👤 Имя"),
        (
            "location",
            "Где ты проводишь большую часть года?",
            "📍 Основная локация",
            "📍 Основная локация",
        ),
        (
            "referral",
            "От кого ты узнал о чате? Укажи @username или ссылку t.me/username.",
            "🔗 От кого узнал о чате",
            "🔗 От кого узнал о чате",
        ),
        (
            "experience",
            "Какой у тебя опыт с вайб-кодингом?",
            "💡 Опыт с вайб-кодингом",
            "💡 Опыт с вайб-кодингом",
        ),
        (
            "projects",
            "Какие проекты и автоматизации ты реализовал с помощью вайб-кодинга?",
            "🚀 Проекты и автоматизации",
            "🚀 Проекты и автоматизации",
        ),
        (
            "hardest",
            "Что самое сложное ты делал в вайб-кодинге за последнее время?",
            "🏋️ Самое сложное",
            "🏋️ Самое сложное",
        ),
        (
            "goals",
            "Что хочешь попробовать, изучить или лучше понять сейчас?",
            "🎯 Цели",
            "🎯 Цели",
        ),
    ]


def test_catalog_lookup_keeps_legacy_v1_and_rejects_unknown_versions() -> None:
    from bot.services.intro_contract import IntroContractError
    from bot.services.intro_contract import get_intro_catalog

    assert get_intro_catalog("legacy-v1")
    with pytest.raises(IntroContractError):
        get_intro_catalog("intro-v3")


def test_render_intro_html_matches_authorized_predko_body_byte_for_byte() -> None:
    from bot.services.intro_contract import render_intro_html

    assert (
        render_intro_html(list(reversed(PREDKO_ANSWERS)), catalog_version="intro-v2") == PREDKO_BODY
    )


def test_render_intro_html_keeps_legacy_v1_template_available() -> None:
    from bot.services.intro_contract import render_intro_html

    assert render_intro_html(PREDKO_ANSWERS, catalog_version="legacy-v1") == LEGACY_BODY


def test_render_intro_html_rejects_unknown_catalog_version() -> None:
    from bot.services.intro_contract import IntroContractError
    from bot.services.intro_contract import render_intro_html

    with pytest.raises(IntroContractError):
        render_intro_html(PREDKO_ANSWERS, catalog_version="intro-v3")


def test_render_intro_html_escapes_every_answer_before_joining_with_lf() -> None:
    from bot.services.intro_contract import render_intro_html

    body = render_intro_html(
        [
            ("name", "Сергей <Admin>"),
            ("location", "UK & EU"),
            ("referral", "@oxanagesina"),
            ("experience", '"<script>"'),
            ("projects", "A 'quote'"),
            ("hardest", "5 > 3"),
            ("goals", "R&D"),
        ],
        catalog_version="intro-v2",
    )

    assert body == "\n".join(
        [
            "👤 Имя: Сергей &lt;Admin&gt;",
            "📍 Основная локация: UK &amp; EU",
            "🔗 От кого узнал о чате: @oxanagesina",
            "💡 Опыт с вайб-кодингом: &quot;&lt;script&gt;&quot;",
            "🚀 Проекты и автоматизации: A &#x27;quote&#x27;",
            "🏋️ Самое сложное: 5 &gt; 3",
            "🎯 Цели: R&amp;D",
        ]
    )


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (PREDKO_BODY, "mrpfJjmr1jpKAq5Aueol1w"),
        (
            PREDKO_BODY.replace("Сергей", "Сергей2", 1),
            "41nL6YuGI3hN0B2PHv0Oxg",
        ),
    ],
)
def test_intro_digest_has_deterministic_callback_vectors(body: str, expected: str) -> None:
    from bot.services.intro_contract import intro_digest

    assert intro_digest(body) == expected


@pytest.mark.parametrize(
    "answers",
    [
        PREDKO_ANSWERS[:-1],
        [*PREDKO_ANSWERS, ("name", "Другой Сергей")],
        [*PREDKO_ANSWERS, ("unknown", "Неизвестное поле")],
    ],
    ids=["missing-field", "duplicate-field", "unknown-field"],
)
def test_render_intro_html_fails_fast_on_inexact_field_coverage(
    answers: list[tuple[str, str]],
) -> None:
    from bot.services.intro_contract import IntroContractError
    from bot.services.intro_contract import render_intro_html

    with pytest.raises(IntroContractError):
        render_intro_html(answers, catalog_version="intro-v2")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("@Nick_Name", "@nick_name"),
        ("Nick_Name", "@nick_name"),
        ("t.me/Nick_Name", "@nick_name"),
        ("https://t.me/Nick_Name", "@nick_name"),
    ],
)
def test_referral_concrete_forms_normalize_to_lowercase_username(
    raw: str,
    expected: str,
) -> None:
    from bot.services.referral_username import normalize_referral_username

    assert normalize_referral_username(raw) == expected


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (None, False),
        ("", False),
        ("От участника чата", False),
        ("https://example.com/member", False),
        ("https://[invalid", False),
        ("@Nick_Name", True),
        ("Nick_Name", True),
        ("t.me/Nick_Name", True),
        ("https://t.me/Nick_Name", True),
    ],
)
def test_stored_referral_predicate_distinguishes_concrete_from_generic(
    stored: str | None,
    expected: bool,
) -> None:
    from bot.services.referral_username import is_concrete_referral

    assert is_concrete_referral(stored) is expected


@pytest.mark.parametrize("raw", ["", "От участника чата", "not a @username"])
def test_new_malformed_referral_is_rejected_at_the_service_boundary(raw: str) -> None:
    from bot.services.referral_username import InvalidReferralUsername
    from bot.services.referral_username import normalize_referral_username

    with pytest.raises(InvalidReferralUsername):
        normalize_referral_username(raw)
