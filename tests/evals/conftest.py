from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
import pytest_asyncio

SEED_ID = "golden_recall_v1"
SEED_VERSION = 1
SEED_CHAT_ID = -1001234567890
SEED_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "golden_recall" / "seed_v1"
CHAT_HISTORY_PATH = SEED_DIR / "chat_history.jsonl"

DEFAULT_LOCAL_POSTGRES_URL = (
    "postgresql+asyncpg://shkoder_dev:shkoder_dev@127.0.0.1:5433/shkoder_dev"
)


@dataclass(frozen=True)
class Seed:
    seed_id: str
    version: int
    seed_hash: str
    chat_id: int
    expected_id_map: dict[str, int]


def _clear_app_modules() -> None:
    for name in list(sys.modules):
        if name == "bot" or name.startswith("bot.") or name == "web" or name.startswith("web."):
            sys.modules.pop(name, None)


def _resolve_test_postgres_url() -> str:
    return (
        os.environ.get("TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or DEFAULT_LOCAL_POSTGRES_URL
    )


def _safe_url_repr(url_str: str) -> str:
    from sqlalchemy.engine.url import make_url

    try:
        return make_url(url_str).render_as_string(hide_password=True)
    except Exception:
        return "<unparseable URL>"


def _eval_env() -> Iterator[None]:
    previous = os.environ.copy()
    os.environ.update(
        {
            "BOT_TOKEN": "123456:test-token",
            "COMMUNITY_CHAT_ID": str(SEED_CHAT_ID),
            "ADMIN_IDS": "[149820031]",
            "REDIS_URL": "redis://redis:6379/0",
            "GOOGLE_SHEETS_CREDS_FILE": "",
            "GOOGLE_SHEET_ID": "",
            "WEB_BASE_URL": "http://localhost:8080",
            "WEB_BOT_USERNAME": "vibeshkoder_dev_bot",
            "DB_PASSWORD": "changeme",
            "WEB_PASSWORD": "test-pass",
            "WEB_SESSION_SECRET": "test-session-secret",
            "DEV_MODE": "true",
        }
    )
    if not os.environ.get("DATABASE_URL"):
        os.environ["DATABASE_URL"] = DEFAULT_LOCAL_POSTGRES_URL
    _clear_app_modules()
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(previous)
        _clear_app_modules()


@pytest.fixture(scope="class")
def eval_app_env() -> Iterator[None]:
    yield from _eval_env()


@pytest_asyncio.fixture(scope="class")
async def eval_postgres_engine(eval_app_env: None) -> AsyncIterator[Any]:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    url = _resolve_test_postgres_url()
    engine = create_async_engine(url, echo=False)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"postgres unreachable at {_safe_url_repr(url)}: {exc!s}")
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(scope="class")
async def eval_db_session(eval_postgres_engine: Any) -> AsyncIterator[Any]:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    async with eval_postgres_engine.connect() as conn:
        outer = await conn.begin()
        Session = async_sessionmaker(bind=conn, class_=AsyncSession, expire_on_commit=False)
        async with Session() as session:
            try:
                yield session
            finally:
                if outer.is_active:
                    await outer.rollback()


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            row = json.loads(stripped)
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_no}: expected JSON object")
            rows.append(row)
    return rows


def _canonical_jsonl_bytes(rows: list[dict[str, Any]]) -> bytes:
    body = "\n".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for row in rows
    )
    return f"{body}\n".encode("utf-8")


def _message_from_seed(row: dict[str, Any]) -> SimpleNamespace:
    message_id = int(str(row["seed_local_id"]).removeprefix("msg_"))
    user_id = int(row["user_id_local"])
    text = str(row["text"])
    when = datetime.fromisoformat(str(row["ts"]))
    raw_json = {
        "message_id": message_id,
        "chat": {"id": SEED_CHAT_ID, "type": "supergroup"},
        "from": {"id": user_id},
        "date": row["ts"],
        "text": text,
    }
    return SimpleNamespace(
        message_id=message_id,
        chat=SimpleNamespace(id=SEED_CHAT_ID, type="supergroup"),
        from_user=SimpleNamespace(
            id=user_id,
            username=f"seed_user_{user_id}",
            first_name=f"Seed {user_id}",
            last_name=None,
        ),
        text=text,
        caption=None,
        date=when,
        model_dump=Mock(return_value=raw_json),
        reply_to_message=None,
        message_thread_id=None,
        photo=None,
        video=None,
        voice=None,
        audio=None,
        document=None,
        sticker=None,
        animation=None,
        video_note=None,
        location=None,
        contact=None,
        poll=None,
        dice=None,
        forward_origin=None,
        new_chat_members=None,
        left_chat_member=None,
        pinned_message=None,
        entities=None,
        caption_entities=None,
    )


