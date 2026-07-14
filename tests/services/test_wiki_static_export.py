"""Security and determinism contract for the pure static wiki exporter."""

from __future__ import annotations

import errno
import json
import shutil
from pathlib import Path

import pytest

from bot.services.wiki_static_export import (
    StaticExportSecurityError,
    StaticWikiPage,
    audit_static_tree,
    export_static_site,
)


def _page(*, slug: str = "memory", title: str = "Memory", body: str | None = None):
    if body is None:
        body = "# Memory\nA sourced decision [^mv:42]."
    return StaticWikiPage(slug=slug, title=title, body_markdown=body, revision_seq=3)


def test_export_builds_atomic_static_site_with_local_search(tmp_path) -> None:
    publish_dir = tmp_path / "site"
    result = export_static_site(
        [_page(), _page(slug="rules", title="Rules", body="Rules [^mv:7].")],
        publish_dir=publish_dir,
        site_title="Шкодер Wiki",
        publication_authorized=True,
    )

    assert publish_dir.is_symlink()
    assert publish_dir.resolve() == result.generation_dir.resolve()
    assert (publish_dir / "index.html").is_file()
    assert (publish_dir / "pages/memory/index.html").is_file()
    assert (publish_dir / "assets/search.js").is_file()
    index_html = (publish_dir / "index.html").read_text(encoding="utf-8")
    assert "Content-Security-Policy" in index_html
    assert '<script src="/assets/search.js" defer></script>' in index_html
    assert "<script>" not in index_html
    search_js = (publish_dir / "assets/search.js").read_text(encoding="utf-8")
    assert 'fetch("/search-index.json")' in search_js
    assert "XMLHttpRequest" not in search_js
    search_data = json.loads((publish_dir / "search-index.json").read_text(encoding="utf-8"))
    assert [item["slug"] for item in search_data] == ["memory", "rules"]
    assert search_data[0]["source_refs"] == ["mv:42"]
    assert (publish_dir / "robots.txt").read_text(encoding="utf-8") == (
        "User-agent: *\nDisallow: /\n"
    )
    assert '<meta name="robots" content="noindex,nofollow,noarchive">' in index_html
    audit_static_tree(result.generation_dir)


def test_export_is_deterministic_for_same_pages(tmp_path) -> None:
    pages = [_page(slug="zeta"), _page(slug="alpha")]
    first = export_static_site(
        pages,
        publish_dir=tmp_path / "site",
        site_title="Wiki",
        publication_authorized=True,
    )
    second = export_static_site(
        list(reversed(pages)),
        publish_dir=tmp_path / "site",
        site_title="Wiki",
        publication_authorized=True,
    )

    assert first.manifest_sha256 == second.manifest_sha256
    assert first.generation_dir == second.generation_dir


@pytest.mark.parametrize("variant", ["title", "assets", "csp"])
def test_public_generation_marker_covers_every_static_surface(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
) -> None:
    pages = [_page()]
    first = export_static_site(
        pages,
        publish_dir=tmp_path / "first",
        site_title="Wiki A",
        publication_authorized=True,
    )
    second_title = "Wiki A"
    if variant == "title":
        second_title = "Wiki B"
    elif variant == "assets":
        exporter_globals = export_static_site.__globals__
        monkeypatch.setitem(
            exporter_globals,
            "_SITE_CSS",
            exporter_globals["_SITE_CSS"] + "/* generation-b */",
        )
    else:
        exporter_globals = export_static_site.__globals__
        monkeypatch.setitem(
            exporter_globals,
            "_CSP",
            exporter_globals["_CSP"] + "; upgrade-insecure-requests",
        )

    second = export_static_site(
        pages,
        publish_dir=tmp_path / "second",
        site_title=second_title,
        publication_authorized=True,
    )

    first_marker = (first.generation_dir / "generation-manifest.json").read_bytes()
    second_marker = (second.generation_dir / "generation-manifest.json").read_bytes()
    assert (first.generation_dir / "search-index.json").read_bytes() == (
        second.generation_dir / "search-index.json"
    ).read_bytes()
    assert first.manifest_sha256 != second.manifest_sha256
    assert first_marker != second_marker
    assert json.loads(first_marker) == {"manifest_sha256": first.manifest_sha256}
    assert json.loads(second_marker) == {"manifest_sha256": second.manifest_sha256}
    assert audit_static_tree(first.generation_dir) == first.manifest_sha256
    assert audit_static_tree(second.generation_dir) == second.manifest_sha256


def test_export_escapes_titles_and_strips_active_html(tmp_path) -> None:
    result = export_static_site(
        [
            _page(
                title='<img src=x onerror="alert(1)">',
                body='<script>alert("x")</script>\nSafe fact [^mv:42].',
            )
        ],
        publish_dir=tmp_path / "site",
        site_title="Wiki",
        publication_authorized=True,
    )
    payload = (result.publish_dir / "pages/memory/index.html").read_text(encoding="utf-8")

    assert "<img" not in payload
    assert "<script" not in payload
    assert "&lt;img" in payload
    assert "mv:42" in payload


