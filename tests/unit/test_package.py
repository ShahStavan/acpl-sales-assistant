"""Package-level invariants: the version, and the dependency boundaries of DESIGN.md §8.2."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import acpl_assistant

PACKAGE_ROOT = Path(acpl_assistant.__file__).parent

# DESIGN.md §8.2.  Each entry reads: nothing under *subpackage* may import any of *forbidden*.
BOUNDARIES: list[tuple[str, tuple[str, ...], str]] = [
    (
        "actions",
        ("llm",),
        "the rules engine is LLM-free by design: no model, no key, no token spent",
    ),
    (
        "prepare",
        ("ask", "actions"),
        "preparation is upstream of both and must build without them",
    ),
]


def test_package_exposes_version() -> None:
    assert acpl_assistant.__version__


def _imported_subpackages(module: Path) -> set[str]:
    """Return the ``acpl_assistant`` subpackages *module* imports, however it spells them."""
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        for name in names:
            parts = name.split(".")
            if parts[0] == "acpl_assistant" and len(parts) > 1:
                found.add(parts[1])
    return found


@pytest.mark.parametrize(("subpackage", "forbidden", "reason"), BOUNDARIES)
def test_dependency_boundary(subpackage: str, forbidden: tuple[str, ...], reason: str) -> None:
    """A boundary the design states in prose is worth failing a build over."""
    breaches = {
        module.relative_to(PACKAGE_ROOT).as_posix(): sorted(
            _imported_subpackages(module) & set(forbidden)
        )
        for module in sorted((PACKAGE_ROOT / subpackage).rglob("*.py"))
        if _imported_subpackages(module) & set(forbidden)
    }
    assert not breaches, f"{subpackage}/ must not import {forbidden}: {reason}. Found {breaches}"


def test_the_boundary_check_can_actually_fail(tmp_path: Path) -> None:
    """Prove the scan sees an import, rather than passing because it sees nothing."""
    module = tmp_path / "offender.py"
    module.write_text("from acpl_assistant.llm.client import call\n", encoding="utf-8")
    assert _imported_subpackages(module) == {"llm"}
