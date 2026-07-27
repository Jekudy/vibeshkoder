from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.texts import CONFIRM_BUTTON, REDO_BUTTON, READY_BUTTON_TEXT, VOUCH_BUTTON_TEXT


class VouchCallback(CallbackData, prefix="vouch"):
    application_id: int


class ReadyCallback(CallbackData, prefix="ready"):
    application_id: int


class ConfirmCallback(CallbackData, prefix="confirm"):
    action: str  # "yes" or "redo"
    application_id: int
    digest: str


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


def confirm_keyboard(application_id: int, digest: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=CONFIRM_BUTTON,
                    callback_data=ConfirmCallback(
                        action="yes", application_id=application_id, digest=digest
                    ).pack(),
                ),
                InlineKeyboardButton(
                    text=REDO_BUTTON,
                    callback_data=ConfirmCallback(
                        action="redo", application_id=application_id, digest=digest
                    ).pack(),
                ),
            ]
        ]
    )
