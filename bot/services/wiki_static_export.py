"""Deterministic, zero-backchannel static export for compiled wiki pages."""

from __future__ import annotations

import errno
import hashlib
import html
import json
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

import bleach
from markdown_it import MarkdownIt

_SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_CITATION_RE = re.compile(
    r"\[\^((?:mv:[1-9]\d*)|(?:card:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}))\]"
)
_INTERNAL_PAGE_PATH_RE = re.compile(r"/pages/[a-z0-9]+(?:-[a-z0-9]+)*/\Z")
_NETWORK_RE = re.compile(
    r"(?i)(?:\b(?:https?|ftp|ssh|postgres(?:ql)?|redis|mongodb(?:\+srv)?|file)://|"
    r"\bwww\.|\blocalhost(?::\d+)?\b|\b127\.0\.0\.1\b|\b10(?:\.\d{1,3}){3}\b|"
    r"\b192\.168(?:\.\d{1,3}){2}\b|\b172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}\b)"
    r"|(?:\[?::1\]?)|\b169\.254\.169\.254\b|\bhost\.docker\.internal\b"
)
_SECRET_RE = re.compile(
    r"(?ix)(?:"
    r"(?:api[_-]?key|token|secret|password)\s*[:=]\s*[^\s<]{8,}|"
    r"\bsk-[a-z0-9_-]{16,}|"
    r"\b\d{8,10}:[a-z0-9_-]{20,}|"
    r"\b(?:akia|asia)[a-z0-9]{16}\b|"
    r"\bgh[pousr]_[a-z0-9]{20,}\b|"
    r"\bgithub_pat_[a-z0-9_]{20,}\b|"
    r"-----BEGIN\s+(?:RSA\s+|EC\s+|OPENSSH\s+)?PRIVATE\s+KEY-----"
    r")"
)
PUBLIC_GENERATION_MANIFEST_PATH = "generation-manifest.json"
_NETWORK_REDACTION_MARKER = "[external reference removed]"
_NON_WHITESPACE_TOKEN_RE = re.compile(r"\S+")
_DANGEROUS_ELEMENT_RE = re.compile(
    r"<(script|style|iframe|form|object|embed|applet|base|link|meta)"
    r"(?:\s[^>]*)?>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_DANGEROUS_VOID_RE = re.compile(
    r"<(script|style|iframe|form|object|embed|applet|base|link|meta)(?:\s[^>]*)?/?>",
    re.IGNORECASE,
)

_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)

_SEARCH_JS = """"use strict";
const input = document.getElementById("wiki-search");
const results = document.getElementById("wiki-search-results");
const status = document.getElementById("wiki-search-status");
let index = [];

function render(items) {
  results.replaceChildren();
  for (const item of items.slice(0, 50)) {
    const li = document.createElement("li");
    const link = document.createElement("a");
    link.href = "/pages/" + item.slug + "/";
    link.textContent = item.title;
    li.appendChild(link);
    results.appendChild(li);
  }
  status.textContent = items.length ? "" : "Ничего не найдено.";
}

function search() {
  const terms = input.value.toLocaleLowerCase("ru").trim().split(/\\s+/).filter(Boolean);
  if (!terms.length) {
    render(index);
    return;
  }
  render(index.filter((item) => {
    const haystack = (item.title + " " + item.content).toLocaleLowerCase("ru");
    return terms.every((term) => haystack.includes(term));
  }));
}

fetch("/search-index.json")
  .then((response) => {
    if (!response.ok) throw new Error("static index unavailable");
    return response.json();
  })
  .then((payload) => {
    index = payload;
    render(index);
    input.addEventListener("input", search);
  })
  .catch(() => {
    status.textContent = "Поиск временно недоступен.";
  });
"""

