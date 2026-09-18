"""Code boundaries a published plugin must keep."""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "ycl"


def _imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    return modules


def test_no_runtime_module_imports_core():
    offenders = {
        str(path.relative_to(ROOT)): sorted(
            m for m in _imported_modules(path) if m == "research_engine" or m.startswith("research_engine.")
        )
        for path in PACKAGE.rglob("*.py")
    }
    assert {k: v for k, v in offenders.items() if v} == {}


def _code_strings(path: Path) -> list[str]:
    """String literals in ``path``, excluding docstrings."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_only_the_path_resolver_names_the_legacy_data_directory():
    # It is read for the explicit migration and for pre-0.3.0 ingest dedupe only.
    hits = [
        str(path.relative_to(ROOT))
        for path in PACKAGE.rglob("*.py")
        if any(".marginalia" in s for s in _code_strings(path))
    ]
    assert hits == ["ycl/_paths.py"]


def test_no_checkout_only_login_command_in_runtime_hints():
    hits = [
        str(path.relative_to(ROOT))
        for path in PACKAGE.rglob("*.py")
        if "python -m ycl.cli.login" in path.read_text(encoding="utf-8")
    ]
    assert hits == []


def test_changelog_matches_package_version():
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    first = re.search(r"^## (\S+)", (ROOT / "CHANGELOG.md").read_text(), re.MULTILINE)
    assert first and first.group(1) == version
