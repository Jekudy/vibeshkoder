"""Offline contract for the Telegram Desktop HTML adapter.

Fixtures are synthetic. Tests never read media files and never call a network or LLM.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

import pytest


FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "td_export_html"
EXCLUDED_RAW_FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "td_export_html_excluded_raw"


def test_discovers_all_pages_in_natural_order(tmp_path: Path) -> None:
    from bot.services.import_html_parser import discover_html_pages

    names = ["messages.html", *[f"messages{index}.html" for index in range(2, 11)]]
    for name in reversed(names):
        (tmp_path / name).write_text("<!doctype html><html></html>", encoding="utf-8")

    assert [page.name for page in discover_html_pages(tmp_path)] == names


def test_passing_one_page_still_discovers_complete_sibling_sequence(tmp_path: Path) -> None:
    from bot.services.import_html_parser import discover_html_pages

    for name in ("messages.html", "messages2.html", "messages3.html"):
        (tmp_path / name).write_text("<!doctype html><html></html>", encoding="utf-8")

    assert [page.name for page in discover_html_pages(tmp_path / "messages2.html")] == [
        "messages.html",
        "messages2.html",
        "messages3.html",
    ]


def test_iter_html_messages_returns_canonical_messages_in_page_order() -> None:
    from bot.services.import_html_parser import iter_html_messages

    messages = list(iter_html_messages(FIXTURE_DIR))

    assert [message["id"] for message in messages] == [100, 101, 102, -1, 103, 104]
    assert messages[0]["from"] == "Author Uno"
    assert messages[1]["from"] == "Author Uno"
    assert messages[0]["from_id"] == messages[1]["from_id"] == messages[3 + 1]["from_id"]
    assert messages[0]["from_id"].startswith("user")
    assert messages[1]["reply_to_message_id"] == 100
    assert messages[0]["text"] == "FIRST_MESSAGE_SENTINEL"
    assert messages[1]["text"] == "JOINED_MESSAGE_SENTINEL"
    assert messages[2]["text"] == "PHOTO_CAPTION_SENTINEL"
    assert messages[4]["media_type"] == "voice_message"
    assert messages[4].get("text", "") == ""
    assert messages[5]["forwarded_from"] == "Forward Source"
    assert messages[5]["text"] == "FORWARDED_MESSAGE_SENTINEL"


def test_media_references_metadata_and_source_paths_are_preserved() -> None:
    from bot.services.import_html_parser import iter_html_messages

    messages = {message["id"]: message for message in iter_html_messages(FIXTURE_DIR)}
    photo = messages[102]
    voice = messages[103]

    assert photo["media_type"] == "photo"
    assert photo["photo"] == "photos/photo_1.jpg"
    assert photo["media_refs"] == ["photos/photo_1.jpg", "photos/photo_1_thumb.jpg"]
    assert photo["media_metadata"] == {
        "title": "Photo",
        "description": "800x600",
        "status": "120 KB",
    }
    assert photo["source_path"] == "messages.html#message102"

    # The referenced file intentionally does not exist: successful parsing proves the
    # adapter records voice metadata without opening/transcribing the voice payload.
    assert voice["file"] == "voice_messages/audio_1.ogg"
    assert voice["media_refs"] == ["voice_messages/audio_1.ogg"]
    assert voice["source_path"] == "messages2.html#message103"


def test_media_caption_is_preserved_explicitly_for_raw_boundary() -> None:
    from bot.services.import_html_parser import iter_html_messages

    message = next(iter_html_messages(EXCLUDED_RAW_FIXTURE_DIR))

    assert message["from"] == "Shkoder"
    assert message["text"] == "SHKODER_CAPTION_SENTINEL SHKODER_ENTITY_SENTINEL"
    assert message["caption"] == message["text"]
    assert message["media_refs"] == [
        "photos/shkoder_full.jpg",
        "photos/shkoder_thumb.jpg",
    ]
    assert message["media_metadata"] == {
        "title": "Photo",
        "description": "1024x768",
        "status": "321 KB",
    }


def test_caption_local_link_is_not_misidentified_as_primary_photo(tmp_path: Path) -> None:
    from bot.services.import_html_parser import iter_html_messages

    page = """<!doctype html><html><body>
    <div class="message default clearfix" id="message8"><div class="body">
      <div class="pull_right date details" title="01.07.2026 08:00:00 UTC+03:00">08:00</div>
      <div class="from_name">A</div>
      <div class="media_wrap clearfix"><div class="media clearfix media_photo">
        <div class="title bold">Photo</div></div></div>
      <div class="text"><a href="stickers/custom.webp">linked custom emoji</a></div>
    </div></div></body></html>"""
    (tmp_path / "messages.html").write_text(page, encoding="utf-8")

    message = next(iter_html_messages(tmp_path))
    assert message["media_refs"] == []
    assert "photo" not in message
    assert message["linked_media_refs"] == ["stickers/custom.webp"]


def test_downloaded_photo_markup_is_supported(tmp_path: Path) -> None:
    from bot.services.import_html_parser import iter_html_messages

    page = """<!doctype html><html><body>
    <div class="message default clearfix" id="message9"><div class="body">
      <div class="pull_right date details" title="01.07.2026 08:00:00 UTC+03:00">08:00</div>
      <div class="from_name">A</div>
      <div class="media_wrap clearfix"><a class="photo" href="photos/full.jpg">
        <img src="photos/thumb.jpg"></a></div>
    </div></div></body></html>"""
    (tmp_path / "messages.html").write_text(page, encoding="utf-8")

    message = next(iter_html_messages(tmp_path))
    assert message["media_type"] == "photo"
    assert message["photo"] == "photos/full.jpg"
    assert message["media_refs"] == ["photos/full.jpg", "photos/thumb.jpg"]


def test_build_canonical_envelope_requires_explicit_chat_id() -> None:
    from bot.services.import_html_parser import build_canonical_envelope

    with pytest.raises(ValueError, match="chat_id"):
        build_canonical_envelope(FIXTURE_DIR, chat_id=None)

    envelope = build_canonical_envelope(FIXTURE_DIR, chat_id=-1001234567890)
    assert envelope["id"] == -1001234567890
    assert envelope["type"] == "private_supergroup"
    assert [message["id"] for message in envelope["messages"]] == [100, 101, 102, -1, 103, 104]


def test_conversion_is_deterministic_and_idempotent() -> None:
    from bot.services.import_html_parser import build_canonical_envelope

    first = build_canonical_envelope(FIXTURE_DIR, chat_id=-1001234567890)
    second = build_canonical_envelope(FIXTURE_DIR, chat_id=-1001234567890)
    assert first == second


def test_html_dry_run_report_has_structure_only() -> None:
    from bot.services.import_html_parser import parse_html_export

    report = parse_html_export(FIXTURE_DIR)
    assert report.chat_id is None
    assert report.chat_name is None
    assert report.total_messages == 6
    assert report.user_messages == 5
    assert report.service_messages == 1
    assert report.reply_count == 1
    assert report.media_count == 2
    assert report.message_kind_counts["photo"] == 1
    assert report.message_kind_counts["voice"] == 1
    assert report.date_range_start is not None
    assert report.date_range_end is not None

    payload = asdict(report)
    serialized = json.dumps(payload, default=str)
    for forbidden in (
        "Author Uno",
        "Author Dos",
        "FIRST_MESSAGE_SENTINEL",
        "PHOTO_CAPTION_SENTINEL",
        "FORWARDED_MESSAGE_SENTINEL",
    ):
        assert forbidden not in serialized


def test_html_dry_run_reports_exact_excluded_author_counts() -> None:
    from bot.services.import_html_parser import parse_html_export

    report = parse_html_export(
        FIXTURE_DIR,
        excluded_author_names=frozenset({"author uno", "missing bot"}),
    )

    assert report.excluded_author_message_count == 4
    assert report.excluded_author_message_counts == {
        "author uno": 4,
        "missing bot": 0,
    }


def test_missing_page_in_sequence_fails_fast(tmp_path: Path) -> None:
    from bot.services.import_html_parser import HtmlExportValidationError, discover_html_pages

    (tmp_path / "messages.html").write_text("<!doctype html><html></html>", encoding="utf-8")
    (tmp_path / "messages3.html").write_text("<!doctype html><html></html>", encoding="utf-8")

    with pytest.raises(HtmlExportValidationError, match="contiguous"):
        discover_html_pages(tmp_path)


def test_duplicate_message_ids_fail_fast(tmp_path: Path) -> None:
    from bot.services.import_html_parser import iter_html_messages

    page = """<!doctype html><html><body>
    <div class="message default clearfix" id="message7"><div class="body">
      <div class="pull_right date details" title="01.07.2026 08:00:00 UTC+03:00">08:00</div>
      <div class="from_name">A</div><div class="text">one</div></div></div>
    <div class="message default clearfix" id="message7"><div class="body">
      <div class="pull_right date details" title="01.07.2026 08:01:00 UTC+03:00">08:01</div>
      <div class="from_name">A</div><div class="text">two</div></div></div>
    </body></html>"""
    (tmp_path / "messages.html").write_text(page, encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate message id"):
        list(iter_html_messages(tmp_path))


def test_apply_iterator_accepts_html_directory() -> None:
    from bot.services.import_apply import _iter_export_messages

    messages = list(_iter_export_messages(FIXTURE_DIR))
    assert [message["id"] for message in messages] == [100, 101, 102, -1, 103, 104]


def test_cli_html_dry_run_prints_structure_without_content(capsys) -> None:
    from bot.cli import main

    result = main(["import_dry_run", str(FIXTURE_DIR)])
    captured = capsys.readouterr()

    assert result == 0
    payload = json.loads(captured.out)
    assert payload["total_messages"] == 6
    assert payload["chat_id"] is None
    assert "FIRST_MESSAGE_SENTINEL" not in captured.out
    assert "Author Uno" not in captured.out


def test_cli_html_dry_run_prints_exact_excluded_author_counts(capsys) -> None:
    from bot.cli import main

    result = main(
        [
            "import_dry_run",
            str(FIXTURE_DIR),
            "--exclude-author-name",
            "Author Uno",
        ]
    )
    captured = capsys.readouterr()

    assert result == 0
    payload = json.loads(captured.out)
    assert payload["excluded_author_message_count"] == 4
    assert payload["excluded_author_message_counts"] == {"author uno": 4}
    assert "FIRST_MESSAGE_SENTINEL" not in captured.out


def test_cli_html_apply_requires_explicit_chat_id(capsys) -> None:
    from bot.cli import main

    result = main(["import_apply", str(FIXTURE_DIR)])
    captured = capsys.readouterr()

    assert result == 2
    assert "chat-id" in captured.err.lower()


def test_cli_html_apply_requires_explicit_excluded_author(capsys, monkeypatch) -> None:
    from bot.cli import main

    monkeypatch.delenv("IMPORT_EXCLUDED_AUTHOR_NAMES_JSON", raising=False)
    result = main(["import_apply", str(FIXTURE_DIR), "--chat-id", "-100123"])
    captured = capsys.readouterr()

    assert result == 2
    assert "exclude-author-name" in captured.err.lower()


def test_cli_html_apply_rejects_unmatched_excluded_author_before_db(
    capsys,
    monkeypatch,
) -> None:
    import sys
    from types import ModuleType

    from bot import cli

    def forbidden_db_session():
        raise AssertionError("DB session must not be opened before exact-author preflight")

    fake_db_engine = ModuleType("bot.db.engine")
    fake_db_engine.async_session = forbidden_db_session  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "bot.db.engine", fake_db_engine)
    result = cli.main(
        [
            "import_apply",
            str(FIXTURE_DIR),
            "--chat-id",
            "-100123",
            "--exclude-author-name",
            "Wrong Bot Name",
        ]
    )
    captured = capsys.readouterr()

    assert result == 2
    assert "wrong bot name" in captured.err.casefold()
    assert "exact" in captured.err.casefold()
    assert "traceback" not in captured.err.casefold()


def test_source_hash_preserves_legacy_json_digest_and_is_stable_for_html(tmp_path: Path) -> None:
    from bot.cli import _hash_import_source

    json_source = tmp_path / "export.json"
    json_source.write_bytes(b"legacy-json-bytes")
    assert _hash_import_source(json_source) == hashlib.sha256(b"legacy-json-bytes").hexdigest()

    assert _hash_import_source(FIXTURE_DIR) == _hash_import_source(FIXTURE_DIR)


def test_source_hash_binds_normalized_excluded_author_config() -> None:
    from bot.cli import _hash_import_source

    base = _hash_import_source(FIXTURE_DIR)
    shkoder = _hash_import_source(
        FIXTURE_DIR,
        excluded_author_names=frozenset({"shkoder"}),
    )
    other = _hash_import_source(
        FIXTURE_DIR,
        excluded_author_names=frozenset({"other bot"}),
    )

    assert shkoder != base
    assert other != shkoder
    assert shkoder == _hash_import_source(
        FIXTURE_DIR,
        excluded_author_names=frozenset({"shkoder"}),
    )


@pytest.mark.parametrize(
    "unsafe_ref",
    [
        "photos/%252e%252e/x.jpg",
        "photos/%2e%2e/x.jpg",
        "photos/%00x.jpg",
        "photos/%0ax.jpg",
        "photos/%C2%85x.jpg",
        "photos/raw\u0085control.jpg",
        "photos/x%2fy.jpg",
        "photos/x%5cy.jpg",
        "photos/x%252fy.jpg",
        "photos/raw\\backslash.jpg",
        "photos/bad%zz.jpg",
        "/photos/absolute.jpg",
        "//example.invalid/photos/x.jpg",
    ],
)
def test_media_ref_rejects_ambiguous_or_encoded_traversal(unsafe_ref: str) -> None:
    from bot.services.import_html_parser import _normalize_media_ref

    assert _normalize_media_ref(unsafe_ref) is None


def test_media_ref_accepts_provably_relative_percent_encoded_filename() -> None:
    from bot.services.import_html_parser import _normalize_media_ref

    assert _normalize_media_ref("photos/photo%20one.jpg") == "photos/photo one.jpg"


@pytest.mark.parametrize("mode", ["dry_run", "apply"])
@pytest.mark.parametrize("shape", ["empty", "gapped"])
def test_cli_invalid_html_directory_returns_clean_error(
    tmp_path: Path,
    capsys,
    mode: str,
    shape: str,
) -> None:
    from bot.cli import main

    if shape == "gapped":
        (tmp_path / "messages.html").write_text("<!doctype html><html></html>", encoding="utf-8")
        (tmp_path / "messages3.html").write_text("<!doctype html><html></html>", encoding="utf-8")

    argv = ["import_dry_run", str(tmp_path)]
    if mode == "apply":
        argv = [
            "import_apply",
            str(tmp_path),
            "--chat-id",
            "-100123",
            "--exclude-author-name",
            "Shkoder",
        ]

    result = main(argv)
    captured = capsys.readouterr()
    assert result != 0
    assert "traceback" not in captured.err.casefold()
    assert "secret message sentinel" not in captured.err.casefold()


def test_cli_apply_does_not_mask_unexpected_hash_error(monkeypatch) -> None:
    from bot import cli

    def unexpected(_path, *, excluded_author_names=frozenset()):
        raise RuntimeError("unexpected-internal-error")

    monkeypatch.setattr(cli, "_hash_import_source", unexpected)
    with pytest.raises(RuntimeError, match="unexpected-internal-error"):
        cli.main(
            [
                "import_apply",
                str(FIXTURE_DIR),
                "--chat-id",
                "-100123",
                "--exclude-author-name",
                "Author Uno",
            ]
        )


async def test_import_run_config_persists_exclusion_preflight_audit(db_session) -> None:
    from sqlalchemy import text as sa_text

    from bot.cli import _save_import_run_config

    row = await db_session.execute(
        sa_text(
            """
            INSERT INTO ingestion_runs (run_type, source_name, source_hash, status, config_json)
            VALUES ('import', 'fixture', 'html_config_audit', 'running',
                    CAST('{"chat_id": -100123}' AS JSON))
            RETURNING id
            """
        )
    )
    run_id = int(row.scalar_one())

    await _save_import_run_config(
        db_session,
        ingestion_run_id=run_id,
        config={
            "source_adapter": "telegram_html",
            "excluded_author_names": ["shkoder"],
            "excluded_author_message_count": 107,
            "excluded_author_message_counts": {"shkoder": 107},
        },
    )
    stored = (
        await db_session.execute(
            sa_text("SELECT config_json FROM ingestion_runs WHERE id = :id"),
            {"id": run_id},
        )
    ).scalar_one()

    assert stored["chat_id"] == -100123
    assert stored["source_adapter"] == "telegram_html"
    assert stored["excluded_author_names"] == ["shkoder"]
    assert stored["excluded_author_message_count"] == 107
    assert stored["excluded_author_message_counts"] == {"shkoder": 107}


def test_excluded_author_config_is_strict_normalized_exact_match() -> None:
    from bot.services.import_author_exclusion import (
        is_import_author_excluded,
        load_import_excluded_author_names,
    )

    names = load_import_excluded_author_names(
        env={"IMPORT_EXCLUDED_AUTHOR_NAMES_JSON": '[" Shkoder ", "ＳＨＫＯＤＥＲ"]'},
        cli_names=["Other Bot"],
    )
    assert names == frozenset({"shkoder", "other bot"})
    assert is_import_author_excluded("SHKODER", names)
    assert is_import_author_excluded("  Shkoder  ", names)
    assert not is_import_author_excluded("Лиля Шкодер", names)
    assert not is_import_author_excluded("Shkoder Bot", names)


@pytest.mark.parametrize(
    "raw",
    ["", "{}", '"Shkoder"', "[1]", '[""]'],
)
def test_excluded_author_env_rejects_invalid_contract(raw: str) -> None:
    from bot.services.import_author_exclusion import load_import_excluded_author_names

    with pytest.raises(ValueError, match="IMPORT_EXCLUDED_AUTHOR_NAMES_JSON"):
        load_import_excluded_author_names(
            env={"IMPORT_EXCLUDED_AUTHOR_NAMES_JSON": raw},
            cli_names=None,
        )