_SITE_CSS = """body{font-family:system-ui,sans-serif;line-height:1.55;max-width:880px;margin:0 auto;padding:2rem;color:#18212b;background:#fff}a{color:#1558d6}header{border-bottom:1px solid #d8dee8;margin-bottom:2rem}nav a{margin-right:1rem}.page-list,.source-list{padding-left:1.3rem}input{box-sizing:border-box;width:100%;padding:.7rem;border:1px solid #9aa8ba;border-radius:.4rem}code{background:#f1f4f8;padding:.1rem .25rem;border-radius:.2rem}.muted{color:#5d6877}@media(prefers-color-scheme:dark){body{color:#ecf1f7;background:#111820}a{color:#83b4ff}code{background:#26313e}}\n"""


class StaticExportError(RuntimeError):
    """Base error for a failed static build or publish swap."""


class StaticExportSecurityError(StaticExportError):
    """Generated content violated the zero-backchannel export policy."""


@dataclass(frozen=True)
class StaticWikiPage:
    slug: str
    title: str
    body_markdown: str
    revision_seq: int


@dataclass(frozen=True)
class StaticExportResult:
    publish_dir: Path
    generation_dir: Path
    manifest_sha256: str
    page_count: int


def export_static_site(
    pages: Iterable[StaticWikiPage],
    *,
    publish_dir: Path,
    site_title: str,
    publication_authorized: bool,
    forbidden_origins: Iterable[str] = (),
) -> StaticExportResult:
    """Build an immutable generation and atomically swap a symlink to it.

    The whole site is rendered and audited in memory before filesystem state
    changes.  ``publish_dir`` is an atomic symlink pointer, while immutable
    generations live in a hidden sibling directory suitable for a one-way
    Cloudflare Pages upload.
    """
    if publication_authorized is not True:
        raise StaticExportSecurityError("explicit publication authorization is required")
    denylist = _normalize_forbidden_origins(forbidden_origins)
    normalized_pages = _validate_inputs(
        list(pages), site_title=site_title, forbidden_origins=denylist
    )
    files = _build_files(normalized_pages, site_title=site_title.strip())
    manifest_sha = _manifest_hash(files)
    files[PUBLIC_GENERATION_MANIFEST_PATH] = _manifest_payload(manifest_sha)
    _audit_file_map(files, forbidden_origins=denylist)

    publish_dir = Path(publish_dir).absolute()
    if os.path.lexists(publish_dir) and not publish_dir.is_symlink():
        raise StaticExportError("publish_dir must be absent or an exporter-managed symlink")
    publish_dir.parent.mkdir(parents=True, exist_ok=True)
    generations_root = publish_dir.parent / f".{publish_dir.name}-generations"
    generations_root.mkdir(mode=0o750, parents=True, exist_ok=True)
    generation_dir = generations_root / manifest_sha

    if not generation_dir.exists():
        temporary = Path(tempfile.mkdtemp(prefix=".build-", dir=generations_root))
        try:
            for relative, payload in files.items():
                target = temporary / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
            if audit_static_tree(temporary, forbidden_origins=denylist) != manifest_sha:
                raise StaticExportSecurityError("static generation manifest mismatch")
            try:
                temporary.rename(generation_dir)
            except OSError as exc:
                if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY} or not generation_dir.is_dir():
                    raise
                shutil.rmtree(temporary)
                if audit_static_tree(generation_dir, forbidden_origins=denylist) != manifest_sha:
                    raise StaticExportSecurityError("winning static generation manifest mismatch")
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    else:
        if audit_static_tree(generation_dir, forbidden_origins=denylist) != manifest_sha:
            raise StaticExportSecurityError("existing static generation manifest mismatch")

    if audit_static_tree(generation_dir, forbidden_origins=denylist) != manifest_sha:
        raise StaticExportSecurityError("static generation changed before publication swap")

    temporary_link = publish_dir.parent / f".{publish_dir.name}.link-{uuid.uuid4().hex}"
    relative_target = os.path.relpath(generation_dir, start=publish_dir.parent)
    try:
        temporary_link.symlink_to(relative_target, target_is_directory=True)
        os.replace(temporary_link, publish_dir)
    finally:
        if os.path.lexists(temporary_link):
            temporary_link.unlink()

    return StaticExportResult(
        publish_dir=publish_dir,
        generation_dir=generation_dir,
        manifest_sha256=manifest_sha,
        page_count=len(normalized_pages),
    )