async def _persist_seed_message(session: Any, row: dict[str, Any]) -> int:
    from sqlalchemy import select

    models = importlib.import_module("bot.db.models")
    message_version_repo = importlib.import_module("bot.db.repos.message_version")
    chat_messages_handler = importlib.import_module("bot.handlers.chat_messages")
    content_hash_service = importlib.import_module("bot.services.content_hash")
    ChatMessage = models.ChatMessage
    MessageVersion = models.MessageVersion
    MessageVersionRepo = message_version_repo.MessageVersionRepo
    save_chat_message = chat_messages_handler.save_chat_message
    compute_content_hash = content_hash_service.compute_content_hash

    message = _message_from_seed(row)
    await save_chat_message(message, session)

    chat_message = await session.scalar(
        select(ChatMessage).where(
            ChatMessage.chat_id == SEED_CHAT_ID,
            ChatMessage.message_id == message.message_id,
        )
    )
    if chat_message is None:
        raise AssertionError(f"seed message was not persisted: {row['seed_local_id']}")
    if chat_message.memory_policy != "normal" or chat_message.is_redacted:
        raise AssertionError(f"governance-clean seed persisted with non-normal policy: {row}")

    message_kind = chat_message.message_kind or str(row["message_kind"])
    content_hash = compute_content_hash(
        chat_message.text,
        chat_message.caption,
        message_kind,
        None,
    )
    version = await MessageVersionRepo.insert_version(
        session,
        chat_message_id=chat_message.id,
        content_hash=content_hash,
        text=chat_message.text,
        caption=chat_message.caption,
        normalized_text=chat_message.text,
        entities_json=None,
        raw_update_id=chat_message.raw_update_id,
        is_redacted=chat_message.is_redacted,
        imported_final=False,
    )
    chat_message.content_hash = content_hash
    chat_message.current_version_id = version.id
    await session.flush()

    persisted_version = await session.scalar(select(MessageVersion).where(MessageVersion.id == version.id))
    if persisted_version is None:
        raise AssertionError(f"seed version was not persisted: {row['seed_local_id']}")
    if persisted_version.is_redacted:
        raise AssertionError(f"governance-clean seed version redacted: {row['seed_local_id']}")

    current_version_id = await session.scalar(
        select(ChatMessage.current_version_id).where(ChatMessage.id == chat_message.id)
    )
    if current_version_id != version.id:
        raise AssertionError(f"current_version_id not populated: {row['seed_local_id']}")
    search_tsv = await session.scalar(
        select(MessageVersion.search_tsv).where(MessageVersion.id == version.id)
    )
    if search_tsv is None:
        raise AssertionError(f"search_tsv not populated: {row['seed_local_id']}")

    return int(version.id)


@pytest_asyncio.fixture(scope="class")
async def golden_recall_seed(eval_db_session: Any) -> Seed:
    rows = _load_jsonl(CHAT_HISTORY_PATH)
    seed_hash = hashlib.sha256(_canonical_jsonl_bytes(rows)).hexdigest()
    expected_id_map: dict[str, int] = {}
    for row in rows:
        seed_local_id = str(row["seed_local_id"])
        expected_id_map[seed_local_id] = await _persist_seed_message(eval_db_session, row)

    return Seed(
        seed_id=SEED_ID,
        version=SEED_VERSION,
        seed_hash=seed_hash,
        chat_id=SEED_CHAT_ID,
        expected_id_map=expected_id_map,
    )


@pytest.fixture(scope="class")
def seed(golden_recall_seed: Seed) -> Seed:
    return golden_recall_seed
