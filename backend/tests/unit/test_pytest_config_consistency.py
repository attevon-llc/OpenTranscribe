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
import warnings
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

#: Markers pytest defines itself. Used by tests, never registered by us.
_BUILTIN_MARKERS = frozenset(
    {"parametrize", "skip", "skipif", "xfail", "usefixtures", "filterwarnings"}
)

#: E2E markers that are REGISTERED but that no test carries, each with a written reason.
#:
#: ``--strict-markers`` only closes one direction: a marker on a test that nobody registered.
#: The other direction is just as silent and had three live offenders (``smoke``, ``gallery``,
#: ``api_e2e``) — a registered marker that selects nothing. ``-m gallery`` collects 0 of
#: ``test_gallery_actions.py``'s 52 tests and pytest exits 5 ("no tests ran"), which reads as
#: "there was nothing to do", not "your selector is broken"; root ``CLAUDE.md`` documents that
#: exact command.
#:
#: Same rules as ``backend/tests/audit-allowlist.txt``: the reason is mandatory, a reason
#: starting ``BACKLOG`` is deferred work rather than an accepted pattern, and a STALE entry
#: fails — so the list can only shrink and an exemption cannot outlive its subject.
#: Empty, and that is the point: `gallery` was the only entry and it was FIXED rather than
#: exempted — `pytestmark = pytest.mark.gallery` now sits at module scope in
#: tests/e2e/test_gallery_actions.py, so the documented `-m gallery` selector picks up its 52
#: tests instead of deselecting everything. Add an entry here only when a registered marker
#: genuinely should select nothing, and say why.
_UNUSED_E2E_MARKERS: dict[str, str] = {}


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


def _e2e_registered_markers() -> set[str]:
    parser = configparser.ConfigParser()
    parser.read(_E2E_INI)
    return _marker_names(
        [line for line in parser.get("pytest", "markers", fallback="").splitlines() if line.strip()]
    )


def _e2e_used_markers() -> set[str]:
    used: set[str] = set()
    for path in (_BACKEND / "tests" / "e2e").rglob("*.py"):
        for match in re.finditer(r"pytest\.mark\.([a-z_][a-z0-9_]*)", path.read_text()):
            used.add(match.group(1))
    return used


def test_the_e2e_marker_scan_actually_finds_markers() -> None:
    """Guard the guard.

    Both directions below are differences against this set. If the scan silently stopped
    matching — a moved directory, a changed decorator spelling — ``used`` would be empty, and
    the "every used marker is registered" assertion would pass while checking nothing.
    """
    used = _e2e_used_markers()

    assert {"skipif", "visual"} <= used, (
        "the tests/e2e marker scan found neither a builtin (skipif) nor a known suite marker "
        f"(visual); it is matching nothing useful. Found: {sorted(used)}"
    )


def test_every_marker_used_in_the_e2e_suite_is_registered() -> None:
    """--strict-markers only helps if the registry is actually complete."""
    missing = _e2e_used_markers() - _e2e_registered_markers() - _BUILTIN_MARKERS

    assert not missing, (
        f"markers used in tests/e2e but not registered in {_E2E_INI.name}: {sorted(missing)}"
    )


def test_every_marker_registered_for_the_e2e_suite_selects_something() -> None:
    """The reverse of the assertion above, and the one that was missing.

    A registered marker no test carries is a selector that matches nothing. Because pytest
    reports that as "0 tests collected" it looks like an empty-but-healthy run rather than a
    broken selector — which is how ``smoke``, ``gallery`` and ``api_e2e`` sat in the registry
    while ``-m gallery`` silently selected none of ``test_gallery_actions.py``'s 52 tests.
    """
    unused = _e2e_registered_markers() - _e2e_used_markers()
    unexplained = unused - set(_UNUSED_E2E_MARKERS)

    assert not unexplained, (
        f"registered in {_E2E_INI.name} but carried by no test: {sorted(unexplained)}.\n"
        "Each one is a selector that matches nothing. Apply it to the tests it names "
        "(module-level `pytestmark` is enough), or delete the registration — and if it must "
        "stay unused for now, add it to _UNUSED_E2E_MARKERS with a written reason."
    )


def test_the_unused_e2e_marker_allowlist_is_not_stale() -> None:
    """An exemption must not outlive its subject.

    Both failure modes are stale: a marker that is used now (the exemption is obsolete —
    delete the line) and a marker that is no longer registered at all (the exemption is
    exempting nothing, and would silently cover a future re-registration).
    """
    registered = _e2e_registered_markers()
    used = _e2e_used_markers()

    now_used = sorted(m for m in _UNUSED_E2E_MARKERS if m in used)
    unregistered = sorted(m for m in _UNUSED_E2E_MARKERS if m not in registered)

    assert not now_used, (
        f"_UNUSED_E2E_MARKERS exempts {now_used}, which the suite now uses — delete the entry."
    )
    assert not unregistered, (
        f"_UNUSED_E2E_MARKERS exempts {unregistered}, which {_E2E_INI.name} no longer "
        "registers — delete the entry."
    )


def test_every_unused_e2e_marker_has_a_written_reason() -> None:
    """A bare exemption is indistinguishable from an oversight.

    ``BACKLOG`` entries are deferred work, not accepted patterns; they are named in the
    failure text of nothing, so this test is where they stay visible.
    """
    unreasoned = sorted(m for m, why in _UNUSED_E2E_MARKERS.items() if len(why.strip()) < 20)

    assert not unreasoned, f"_UNUSED_E2E_MARKERS entries with no real reason: {unreasoned}"

    backlog = sorted(m for m, why in _UNUSED_E2E_MARKERS.items() if why.startswith("BACKLOG"))
    if backlog:
        warnings.warn(
            f"{len(backlog)} e2e marker(s) are registered but select no tests, deferred as "
            f"BACKLOG: {backlog}. A green run here is not a clean registry.",
            UserWarning,
            stacklevel=1,
        )
