"""Phase 11 §5.6 — invariant G1 binding test for T9-08.

Walks the AST of all wiki-related Python files and asserts that none of them
import graph-backend modules:  bot.services.graph_*, neo4j, graphiti, networkx.

Phase 9 (wiki) is a pure-Postgres feature.  Phase 10 (graph projection / Neo4j)
ships later; importing graph dependencies from wiki code would violate the
Phase 9 / Phase 10 boundary and is a §8 stop signal.

This file does NOT depend on any DB fixture or EVAL_HARNESS_ENABLED — it only
reads source code from disk so it always runs.

References:
  PHASE9_PLAN.md §T9-08 AC G1
  tests/evals/test_no_llm_imports.py (canonical AST-walk pattern)
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Forbidden import prefixes — any import whose module starts with one of these
# (or exactly equals one of these) is a Phase 9/10 boundary violation.
FORBIDDEN_IMPORT_PREFIXES: tuple[str, ...] = (
    "bot.services.graph_",
    "neo4j",
    "graphiti",
    "networkx",
)

# Canonical set of wiki source files covered by G1.
# Pattern: bot/services/wiki_*.py + web/routes/wiki.py + bot/handlers/wiki.py
_WIKI_GLOB_PATTERNS: list[tuple[Path, str]] = [
    (REPO_ROOT / "bot" / "services", "wiki_*.py"),
    (REPO_ROOT / "bot" / "handlers", "wiki.py"),
    (REPO_ROOT / "web" / "routes", "wiki.py"),
]


def _relative_to_repo(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def _module_matches_forbidden(module: str | None) -> bool:
    """Return True if *module* matches any of the FORBIDDEN_IMPORT_PREFIXES.

    Rules:
    - "bot.services.graph_" is a prefix match (catches graph_foo, graph_bar …).
    - "neo4j", "graphiti", "networkx" are exact-first-token matches
      (catches neo4j.v1.api, networkx.algorithms.*, etc.).
    """
    if module is None:
        return False
    for prefix in FORBIDDEN_IMPORT_PREFIXES:
        if prefix.endswith("_"):
            # prefix match — module starts with this string
            if module == prefix.rstrip("_") or module.startswith(prefix):
                return True
        else:
            # exact-first-token match
            first_token = module.split(".", 1)[0]
            if first_token == prefix:
                return True
    return False


def _collect_wiki_files() -> list[Path]:
    """Return sorted list of wiki source files that G1 must scan."""
    files: list[Path] = []
    for directory, pattern in _WIKI_GLOB_PATTERNS:
        files.extend(sorted(directory.glob(pattern)))
    return files


def _graph_import_sites(path: Path) -> list[tuple[int, str]]:
    """Return (lineno, statement) pairs for forbidden graph imports in *path*."""
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        pytest.fail(f"unable to read {_relative_to_repo(path)}: {exc!s}")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        pytest.fail(f"unable to parse {_relative_to_repo(path)}: {exc!s}")

    sites: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _module_matches_forbidden(alias.name):
                    sites.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            module = node.module
            if _module_matches_forbidden(module):
                names = ", ".join(alias.name for alias in node.names)
                sites.append((node.lineno, f"from {module} import {names}"))
    return sites


def _wiki_file_params() -> list[pytest.param]:  # type: ignore[type-arg]
    files = _collect_wiki_files()
    if not files:
        return [pytest.param(None, id="no-wiki-files-found")]
    return [pytest.param(f, id=_relative_to_repo(f)) for f in files]


@pytest.mark.parametrize("wiki_file", _wiki_file_params())
def test_g1_wiki_files_have_no_graph_imports(wiki_file: Path | None) -> None:
    """G1: wiki source files must not import graph-backend modules.

    Covers: bot/services/wiki_*.py, web/routes/wiki.py, bot/handlers/wiki.py.
    Forbidden prefixes: bot.services.graph_*, neo4j, graphiti, networkx.
    """
    if wiki_file is None:
        pytest.skip("no wiki source files found — directory layout unexpected")

    if not wiki_file.is_file():
        pytest.fail(
            f"expected wiki source file not found: {_relative_to_repo(wiki_file)}"
        )

    sites = _graph_import_sites(wiki_file)
    if sites:
        lines = "\n".join(
            f"  {_relative_to_repo(wiki_file)}:{lineno}: {stmt}"
            for lineno, stmt in sites
        )
        pytest.fail(
            f"G1 violation — graph-backend import detected in wiki file "
            f"({_relative_to_repo(wiki_file)}):\n{lines}\n\n"
            "Phase 9 wiki must stay graph-free until Phase 10 ships. "
            "Remove these imports or move the code to a Phase 10 module."
        )


def test_g1_at_least_one_wiki_file_scanned() -> None:
    """G1 meta-test: the scanner must find at least one wiki file.

    Prevents silent false-pass when the directory layout changes and the glob
    matches nothing.
    """
    files = _collect_wiki_files()
    assert files, (
        "G1 scanner found zero wiki files. "
        "Expected at least one of: bot/services/wiki_*.py, "
        "web/routes/wiki.py, bot/handlers/wiki.py. "
        "Check that the repo layout matches PHASE9_PLAN.md §T9-08."
    )
