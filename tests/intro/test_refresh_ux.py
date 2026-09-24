from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tests.conftest import import_module


def _values(referral: str) -> dict[str, str]:
    return {
        "name": "Сергей",
        "location": "Лондон",
        "referral": referral,
        "experience": "Делаю продукты с LLM",
        "projects": "Бот",
        "hardest": "Поиск",
        "goals": "Агенты",
    }


def test_referral_is_selectable_only_without_a_concrete_telegram_username(app_env) -> None:
    handler = import_module("bot.handlers.intro_refresh")

    concrete = dict(handler._selectable_fields(_values("@real_person")))
    generic = dict(handler._selectable_fields(_values("Узнал на конференции")))

    assert 2 not in concrete
    assert generic[2] == "🔗 От кого узнал о чате"


def test_copy_button_exists_only_within_telegram_256_character_limit(app_env) -> None:
    keyboards = import_module("bot.keyboards.inline")

    short = keyboards.intro_refresh_edit_keyboard(10, "experience", "x" * 256)
    long = keyboards.intro_refresh_edit_keyboard(10, "experience", "x" * 257)

    assert short.inline_keyboard[0][0].copy_text.text == "x" * 256
    assert all(button.copy_text is None for row in long.inline_keyboard for button in row)


def test_all_refresh_callback_payloads_stay_below_telegram_limit(app_env) -> None:
    keyboards = import_module("bot.keyboards.inline")
    markups = [
        keyboards.intro_refresh_offer_keyboard("20260901"),
        keyboards.intro_refresh_selection_keyboard(
            [(index, f"Поле {index}") for index in range(7)],
            127,
            2_147_483_647,
            "a2147483647.0123456789ab",
        ),
        keyboards.intro_refresh_edit_keyboard(2_147_483_647, "experience", "old"),
        keyboards.intro_refresh_cancel_keyboard(2_147_483_647, "experience"),
    ]

    payloads = [
        button.callback_data
        for markup in markups
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data is not None
    ]
    assert payloads
    assert max(len(payload.encode()) for payload in payloads) <= 64


def test_continue_without_selection_shows_alert(app_env, monkeypatch) -> None:
    handler = import_module("bot.handlers.intro_refresh")
    intro = SimpleNamespace(application_id=10, intro_text="intro")
    monkeypatch.setattr(
        handler, "_source_values", AsyncMock(return_value=(intro, _values("@nick")))
    )
    monkeypatch.setattr(handler.ApplicationRepo, "get_active_refresh", AsyncMock(return_value=None))
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=111),
        message=SimpleNamespace(edit_text=AsyncMock(), edit_reply_markup=AsyncMock()),
        answer=AsyncMock(),
    )
    data = SimpleNamespace(
        action="continue",
        field_index=0,
        mask=0,
        source_application_id=10,
        context="manual",
    )

    asyncio.run(handler.handle_refresh_selection(callback, data, AsyncMock(), AsyncMock()))

    callback.answer.assert_awaited_once_with("Выбери хотя бы один блок.", show_alert=True)


def test_stale_skip_button_cannot_skip_the_next_field(app_env, monkeypatch) -> None:
    handler = import_module("bot.handlers.intro_refresh")
    monkeypatch.setattr(handler, "next_field_id", AsyncMock(return_value="goals"))
    keep = AsyncMock()
    monkeypatch.setattr(handler, "keep_base_answer", keep)
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=111),
        message=SimpleNamespace(edit_text=AsyncMock(), edit_reply_markup=AsyncMock()),
        answer=AsyncMock(),
    )
    data = SimpleNamespace(action="skip", application_id=222, field_id="experience")

    asyncio.run(handler.handle_refresh_edit(callback, data, AsyncMock(), AsyncMock()))

    callback.answer.assert_awaited_once_with("Этот блок уже обработан.", show_alert=True)
    keep.assert_not_awaited()


def test_legacy_copy_uses_dynamic_catalog_length_and_folded_intro(app_env, monkeypatch) -> None:
    handler = import_module("bot.handlers.intro_refresh")
    intro = SimpleNamespace(application_id=None, intro_text="старое интро")
    monkeypatch.setattr(handler, "_source_values", AsyncMock(return_value=(intro, None)))
    message = SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock())

    asyncio.run(handler.show_refresh_selection(message, AsyncMock(), 111, edit=False))

    text = message.answer.await_args.args[0]
    assert f"{len(handler.CATALOG)} вопросов" in text
    assert "<blockquote expandable>старое интро</blockquote>" in text


def test_refresh_preview_keyboard_has_back_and_cancel_actions(app_env) -> None:
    keyboards = import_module("bot.keyboards.inline")

    markup = keyboards.confirm_keyboard(
        42,
        "0123456789ab",
        redo_text="Изменить выбор блоков",
        cancel_text="Отменить обновление",
    )

    actions = {
        keyboards.ConfirmCallback.unpack(button.callback_data).action
        for row in markup.inline_keyboard
        for button in row
    }
    assert actions == {"yes", "redo", "cancel"}


def test_delivered_claimed_offer_still_accepts_callback(app_env) -> None:
    handler = import_module("bot.handlers.intro_refresh")
    tracking = SimpleNamespace(
        phase="claimed",
        reminders_sent=0,
        last_reminder_at=None,
        completed=False,
    )
    result = SimpleNamespace(scalar_one_or_none=lambda: tracking)
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=111),
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )
    data = SimpleNamespace(action="no", wave="20260901")

    asyncio.run(handler.handle_refresh_offer(callback, data, session))

    assert tracking.phase == "declined"
    assert tracking.reminders_sent == 1
    assert tracking.last_reminder_at is not None
    assert tracking.completed is True
    callback.answer.assert_awaited_once_with()


def test_stale_selector_cannot_start_from_an_old_intro_version(app_env, monkeypatch) -> None:
    handler = import_module("bot.handlers.intro_refresh")
    intro = SimpleNamespace(application_id=11, intro_text="intro")
    monkeypatch.setattr(
        handler, "_source_values", AsyncMock(return_value=(intro, _values("@nick")))
    )
    start = AsyncMock()
    monkeypatch.setattr(handler, "start_or_resume_refresh", start)
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=111),
        message=SimpleNamespace(edit_text=AsyncMock(), edit_reply_markup=AsyncMock()),
        answer=AsyncMock(),
    )
    data = SimpleNamespace(
        action="continue",
        field_index=0,
        mask=1,
        source_application_id=10,
        context="manual",
    )

    asyncio.run(handler.handle_refresh_selection(callback, data, AsyncMock(), AsyncMock()))

    callback.answer.assert_awaited_once_with(
        "Этот выбор устарел. Запусти /refresh ещё раз.", show_alert=True
    )
    start.assert_not_awaited()
