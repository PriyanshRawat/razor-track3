"""Structural contract tests: the properties the Phase 0 package claims about itself.

``reclaim/__init__.py`` says the contracts are pure schemas with no I/O, and
``reclaim/contracts/__init__.py`` publishes a dependency layering. Both are claims
in a docstring, which is where claims go to rot. These tests are the enforcement.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

_CONTRACTS_DIR = Path(__file__).resolve().parents[1] / "reclaim" / "contracts"
_REPO_ROOT = _CONTRACTS_DIR.parents[1]

#: Modules a *contract* may never touch. A schema that opens a socket, reads a
#: file, or shells out is no longer a schema: it cannot be imported by the
#: simulator-integrity test (§12.5.4), it cannot be reasoned about offline, and it
#: gives a Phase 1 detector somewhere to hide behaviour.
_FORBIDDEN_IMPORTS = frozenset(
    {
        "asyncio",
        "http",
        "httpx",
        "io",
        "logging",
        "os",
        "pathlib",
        "requests",
        "shutil",
        "socket",
        "sqlite3",
        "subprocess",
        "sys",
        "tempfile",
        "threading",
        "urllib",
    }
)

#: Names that would let a contract reach the filesystem or the clock without an
#: import statement to give it away.
_FORBIDDEN_CALLS = frozenset({"open", "eval", "exec", "compile", "__import__"})


def _contract_modules() -> dict[str, Path]:
    return {
        path.stem: path
        for path in sorted(_CONTRACTS_DIR.glob("*.py"))
        if path.stem != "__init__"
    }


def _internal_dependencies(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    deps: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("reclaim.contracts."):
                deps.add(node.module.rsplit(".", 1)[-1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("reclaim.contracts."):
                    deps.add(alias.name.rsplit(".", 1)[-1])
    return deps


def test_the_contracts_package_is_not_empty():
    """Guards every other test in this file: an empty glob would make them all
    vacuously true."""
    assert len(_contract_modules()) >= 15


def test_the_contract_dependency_graph_is_acyclic():
    """A cycle here is not a style problem. Two contracts that import each other
    can only be loaded in one order, and the failure surfaces as an
    ImportError at interpreter start with a traceback that names neither cause."""
    graph = {name: _internal_dependencies(path) for name, path in _contract_modules().items()}
    for name in graph:
        reachable: set[str] = set()
        frontier = list(graph[name])
        while frontier:
            current = frontier.pop()
            if current in reachable:
                continue
            reachable.add(current)
            frontier.extend(graph.get(current, ()))
        assert name not in reachable, f"{name} transitively imports itself: {sorted(reachable)}"


def test_no_contract_imports_the_outside_world():
    """§12.5.4's sim-integrity separation starts here: contracts describe shapes,
    they do not reach anywhere."""
    offences: list[str] = []
    for name, path in _contract_modules().items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            roots: list[str] = []
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                roots = [node.module.split(".")[0]]
            for root in roots:
                if root in _FORBIDDEN_IMPORTS:
                    offences.append(f"{name}.py imports {root}")
    assert offences == []


def test_no_contract_calls_a_filesystem_or_eval_builtin():
    offences: list[str] = []
    for name, path in _contract_modules().items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in _FORBIDDEN_CALLS
            ):
                offences.append(f"{name}.py line {node.lineno} calls {node.func.id}()")
    assert offences == []


@pytest.mark.parametrize("module", sorted(_contract_modules()))
def test_every_contract_module_imports_on_its_own(module: str):
    """Import-order independence. A module that only loads because something else
    loaded first works in the test suite and fails in the one script that imports
    just it -- which, in Phase 1, is every worker process."""
    completed = subprocess.run(
        [sys.executable, "-c", f"import reclaim.contracts.{module}"],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
    )
    assert completed.returncode == 0, completed.stderr


def test_the_freeze_flag_is_still_open():
    """Phase 0 ends when a human reviews it, not when the tests pass. This test is
    the reminder to flip the flag deliberately -- and to delete this test when the
    review has happened."""
    from reclaim.contracts.versions import PHASE_0_FROZEN

    assert PHASE_0_FROZEN is False