def test_security_failure_keeps_previous_atomic_generation(tmp_path) -> None:
    publish_dir = tmp_path / "site"
    first = export_static_site(
        [_page()],
        publish_dir=publish_dir,
        site_title="Wiki",
        publication_authorized=True,
    )
    previous_target = publish_dir.resolve()

    with pytest.raises(StaticExportSecurityError, match="network reference"):
        export_static_site(
            [_page(body="Never publish http://127.0.0.1:8080/private [^mv:42].")],
            publish_dir=publish_dir,
            site_title="Wiki",
            publication_authorized=True,
        )

    assert publish_dir.resolve() == previous_target
    assert publish_dir.resolve() == first.generation_dir.resolve()


def test_export_rejects_secret_like_material(tmp_path) -> None:
    with pytest.raises(StaticExportSecurityError, match="secret-like"):
        export_static_site(
            [_page(body="DEEPSEEK_API_KEY=sk-test-not-a-real-key-1234567890 [^mv:42].")],
            publish_dir=tmp_path / "site",
            site_title="Wiki",
            publication_authorized=True,
        )


def test_export_preserves_only_strict_internal_page_links(tmp_path) -> None:
    body = (
        "[Valid](/pages/rules/) "
        "[Query](/pages/rules/?debug=1) "
        "[Fragment](/pages/rules/#private) [^mv:42]."
    )
    result = export_static_site(
        [_page(body=body)],
        publish_dir=tmp_path / "site",
        site_title="Wiki",
        publication_authorized=True,
    )
    payload = (result.publish_dir / "pages/memory/index.html").read_text(encoding="utf-8")

    assert 'href="/pages/rules/"' in payload
    assert "/pages/rules/?debug=1" not in payload
    assert "/pages/rules/#private" not in payload

    with pytest.raises(StaticExportSecurityError, match="network reference"):
        export_static_site(
            [_page(body="[External](https://example.com/private) [^mv:42].")],
            publish_dir=tmp_path / "external-site",
            site_title="Wiki",
            publication_authorized=True,
        )


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("AKIA" + "ABCDEFGHIJKLMNOP [^mv:42].", "secret-like"),
        ("ghp_" + "abcdefghijklmnopqrstuvwxyz123456 [^mv:42].", "secret-like"),
        ("[::1] [^mv:42].", "network reference"),
        ("169.254.169.254/latest/meta-data [^mv:42].", "network reference"),
        ("host.docker.internal [^mv:42].", "network reference"),
    ],
)
def test_export_blocks_extended_secret_and_internal_host_patterns(
    tmp_path, body: str, expected: str
) -> None:
    with pytest.raises(StaticExportSecurityError, match=expected):
        export_static_site(
            [_page(body=body)],
            publish_dir=tmp_path / "site",
            site_title="Wiki",
            publication_authorized=True,
        )


def test_export_honors_caller_forbidden_origin_denylist(tmp_path) -> None:
    with pytest.raises(StaticExportSecurityError, match="forbidden origin"):
        export_static_site(
            [_page(body="prod-vps.example.net admin [^mv:42].")],
            publish_dir=tmp_path / "site",
            site_title="Wiki",
            publication_authorized=True,
            forbidden_origins=["prod-vps.example.net"],
        )


def test_adversarial_generation_race_never_swaps_publish_pointer(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish_dir = tmp_path / "site"
    first = export_static_site(
        [_page()],
        publish_dir=publish_dir,
        site_title="Wiki",
        publication_authorized=True,
    )
    previous_target = publish_dir.resolve()
    original_rename = Path.rename

    def race_with_modified_winner(source: Path, target: Path):
        shutil.copytree(source, target)
        with (target / "index.html").open("a", encoding="utf-8") as stream:
            stream.write(" ")
        raise OSError(errno.EEXIST, "adversarial winner")

    monkeypatch.setattr(Path, "rename", race_with_modified_winner)
    with pytest.raises(StaticExportSecurityError, match="manifest mismatch"):
        export_static_site(
            [_page(body="A new generation [^mv:43].")],
            publish_dir=publish_dir,
            site_title="Wiki",
            publication_authorized=True,
        )
    monkeypatch.setattr(Path, "rename", original_rename)

    assert publish_dir.resolve() == previous_target
    assert publish_dir.resolve() == first.generation_dir.resolve()


def test_export_requires_explicit_publication_authorization(tmp_path) -> None:
    publish_dir = tmp_path / "site"
    with pytest.raises(StaticExportSecurityError, match="publication authorization"):
        export_static_site(
            [_page()],
            publish_dir=publish_dir,
            site_title="Wiki",
            publication_authorized=False,
        )
    assert not publish_dir.exists()


def test_empty_export_replaces_previous_generation_without_page_or_network_surface(
    tmp_path,
) -> None:
    publish_dir = tmp_path / "site"
    export_static_site(
        [_page()],
        publish_dir=publish_dir,
        site_title="Wiki",
        publication_authorized=True,
    )
    previous_target = publish_dir.resolve()

    empty = export_static_site(
        [],
        publish_dir=publish_dir,
        site_title="Wiki",
        publication_authorized=True,
    )

    assert empty.page_count == 0
    assert publish_dir.resolve() != previous_target
    assert publish_dir.resolve() == empty.generation_dir.resolve()
    assert not (publish_dir / "pages").exists()
    assert json.loads((publish_dir / "search-index.json").read_text(encoding="utf-8")) == []
    index_html = (publish_dir / "index.html").read_text(encoding="utf-8")
    assert "Пока нет опубликованных статей" in index_html
    assert '<script src="/assets/search.js" defer></script>' in index_html
    assert audit_static_tree(empty.generation_dir) == empty.manifest_sha256
