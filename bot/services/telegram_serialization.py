"""Strict JSON serialization for aiogram objects received from Telegram."""

from __future__ import annotations

import json
from typing import Any

from aiogram.types import TelegramObject
from aiogram.utils.serialization import deserialize_telegram_object


def serialize_telegram_object(value: TelegramObject) -> dict[str, Any]:
    """Return aiogram's JSON-compatible representation of a Telegram object.

    Direct ``model_dump(mode="json")`` cannot serialize nested aiogram ``Default``
    sentinels.  Aiogram's public serializer resolves those sentinels against empty bot
    defaults and omits them, preserving the received payload without inventing values.
    """
    serialized = deserialize_telegram_object(
        value,
        default=None,
        include_api_method_name=False,
    )
    if serialized.files:
        raise TypeError("incoming Telegram object unexpectedly contains upload files")
    payload = serialized.data
    if not isinstance(payload, dict):
        raise TypeError("aiogram TelegramObject serialization did not produce an object")
    json.dumps(payload, allow_nan=False)
    return payload
