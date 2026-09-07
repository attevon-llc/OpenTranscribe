"""Every marked test must live where the pre-merge gate's phase for that marker looks.

``scripts/run-integration-tests.sh`` runs its marker phases over an explicit path list rather
than all of ``tests/``: collecting the full tree to find a couple of dozen tests costs tens of
seconds per phase. That is a worthwhile saving, but it introduces a way to be silently wrong —
a marked test added anywhere else would simply never run, and the gate would still report
success.

This is the same failure shape as ``gpu`` being unregistered before #297: the marker existed,
nothing selected it, and nobody noticed. So each narrowing is paired with a check rather than
trusted (issue #431).

⚠️ This file used to cover ``integration`` **only**, with a docstring asserting that ``gpu``
"deliberately keeps the full-tree sweep". That reason (the tests are scattered) had stopped
being true: all twelve modules carrying the marker are under ``tests/integration/`` plus
``tests/unit/test_cuda_device_guard.py``, so the GPU phase was paying a whole-tree collection
for nothing. It is now narrowed too — and therefore needs the same guard, which is why the
single ``_MARKER`` constant became a table.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_TESTS_ROOT = Path(__file__).resolve().parents[1]
_GATE_SCRIPT = _TESTS_ROOT.parents[1] / "scripts" / "run-integration-tests.sh"

#: (marker, phase title in the gate script). One row per narrowed phase. Adding a phase that
#: selects by marker over an explicit path list means adding a row here, or the narrowing is
#: unguarded.
_NARROWED_PHASES = [
    ("integration", "Integration-marked tests"),
    ("gpu", "GPU-marked tests"),
]


def _marked_modules(marker: str) -> set[str]:
    """Modules containing at least one ``marker``-marked test or module-level mark."""
    found: set[str] = set()
    for path in sorted(_TESTS_ROOT.rglob("*.py")):
        if "e2e" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover - a syntax error fails collection anyway
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or node.attr != marker:
                continue
            # `pytest.mark.<marker>` — confirm it is a mark, not an attribute named the same.
            if isinstance(node.value, ast.Attribute) and node.value.attr == "mark":
                found.add(path.relative_to(_TESTS_ROOT).as_posix())
                break
    return found


def _gate_phase_paths(title: str) -> list[str]:
    """The ``tests/`` paths the gate's phase named ``title`` actually passes to pytest.

    Matches ``run_phase`` **or** ``run_phase_watching_skips``, optionally preceded by an
    environment-variable prefix (the GPU phase sets ``PHASE_SKIP_CEILING`` that way): the
    integration phase moved from one dispatcher to the other so a mass-skipped run cannot
    report as a pass, and a pattern naming only the original went red for a rename rather than
    for a real gap. The phase TITLE is the stable identity here, not how it is invoked.
    """
    source = _GATE_SCRIPT.read_text()
    match = re.search(rf'run_phase\w* "{re.escape(title)}".*?\n\n', source, re.S)
    assert match, f"could not find the {title!r} phase in run-integration-tests.sh"
    return re.findall(r"tests/[\w./-]+", match.group(0))


@pytest.mark.parametrize(("marker", "title"), _NARROWED_PHASES)
def test_the_gate_script_still_has_the_phase(marker: str, title: str) -> None:
    """If a phase is renamed or removed, this module's premise is gone — fail loudly."""
    assert _gate_phase_paths(title), (
        f"the {title!r} phase in run-integration-tests.sh passes no tests/ path; "
        f"either restore it or drop the ({marker}, {title}) row from _NARROWED_PHASES"
    )


@pytest.mark.parametrize(("marker", "title"), _NARROWED_PHASES)
def test_every_marked_test_is_reachable_by_its_phase(marker: str, title: str) -> None:
    """A marked test outside its phase's paths never runs, and the gate still reports green."""
    gate_paths = _gate_phase_paths(title)
    modules = sorted(_marked_modules(marker))
    assert modules, (
        f"no module carries @pytest.mark.{marker} at all — either the marker was renamed "
        f"(in which case the {title!r} phase now selects nothing, which is the #297 bug) or "
        f"this detector has stopped matching, which would make the check below vacuous"
    )

    unreachable = [
        f"tests/{module}"
        for module in modules
        if not any(
            f"tests/{module}" == p or f"tests/{module}".startswith(p.rstrip("/") + "/")
            for p in gate_paths
        )
    ]

    assert not unreachable, (
        f"These modules carry @pytest.mark.{marker} but sit outside the paths "
        f"run-integration-tests.sh collects for {title!r} ({gate_paths}), so the gate would "
        "skip them silently:\n  " + "\n  ".join(unreachable) + "\nAdd the path to the gate "
        "script, or move the test under a path it already collects."
    )
