"""Phase 11 §5.6 — invariant 2 binding test.

Walks the AST of every Python file under bot/ and asserts that no module
imports an LLM-provider client outside the allow-list. The allow-list will
include bot/services/llm_gateway.py once Phase 5 lands; until then it must
be empty (no Phase 4-or-earlier file may pull in such a dependency).

This file does NOT depend on any DB fixture: it only reads source code on
disk, so it always runs even if the eval harness env var is off.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# Single source of truth: LLM_PROVIDER_HOSTNAMES in tests/evals/_llm_guard.py.
# Imported directly to avoid drift between the runtime guard and the AST check.
from tests.evals._llm_guard import LLM_PROVIDER_HOSTNAMES

REPO_ROOT = Path(__file__).resolve().parents[2]
BOT_ROOT = REPO_ROOT / "bot"

LLM_PROVIDER_PREFIXES: tuple[str, ...] = (
    "anthropic",
    "openai",
    "langchain",
    "langchain_core",
    "langchain_anthropic",
    "langchain_openai",
    "transformers",
    "huggingface_hub",
    "ollama",
    "cohere",
    "mistralai",
    "replicate",
)

# Phase 5 (T5-01) has shipped: llm_gateway.py orchestrates calls; the actual
# SDK imports live in llm_providers/anthropic.py + llm_providers/openai.py
# (gateway itself contains zero `import anthropic` / `import openai` — verified
# via grep). Both provider files are part of the Phase 5 invariant-#2 boundary.
ALLOWED_LLM_IMPORT_FILES: frozenset[str] = frozenset(
    [
        "bot/services/llm_providers/anthropic.py",
        "bot/services/llm_providers/deepseek.py",
        "bot/services/llm_providers/openai.py",
        "bot/services/llm_providers/openai_vision.py",
    ]
)

# I4 — URL-level guard. The provider SDK imports above (anthropic / openai)
# are not the only way to call an LLM endpoint: a direct httpx / requests /
# aiohttp call to a provider hostname would bypass the invariant-#2 contract
# while passing the import-graph check. LLM_PROVIDER_HOSTNAMES (imported from
# _llm_guard) is the single source of truth — both the runtime httpx guard
# and this static scan use the same set, so they cannot drift.
_LLM_PROVIDER_HOSTNAMES_TUPLE: tuple[str, ...] = tuple(sorted(LLM_PROVIDER_HOSTNAMES))


def _relative_to_repo(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def _module_matches_provider(module: str | None) -> bool:
    if module is None:
        return False
    head = module.split(".", 1)[0]
    return head in LLM_PROVIDER_PREFIXES


def _collect_python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _llm_import_sites(path: Path) -> list[tuple[int, str]]:
    sites: list[tuple[int, str]] = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        pytest.fail(f"unable to parse {_relative_to_repo(path)}: {exc!s}")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _module_matches_provider(alias.name):
                    sites.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            module = node.module
            if _module_matches_provider(module):
                names = ", ".join(alias.name for alias in node.names)
                sites.append((node.lineno, f"from {module} import {names}"))
    return sites


def test_i1_no_llm_provider_imports_anywhere_in_bot() -> None:
    """I1: no module under bot/ imports an LLM provider outside the allow-list."""
    if not BOT_ROOT.is_dir():
        pytest.skip(f"{BOT_ROOT} not found; harness assumes monorepo layout")

    violations: list[str] = []
    for path in _collect_python_files(BOT_ROOT):
        rel = _relative_to_repo(path)
        if rel in ALLOWED_LLM_IMPORT_FILES:
            continue
        for line_no, statement in _llm_import_sites(path):
            violations.append(f"{rel}:{line_no}: {statement}")

    assert not violations, (
        "invariant 2 violation — LLM provider import detected outside the allow-list:\n"
        + "\n".join(violations)
    )


def test_i2_no_llm_provider_in_runtime_dependencies() -> None:
    """I2: pyproject.toml does not list LLM-provider packages as direct runtime deps."""
    pyproject = REPO_ROOT / "pyproject.toml"
    if not pyproject.is_file():
        pytest.fail(f"pyproject.toml missing at {pyproject}")
    content = pyproject.read_text(encoding="utf-8")

    in_dependencies = False
    in_optional = False
    optional_section_name: str | None = None
    forbidden_runtime_hits: list[str] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if line.startswith("dependencies"):
            in_dependencies = True
            in_optional = False
            continue
        if (
            line.startswith("[project.optional-dependencies]")
            or line.startswith("[tool.")
            or line.startswith("[build-system]")
            or line == ""
        ):
            if line.startswith("[project.optional-dependencies]"):
                in_optional = True
            else:
                in_optional = False
                optional_section_name = None
            in_dependencies = False
            continue
        if in_optional and line.startswith("[") and line.endswith("]"):
            optional_section_name = line.strip("[]")
            continue

        if in_dependencies:
            stripped = line.strip(" \t,\"'")
            head = (
                stripped.split("[", 1)[0]
                .split("=", 1)[0]
                .split(">", 1)[0]
                .split("<", 1)[0]
                .split("~", 1)[0]
                .split("!", 1)[0]
                .strip()
            )
            if head and head.replace("-", "_").lower() in {
                p.replace("-", "_").lower() for p in LLM_PROVIDER_PREFIXES
            }:
                forbidden_runtime_hits.append(stripped)

    assert not forbidden_runtime_hits, (
        "invariant 2 violation — LLM provider in [project.dependencies]:\n"
        + "\n".join(forbidden_runtime_hits)
        + "\n\nLLM provider packages must live in a Phase-5+ optional-dependencies group."
    )
    # Note: optional_section_name tracked above so future ALLOWED groups can be
    # asserted; current state forbids any provider anywhere in runtime deps.
    del optional_section_name


def _llm_hostname_sites(path: Path) -> list[tuple[int, str]]:
    """Find lines that mention an LLM provider hostname as a string literal."""
    sites: list[tuple[int, str]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return sites
    for line_no, line in enumerate(text.splitlines(), start=1):
        for host in LLM_PROVIDER_HOSTNAMES:
            if host in line:
                sites.append((line_no, host))
                break
    return sites


def test_i4_no_llm_provider_url_outside_gateway() -> None:
    """I4: a direct httpx/aiohttp/requests call to an LLM provider hostname
    must not exist outside the allow-listed gateway files. AST imports (I1)
    catch SDK import; this check catches the raw-URL escape hatch.
    """
    if not BOT_ROOT.is_dir():
        pytest.skip(f"{BOT_ROOT} not found; harness assumes monorepo layout")

    violations: list[str] = []
    for path in _collect_python_files(BOT_ROOT):
        rel = _relative_to_repo(path)
        if rel in ALLOWED_LLM_IMPORT_FILES:
            continue
        for line_no, host in _llm_hostname_sites(path):
            violations.append(f"{rel}:{line_no}: hostname {host!r}")

    assert not violations, (
        "invariant 2 URL-level violation — LLM provider hostname referenced "
        "outside the allow-list:\n" + "\n".join(violations)
    )


def _scan_files_for_hostnames(
    paths: list[Path],
    hostnames: frozenset[str],
) -> list[tuple[Path, str]]:
    """Scan *paths* for lines containing any of *hostnames*.

    Returns a list of (path, hostname) pairs — one entry per file/host
    match (at most one per file line, first hostname match wins).  Both
    the production assertion and the honeypot assertion call this helper
    directly, so a broken scanner fails both assertions simultaneously.
    """
    matches: list[tuple[Path, str]] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for line in text.splitlines():
            for host in hostnames:
                if host in line:
                    matches.append((path, host))
                    break
    return matches


def test_no_llm_provider_urls_outside_gateway() -> None:
    """I4b: LLM provider URLs must not appear in bot/, web/, or ops/ outside the
    allow-listed gateway files.

    Extends test_i4_no_llm_provider_url_outside_gateway (bot/-only) to cover
    web/ and ops/ as well, since a direct httpx call in a web route or ops script
    would bypass the invariant-#2 contract just as much as one in bot/.

    Hostname set is LLM_PROVIDER_HOSTNAMES from _llm_guard.py — the single
    source of truth for both the runtime httpx guard and this static scan.
    Previously extra hostnames (api.together.xyz, api.groq.com, etc.) were
    defined inline; they are now part of LLM_PROVIDER_HOSTNAMES in _llm_guard.py.

    Honeypot assertion: tests/fixtures/honeypot_llm_url.py contains a known
    LLM hostname; the honeypot is scanned with the SAME _scan_files_for_hostnames
    helper as the production scan — so a broken scanner fails BOTH assertions.
    """
    scan_roots = [
        root
        for root in [
            REPO_ROOT / "bot",
            REPO_ROOT / "web",
            REPO_ROOT / "ops",
        ]
        if root.is_dir()
    ]

    allowed_url_files: frozenset[str] = frozenset(
        [
            "bot/services/llm_gateway.py",
            "bot/services/llm_providers/anthropic.py",
            "bot/services/llm_providers/deepseek.py",
            "bot/services/llm_providers/openai.py",
            "bot/services/llm_providers/openai_vision.py",
        ]
    )

    # Production scan: collect all python files, excluding allow-listed gateway files.
    production_paths: list[Path] = []
    for root in scan_roots:
        for path in _collect_python_files(root):
            if _relative_to_repo(path) not in allowed_url_files:
                production_paths.append(path)

    prod_matches = _scan_files_for_hostnames(production_paths, LLM_PROVIDER_HOSTNAMES)

    violations = [f"{_relative_to_repo(path)}:hostname {host!r}" for path, host in prod_matches]
    assert not violations, (
        "invariant 2 URL-level violation (I4b) — LLM provider hostname in "
        "bot/, web/, or ops/ outside the allow-list:\n" + "\n".join(violations)
    )

    # Honeypot: the SAME helper must detect the known-bad fixture.
    honeypot_path = REPO_ROOT / "tests" / "fixtures" / "honeypot_llm_url.py"
    assert honeypot_path.is_file(), (
        f"honeypot fixture missing at {honeypot_path} — "
        "create tests/fixtures/honeypot_llm_url.py with '# api.openai.com'"
    )
    honeypot_matches = _scan_files_for_hostnames([honeypot_path], LLM_PROVIDER_HOSTNAMES)
    assert len(honeypot_matches) >= 1, (
        "honeypot fixture was not detected by _scan_files_for_hostnames — "
        "the scanner is broken or the fixture no longer contains a known LLM hostname"
    )


def test_i3_allow_list_contract_documented() -> None:
    """I3: allow-list contains exactly the audited provider boundary files."""
    assert ALLOWED_LLM_IMPORT_FILES == frozenset(
        [
            "bot/services/llm_providers/anthropic.py",
            "bot/services/llm_providers/deepseek.py",
            "bot/services/llm_providers/openai.py",
            "bot/services/llm_providers/openai_vision.py",
        ]
    ), (
        "ALLOWED_LLM_IMPORT_FILES contract drift — the audited boundary is "
        "bot/services/llm_providers/*.py. If a new provider "
        "is added, extend the allow-list AND re-confirm gateway invariant #2."
    )


# ---------------------------------------------------------------------------
# G3.a (T12-09) — per-path forbidden-import scan.
#
# Extends — does NOT replace — the global LLM_PROVIDER_PREFIXES scan above with
# a path-keyed map so a specific subsystem can forbid additional modules. The
# Butler boundary (Phase 12) forbids anthropic / openai (Hard Constraint #1) AND
# bot.services.graph_query (preserves Phase 10 admin-only graph stance, §5.7).
# ---------------------------------------------------------------------------


def _module_matches_forbidden(module: str | None, forbidden: frozenset[str]) -> str | None:
    """Return the forbidden entry a module matches, else None.

    A module matches a forbidden name F if module == F or module starts with
    ``F + "."`` (sub-module import). This catches both ``import F`` /
    ``from F import x`` and ``from F.sub import y``.
    """
    if module is None:
        return None
    for f in forbidden:
        if module == f or module.startswith(f + "."):
            return f
    return None


def _forbidden_import_sites(path: Path, forbidden: frozenset[str]) -> list[tuple[int, str]]:
    sites: list[tuple[int, str]] = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        pytest.fail(f"unable to parse {_relative_to_repo(path)}: {exc!s}")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                hit = _module_matches_forbidden(alias.name, forbidden)
                if hit is not None:
                    sites.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            # node.module is None for relative imports like `from . import x`.
            hit = _module_matches_forbidden(node.module, forbidden)
            if hit is not None:
                names = ", ".join(alias.name for alias in node.names)
                sites.append((node.lineno, f"from {node.module} import {names}"))
    return sites


def assert_no_forbidden_imports_per_path(
    forbidden_map: dict[str, frozenset[str]],
) -> None:
    """Assert that every file matching each glob key imports none of its forbidden modules.

    ``forbidden_map`` keys are repo-relative glob patterns under ``bot/``; values
    are frozensets of forbidden module names. A glob that matches NO file is a
    harness misconfiguration (coverage must not silently drop) and fails the
    assertion. Extends — does not replace — the global LLM_PROVIDER_PREFIXES scan.
    """
    violations: list[str] = []
    empty_globs: list[str] = []
    for glob_pattern, forbidden in forbidden_map.items():
        matched = sorted(p for p in REPO_ROOT.glob(glob_pattern) if "__pycache__" not in p.parts)
        if not matched:
            empty_globs.append(glob_pattern)
            continue
        for path in matched:
            rel = _relative_to_repo(path)
            for line_no, statement in _forbidden_import_sites(path, forbidden):
                violations.append(f"{rel}:{line_no}: {statement}")

    assert not empty_globs, (
        "G3.a harness misconfiguration — these glob patterns matched no files "
        "(coverage would silently drop):\n" + "\n".join(empty_globs)
    )
    assert not violations, (
        "G3.a violation — forbidden import detected in a path-scoped file:\n"
        + "\n".join(violations)
    )


# Verbatim map from PHASE12_PLAN_REFRESH.md §12.5 G3.a.
_BUTLER_FORBIDDEN_IMPORTS: dict[str, frozenset[str]] = {
    "bot/services/butler*.py": frozenset({"anthropic", "openai", "bot.services.graph_query"}),
    "bot/handlers/butler.py": frozenset({"anthropic", "openai", "bot.services.graph_query"}),
    "bot/services/butler_tools/*.py": frozenset(
        {"anthropic", "openai", "bot.services.graph_query"}
    ),
}


def test_g3a_butler_paths_have_no_forbidden_imports() -> None:
    """G3.a: Butler service/handler/tool files import no anthropic/openai/graph_query."""
    assert_no_forbidden_imports_per_path(_BUTLER_FORBIDDEN_IMPORTS)


def test_g3a_helper_detects_a_planted_violation(tmp_path) -> None:
    """G3.a meta-test: the helper actually flags a forbidden import (no silent pass).

    Guards against a broken scanner that would let real violations through —
    same honeypot rationale as the I4b URL scan.
    """
    bad = tmp_path / "bot" / "services" / "butler_planted.py"
    bad.parent.mkdir(parents=True)
    bad.write_text("from bot.services.graph_query import run_query\n", encoding="utf-8")

    # Point the scan at tmp_path by temporarily swapping REPO_ROOT semantics:
    # build the absolute glob ourselves and feed the helper a map it can resolve.
    import tests.evals.test_no_llm_imports as mod

    original_root = mod.REPO_ROOT
    mod.REPO_ROOT = tmp_path
    try:
        with pytest.raises(AssertionError, match="graph_query"):
            mod.assert_no_forbidden_imports_per_path(
                {"bot/services/butler_*.py": frozenset({"bot.services.graph_query"})}
            )
    finally:
        mod.REPO_ROOT = original_root
