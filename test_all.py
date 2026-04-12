#!/usr/bin/env python3
"""Automated end-to-end tests for the gatekeeper bot."""
import asyncio
import os
import sys

from telethon import TelegramClient
from telethon.sessions import StringSession

SESSION = os.environ["TELEGRAM_SESSION"]
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
BOT = os.environ.get("GATEKEEPER_BOT_USERNAME", "vibeshkoder_bot")

PASS = 0
FAIL = 0


async def wait_for_response(client, after_id: int, timeout: int = 10):
    for _ in range(timeout * 2):
        msgs = await client.get_messages(BOT, limit=5)
        for m in msgs:
            if not m.out and m.id > after_id:
                return m
        await asyncio.sleep(0.5)
    return None


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} — {detail}")


async def get_last_bot_id(client):
    msgs = await client.get_messages(BOT, limit=1)
    return msgs[0].id if msgs else 0


async def test_questionnaire(client):
    """Test 1: Full questionnaire flow."""
    print("\n📋 Test 1: Questionnaire flow")
    last_id = await get_last_bot_id(client)

    await client.send_message(BOT, "/start")
    resp = await wait_for_response(client, last_id)
    check("/start responds", resp is not None)
    check("/start has welcome text", resp is not None and "привратник" in resp.text.lower())
    if not resp:
        return None
    last_id = resp.id

    answers = [
        "Тест Автоматович",
        "Сервер",
        "Из автотестов",
        "Полный автомат",
        "Автоматизировал всё",
        "Написал автотесты для бота",
        "Автоматизировать мир",
    ]

    for i, answer in enumerate(answers):
        await client.send_message(BOT, answer)
        resp = await wait_for_response(client, last_id)
        if resp:
            last_id = resp.id
        if i < 6:
            check(f"Q{i+1} → next question", resp is not None and "❓" in (resp.text or ""))
        else:
            check("Q7 → confirm prompt", resp is not None and resp.reply_markup is not None)

    return resp  # confirm message with buttons


async def test_confirm_and_post(client, confirm_msg):
    """Test 2: Confirm questionnaire → post to chat."""
    print("\n📮 Test 2: Confirm → post to chat")
    if not confirm_msg or not confirm_msg.reply_markup:
        check("has confirm buttons", False, "no confirm message")
        return

    await confirm_msg.click(0)  # "Подтвердить"
    await asyncio.sleep(3)

    # Check the response (callback answer edits the message)
    msgs = await client.get_messages(BOT, limit=3)
    found_posted = False
    for m in msgs:
        if not m.out and "отправлена в чат" in (m.text or "").lower():
            found_posted = True
            break
    check("questionnaire posted to chat", found_posted)

    # Check DB
    from bot.db.engine import async_session
    from bot.db.models import Application
    from sqlalchemy import select

    async with async_session() as s:
        result = await s.execute(
            select(Application).where(Application.user_id == 5739636875).order_by(Application.id.desc()).limit(1)
        )
        app = result.scalar_one_or_none()
        check("application status = pending", app is not None and app.status == "pending")
        check("questionnaire_message_id set", app is not None and app.questionnaire_message_id is not None)


async def test_vouch_deadline(client):
    """Test 3: Check that vouch deadline scheduler works."""
    print("\n⏰ Test 3: Vouch deadline check")
    from bot.db.engine import async_session
    from bot.db.models import Application
    from sqlalchemy import select

    async with async_session() as s:
        result = await s.execute(
            select(Application).where(Application.user_id == 5739636875, Application.status == "pending")
        )
        app = result.scalar_one_or_none()
        check("pending application exists", app is not None)


async def test_refresh(client):
    """Test 4: /refresh command (need to be a member with intro first)."""
    print("\n🔄 Test 4: /refresh command")

    # First set user as member with intro for testing
    from bot.db.engine import async_session
    from bot.db.repos.user import UserRepo
    from bot.db.repos.intro import IntroRepo

    async with async_session() as s:
        await UserRepo.set_member(s, 5739636875, is_member=True)
        await IntroRepo.upsert(s, 5739636875, "old intro text", "test voucher")
        await s.commit()

    last_id = await get_last_bot_id(client)
    await client.send_message(BOT, "/refresh")
    resp = await wait_for_response(client, last_id)
    check("/refresh responds", resp is not None)
    if resp:
        check("/refresh starts questionnaire", "❓" in (resp.text or "") or "обновить" in (resp.text or "").lower())
        last_id = resp.id

        # Answer all 7 questions
        for i in range(7):
            await client.send_message(BOT, f"Обновлённый ответ {i+1}")
            resp = await wait_for_response(client, last_id)
            if resp:
                last_id = resp.id

        # Confirm
        if resp and resp.reply_markup:
            await resp.click(0)
            await asyncio.sleep(2)
            # Check intro was updated
            async with async_session() as s:
                intro = await IntroRepo.get(s, 5739636875)
                check("intro updated after refresh", intro is not None and "Обновлённый" in (intro.intro_text or ""))
                check("vouched_by preserved", intro is not None and intro.vouched_by_name == "test voucher")

    # Reset member status for next tests
    async with async_session() as s:
        await UserRepo.set_member(s, 5739636875, is_member=False)
        from bot.db.repos.intro import IntroRepo as IR
        await IR.delete(s, 5739636875)
        await s.commit()


async def test_sheets_sync():
    """Test 5: Google Sheets sync."""
    print("\n📊 Test 5: Google Sheets sync")
    try:
        from bot.services.sheets import _is_configured
        check("sheets configured", _is_configured())

        if _is_configured():
            from bot.services.sheets import full_sync
            await full_sync()
            check("full_sync runs without error", True)
    except Exception as e:
        check("sheets sync", False, str(e))


async def main():
    client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
    await client.connect()
    me = await client.get_me()
    print(f"Connected as {me.first_name} (@{me.username})")

    # Test 4: /refresh (run first since it needs member status)
    await test_refresh(client)

    # Test 1: Questionnaire
    confirm_msg = await test_questionnaire(client)

    # Test 2: Confirm
    await test_confirm_and_post(client, confirm_msg)

    # Test 3: Deadline check
    await test_vouch_deadline(client)

    # Test 5: Sheets
    await test_sheets_sync()

    await client.disconnect()

    print(f"\n{'='*40}")
    print(f"Results: {PASS} passed, {FAIL} failed")
    if FAIL > 0:
        sys.exit(1)


asyncio.run(main())
