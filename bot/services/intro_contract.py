from __future__ import annotations

from base64 import urlsafe_b64encode
from dataclasses import dataclass
from hashlib import sha256
from html import escape
from typing import Iterable


class IntroContractError(ValueError):
    """Raised when answers do not match a frozen intro catalog."""


@dataclass(frozen=True)
class IntroField:
    field_id: str
    question: str
    public_label: str
    sheet_header: str


_INTRO_V2 = (
    IntroField("name", "Как тебя зовут?", "👤 Имя", "👤 Имя"),
    IntroField(
        "location",
        "Где ты проводишь большую часть года?",
        "📍 Основная локация",
        "📍 Основная локация",
    ),
    IntroField(
        "referral",
        "От кого ты узнал о чате? Укажи @username или ссылку t.me/username.",
        "🔗 От кого узнал о чате",
        "🔗 От кого узнал о чате",
    ),
    IntroField(
        "experience",
        "Какой у тебя опыт с вайб-кодингом?",
        "💡 Опыт с вайб-кодингом",
        "💡 Опыт с вайб-кодингом",
    ),
    IntroField(
        "projects",
        "Какие проекты и автоматизации ты реализовал с помощью вайб-кодинга?",
        "🚀 Проекты и автоматизации",
        "🚀 Проекты и автоматизации",
    ),
    IntroField(
        "hardest",
        "Что самое сложное ты делал в вайб-кодинге за последнее время?",
        "🏋️ Самое сложное",
        "🏋️ Самое сложное",
    ),
    IntroField(
        "goals",
        "Что хочешь попробовать, изучить или лучше понять сейчас?",
        "🎯 Цели",
        "🎯 Цели",
    ),
)

_LEGACY_V1 = (
    IntroField("name", "Как зовут тебя?", "👤", "Имя"),
    IntroField("location", "Где ты проводишь большую часть года?", "📍", "Локация"),
    IntroField("referral", "От кого узнал — укажи Telegram ID", "🔗 Откуда узнал", "Откуда узнал"),
    IntroField(
        "experience",
        "Какие отношения у тебя с вайб-кодингом (какой опыт)?",
        "💡 Опыт",
        "Опыт",
    ),
    IntroField(
        "projects",
        "Расскажи коротко про свои проекты, автоматизации и приколюшки, которые ты реализовал с помощью вайб-кодинга?",
        "🚀 Проекты",
        "Проекты",
    ),
    IntroField(
        "hardest",
        "Что самое сложное делал в вайб-кодинге за последнее время?",
        "🏋️ Самое сложное",
        "Самое сложное",
    ),
    IntroField(
        "goals",
        "Что хочешь попробовать, чему научиться и что тебя озадачивает в этом ремесле сейчас?",
        "🎯 Цели",
        "Цели",
    ),
)

_CATALOGS = {"legacy-v1": _LEGACY_V1, "intro-v2": _INTRO_V2}
# 4096 minus the admission header with 64-char first/voucher names escaped as ``&quot;``
# (6 chars each) and a 32-char username: 4096 - 836.
_FROZEN_INTRO_BODY_LIMIT = 3_260


def get_intro_catalog(catalog_version: str) -> tuple[IntroField, ...]:
    """Return the frozen field catalog for an application version."""
    try:
        return _CATALOGS[catalog_version]
    except KeyError as error:
        raise IntroContractError(f"Unknown intro catalog: {catalog_version}") from error


def normalize_intro_answer(value: str) -> str:
    """Normalize one answer for its one-line Telegram representation."""
    value = value.replace("\r\n", " ").replace("\r", " ").replace("\n", " ").strip()
    if not value:
        raise IntroContractError("Intro answers must not be blank")
    return value


def render_intro_html(answers: Iterable[tuple[str, str]], *, catalog_version: str) -> str:
    """Render one complete escaped user block in its catalog's fixed order."""
    catalog = get_intro_catalog(catalog_version)
    values: dict[str, str] = {}
    expected_ids = {field.field_id for field in catalog}

    for field_id, value in answers:
        if field_id not in expected_ids or field_id in values:
            raise IntroContractError("Answers must contain every catalog field exactly once")
        values[field_id] = normalize_intro_answer(value)

    if set(values) != expected_ids:
        raise IntroContractError("Answers must contain every catalog field exactly once")

    if catalog_version == "intro-v2":
        rendered = "\n".join(
            f"{field.public_label}: {escape(values[field.field_id], quote=True)}"
            for field in catalog
        )
    else:
        rendered = "\n".join(
            f"{field.public_label}{': ' if field.field_id not in {'name', 'location'} else ' '}"
            f"{escape(values[field.field_id], quote=True)}"
            for field in catalog
        )
    if len(rendered) > _FROZEN_INTRO_BODY_LIMIT:
        raise IntroContractError("Intro exceeds the Telegram message limit")
    return rendered


def intro_digest(snapshot: str) -> str:
    """Return the callback-safe digest of a rendered UTF-8 snapshot."""
    return urlsafe_b64encode(sha256(snapshot.encode("utf-8")).digest()[:16]).rstrip(b"=").decode()
