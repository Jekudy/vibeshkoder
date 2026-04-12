#!/usr/bin/env python3
"""Transcribe a Telegram voice message using Premium account's built-in transcription."""
import asyncio
import os
import sys

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.messages import TranscribeAudioRequest

SESSION = os.environ["TELEGRAM_SESSION"]
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]


async def transcribe(bot_id: int, msg_id: int) -> str:
    """Transcribe a voice message by bot_id and message_id."""
    client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
    await client.connect()

    # Find entity via dialogs
    entity = None
    async for dialog in client.iter_dialogs():
        if dialog.entity.id == bot_id:
            entity = dialog.entity
            break

    if not entity:
        await client.disconnect()
        return "ERROR: entity not found"

    # Poll transcription (up to 30 seconds)
    text = ""
    for _ in range(10):
        try:
            result = await client(TranscribeAudioRequest(peer=entity, msg_id=msg_id))
            if not result.pending and result.text:
                text = result.text
                break
            if result.text:
                text = result.text
        except Exception as e:
            text = f"ERROR: {e}"
            break
        await asyncio.sleep(3)

    await client.disconnect()
    return text


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: transcribe_voice.py <bot_id> <msg_id>")
        sys.exit(1)
    result = asyncio.run(transcribe(int(sys.argv[1]), int(sys.argv[2])))
    print(result)
