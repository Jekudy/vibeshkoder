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


def test_export_redacts_network_references_without_mutating_input(tmp_path) -> None:
    marker = "[external reference removed]"
    page = _page(
        title="Docs https://title.example/path?q=one&x=two).",
        body=(
            "Plain https://body.example/a_(b)?q=one&x=two#part), "
            "[External](https://link.example/private?q=one). "
            "Also www.example.net/docs;param![^mv:42]."
        ),
    )
    original = StaticWikiPage(**page.__dict__)

    first = export_static_site(
        [page],
        publish_dir=tmp_path / "site",
        site_title="Wiki",
        publication_authorized=True,
    )
    second = export_static_site(
        [page],
        publish_dir=tmp_path / "site",
        site_title="Wiki",
        publication_authorized=True,
    )

    page_html = (first.generation_dir / "pages/memory/index.html").read_text(encoding="utf-8")
    search_payload = json.loads(
        (first.generation_dir / "search-index.json").read_text(encoding="utf-8")
    )[0]
    public_text = page_html + json.dumps(search_payload, ensure_ascii=False)
    network_re = export_static_site.__globals__["_NETWORK_RE"]
    assert marker in page_html
    assert marker in search_payload["title"]
    assert marker in search_payload["content"]
    assert search_payload["source_refs"] == ["mv:42"]
    assert search_payload["revision_seq"] == 3
    assert not network_re.search(public_text)
    assert "title.example" not in public_text
    assert "body.example" not in public_text
    assert "link.example" not in public_text
    assert "www.example.net" not in public_text
    assert "q=one" not in public_text
    assert "x=two" not in public_text
    assert "a_(b)" not in public_text
    assert "docs;param" not in public_text
    assert page == original
    assert first.manifest_sha256 == second.manifest_sha256
    assert first.generation_dir == second.generation_dir


def test_network_redaction_preserves_adjacent_citations(tmp_path) -> None:
    card_id = "123e4567-e89b-12d3-a456-426614174000"
    page = _page(
        body=(
            "https://example.test/a?q=one#part).[^mv:42]. "
            f"Middle www.example.test/b;[^card:{card_id}] after."
        )
    )

    result = export_static_site(
        [page],
        publish_dir=tmp_path / "site",
        site_title="Wiki",
        publication_authorized=True,
    )

    search_payload = json.loads(
        (result.generation_dir / "search-index.json").read_text(encoding="utf-8")
    )[0]
    assert "Middle " in search_payload["content"]
    assert " after." in search_payload["content"]
    assert search_payload["source_refs"] == ["mv:42", f"card:{card_id}"]
    assert search_payload["content"].count("[external reference removed]") == 2
    assert "example.test" not in search_payload["content"]


def test_network_redaction_marker_is_safe_for_static_audit() -> None:
    exporter_globals = export_static_site.__globals__
    marker = exporter_globals["_NETWORK_REDACTION_MARKER"]

    assert not exporter_globals["_NETWORK_RE"].search(marker)
    assert not exporter_globals["_SECRET_RE"].search(marker)


def test_export_rejects_secret_inside_network_url_before_redaction(tmp_path) -> None:
    with pytest.raises(StaticExportSecurityError, match="secret-like"):
        export_static_site(
            [_page(body="https://example.test/?token=abcdefghijk [^mv:42].")],
            publish_dir=tmp_path / "site",
            site_title="Wiki",
            publication_authorized=True,
        )


def test_export_rejects_forbidden_origin_inside_url_before_redaction(tmp_path) -> None:
    with pytest.raises(StaticExportSecurityError, match="forbidden origin"):
        export_static_site(
            [_page(body="https://prod-vps.example.net/private [^mv:42].")],
            publish_dir=tmp_path / "site",
            site_title="Wiki",
            publication_authorized=True,
            forbidden_origins=["prod-vps.example.net"],
        )


