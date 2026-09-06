from aiogram.filters.callback_data import CallbackData
from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup

from bot.texts import CONFIRM_BUTTON, REDO_BUTTON, READY_BUTTON_TEXT, VOUCH_BUTTON_TEXT


class VouchCallback(CallbackData, prefix="vouch"):
    application_id: int


class ReadyCallback(CallbackData, prefix="ready"):
    application_id: int


class ConfirmCallback(CallbackData, prefix="confirm"):
    action: str  # "yes" or "redo"
    application_id: int
    digest: str


class IntroRefreshOfferCallback(CallbackData, prefix="ir_offer"):
    action: str
    wave: str


class IntroRefreshSelectCallback(CallbackData, prefix="ir_select"):
    action: str
    field_index: int
    mask: int
    source_application_id: int
    context: str


class IntroRefreshEditCallback(CallbackData, prefix="ir_edit"):
    action: str
    application_id: int
    field_id: str


def vouch_keyboard(application_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=VOUCH_BUTTON_TEXT,
                    callback_data=VouchCallback(application_id=application_id).pack(),
                )
            ]
        ]
    )


def ready_keyboard(application_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=READY_BUTTON_TEXT,
                    callback_data=ReadyCallback(application_id=application_id).pack(),
                )
            ]
        ]
    )


def confirm_keyboard(
    application_id: int,
    digest: str,
    *,
    redo_text: str = REDO_BUTTON,
    cancel_text: str | None = None,
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=CONFIRM_BUTTON,
                callback_data=ConfirmCallback(
                    action="yes", application_id=application_id, digest=digest
                ).pack(),
            ),
            InlineKeyboardButton(
                text=redo_text,
                callback_data=ConfirmCallback(
                    action="redo", application_id=application_id, digest=digest
                ).pack(),
            ),
        ]
    ]
    if cancel_text is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    text=cancel_text,
                    callback_data=ConfirmCallback(
                        action="cancel", application_id=application_id, digest=digest
                    ).pack(),
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def intro_refresh_offer_keyboard(wave: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да",
                    callback_data=IntroRefreshOfferCallback(action="yes", wave=wave).pack(),
                ),
                InlineKeyboardButton(
                    text="Нет",
                    callback_data=IntroRefreshOfferCallback(action="no", wave=wave).pack(),
                ),
            ]
        ]
    )


def intro_refresh_selection_keyboard(
    fields: list[tuple[int, str]],
    mask: int,
    source_application_id: int,
    context: str,
    *,
    close_text: str = "Отмена",
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'☑' if mask & (1 << index) else '☐'} {label}",
                callback_data=IntroRefreshSelectCallback(
                    action="toggle",
                    field_index=index,
                    mask=mask,
                    source_application_id=source_application_id,
                    context=context,
                ).pack(),
            )
        ]
        for index, label in fields
    ]
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text=f"Продолжить · {mask.bit_count()}",
                    callback_data=IntroRefreshSelectCallback(
                        action="continue",
                        field_index=0,
                        mask=mask,
                        source_application_id=source_application_id,
                        context=context,
                    ).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text=close_text,
                    callback_data=IntroRefreshSelectCallback(
                        action="close",
                        field_index=0,
                        mask=mask,
                        source_application_id=source_application_id,
                        context=context,
                    ).pack(),
                )
            ],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def intro_refresh_legacy_keyboard(
    source_application_id: int,
    context: str,
    *,
    close_text: str = "Отмена",
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Обновить всё интро",
                    callback_data=IntroRefreshSelectCallback(
                        action="legacy",
                        field_index=0,
                        mask=0,
                        source_application_id=source_application_id,
                        context=context,
                    ).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text=close_text,
                    callback_data=IntroRefreshSelectCallback(
                        action="close",
                        field_index=0,
                        mask=0,
                        source_application_id=source_application_id,
                        context=context,
                    ).pack(),
                )
            ],
        ]
    )


def intro_refresh_edit_keyboard(
    application_id: int, field_id: str, old_text: str | None
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if old_text is not None and len(old_text) <= 256:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Скопировать текст",
                    copy_text=CopyTextButton(text=old_text),
                )
            ]
        )
    if old_text is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Оставить без изменений",
                    callback_data=IntroRefreshEditCallback(
                        action="skip", application_id=application_id, field_id=field_id
                    ).pack(),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="Отменить обновление",
                callback_data=IntroRefreshEditCallback(
                    action="cancel", application_id=application_id, field_id=field_id
                ).pack(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def intro_refresh_cancel_keyboard(application_id: int, field_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, отменить",
                    callback_data=IntroRefreshEditCallback(
                        action="confirm_cancel",
                        application_id=application_id,
                        field_id=field_id,
                    ).pack(),
                ),
                InlineKeyboardButton(
                    text="Продолжить",
                    callback_data=IntroRefreshEditCallback(
                        action="keep_editing",
                        application_id=application_id,
                        field_id=field_id,
                    ).pack(),
                ),
            ]
        ]
    )
