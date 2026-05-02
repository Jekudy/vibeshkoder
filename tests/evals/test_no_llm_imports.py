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

# Once Phase 5 lands, llm_gateway.py is the ONLY file allowed to import these.
ALLOWED_LLM_IMPORT_FILES: frozenset[str] = frozenset(
    [
        # "bot/services/llm_gateway.py",  # uncomment when Phase 5 ships
    ]
)


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
        if line.startswith("[project.optional-dependencies]") or line.startswith(
            "[tool."
        ) or line.startswith("[build-system]") or line == "":
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
            head = stripped.split("[", 1)[0].split("=", 1)[0].split(">", 1)[0].split("<", 1)[0].split("~", 1)[0].split("!", 1)[0].strip()
            if head and head.replace("-", "_").lower() in {p.replace("-", "_").lower() for p in LLM_PROVIDER_PREFIXES}:
                forbidden_runtime_hits.append(stripped)

    assert not forbidden_runtime_hits, (
        "invariant 2 violation — LLM provider in [project.dependencies]:\n"
        + "\n".join(forbidden_runtime_hits)
        + "\n\nLLM provider packages must live in a Phase-5+ optional-dependencies group."
    )
    # Note: optional_section_name tracked above so future ALLOWED groups can be
    # asserted; current state forbids any provider anywhere in runtime deps.
    del optional_section_name


def test_i3_allow_list_contract_documented() -> None:
    """I3: allow-list constant is the explicit contract; will be extended in Phase 5."""
    assert ALLOWED_LLM_IMPORT_FILES == frozenset(), (
        "ALLOWED_LLM_IMPORT_FILES is not empty — Phase 5 has shipped llm_gateway.py "
        "and this test must be updated to reflect the new contract: the gateway is "
        "the ONLY file allowed to import LLM providers."
    )