def test_export_redacts_production_like_93_page_generation(tmp_path) -> None:
    marker = "[external reference removed]"
    pages = [
        _page(
            slug=f"page-{index:03d}",
            title=f"Page {index:03d}",
            body=(
                f"Primary https://source-{index}.example/path?q={index}). "
                + (f"Secondary www.extra-{index}.example/docs; " if index < 54 else "")
                + f"[^mv:{index + 1}]."
            ),
        )
        for index in range(93)
    ]
    originals = tuple(
        (page.slug, page.title, page.body_markdown, page.revision_seq) for page in pages
    )

    result = export_static_site(
        pages,
        publish_dir=tmp_path / "site",
        site_title="Wiki",
        publication_authorized=True,
    )

    search_payload = json.loads(
        (result.generation_dir / "search-index.json").read_text(encoding="utf-8")
    )
    public_content = " ".join(item["content"] for item in search_payload)
    assert result.page_count == 93
    assert len(search_payload) == 93
    assert public_content.count(marker) == 147
    assert not export_static_site.__globals__["_NETWORK_RE"].search(public_content)
    assert (
        tuple((page.slug, page.title, page.body_markdown, page.revision_seq) for page in pages)
        == originals
    )
    assert audit_static_tree(result.generation_dir) == result.manifest_sha256


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


def test_forbidden_origin_failure_keeps_previous_atomic_generation(tmp_path) -> None:
    publish_dir = tmp_path / "site"
    first = export_static_site(
        [_page()],
        publish_dir=publish_dir,
        site_title="Wiki",
        publication_authorized=True,
    )
    previous_target = publish_dir.resolve()

    with pytest.raises(StaticExportSecurityError, match="forbidden origin"):
        export_static_site(
            [_page(body="Never publish http://127.0.0.1:8080/private [^mv:42].")],
            publish_dir=publish_dir,
            site_title="Wiki",
            publication_authorized=True,
            forbidden_origins=["127.0.0.1"],
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

    external = export_static_site(
        [_page(body="[External](https://example.com/private) [^mv:42].")],
        publish_dir=tmp_path / "external-site",
        site_title="Wiki",
        publication_authorized=True,
    )
    external_payload = (external.publish_dir / "pages/memory/index.html").read_text(
        encoding="utf-8"
    )
    assert "[external reference removed]" in external_payload
    assert "example.com" not in external_payload


@pytest.mark.parametrize(
    "body",
    [
        "AKIA" + "ABCDEFGHIJKLMNOP [^mv:42].",
        "ghp_" + "abcdefghijklmnopqrstuvwxyz123456 [^mv:42].",
    ],
)
def test_export_blocks_extended_secret_patterns(tmp_path, body: str) -> None:
    with pytest.raises(StaticExportSecurityError, match="secret-like"):
        export_static_site(
            [_page(body=body)],
            publish_dir=tmp_path / "site",
            site_title="Wiki",
            publication_authorized=True,
        )


@pytest.mark.parametrize(
    "body",
    [
        "ftp://example.test/archive). [^mv:42].",
        "ssh://example.test/private; [^mv:42].",
        "postgresql://example.test/database?ssl=1 [^mv:42].",
        "redis://example.test/cache#db [^mv:42].",
        "mongodb+srv://example.test/data [^mv:42].",
        "file:///etc/passwd [^mv:42].",
        "www.example.test/docs [^mv:42].",
        "localhost:8080/admin [^mv:42].",
        "127.0.0.1/private [^mv:42].",
        "10.0.0.1/private [^mv:42].",
        "192.168.1.1/private [^mv:42].",
        "172.31.255.254/private [^mv:42].",
        "[::1]/admin [^mv:42].",
        "2001:db8::1/path [^mv:42].",
        "169.254.169.254/latest/meta-data [^mv:42].",
        "host.docker.internal/private [^mv:42].",
        "https://a.test,https://b.test [^mv:42].",
    ],
)
def test_export_redacts_extended_network_patterns(tmp_path, body: str) -> None:
    result = export_static_site(
        [_page(body=body)],
        publish_dir=tmp_path / "site",
        site_title="Wiki",
        publication_authorized=True,
    )
    payload = (result.publish_dir / "pages/memory/index.html").read_text(encoding="utf-8")

    assert "[external reference removed]" in payload
    assert not export_static_site.__globals__["_NETWORK_RE"].search(payload)


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
