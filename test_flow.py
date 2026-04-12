#!/usr/bin/env python3
"""Test the bot flow as @lookingformeow."""
import asyncio
import os

from telethon import TelegramClient
from telethon.sessions import StringSession

SESSION = os.environ["TELEGRAM_SESSION"]
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
BOT = os.environ.get("GATEKEEPER_BOT_USERNAME", "vibeshkoder_bot")


async def wait_for_response(client, after_id: int, timeout: int = 10):
    """Wait for a new bot message with id > after_id."""
    for _ in range(timeout * 2):
        msgs = await client.get_messages(BOT, limit=5)
        for m in msgs:
            if not m.out and m.id > after_id:
                return m
        await asyncio.sleep(0.5)
    return None


async def main():
    client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
    await client.connect()
    me = await client.get_me()
    print(f"Connected as {me.first_name} (@{me.username})")

    # Track last seen bot message
    msgs = await client.get_messages(BOT, limit=1)
    last_id = msgs[0].id if msgs else 0

    print("\n=== Step 1: /start ===")
    await client.send_message(BOT, "/start")
    resp = await wait_for_response(client, last_id)
    if resp:
        print(f"  BOT: {resp.text[:150] if resp.text else '(no text)'}")
        last_id = resp.id
    else:
        print("  (no response)")
        return

    answers = [
        "Тестовый Человек",
        "Москва",
        "Из тестов",
        "Вайб-кодинг — это моя жизнь",
        "Сделал бота-привратника",
        "Написал 3800 строк кода за вечер",
        "Хочу научиться тестировать ботов автоматически",
    ]

    print("\n=== Step 2: Questionnaire ===")
    for i, answer in enumerate(answers):
        await client.send_message(BOT, answer)
        resp = await wait_for_response(client, last_id)
        if resp:
            preview = resp.text[:100] if resp.text else "(no text)"
            print(f"  Q{i+1}: {answer[:30]}... → BOT: {preview}")
            has_buttons = bool(resp.reply_markup)
            if has_buttons:
                print("       [has buttons]")
            last_id = resp.id
        else:
            print(f"  Q{i+1}: {answer[:30]}... → (no response)")

    # After last answer, we should get confirm prompt with buttons
    if resp and resp.reply_markup:
        print("\n=== Step 3: Confirm ===")
        await resp.click(0)  # "Подтвердить"
        confirm_resp = await wait_for_response(client, last_id)
        if confirm_resp:
            print(f"  BOT: {confirm_resp.text[:200] if confirm_resp.text else '(no text)'}")
        else:
            print("  (no response after confirm)")

    await client.disconnect()
    print("\nDone!")


asyncio.run(main())
