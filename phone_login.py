#!/usr/bin/env python3
"""Phone-based login: sends code, waits for code in /tmp/tg_code.txt, outputs session string."""
import asyncio
import os
import sys

from telethon import TelegramClient, errors
from telethon.sessions import StringSession

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
PHONE = os.environ["TELEGRAM_PHONE"]

async def main():
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()

    print("SENDING_CODE", flush=True)
    try:
        result = await client.send_code_request(PHONE)
        print(f"CODE_SENT phone_hash={result.phone_code_hash}", flush=True)
    except errors.FloodWaitError as e:
        print(f"FLOOD_WAIT={e.seconds}", flush=True)
        await client.disconnect()
        sys.exit(1)
    except errors.PhoneNumberInvalidError:
        print("PHONE_INVALID", flush=True)
        await client.disconnect()
        sys.exit(1)
    except Exception as e:
        print(f"ERROR={e}", flush=True)
        await client.disconnect()
        sys.exit(1)

    print("WAITING_FOR_CODE", flush=True)
    code = None
    for _ in range(300):
        if os.path.exists("/tmp/tg_code.txt"):
            code = open("/tmp/tg_code.txt").read().strip()
            os.remove("/tmp/tg_code.txt")
            break
        await asyncio.sleep(1)

    if not code:
        print("CODE_TIMEOUT", flush=True)
        await client.disconnect()
        sys.exit(1)

    print(f"GOT_CODE={code}", flush=True)

    try:
        await client.sign_in(PHONE, code)
    except errors.SessionPasswordNeededError:
        print("2FA_REQUIRED", flush=True)
        pw = None
        for _ in range(300):
            if os.path.exists("/tmp/tg_2fa.txt"):
                pw = open("/tmp/tg_2fa.txt").read().strip()
                os.remove("/tmp/tg_2fa.txt")
                break
            await asyncio.sleep(1)
        if not pw:
            print("2FA_TIMEOUT", flush=True)
            await client.disconnect()
            sys.exit(1)
        await client.sign_in(password=pw)
    except errors.PhoneCodeInvalidError:
        print("CODE_INVALID", flush=True)
        await client.disconnect()
        sys.exit(1)
    except Exception as e:
        print(f"SIGN_IN_ERROR={e}", flush=True)
        await client.disconnect()
        sys.exit(1)

    me = await client.get_me()
    print(f"LOGGED_IN={me.first_name} (ID: {me.id})", flush=True)

    ss = StringSession.save(client.session)
    print(f"SESSION_STRING={ss}", flush=True)
    with open("/tmp/tg_session2.txt", "w") as f:
        f.write(ss)
    print("DONE", flush=True)
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