def audit_static_tree(root: Path, *, forbidden_origins: Iterable[str] = ()) -> str:
    """Fail closed if a built generation contains a dynamic or unsafe surface."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise StaticExportSecurityError("static generation is not a directory")
    files: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise StaticExportSecurityError("static generation contains a symlink")
        if path.is_file():
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    _audit_file_map(files, forbidden_origins=_normalize_forbidden_origins(forbidden_origins))
    return _manifest_hash(files)


def _validate_inputs(
    pages: list[StaticWikiPage], *, site_title: str, forbidden_origins: tuple[str, ...]
) -> list[StaticWikiPage]:
    if not site_title.strip() or len(site_title.strip()) > 120:
        raise ValueError("site_title must be non-empty and at most 120 characters")
    _scan_user_content(site_title, forbidden_origins=forbidden_origins)
    slugs: set[str] = set()
    normalized: list[StaticWikiPage] = []
    for page in pages:
        if not _SLUG_RE.fullmatch(page.slug) or len(page.slug) > 120:
            raise ValueError("page slug must be lowercase kebab-case")
        if page.slug in slugs:
            raise ValueError(f"duplicate page slug: {page.slug}")
        if not page.title.strip() or len(page.title.strip()) > 240:
            raise ValueError("page title must be non-empty and at most 240 characters")
        if not page.body_markdown.strip() or page.revision_seq <= 0:
            raise ValueError("page body and positive revision_seq are required")
        if not _CITATION_RE.search(page.body_markdown):
            raise ValueError("every exported page must contain at least one source citation")
        public_title = _prepare_public_text(page.title, forbidden_origins=forbidden_origins)
        public_body = _prepare_public_text(page.body_markdown, forbidden_origins=forbidden_origins)
        slugs.add(page.slug)
        normalized.append(
            StaticWikiPage(
                slug=page.slug,
                title=public_title.strip(),
                body_markdown=public_body.strip(),
                revision_seq=page.revision_seq,
            )
        )
    return sorted(normalized, key=lambda value: value.slug)


def _scan_user_content(value: str, *, forbidden_origins: tuple[str, ...]) -> None:
    _assert_no_forbidden_origin(value, forbidden_origins=forbidden_origins)
    if _NETWORK_RE.search(value):
        raise StaticExportSecurityError("network reference is forbidden in static export")
    if _SECRET_RE.search(value):
        raise StaticExportSecurityError("secret-like material is forbidden in static export")


def _prepare_public_text(value: str, *, forbidden_origins: tuple[str, ...]) -> str:
    """Redact network tokens after fail-closed checks on the private source."""
    _assert_no_forbidden_origin(value, forbidden_origins=forbidden_origins)
    if _SECRET_RE.search(value):
        raise StaticExportSecurityError("secret-like material is forbidden in static export")
    redacted = _redact_network_references(value)
    _scan_user_content(redacted, forbidden_origins=forbidden_origins)
    return redacted


def _redact_network_references(value: str) -> str:
    """Conservatively replace every whitespace-delimited token containing a network ref."""

    def redact_token(match: re.Match[str]) -> str:
        token = match.group(0)
        if not _NETWORK_RE.search(token):
            return token
        citations = "".join(citation.group(0) for citation in _CITATION_RE.finditer(token))
        return f"{_NETWORK_REDACTION_MARKER}{citations}"

    # ponytail: redact the whole token; use Markdown-aware spans only if public copy
    # quality requires it.
    return _NON_WHITESPACE_TOKEN_RE.sub(redact_token, value)


def _normalize_forbidden_origins(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, str):
        raise ValueError("forbidden_origins must be an iterable of host/origin strings")
    normalized: set[str] = set()
    for raw_value in values:
        if not isinstance(raw_value, str) or not raw_value.strip():
            raise ValueError("forbidden_origins entries must be non-empty strings")
        value = raw_value.strip().casefold().rstrip("/")
        normalized.add(value)
        parsed = urlsplit(value if "://" in value else f"//{value}")
        if parsed.hostname:
            normalized.add(parsed.hostname.casefold())
    return tuple(sorted(normalized))


def _assert_no_forbidden_origin(value: str, *, forbidden_origins: tuple[str, ...]) -> None:
    folded = value.casefold()
    if any(origin in folded for origin in forbidden_origins):
        raise StaticExportSecurityError("caller-forbidden origin is present in static export")


def _build_files(pages: list[StaticWikiPage], *, site_title: str) -> dict[str, bytes]:
    rendered: list[tuple[StaticWikiPage, str, list[str], str]] = []
    for page in pages:
        source_refs = list(dict.fromkeys(_CITATION_RE.findall(page.body_markdown)))
        html_body = _render_markdown(page.body_markdown)
        plain_text = _plain_text(html_body)
        rendered.append((page, html_body, source_refs, plain_text))

    files: dict[str, bytes] = {
        "assets/search.js": _SEARCH_JS.encode(),
        "assets/site.css": _SITE_CSS.encode(),
        "_headers": (
            "/*\n"
            f"  Content-Security-Policy: {_CSP}\n"
            "  X-Content-Type-Options: nosniff\n"
            "  Referrer-Policy: no-referrer\n"
            "  Permissions-Policy: camera=(), microphone=(), geolocation=()\n"
        ).encode(),
        "robots.txt": b"User-agent: *\nDisallow: /\n",
    }
    page_links = "".join(
        f'<li><a href="/pages/{page.slug}/">{html.escape(page.title)}</a></li>'
        for page, _body, _refs, _plain in rendered
    )
    empty_notice = "" if rendered else '<p class="muted">Пока нет опубликованных статей.</p>'
    files["index.html"] = _html_document(
        site_title,
        (
            f"<h1>{html.escape(site_title)}</h1>"
            '<label for="wiki-search">Поиск по wiki</label>'
            '<input id="wiki-search" type="search" autocomplete="off">'
            '<p id="wiki-search-status" class="muted" aria-live="polite"></p>'
            f"{empty_notice}"
            f'<ul id="wiki-search-results" class="page-list">{page_links}</ul>'
        ),
        include_search=True,
    ).encode()

    search_payload: list[dict[str, object]] = []
    for page, html_body, source_refs, plain_text in rendered:
        source_list = "".join(f"<li><code>{html.escape(ref)}</code></li>" for ref in source_refs)
        detail = (
            f"<article><h1>{html.escape(page.title)}</h1>{html_body}</article>"
            f'<section><h2>Источники</h2><ul class="source-list">{source_list}</ul>'
            f'<p class="muted">Ревизия {page.revision_seq}</p></section>'
        )
        files[f"pages/{page.slug}/index.html"] = _html_document(
            page.title, detail, include_search=False
        ).encode()
        search_payload.append(
            {
                "content": plain_text,
                "revision_seq": page.revision_seq,
                "slug": page.slug,
                "source_refs": source_refs,
                "title": page.title,
            }
        )
    files["search-index.json"] = (
        json.dumps(search_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()
    return files


def _render_markdown(body: str) -> str:
    body = _DANGEROUS_ELEMENT_RE.sub("", body)
    body = _DANGEROUS_VOID_RE.sub("", body)
    body = _CITATION_RE.sub(lambda match: f"`[{match.group(1)}]`", body)
    raw_html = MarkdownIt("commonmark").render(body)

    def allow_attribute(tag: str, name: str, value: str) -> bool:
        return tag == "a" and name == "href" and bool(_INTERNAL_PAGE_PATH_RE.fullmatch(value))

    return bleach.clean(
        raw_html,
        tags=[
            "p",
            "br",
            "strong",
            "em",
            "h1",
            "h2",
            "h3",
            "h4",
            "ul",
            "ol",
            "li",
            "blockquote",
            "code",
            "pre",
            "a",
            "hr",
        ],
        attributes=allow_attribute,
        protocols=["http", "https"],
        strip=True,
    )


def _plain_text(html_body: str) -> str:
    value = bleach.clean(html_body, tags=[], attributes={}, strip=True)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def _html_document(title: str, body: str, *, include_search: bool) -> str:
    search_script = '<script src="/assets/search.js" defer></script>' if include_search else ""
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow,noarchive">'
        f'<meta http-equiv="Content-Security-Policy" content="{html.escape(_CSP, quote=True)}">'
        f"<title>{html.escape(title)}</title>"
        '<link rel="stylesheet" href="/assets/site.css">'
        f'{search_script}</head><body><header><nav><a href="/">Wiki</a></nav></header>'
        f"<main>{body}</main></body></html>\n"
    )


def _audit_file_map(files: dict[str, bytes], *, forbidden_origins: tuple[str, ...]) -> None:
    required = {
        "index.html",
        "search-index.json",
        "assets/search.js",
        "assets/site.css",
        "_headers",
        "robots.txt",
        PUBLIC_GENERATION_MANIFEST_PATH,
    }
    if not required.issubset(files):
        raise StaticExportSecurityError("static generation is incomplete")
    for relative, payload in files.items():
        if relative not in required and not re.fullmatch(
            r"pages/[a-z0-9]+(?:-[a-z0-9]+)*/index\.html", relative
        ):
            raise StaticExportSecurityError("static generation contains an unexpected file")
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            raise StaticExportSecurityError("static generation contains an unsafe path")
        try:
            content = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StaticExportSecurityError("static generation contains non-text content") from exc
        _assert_no_forbidden_origin(content, forbidden_origins=forbidden_origins)
        if _NETWORK_RE.search(content):
            raise StaticExportSecurityError("static generation contains a network reference")
        if _SECRET_RE.search(content):
            raise StaticExportSecurityError("static generation contains secret-like material")
        if relative.endswith(".html"):
            if '<meta name="robots" content="noindex,nofollow,noarchive">' not in content:
                raise StaticExportSecurityError("static HTML is missing noindex policy")
            if re.search(r"<script(?![^>]*\bsrc=)", content, re.IGNORECASE):
                raise StaticExportSecurityError("inline scripts are forbidden")
            if re.search(r"<[^>]*\s(?:on[a-z]+|style)\s*=", content, re.IGNORECASE):
                raise StaticExportSecurityError("inline event/style attributes are forbidden")
            if re.search(r"<(?:form|iframe|object|embed|base)\b", content, re.IGNORECASE):
                raise StaticExportSecurityError("dynamic HTML elements are forbidden")
            for href in re.findall(r'<a\b[^>]*\bhref="([^"]+)"', content, re.IGNORECASE):
                if href != "/" and not _INTERNAL_PAGE_PATH_RE.fullmatch(href):
                    raise StaticExportSecurityError("non-local page link is forbidden")

    manifest_sha = _manifest_hash(files)
    if files[PUBLIC_GENERATION_MANIFEST_PATH] != _manifest_payload(manifest_sha):
        raise StaticExportSecurityError("static generation manifest mismatch")


def _manifest_payload(manifest_sha256: str) -> bytes:
    return (
        json.dumps(
            {"manifest_sha256": manifest_sha256},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()


def _manifest_hash(files: dict[str, bytes]) -> str:
    """Hash every public generation file except its self-describing marker.

    The marker is then required to contain this exact digest.  Excluding that
    one file avoids a circular hash while still making marker tampering fail
    ``_audit_file_map``.
    """
    digest = hashlib.sha256()
    for relative in sorted(files):
        if relative == PUBLIC_GENERATION_MANIFEST_PATH:
            continue
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(files[relative]).digest())
    return digest.hexdigest()


__all__ = [
    "PUBLIC_GENERATION_MANIFEST_PATH",
    "StaticExportError",
    "StaticExportResult",
    "StaticExportSecurityError",
    "StaticWikiPage",
    "audit_static_tree",
    "export_static_site",
]
