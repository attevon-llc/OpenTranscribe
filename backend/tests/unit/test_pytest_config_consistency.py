"""The three pytest configurations must not drift apart.

This repo has three independent pytest configs, and which one applies depends only on where
you invoked pytest from:

* ``backend/pyproject.toml`` — CANONICAL. Every runner cd's to ``backend/`` first.
* ``pyproject.toml`` (repo root) — wins when someone runs ``pytest`` from the root by hand.
* ``backend/tests/e2e/pytest.ini`` — a separate rootdir, so the pyproject ``addopts`` do not
  apply to E2E at all.

Silent divergence is the failure mode. The root table used to read
``-v --tb=short --strict-markers`` with a three-marker list, so a root-run went serial,
selected the ``integration`` and ``gpu`` suites the fast suite deliberately excludes, and then
failed collection on ``--strict-markers`` for markers it had not registered. Nothing reported
that; you just got different results depending on your shell's cwd (issue #431).
"""

from __future__ import annotations

import configparser
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

_BACKEND = Path(__file__).resolve().parents[2]
_REPO = _BACKEND.parent

_BACKEND_PYPROJECT = _BACKEND / "pyproject.toml"
_ROOT_PYPROJECT = _REPO / "pyproject.toml"
_E2E_INI = _BACKEND / "tests" / "e2e" / "pytest.ini"

#: Flags whose absence changes *which tests run* or *how they are scheduled*. A root-run that
#: drops any of these is not the same run, which is the whole point of this module.
_SEMANTIC_FLAGS = ("-n auto", "--dist loadgroup", "--strict-markers", "--timeout")


def _pytest_table(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        table = tomllib.load(handle)["tool"]["pytest"]["ini_options"]
    assert isinstance(table, dict), f"{path} has no [tool.pytest.ini_options] table"
    return table


def _marker_names(markers: list[str]) -> set[str]:
    """``"gpu: needs a GPU"`` -> ``"gpu"``."""
    return {m.split(":", 1)[0].strip() for m in markers}


def _marker_filter(addopts: str) -> str | None:
    match = re.search(r"-m\s+('[^']*'|\"[^\"]*\")", addopts)
    return match.group(1).strip("'\"") if match else None


def test_the_two_pyproject_marker_sets_are_identical() -> None:
    """An unregistered marker is a collection error under --strict-markers.

    Registering ``ddl_exclusive`` in only one of the two files means a root-run cannot even
    collect the migration suites.
    """
    backend = _marker_names(_pytest_table(_BACKEND_PYPROJECT)["markers"])
    root = _marker_names(_pytest_table(_ROOT_PYPROJECT)["markers"])

    assert root == backend, (
        "the root and backend pytest marker registries have drifted.\n"
        f"  only in backend/pyproject.toml: {sorted(backend - root)}\n"
        f"  only in root pyproject.toml:    {sorted(root - backend)}\n"
        "backend/pyproject.toml is canonical — mirror it."
    )


def test_both_configs_select_the_same_tests() -> None:
    """The ``-m`` filter decides whether integration/gpu tests run at all."""
    backend = _marker_filter(_pytest_table(_BACKEND_PYPROJECT)["addopts"])
    root = _marker_filter(_pytest_table(_ROOT_PYPROJECT)["addopts"])

    assert backend is not None, (
        "backend addopts lost its -m filter; the fast suite would run gpu tests"
    )
    assert root == backend, (
        f"marker filters differ — backend {backend!r} vs root {root!r}. A root-run would "
        "select a different set of tests than the gate does."
    )


@pytest.mark.parametrize("flag", _SEMANTIC_FLAGS)
def test_both_configs_share_the_flags_that_change_behaviour(flag: str) -> None:
    """Cosmetic differences (-q vs -v) are fine; these are not."""
    backend = _pytest_table(_BACKEND_PYPROJECT)["addopts"]
    root = _pytest_table(_ROOT_PYPROJECT)["addopts"]

    if flag not in backend:
        pytest.skip(f"{flag} is not in the canonical config")
    assert flag in root, (
        f"{flag!r} is in backend/pyproject.toml but not the root table. A root-run would "
        "behave differently — mirror the canonical config."
    )


def test_the_e2e_config_registers_its_markers_strictly() -> None:
    """E2E is a separate rootdir, so it needs its own --strict-markers.

    Without it a typo'd marker silently selects nothing: ``-m uplaod`` reports "0 tests" and
    exits 0, which reads exactly like a clean run.
    """
    parser = configparser.ConfigParser()
    parser.read(_E2E_INI)
    addopts = parser.get("pytest", "addopts", fallback="")

    assert "--strict-markers" in addopts, (
        f"{_E2E_INI.relative_to(_REPO)} does not pass --strict-markers, so a misspelled "
        "marker there selects no tests and still exits 0."
    )


def test_every_marker_used_in_the_e2e_suite_is_registered() -> None:
    """--strict-markers only helps if the registry is actually complete."""
    parser = configparser.ConfigParser()
    parser.read(_E2E_INI)
    registered = _marker_names(
        [line for line in parser.get("pytest", "markers", fallback="").splitlines() if line.strip()]
    )

    used: set[str] = set()
    for path in (_BACKEND / "tests" / "e2e").rglob("*.py"):
        for match in re.finditer(r"pytest\.mark\.([a-z_][a-z0-9_]*)", path.read_text()):
            used.add(match.group(1))

    # Markers that are pytest builtins, not suite-defined.
    builtins = {"parametrize", "skip", "skipif", "xfail", "usefixtures", "filterwarnings"}
    missing = used - registered - builtins

    assert not missing, (
        f"markers used in tests/e2e but not registered in {_E2E_INI.name}: {sorted(missing)}"
    )
