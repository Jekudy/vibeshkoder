#!/usr/bin/env python3
"""Scan 📟 folder (all unread private chats) for unanswered messages."""
import asyncio
import os
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import User as TgUser

SESSION = os.environ["TELEGRAM_SESSION"]
MY_ID = int(os.environ["TELEGRAM_SELF_ID"])


async def main():
    client = TelegramClient(
        StringSession(SESSION),
        int(os.environ["TELEGRAM_API_ID"]),
        os.environ["TELEGRAM_API_HASH"],
    )
    await client.connect()

    print("Сканирую 📟 (все непрочитанные приватные чаты)...\n")

    unread_chats = []
    async for dialog in client.iter_dialogs():
        # 📟 filter: contacts + non_contacts + bots, exclude_read, no groups/channels
        if not isinstance(dialog.entity, TgUser):
            continue
        if dialog.unread_count == 0:
            continue

        entity = dialog.entity
        name = entity.first_name or "?"
        if entity.last_name:
            name += f" {entity.last_name}"
        username = entity.username or ""

        messages = await client.get_messages(entity, limit=7)

        last_from_me = False
        for m in messages:
            if m.sender_id == MY_ID:
                last_from_me = True
                break

        context = []
        for m in messages[:5]:
            sender = "Я" if m.sender_id == MY_ID else (entity.first_name or "?")
            text = (m.text or "(медиа)")[:100]
            context.append(f"  [{sender}]: {text}")

        unread_chats.append({
            "name": name,
            "username": username,
            "unread": dialog.unread_count,
            "last_from_me": last_from_me,
            "context": context,
        })

    # Sort: unanswered first
    unread_chats.sort(key=lambda c: (c["last_from_me"], -c["unread"]))

    print(f"Непрочитанных приватных чатов: {len(unread_chats)}\n")
    for chat in unread_chats:
        status = "✅ последнее от меня" if chat["last_from_me"] else "❗ ждут ответа"
        at = f" (@{chat['username']})" if chat["username"] else ""
        print(f"--- {chat['name']}{at} — {chat['unread']} непрочит. — {status} ---")
        for c in chat["context"]:
            print(c)
        print()

    await client.disconnect()


asyncio.run(main())
